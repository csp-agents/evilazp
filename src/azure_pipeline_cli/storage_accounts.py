"""Azure Storage account management through Azure CLI."""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Literal

StorageAccountAction = Literal["list", "create", "remove", "key"]


@dataclass(frozen=True)
class StorageAccount:
    """A storage account record suitable for shell display."""

    name: str
    resource_group: str
    location: str
    sku: str
    kind: str


@dataclass(frozen=True)
class StorageAccountCommand:
    """Parsed `/azure-files storage-account` command."""

    action: StorageAccountAction
    name: str = ""
    resource_group: str = ""
    location: str = ""
    sku: str = "Standard_LRS"
    kind: str = "StorageV2"


class StorageAccountError(RuntimeError):
    """A user-facing Azure Storage account management failure."""


def parse_storage_account_command(arg: str) -> StorageAccountCommand:
    tokens = shlex.split(arg)
    if not tokens:
        raise ValueError("usage: /azure-files storage-account <list|create|remove|key>")
    action = tokens[0].lower()
    if action not in {"list", "create", "remove", "key"}:
        raise ValueError("usage: /azure-files storage-account <list|create|remove|key>")
    options, positional = _parse_options(tokens[1:])
    if positional:
        if "account" not in options and "name" not in options and "n" not in options:
            options["name"] = positional.pop(0)
        if positional:
            raise ValueError(f"unexpected argument: {positional[0]}")
    if action == "list":
        if options:
            raise ValueError("usage: /azure-files storage-account list")
        return StorageAccountCommand("list")
    name = options.get("account") or options.get("name") or options.get("n")
    resource_group = options.get("g") or options.get("resource-group")
    if not name:
        raise ValueError("missing required option: --account <storage-account>")
    if action == "key":
        return StorageAccountCommand("key", name=name, resource_group=resource_group or "")
    if not resource_group:
        raise ValueError("missing required option: -g <resource-group>")
    if action == "create":
        location = options.get("l") or options.get("location")
        if not location:
            raise ValueError("missing required option: -l <location>")
        return StorageAccountCommand(
            "create",
            name=name,
            resource_group=resource_group,
            location=location,
            sku=options.get("sku") or "Standard_LRS",
            kind=options.get("kind") or "StorageV2",
        )
    return StorageAccountCommand("remove", name=name, resource_group=resource_group)


class StorageAccountManager:
    """List, create, and remove Azure Storage accounts with Azure CLI."""

    def __init__(self, az_command: list[str] | None = None) -> None:
        self.az_command = az_command or ["az"]

    def list(self) -> list[StorageAccount]:
        payload = self._run_json("storage", "account", "list")
        return [_storage_account(item) for item in payload if item.get("name")]

    def create(self, name: str, resource_group: str, location: str, sku: str = "Standard_LRS", kind: str = "StorageV2") -> StorageAccount:
        payload = self._run_json(
            "storage",
            "account",
            "create",
            "-n",
            name,
            "-g",
            resource_group,
            "-l",
            location,
            "--sku",
            sku,
            "--kind",
            kind,
        )
        return _storage_account(payload)

    def remove(self, name: str, resource_group: str) -> None:
        self._run("storage", "account", "delete", "-n", name, "-g", resource_group, "--yes")

    def key(self, name: str, resource_group: str = "") -> str:
        args = ["storage", "account", "keys", "list", "-n", name]
        if resource_group:
            args.extend(["-g", resource_group])
        payload = self._run_json(*args)
        if not isinstance(payload, list) or not payload:
            raise StorageAccountError("Azure CLI returned no storage account keys")
        key = str(payload[0].get("value", "")) if isinstance(payload[0], dict) else ""
        if not key:
            raise StorageAccountError("Azure CLI returned an empty storage account key")
        return key

    def _run_json(self, *args: str):
        output = self._run(*args, "-o", "json")
        try:
            return json.loads(output or "null")
        except json.JSONDecodeError as exc:
            raise StorageAccountError(f"Azure CLI returned invalid JSON: {exc}") from exc

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
            raise StorageAccountError(f"failed to run Azure CLI: {exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or f"Azure CLI exited with {result.returncode}"
            raise StorageAccountError(message)
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
        if key not in {"account", "n", "name", "g", "resource-group", "l", "location", "sku", "kind"}:
            raise ValueError(f"unexpected option: {token}")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
            raise ValueError(f"missing value for option: {token}")
        options[key] = tokens[index + 1]
        index += 2
    return options, positional


def _storage_account(payload: dict) -> StorageAccount:
    sku = payload.get("sku") or {}
    return StorageAccount(
        name=str(payload.get("name", "")),
        resource_group=str(payload.get("resourceGroup", "")),
        location=str(payload.get("location", "")),
        sku=str(sku.get("name", "")) if isinstance(sku, dict) else str(sku),
        kind=str(payload.get("kind", "")),
    )
