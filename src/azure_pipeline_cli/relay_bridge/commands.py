"""Parsing for the `/relay-bridge` shell command.

This module keeps Azure Relay Bridge command parsing independent from shell UI
so process lifecycle and validation can be tested without an interactive shell.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal


Action = Literal[
    "connect",
    "list",
    "stop",
    "hc-list",
    "hc-create",
    "hc-delete",
    "ns-list",
    "ns-create",
    "ns-delete",
    "ns-keys",
]
ForwardMode = Literal["local", "remote", "remote-http"]


@dataclass(frozen=True)
class RelayBridgeForward:
    """A single Relay Bridge forward expression and shell-safe summary."""

    mode: ForwardMode
    expression: str
    relay_name: str
    endpoint: str
    local_ports: tuple[int, ...] = ()


@dataclass(frozen=True)
class RelayBridgeSpec:
    """A requested Relay Bridge helper process configuration."""

    connection_string: str
    local_forwards: tuple[RelayBridgeForward, ...] = ()
    remote_forwards: tuple[RelayBridgeForward, ...] = ()
    remote_http_forwards: tuple[RelayBridgeForward, ...] = ()

    @property
    def forwards(self) -> tuple[RelayBridgeForward, ...]:
        return self.local_forwards + self.remote_forwards + self.remote_http_forwards


@dataclass(frozen=True)
class RelayBridgeCommand:
    """Parsed `/relay-bridge` command."""

    action: Action
    spec: RelayBridgeSpec | None = None
    target: str | None = None
    resource_group: str | None = None
    namespace: str | None = None
    hc_name: str | None = None
    location: str | None = None
    auth_rule: str | None = None
    requires_client_authorization: bool = True


def parse_relay_bridge_command(arg: str) -> RelayBridgeCommand:
    tokens = shlex.split(arg)
    if not tokens:
        raise ValueError('usage: /relay-bridge start -x "<connection-string>" [-L expr] [-T expr] [-H expr]')

    verb = tokens[0].lower()
    if verb == "start":
        tokens = tokens[1:]
        if not tokens:
            raise ValueError('usage: /relay-bridge start -x "<connection-string>" [-L expr] [-T expr] [-H expr]')
    elif verb == "hc":
        return _parse_hc_command(tokens[1:])
    elif verb == "ns":
        return _parse_namespace_command(tokens[1:])
    elif tokens[0].startswith("-"):
        raise ValueError('usage: /relay-bridge start -x "<connection-string>" [-L expr] [-T expr] [-H expr]')
    if verb == "list":
        if len(tokens) != 1:
            raise ValueError("usage: /relay-bridge list")
        return RelayBridgeCommand("list")
    if verb == "stop":
        if len(tokens) != 2:
            raise ValueError("usage: /relay-bridge stop <id|localPort|relayName|all>")
        return RelayBridgeCommand("stop", target=tokens[1])

    options = _parse_repeatable_options(tokens)
    connection_string = _single_option(options, "x")
    if not connection_string:
        raise ValueError('missing required option: -x "<connection-string>"')

    local_forwards = tuple(_parse_local_forward(item) for item in options.get("L", ()))
    remote_forwards = tuple(_parse_remote_forward(item) for item in options.get("T", ()))
    remote_http_forwards = tuple(_parse_remote_http_forward(item) for item in options.get("H", ()))
    if not local_forwards and not remote_forwards and not remote_http_forwards:
        raise ValueError("at least one -L, -T, or -H forward is required")

    return RelayBridgeCommand(
        "connect",
        spec=RelayBridgeSpec(
            connection_string=connection_string,
            local_forwards=local_forwards,
            remote_forwards=remote_forwards,
            remote_http_forwards=remote_http_forwards,
        ),
    )


def _parse_hc_command(tokens: list[str]) -> RelayBridgeCommand:
    if not tokens:
        raise ValueError("usage: /relay-bridge hc <list|create|remove> -g <resource-group> -n <namespace> [name]")
    verb = tokens[0].lower()
    if verb not in {"list", "create", "remove"}:
        raise ValueError("usage: /relay-bridge hc <list|create|remove> -g <resource-group> -n <namespace> [name]")
    options, positional = _parse_options_and_positionals(tokens[1:], allowed={"g", "resource-group", "n", "namespace", "no-auth"})
    resource_group = _get_option(options, "g", "resource-group")
    namespace = _get_option(options, "n", "namespace")
    if not resource_group:
        raise ValueError("missing required option: -g <resource-group>")
    if not namespace:
        raise ValueError("missing required option: -n <namespace>")
    if verb == "list":
        if positional:
            raise ValueError("usage: /relay-bridge hc list -g <resource-group> -n <namespace>")
        return RelayBridgeCommand("hc-list", resource_group=resource_group, namespace=namespace)
    if len(positional) != 1:
        raise ValueError(f"usage: /relay-bridge hc {verb} -g <resource-group> -n <namespace> <hybrid-connection>")
    action: Action = "hc-create" if verb == "create" else "hc-delete"
    return RelayBridgeCommand(
        action,
        resource_group=resource_group,
        namespace=namespace,
        hc_name=positional[0],
        requires_client_authorization="no-auth" not in options,
    )


def _parse_namespace_command(tokens: list[str]) -> RelayBridgeCommand:
    if not tokens:
        raise ValueError("usage: /relay-bridge ns <list|create|remove|keys> -g <resource-group> [-n <namespace>] [-l <location>]")
    verb = tokens[0].lower()
    if verb not in {"list", "create", "remove", "keys"}:
        raise ValueError("usage: /relay-bridge ns <list|create|remove|keys> -g <resource-group> [-n <namespace>] [-l <location>]")
    options, positional = _parse_options_and_positionals(
        tokens[1:],
        allowed={"g", "resource-group", "n", "namespace", "l", "location", "rule", "auth-rule"},
    )
    if positional:
        raise ValueError(f"unexpected argument: {positional[0]}")
    resource_group = _get_option(options, "g", "resource-group")
    namespace = _get_option(options, "n", "namespace")
    location = _get_option(options, "l", "location")
    auth_rule = _get_option(options, "rule", "auth-rule") or "RootManageSharedAccessKey"
    if not resource_group:
        raise ValueError("missing required option: -g <resource-group>")
    if verb == "list":
        if namespace:
            raise ValueError("usage: /relay-bridge ns list -g <resource-group>")
        return RelayBridgeCommand("ns-list", resource_group=resource_group)
    if not namespace:
        raise ValueError("missing required option: -n <namespace>")
    if verb == "create":
        return RelayBridgeCommand("ns-create", resource_group=resource_group, namespace=namespace, location=location)
    if verb == "keys":
        return RelayBridgeCommand("ns-keys", resource_group=resource_group, namespace=namespace, auth_rule=auth_rule)
    return RelayBridgeCommand("ns-delete", resource_group=resource_group, namespace=namespace)


def _parse_repeatable_options(tokens: list[str]) -> dict[str, tuple[str, ...]]:
    options: dict[str, list[str]] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            raise ValueError(f"unexpected argument: {token}")
        key = token.lstrip("-")
        if key not in {"x", "L", "T", "H"}:
            raise ValueError(f"unexpected option: {token}")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
            raise ValueError(f"missing value for option: {token}")
        options.setdefault(key, []).append(tokens[index + 1])
        index += 2
    return {key: tuple(values) for key, values in options.items()}


def _parse_options_and_positionals(tokens: list[str], allowed: set[str]) -> tuple[dict[str, str], list[str]]:
    options: dict[str, str] = {}
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            positional.append(token)
            index += 1
            continue
        key = token.lstrip("-")
        if key not in allowed:
            raise ValueError(f"unexpected option: {token}")
        if key == "no-auth":
            options[key] = "true"
            index += 1
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
            raise ValueError(f"missing value for option: {token}")
        options[key] = tokens[index + 1]
        index += 2
    return options, positional


def _single_option(options: dict[str, tuple[str, ...]], key: str) -> str | None:
    values = options.get(key, ())
    if len(values) > 1:
        raise ValueError(f"option -{key} can only be provided once")
    return values[0] if values else None


def _get_option(options: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = options.get(name)
        if value:
            return value
    return None


def _parse_local_forward(expression: str) -> RelayBridgeForward:
    split_at = expression.rfind(":")
    if split_at <= 0 or split_at == len(expression) - 1:
        raise ValueError(f"invalid -L expression: {expression}")
    relay_name = expression[split_at + 1 :]
    bindings = expression[:split_at].split(";")
    local_ports = tuple(port for binding in bindings for port in _extract_local_ports(binding, expression))
    normalized_bindings = tuple(_normalize_local_binding(binding) for binding in bindings)
    normalized_expression = f"{';'.join(normalized_bindings)}:{relay_name}"
    endpoint = ",".join(str(port) for port in local_ports) if local_ports else expression[:split_at]
    return RelayBridgeForward("local", normalized_expression, relay_name, endpoint, local_ports)


def _normalize_local_binding(binding: str) -> str:
    if binding.isdigit():
        return f"127.0.0.1:{binding}"
    return binding


def _parse_remote_forward(expression: str) -> RelayBridgeForward:
    split_at = expression.find(":")
    if split_at <= 0 or split_at == len(expression) - 1:
        raise ValueError(f"invalid -T expression: {expression}")
    relay_name = expression[:split_at]
    return RelayBridgeForward("remote", expression, relay_name, expression[split_at + 1 :])


def _parse_remote_http_forward(expression: str) -> RelayBridgeForward:
    split_at = expression.find(":")
    if split_at <= 0 or split_at == len(expression) - 1:
        raise ValueError(f"invalid -H expression: {expression}")
    relay_name = expression[:split_at]
    target = expression[split_at + 1 :]
    if not (target.startswith("http/") or target.startswith("https/")):
        raise ValueError(f"invalid -H expression: {expression}")
    return RelayBridgeForward("remote-http", expression, relay_name, target)


def _extract_local_ports(binding: str, full_expression: str) -> tuple[int, ...]:
    parts = binding.split(":")
    if len(parts) > 2:
        raise ValueError(f"invalid -L expression: {full_expression}")
    port_part = parts[-1].split("/", 1)[0]
    if port_part.endswith("U"):
        port_part = port_part[:-1]
    if not port_part.isdigit():
        return ()
    port = int(port_part, 10)
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid local port in -L expression: {binding}")
    return (port,)
