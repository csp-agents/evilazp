"""Parsing for the `/devtunnels` shell command.

The shell owns UX and session state; this module only converts the slash
command text into small command objects so lifecycle code can stay testable.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal


Action = Literal["connect", "list", "stop", "sp"]


@dataclass(frozen=True)
class DevTunnelSpec:
    """A requested local forward to an already-hosted DevTunnel port."""

    tunnel_id: str
    remote_port: int
    local_port: int


@dataclass(frozen=True)
class DevTunnelCommand:
    """Parsed `/devtunnels` command."""

    action: Action
    spec: DevTunnelSpec | None = None
    target: str | None = None
    sp_tenant_id: str | None = None
    sp_client_id: str | None = None
    sp_client_secret: str | None = None


def parse_devtunnels_command(arg: str) -> DevTunnelCommand:
    tokens = shlex.split(arg)
    if not tokens:
        raise ValueError("usage: /devtunnels start --tunnel-id <tunnel-id> --port <remote-port> [--local <local-port>]")

    verb = tokens[0].lower()
    if verb == "start":
        tokens = tokens[1:]
        if not tokens:
            raise ValueError("usage: /devtunnels start --tunnel-id <tunnel-id> --port <remote-port> [--local <local-port>]")
    if verb == "list":
        if len(tokens) != 1:
            raise ValueError("usage: /devtunnels list")
        return DevTunnelCommand("list")
    if verb == "stop":
        if len(tokens) != 2:
            raise ValueError("usage: /devtunnels stop <id|localPort|all>")
        return DevTunnelCommand("stop", target=tokens[1])
    if verb == "sp":
        options = _parse_options(tokens[1:])
        tenant_id = _get_option(options, "tenant-id", "sp-tenant-id", "tenant")
        client_id = _get_option(options, "sp-client-id", "client-id", "client")
        client_secret = _get_option(options, "sp-secret", "sp-client-secret", "client-secret", "secret")
        missing = [
            name
            for name, value in (
                ("tenant-id", tenant_id),
                ("sp-client-id", client_id),
                ("sp-secret", client_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError("missing SP option(s): " + ", ".join(missing))
        return DevTunnelCommand(
            "sp",
            sp_tenant_id=tenant_id,
            sp_client_id=client_id,
            sp_client_secret=client_secret,
        )

    options = _parse_options(tokens)
    tunnel_id = _get_option(options, "tunnel-id", "id")
    remote_port = _parse_port(_get_option(options, "port", "remote-port"), "remote")
    local_port = _parse_port(_get_option(options, "local", "local-port") or str(remote_port), "local")
    if not tunnel_id:
        raise ValueError("missing required option: --tunnel-id <tunnel-id>")
    return DevTunnelCommand("connect", spec=DevTunnelSpec(tunnel_id, remote_port, local_port))


def _parse_options(tokens: list[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            raise ValueError(f"unexpected argument: {token}")
        key = token.lstrip("-")
        if not key:
            raise ValueError(f"invalid option: {token}")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
            raise ValueError(f"missing value for option: {token}")
        options[key] = tokens[index + 1]
        index += 2
    return options


def _get_option(options: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = options.get(name)
        if value:
            return value
    return None


def _parse_port(value: str | None, name: str) -> int:
    if not value:
        raise ValueError(f"missing required option: -{name} <port>")
    try:
        port = int(value, 10)
    except ValueError as exc:
        raise ValueError(f"invalid {name} port: {value}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid {name} port: {value}")
    return port
