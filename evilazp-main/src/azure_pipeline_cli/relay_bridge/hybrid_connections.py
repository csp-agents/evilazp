"""Azure Relay Hybrid Connection management through Azure CLI.

The Relay Bridge connection string can open data-plane tunnels, but creating
or deleting Hybrid Connections is an Azure management-plane operation. This
wrapper keeps that operation explicit and isolated behind `az relay hyco`.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class HybridConnection:
    """A Hybrid Connection record suitable for shell display."""

    name: str
    requires_client_authorization: bool
    user_metadata: str | None = None


@dataclass(frozen=True)
class RelayNamespace:
    """An Azure Relay namespace record suitable for shell display."""

    name: str
    location: str
    provisioning_state: str
    service_bus_endpoint: str | None = None


@dataclass(frozen=True)
class RelayNamespaceKeys:
    """Namespace-level SAS keys and connection strings from Azure CLI."""

    primary_connection_string: str
    secondary_connection_string: str
    primary_key: str
    secondary_key: str
    key_name: str


class HybridConnectionError(RuntimeError):
    """A user-facing Hybrid Connection management failure."""


_AZURE_ERROR_CODE_RE = re.compile(r"(?:ERROR:\s*)?\(?([A-Za-z][A-Za-z0-9]+)\)?|Code[:=]\s*\"?([A-Za-z][A-Za-z0-9]+)")
_RESOURCE_GROUP_RE = re.compile(r"resourceGroups/([^/\s'\"]+)|resource group ['\"]?([^'\".\s]+)", re.IGNORECASE)
_RELAY_NAMESPACE_RE = re.compile(r"Microsoft\.Relay/namespaces/([^/\s'\"]+)", re.IGNORECASE)


def format_azure_cli_error(message: str, command: list[str]) -> str:
    """Convert Azure CLI/ARM errors into concise operator guidance.

    Azure CLI usually includes an ARM error code in stderr, but the raw text is
    long and points at provider internals. This function keeps the actionable
    code while replacing the message with the likely user mistake and the next
    command to run.
    """

    clean_message = " ".join((message or "").split())
    code = _extract_azure_error_code(clean_message)
    resource_group = _command_value(command, "-g", "--resource-group") or _extract_first(_RESOURCE_GROUP_RE, clean_message)
    namespace = _command_value(command, "--namespace-name", "-n") or _extract_first(_RELAY_NAMESPACE_RE, clean_message)
    action = _relay_action_label(command)
    rg_hint = resource_group or "<resource-group>"
    ns_hint = namespace or "<namespace>"

    if code == "ParentResourceNotFound":
        if "authorization-rule" in command or "hyco" in command:
            return (
                f"Azure Relay namespace를 찾지 못해서 {action} 작업을 할 수 없습니다. "
                f"resource group '{resource_group or '?'}' 안에 namespace '{namespace or '?'}'가 실제로 있는지 확인하세요. "
                f"먼저 `/relay-bridge ns list -g {rg_hint}`로 이름을 확인하고, 없으면 "
                f"`/relay-bridge ns create -g {rg_hint} -n {ns_hint} -l <location>`로 생성하세요. "
                "다른 구독에 만든 namespace라면 Azure CLI의 현재 subscription도 맞춰야 합니다."
            )
        return (
            f"부모 Azure 리소스를 찾지 못해서 {action} 작업을 할 수 없습니다. "
            "리소스 이름, resource group, subscription이 맞는지 확인하세요."
        )

    if code in {"ResourceNotFound", "NotFound"}:
        return (
            f"대상 Azure 리소스를 찾지 못해서 {action} 작업을 할 수 없습니다. "
            f"resource group '{resource_group or '?'}', namespace '{namespace or '?'}', Hybrid Connection 이름을 다시 확인하세요. "
            f"namespace는 `/relay-bridge ns list -g {rg_hint}`로, HC는 "
            f"`/relay-bridge hc list -g {rg_hint} -n {ns_hint}`로 확인할 수 있습니다. "
            "리소스가 다른 구독에 있으면 Azure CLI subscription을 먼저 전환하세요."
        )

    if code == "ResourceGroupNotFound":
        return (
            f"resource group '{resource_group or '?'}'를 찾지 못했습니다. "
            "resource group 이름과 현재 Azure CLI subscription을 확인한 뒤, 없으면 Azure에서 resource group을 먼저 생성하세요."
        )

    if code == "AuthorizationFailed":
        return (
            f"현재 Azure 계정에 {action} 작업 권한이 없습니다. "
            "Azure CLI가 올바른 tenant/subscription에 로그인되어 있는지 확인하고, 해당 resource group 또는 Relay namespace에 "
            "`Contributor` 또는 Relay 관리 권한을 부여하세요."
        )

    if code in {"MissingSubscriptionRegistration", "NoRegisteredProviderFound"}:
        return (
            "현재 subscription에서 Microsoft.Relay resource provider를 사용할 수 없습니다. "
            "`az provider register --namespace Microsoft.Relay`로 provider를 등록한 뒤 다시 시도하세요. "
            "등록 완료 여부는 `az provider show --namespace Microsoft.Relay --query registrationState -o tsv`로 확인할 수 있습니다."
        )

    if code in {"InvalidResourceName", "BadRequest", "InvalidTemplate"}:
        return (
            f"Azure Relay 리소스 이름 또는 요청 형식이 올바르지 않아 {action} 작업이 실패했습니다. "
            "namespace와 Hybrid Connection 이름에 허용되지 않는 문자, 공백, 잘못된 구분자가 없는지 확인하세요."
        )

    if code == "Conflict":
        return (
            f"같은 이름의 Azure Relay 리소스가 이미 있거나 현재 변경 중이라 {action} 작업이 충돌했습니다. "
            "기존 리소스를 list 명령으로 확인하거나 잠시 후 다시 시도하세요."
        )

    if "az: command not found" in clean_message or "No such file or directory" in clean_message:
        return "Azure CLI를 실행할 수 없습니다. `az`가 설치되어 있고 PATH에 잡혀 있는지 확인하세요."

    if code:
        return f"Azure CLI 작업이 실패했습니다. 코드: {code}. 리소스 이름, resource group, subscription, 권한을 확인하세요."
    return f"Azure CLI 작업이 실패했습니다. 리소스 이름, resource group, subscription, 권한을 확인하세요. 원인: {clean_message}"


def _extract_azure_error_code(message: str) -> str | None:
    for match in _AZURE_ERROR_CODE_RE.finditer(message):
        code = match.group(2) or match.group(1)
        if code and code not in {"ERROR", "Failed", "Message", "Code"}:
            return code
    return None


def _extract_first(pattern: re.Pattern[str], message: str) -> str | None:
    match = pattern.search(message)
    if not match:
        return None
    for value in match.groups():
        if value:
            return value
    return None


def _command_value(command: list[str], *names: str) -> str | None:
    for index, item in enumerate(command):
        if item in names and index + 1 < len(command):
            return command[index + 1]
    return None


def _relay_action_label(command: list[str]) -> str:
    if command[1:6] == ["relay", "namespace", "authorization-rule", "keys", "list"]:
        return "namespace key 조회"
    if command[1:4] == ["relay", "namespace", "list"]:
        return "namespace 목록 조회"
    if command[1:4] == ["relay", "namespace", "create"]:
        return "namespace 생성"
    if command[1:4] == ["relay", "namespace", "delete"]:
        return "namespace 삭제"
    if command[1:3] == ["relay", "hyco"]:
        if "list" in command:
            return "Hybrid Connection 목록 조회"
        if "create" in command:
            return "Hybrid Connection 생성"
        if "delete" in command:
            return "Hybrid Connection 삭제"
    return "Azure Relay 관리"


class HybridConnectionManager:
    """List, create, and delete Azure Relay Hybrid Connections with Azure CLI."""

    def __init__(self, az_command: list[str] | None = None) -> None:
        self.az_command = az_command or ["az"]

    def list_namespaces(self, resource_group: str) -> list[RelayNamespace]:
        payload = self._run_json(
            "relay",
            "namespace",
            "list",
            "-g",
            resource_group,
        )
        return [
            RelayNamespace(
                name=str(item.get("name", "")),
                location=str(item.get("location", "")),
                provisioning_state=str(item.get("provisioningState", "")),
                service_bus_endpoint=item.get("serviceBusEndpoint"),
            )
            for item in payload
            if item.get("name")
        ]

    def create_namespace(self, resource_group: str, name: str, location: str | None = None) -> RelayNamespace:
        args = [
            "relay",
            "namespace",
            "create",
            "-g",
            resource_group,
            "-n",
            name,
        ]
        if location:
            args.extend(["-l", location])
        payload = self._run_json(*args)
        return RelayNamespace(
            name=str(payload.get("name", name)),
            location=str(payload.get("location", "")),
            provisioning_state=str(payload.get("provisioningState", "")),
            service_bus_endpoint=payload.get("serviceBusEndpoint"),
        )

    def delete_namespace(self, resource_group: str, name: str) -> None:
        self._run(
            "relay",
            "namespace",
            "delete",
            "-g",
            resource_group,
            "-n",
            name,
        )

    def get_namespace_keys(
        self,
        resource_group: str,
        namespace: str,
        auth_rule: str = "RootManageSharedAccessKey",
    ) -> RelayNamespaceKeys:
        payload = self._run_json(
            "relay",
            "namespace",
            "authorization-rule",
            "keys",
            "list",
            "-g",
            resource_group,
            "--namespace-name",
            namespace,
            "-n",
            auth_rule,
        )
        return RelayNamespaceKeys(
            primary_connection_string=str(payload.get("primaryConnectionString", "")),
            secondary_connection_string=str(payload.get("secondaryConnectionString", "")),
            primary_key=str(payload.get("primaryKey", "")),
            secondary_key=str(payload.get("secondaryKey", "")),
            key_name=str(payload.get("keyName", auth_rule)),
        )

    def list(self, resource_group: str, namespace: str) -> list[HybridConnection]:
        payload = self._run_json(
            "relay",
            "hyco",
            "list",
            "-g",
            resource_group,
            "--namespace-name",
            namespace,
        )
        return [
            HybridConnection(
                name=str(item.get("name", "")),
                requires_client_authorization=bool(item.get("requiresClientAuthorization", True)),
                user_metadata=item.get("userMetadata"),
            )
            for item in payload
            if item.get("name")
        ]

    def create(
        self,
        resource_group: str,
        namespace: str,
        name: str,
        requires_client_authorization: bool = True,
    ) -> HybridConnection:
        payload = self._run_json(
            "relay",
            "hyco",
            "create",
            "-g",
            resource_group,
            "--namespace-name",
            namespace,
            "-n",
            name,
            "--requires-client-authorization",
            "true" if requires_client_authorization else "false",
        )
        return HybridConnection(
            name=str(payload.get("name", name)),
            requires_client_authorization=bool(payload.get("requiresClientAuthorization", requires_client_authorization)),
            user_metadata=payload.get("userMetadata"),
        )

    def delete(self, resource_group: str, namespace: str, name: str) -> None:
        self._run(
            "relay",
            "hyco",
            "delete",
            "-g",
            resource_group,
            "--namespace-name",
            namespace,
            "-n",
            name,
            "--yes",
        )

    def _run_json(self, *args: str):
        output = self._run(*args, "-o", "json")
        try:
            return json.loads(output or "null")
        except json.JSONDecodeError as exc:
            raise HybridConnectionError(f"Azure CLI returned invalid JSON: {exc}") from exc

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
            raise HybridConnectionError(f"failed to run Azure CLI: {exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or f"Azure CLI exited with {result.returncode}"
            raise HybridConnectionError(format_azure_cli_error(message, [*self.az_command, *args]))
        return result.stdout
