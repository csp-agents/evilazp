"""Azure resource group management through Azure CLI.

Relay namespaces must live inside a resource group. This module keeps that
Azure management-plane dependency explicit and testable behind `az group`.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Literal

from .relay_bridge.hybrid_connections import format_azure_cli_error


ResourceGroupAction = Literal["list", "create", "remove"]


@dataclass(frozen=True)
class ResourceGroup:
    """A resource group record suitable for shell display."""

    name: str
    location: str
    provisioning_state: str | None = None


@dataclass(frozen=True)
class ResourceGroupCommand:
    """Parsed `/resource-group` command."""

    action: ResourceGroupAction
    name: str | None = None
    location: str | None = None
    yes: bool = False


class ResourceGroupError(RuntimeError):
    """A user-facing Azure resource group management failure."""


def parse_resource_group_command(arg: str) -> ResourceGroupCommand:
    tokens = shlex.split(arg)
    if not tokens:
        raise ValueError("usage: /resource-group <list|create|remove>")
    action = tokens[0].lower()
    if action not in {"list", "create", "remove"}:
        raise ValueError("usage: /resource-group <list|create|remove>")
    options, positional = _parse_options(tokens[1:])
    if action == "list":
        if options or positional:
            raise ValueError("usage: /resource-group list")
        return ResourceGroupCommand("list")
    if positional:
        if "g" not in options and "name" not in options:
            options["g"] = positional.pop(0)
        if positional:
            raise ValueError(f"unexpected argument: {positional[0]}")
    name = options.get("g") or options.get("name") or options.get("n")
    if not name:
        raise ValueError("missing required option: -g <resource-group>")
    if action == "create":
        location = options.get("l") or options.get("location")
        if not location:
            raise ValueError("missing required option: -l <location>")
        return ResourceGroupCommand("create", name=name, location=location)
    return ResourceGroupCommand("remove", name=name, yes="yes" in options)


class ResourceGroupManager:
    """List, create, and remove Azure resource groups with Azure CLI."""

    def __init__(self, az_command: list[str] | None = None) -> None:
        self.az_command = az_command or ["az"]

    def list(self) -> list[ResourceGroup]:
        payload = self._run_json("group", "list")
        return [
            ResourceGroup(
                name=str(item.get("name", "")),
                location=str(item.get("location", "")),
                provisioning_state=_provisioning_state(item),
            )
            for item in payload
            if item.get("name")
        ]

    def create(self, name: str, location: str) -> ResourceGroup:
        payload = self._run_json("group", "create", "-n", name, "-l", location)
        return ResourceGroup(
            name=str(payload.get("name", name)),
            location=str(payload.get("location", location)),
            provisioning_state=_provisioning_state(payload),
        )

    def remove(self, name: str) -> None:
        self._run("group", "delete", "-n", name, "--yes")

    def _run_json(self, *args: str):
        output = self._run(*args, "-o", "json")
        try:
            return json.loads(output or "null")
        except json.JSONDecodeError as exc:
            raise ResourceGroupError(f"Azure CLI returned invalid JSON: {exc}") from exc

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                [*self.az_command, *args],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise ResourceGroupError(f"failed to run Azure CLI: {exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or f"Azure CLI exited with {result.returncode}"
            raise ResourceGroupError(format_azure_cli_error(message, [*self.az_command, *args]))
        return result.stdout


def _parse_options(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
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
        if key == "yes":
            options[key] = "true"
            index += 1
            continue
        if key not in {"g", "name", "n", "l", "location"}:
            raise ValueError(f"unexpected option: {token}")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
            raise ValueError(f"missing value for option: {token}")
        options[key] = tokens[index + 1]
        index += 2
    return options, positional


def _provisioning_state(payload: dict) -> str | None:
    properties = payload.get("properties") or {}
    value = payload.get("provisioningState") or properties.get("provisioningState")
    return str(value) if value else None
