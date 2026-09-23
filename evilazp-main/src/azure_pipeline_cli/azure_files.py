"""Azure Files transfer helpers for the interactive shell.

This module loads Azure Files SAS settings from the existing operator `.env`
file, validates `/share/path` remote paths, and performs file or recursive
directory transfers. Shell code should only parse user input and print results;
all Azure SDK details stay here.
"""

from __future__ import annotations

import os
import shlex
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from .agent_builder import DEFAULT_YAML_PATH, load_yaml_config


Action = Literal["upload", "download"]
Target = Literal["local", "agent"]
AzureFilesManagementAction = Literal[
    "share-list",
    "share-create",
    "share-remove",
    "directory-create",
    "directory-remove",
    "list",
    "sas",
    "storage-account-list",
    "storage-account-create",
    "storage-account-remove",
    "storage-account-key",
]


class AzureFilesError(RuntimeError):
    """A user-facing Azure Files transfer failure."""


@dataclass(frozen=True)
class AzureFilesConfig:
    """Storage account settings read from the operator `.env` file."""

    account: str
    sas: str
    account_key: str = ""


@dataclass(frozen=True)
class AzureFilesRemotePath:
    """A validated Azure Files path of the form `/share/path/to/item`."""

    share: str
    path: str

    @property
    def display(self) -> str:
        return f"/{self.share}/{self.path}"


@dataclass(frozen=True)
class AzureFilesCommand:
    """Parsed `/upload` or `/download` command arguments."""

    action: Action
    source: str
    destination: str
    overwrite: bool = False
    target: Target = "local"


@dataclass(frozen=True)
class AzureFilesTransferResult:
    """Summary displayed by the shell after a successful transfer."""

    action: Action
    source: str
    destination: str
    files: int


@dataclass(frozen=True)
class AzureFilesListItem:
    """One file or directory entry returned by an Azure Files list command."""

    type: str
    name: str
    size: str = "-"


@dataclass(frozen=True)
class AzureFilesManagementCommand:
    """Parsed `/azure-files` management command."""

    action: AzureFilesManagementAction
    share: str = ""
    path: str = ""
    permissions: str = "rwld"
    hours: int = 24
    resource_group: str = ""
    location: str = ""
    sku: str = "Standard_LRS"
    kind: str = "StorageV2"


ServiceClientFactory = Callable[[AzureFilesConfig], object]


def parse_azure_files_command(action: Action, arg: str) -> AzureFilesCommand:
    """Parse two positional transfer arguments and transfer options."""

    try:
        tokens = _split_transfer_args(arg)
    except ValueError as exc:
        raise AzureFilesError(str(exc)) from exc

    overwrite = action == "upload"
    target: Target = "local"
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--overwrite":
            overwrite = True
            index += 1
            continue
        if token == "--target":
            if index + 1 >= len(tokens):
                raise AzureFilesError(_usage(action))
            value = tokens[index + 1].lower()
            if value not in {"local", "agent"}:
                raise AzureFilesError("target must be local or agent")
            target = value  # type: ignore[assignment]
            index += 2
            continue
        if token.startswith("-"):
            raise AzureFilesError(_usage(action))
        positional.append(token)
        index += 1
    if len(positional) != 2:
        raise AzureFilesError(_usage(action))
    if action == "upload":
        parse_remote_path(positional[1])
    else:
        parse_remote_path(positional[0])
    return AzureFilesCommand(action, positional[0], positional[1], overwrite, target)


def parse_azure_files_management_command(arg: str) -> AzureFilesManagementCommand:
    """Parse `/azure-files` share, directory, list, and SAS commands."""

    try:
        tokens = shlex.split(arg)
    except ValueError as exc:
        raise AzureFilesError(str(exc)) from exc
    if not tokens:
        raise AzureFilesError(_azure_files_usage())

    group = tokens[0].lower()
    if group == "share":
        return _parse_share_command(tokens[1:])
    if group == "directory":
        return _parse_directory_command(tokens[1:])
    if group == "list":
        options, positional = _parse_options_and_positionals(tokens[1:], allowed={"share"})
        if positional or not options.get("share"):
            raise AzureFilesError("usage: /azure-files list --share /<share-name>[/directory]")
        remote = parse_share_path(options["share"], allow_root=True)
        return AzureFilesManagementCommand("list", share=remote.share, path=remote.path)
    if group == "sas":
        options, positional = _parse_options_and_positionals(tokens[1:], allowed={"permissions", "hours"})
        if positional:
            raise AzureFilesError("usage: /azure-files sas [--permissions rwld] [--hours 24]")
        hours = _parse_positive_int(options.get("hours", "24"), "hours")
        permissions = options.get("permissions", "rwld")
        if not permissions or any(item not in "racwdl" for item in permissions):
            raise AzureFilesError("permissions must contain only r, a, c, w, d, l")
        return AzureFilesManagementCommand("sas", share="", permissions=permissions, hours=hours)
    if group == "storage-account":
        return _parse_storage_account_management_command(tokens[1:])
    raise AzureFilesError(_azure_files_usage())


def load_azure_files_config(path: str | os.PathLike[str] | None = None, *, require_sas: bool = True) -> AzureFilesConfig:
    """Load Azure Files account and SAS values from the existing YAML parser."""

    config = load_yaml_config(str(path or DEFAULT_YAML_PATH))
    account = (config.get("azure_files_account") or "").strip()
    sas = (config.get("azure_files_sas") or "").strip()
    account_key = (config.get("azure_files_account_key") or "").strip()
    required = [("azure_files_account", account)]
    if require_sas:
        required.append(("azure_files_sas", sas))
    missing = [name for name, value in required if not value]
    if missing:
        raise AzureFilesError(f"missing Azure Files config value(s) in {path or DEFAULT_YAML_PATH}: {', '.join(missing)}")
    return AzureFilesConfig(account=account, sas=sas, account_key=account_key)


def save_azure_files_config_values(
    values: dict[str, str],
    path: str | os.PathLike[str] | None = None,
) -> None:
    """Update Azure Files keys in the operator flat YAML config."""

    allowed = {"azure_files_account", "azure_files_account_key", "azure_files_sas"}
    unexpected = sorted(set(values) - allowed)
    if unexpected:
        raise AzureFilesError(f"unsupported Azure Files config key(s): {', '.join(unexpected)}")
    config_path = Path(path or DEFAULT_YAML_PATH)
    try:
        text = config_path.read_text() if config_path.exists() else ""
    except OSError as exc:
        raise AzureFilesError(f"failed to read config file {config_path}: {exc}") from exc

    remaining = dict(values)
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        matched = False
        for key in tuple(remaining):
            if stripped.startswith(f"{key}:"):
                indent = line[: len(line) - len(stripped)]
                lines.append(f"{indent}{key}: {_yaml_quote(remaining.pop(key))}")
                matched = True
                break
        if not matched:
            lines.append(line)
    if remaining and lines and lines[-1].strip():
        lines.append("")
    for key, value in remaining.items():
        lines.append(f"{key}: {_yaml_quote(value)}")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("\n".join(lines) + ("\n" if lines else ""))
    except OSError as exc:
        raise AzureFilesError(f"failed to write config file {config_path}: {exc}") from exc


def parse_remote_path(value: str) -> AzureFilesRemotePath:
    """Validate `/share/path/to/item` and split it into share and path."""

    if not value.startswith("/"):
        raise AzureFilesError("remote path must use /<share-name>/<remote-path>")
    parts = [part for part in value.split("/") if part]
    if len(parts) < 2:
        raise AzureFilesError("remote path must use /<share-name>/<remote-path>")
    share = parts[0]
    path = str(PurePosixPath(*parts[1:]))
    if not share or not path or path == ".":
        raise AzureFilesError("remote path must use /<share-name>/<remote-path>")
    return AzureFilesRemotePath(share=share, path=path)


def parse_share_path(value: str, *, allow_root: bool = False) -> AzureFilesRemotePath:
    """Parse `/share` or `/share/path` command arguments."""

    if not value.startswith("/"):
        raise AzureFilesError("share path must use /<share-name>[/directory]")
    parts = [part for part in value.split("/") if part]
    if not parts:
        raise AzureFilesError("share path must use /<share-name>[/directory]")
    share = parts[0]
    path = str(PurePosixPath(*parts[1:])) if len(parts) > 1 else ""
    if not allow_root and not path:
        raise AzureFilesError("share path must include a directory")
    return AzureFilesRemotePath(share=share, path=path)


def list_shares(
    *,
    config_path: str | os.PathLike[str] | None = None,
    service_client_factory: ServiceClientFactory | None = None,
) -> list[str]:
    service = _configured_service(config_path, service_client_factory)
    try:
        return sorted(str(getattr(item, "name", item.get("name") if isinstance(item, dict) else item)) for item in service.list_shares())
    except Exception as exc:
        raise AzureFilesError(str(exc)) from exc


def create_share(
    share: str,
    *,
    config_path: str | os.PathLike[str] | None = None,
    service_client_factory: ServiceClientFactory | None = None,
) -> str:
    service = _configured_service(config_path, service_client_factory)
    share = _normalize_share_name(share)
    try:
        service.create_share(share)
    except Exception as exc:
        raise AzureFilesError(str(exc)) from exc
    return share


def remove_share(
    share: str,
    *,
    config_path: str | os.PathLike[str] | None = None,
    service_client_factory: ServiceClientFactory | None = None,
) -> str:
    service = _configured_service(config_path, service_client_factory)
    share = _normalize_share_name(share)
    try:
        service.delete_share(share)
    except Exception as exc:
        raise AzureFilesError(str(exc)) from exc
    return share


def create_directory(
    share: str,
    path: str,
    *,
    config_path: str | os.PathLike[str] | None = None,
    service_client_factory: ServiceClientFactory | None = None,
) -> AzureFilesRemotePath:
    service = _configured_service(config_path, service_client_factory)
    remote = AzureFilesRemotePath(_normalize_share_name(share), _normalize_directory_path(path))
    try:
        _ensure_remote_directories(service.get_share_client(remote.share), remote.path)
    except Exception as exc:
        raise AzureFilesError(str(exc)) from exc
    return remote


def remove_directory(
    share: str,
    path: str,
    *,
    config_path: str | os.PathLike[str] | None = None,
    service_client_factory: ServiceClientFactory | None = None,
) -> AzureFilesRemotePath:
    service = _configured_service(config_path, service_client_factory)
    remote = AzureFilesRemotePath(_normalize_share_name(share), _normalize_directory_path(path))
    try:
        service.get_share_client(remote.share).get_directory_client(remote.path).delete_directory()
    except Exception as exc:
        raise AzureFilesError(str(exc)) from exc
    return remote


def list_directory(
    share: str,
    path: str = "",
    *,
    config_path: str | os.PathLike[str] | None = None,
    service_client_factory: ServiceClientFactory | None = None,
) -> list[AzureFilesListItem]:
    service = _configured_service(config_path, service_client_factory)
    try:
        share_client = service.get_share_client(_normalize_share_name(share))
        items = share_client.list_directories_and_files(directory_name=path or None)
        return sorted((_list_item(item) for item in items), key=lambda item: (item.type, item.name))
    except Exception as exc:
        raise AzureFilesError(str(exc)) from exc


def generate_share_sas_token(
    share: str,
    *,
    permissions: str = "rwld",
    hours: int = 24,
    config_path: str | os.PathLike[str] | None = None,
) -> str:
    config = load_azure_files_config(config_path, require_sas=False)
    if not config.account_key:
        raise AzureFilesError(f"missing Azure Files config value(s) in {config_path or DEFAULT_YAML_PATH}: azure_files_account_key")
    try:
        from azure.storage.fileshare import AccountSasPermissions, ResourceTypes, Services, generate_account_sas
    except ImportError as exc:
        raise AzureFilesError("missing dependency: install azure-storage-file-share") from exc
    expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
    try:
        return generate_account_sas(
            account_name=config.account,
            account_key=config.account_key,
            resource_types=ResourceTypes.from_string("sco"),
            permission=AccountSasPermissions.from_string(permissions),
            expiry=expiry,
            services=Services.from_string("f"),
        )
    except Exception as exc:
        raise AzureFilesError(mask_secret(str(exc), config.account_key)) from exc


def upload(
    local_path: str | os.PathLike[str],
    remote_value: str,
    *,
    overwrite: bool = True,
    config_path: str | os.PathLike[str] | None = None,
    service_client_factory: ServiceClientFactory | None = None,
) -> AzureFilesTransferResult:
    """Upload a file or directory tree to Azure Files."""

    source = Path(local_path)
    if not source.exists():
        raise AzureFilesError(f"local path not found: {source}")
    remote = parse_remote_path(remote_value)
    if _remote_path_is_directory_target(remote_value):
        remote = AzureFilesRemotePath(remote.share, _remote_join(remote.path, source.name))
    config = load_azure_files_config(config_path)
    try:
        service = _service_client(config, service_client_factory)
        share_client = service.get_share_client(remote.share)

        if source.is_dir():
            files = 0
            for item in sorted(path for path in source.rglob("*") if path.is_file()):
                relative = item.relative_to(source).as_posix()
                target_path = _remote_join(remote.path, relative)
                _upload_one(share_client, item, target_path, overwrite=overwrite)
                files += 1
            return AzureFilesTransferResult("upload", str(source), remote.display, files)

        if not source.is_file():
            raise AzureFilesError(f"local path is not a file or directory: {source}")
        _upload_one(share_client, source, remote.path, overwrite=overwrite)
        return AzureFilesTransferResult("upload", str(source), remote.display, 1)
    except AzureFilesError as exc:
        raise AzureFilesError(mask_secret(str(exc), config.sas)) from exc
    except Exception as exc:
        raise AzureFilesError(mask_secret(str(exc), config.sas)) from exc


def download(
    remote_value: str,
    local_path: str | os.PathLike[str],
    *,
    overwrite: bool = False,
    config_path: str | os.PathLike[str] | None = None,
    service_client_factory: ServiceClientFactory | None = None,
) -> AzureFilesTransferResult:
    """Download a remote file or directory tree from Azure Files."""

    remote = parse_remote_path(remote_value)
    destination = Path(local_path)
    config = load_azure_files_config(config_path)
    try:
        service = _service_client(config, service_client_factory)
        share_client = service.get_share_client(remote.share)

        file_client = share_client.get_file_client(remote.path)
        if _remote_file_exists(file_client):
            if destination.exists() and destination.is_dir() or _local_path_is_directory_target(local_path):
                destination = destination / PurePosixPath(remote.path).name
            _download_one(file_client, destination, overwrite=overwrite)
            return AzureFilesTransferResult("download", remote.display, str(destination), 1)

        files = _download_directory(share_client, remote.path, destination, overwrite=overwrite)
        return AzureFilesTransferResult("download", remote.display, str(destination), files)
    except AzureFilesError as exc:
        raise AzureFilesError(mask_secret(str(exc), config.sas)) from exc
    except Exception as exc:
        raise AzureFilesError(mask_secret(str(exc), config.sas)) from exc


def create_service_client(config: AzureFilesConfig) -> object:
    """Build an Azure Files service client from account name and SAS token."""

    try:
        from azure.storage.fileshare import ShareServiceClient
    except ImportError as exc:
        raise AzureFilesError("missing dependency: install azure-storage-file-share") from exc

    return ShareServiceClient(
        account_url=f"https://{config.account}.file.core.windows.net",
        credential=config.sas.lstrip("?"),
    )


def mask_secret(text: str, secret: str) -> str:
    """Remove a SAS token from SDK error text before showing it."""

    if not secret:
        return text
    masked = text.replace(secret, "***")
    stripped = secret.lstrip("?")
    if stripped != secret:
        masked = masked.replace(stripped, "***")
    return masked


def build_agent_transfer_script(command: AzureFilesCommand, config: AzureFilesConfig) -> str:
    """Render a PowerShell Azure Files REST transfer for the selected agent."""

    if command.action == "upload":
        remote = parse_remote_path(command.destination)
        local_path = command.source
    else:
        remote = parse_remote_path(command.source)
        local_path = command.destination

    return f"""$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Account = {_ps_quote(config.account)}
$Sas = {_ps_quote(config.sas)}.TrimStart('?')
$Share = {_ps_quote(remote.share)}
$RemoteRoot = {_ps_quote(remote.path)}
$LocalPath = {_ps_quote(local_path)}
$Overwrite = ${str(command.overwrite).lower()}
$RemoteRootIsDirectoryTarget = ${str(command.action == "upload" and _remote_path_is_directory_target(command.destination)).lower()}
$LocalPathIsDirectoryTarget = ${str(command.action == "download" and _local_path_is_directory_target(command.destination)).lower()}

function ConvertTo-AzFileEncodedPath([string]$Path) {{
    (($Path -split '/') | Where-Object {{ $_ -ne '' }} | ForEach-Object {{ [System.Uri]::EscapeDataString($_) }}) -join '/'
}}

function Join-AzFileRemotePath([string]$Base, [string]$Child) {{
    if ([string]::IsNullOrWhiteSpace($Base)) {{ return ($Child -replace '\\\\', '/') }}
    if ([string]::IsNullOrWhiteSpace($Child)) {{ return ($Base -replace '\\\\', '/') }}
    return (($Base.TrimEnd('/') + '/' + ($Child -replace '\\\\', '/').TrimStart('/')) -replace '//+', '/')
}}

function Get-AzFileUri([string]$Path, [string]$ExtraQuery = '') {{
    $encodedShare = [System.Uri]::EscapeDataString($Share)
    $encodedPath = ConvertTo-AzFileEncodedPath $Path
    $uri = "https://$Account.file.core.windows.net/$encodedShare/$encodedPath`?$Sas"
    if ($ExtraQuery) {{ $uri = "$uri&$ExtraQuery" }}
    return $uri
}}

function New-AzFileHeaders([hashtable]$Extra = @{{}}) {{
    $headers = @{{
        'x-ms-version' = '2022-11-02'
        'x-ms-date' = [DateTime]::UtcNow.ToString('R')
    }}
    foreach ($key in $Extra.Keys) {{ $headers[$key] = $Extra[$key] }}
    return $headers
}}

function Test-AzFileRemoteFile([string]$Path) {{
    try {{
        Invoke-WebRequest -UseBasicParsing -Method Head -Uri (Get-AzFileUri $Path) -Headers (New-AzFileHeaders) | Out-Null
        return $true
    }} catch {{
        $status = $_.Exception.Response.StatusCode.value__
        if ($status -eq 404) {{ return $false }}
        throw
    }}
}}

function Ensure-AzFileDirectory([string]$Path) {{
    $parts = @($Path -split '/' | Where-Object {{ $_ -ne '' }})
    $current = ''
    foreach ($part in $parts) {{
        $current = Join-AzFileRemotePath $current $part
        try {{
            Invoke-WebRequest -UseBasicParsing -Method Put -Uri (Get-AzFileUri $current 'restype=directory') -Headers (New-AzFileHeaders) | Out-Null
        }} catch {{
            $status = $_.Exception.Response.StatusCode.value__
            if ($status -ne 409) {{ throw }}
        }}
    }}
}}

function Ensure-AzFileParentDirectory([string]$Path) {{
    $normalized = $Path -replace '\\\\', '/'
    $index = $normalized.LastIndexOf('/')
    if ($index -gt 0) {{ Ensure-AzFileDirectory $normalized.Substring(0, $index) }}
}}

function Send-AzFileOne([string]$SourcePath, [string]$RemotePath) {{
    $item = Get-Item -LiteralPath $SourcePath
    if (-not $item.PSIsContainer) {{
        if ((Test-AzFileRemoteFile $RemotePath) -and -not $Overwrite) {{ throw "remote file already exists: $RemotePath" }}
        Ensure-AzFileParentDirectory $RemotePath
        $length = [int64]$item.Length
        Invoke-WebRequest -UseBasicParsing -Method Put -Uri (Get-AzFileUri $RemotePath) -Headers (New-AzFileHeaders @{{ 'x-ms-type' = 'file'; 'x-ms-content-length' = "$length" }}) | Out-Null
        if ($length -eq 0) {{ return }}
        $stream = [System.IO.File]::OpenRead($item.FullName)
        try {{
            $bufferSize = 4MB
            $buffer = New-Object byte[] $bufferSize
            $offset = [int64]0
            while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {{
                if ($read -eq $buffer.Length) {{
                    $body = $buffer
                }} else {{
                    $body = New-Object byte[] $read
                    [Array]::Copy($buffer, $body, $read)
                }}
                $end = $offset + $read - 1
                Invoke-WebRequest -UseBasicParsing -Method Put -Uri (Get-AzFileUri $RemotePath 'comp=range') -Headers (New-AzFileHeaders @{{ 'x-ms-range' = "bytes=$offset-$end"; 'x-ms-write' = 'update'; 'Content-Type' = 'application/octet-stream' }}) -Body $body | Out-Null
                $offset += $read
            }}
        }} finally {{
            $stream.Dispose()
        }}
        return
    }}
    throw "local path is not a file: $SourcePath"
}}

function Upload-AzFilePath {{
    $source = Get-Item -LiteralPath $LocalPath
    $effectiveRemoteRoot = $RemoteRoot
    if ($RemoteRootIsDirectoryTarget) {{
        $effectiveRemoteRoot = Join-AzFileRemotePath $RemoteRoot $source.Name
    }}
    $count = 0
    if ($source.PSIsContainer) {{
        $basePath = $source.FullName.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
        Get-ChildItem -LiteralPath $source.FullName -Recurse -File | Sort-Object FullName | ForEach-Object {{
            $relative = $_.FullName.Substring($basePath.Length).TrimStart('\\', '/') -replace '\\\\', '/'
            Send-AzFileOne $_.FullName (Join-AzFileRemotePath $effectiveRemoteRoot $relative)
            $script:count++
        }}
    }} else {{
        Send-AzFileOne $source.FullName $effectiveRemoteRoot
        $count = 1
    }}
    Write-Output "[upload] $LocalPath -> /$Share/$effectiveRemoteRoot"
}}

function Get-AzFileDirectoryEntries([string]$Path) {{
    try {{
        [xml]$xml = (Invoke-WebRequest -UseBasicParsing -Method Get -Uri (Get-AzFileUri $Path 'restype=directory&comp=list') -Headers (New-AzFileHeaders)).Content
        return $xml.EnumerationResults.Entries
    }} catch {{
        $status = $_.Exception.Response.StatusCode.value__
        if ($status -eq 404) {{ throw "remote path not found: /$Share/$Path" }}
        throw
    }}
}}

function Receive-AzFileOne([string]$RemotePath, [string]$DestinationPath) {{
    if ((Test-Path -LiteralPath $DestinationPath) -and -not $Overwrite) {{ throw "local file already exists: $DestinationPath" }}
    $parent = Split-Path -Parent $DestinationPath
    if ($parent) {{ New-Item -ItemType Directory -Force -Path $parent | Out-Null }}
    Invoke-WebRequest -UseBasicParsing -Method Get -Uri (Get-AzFileUri $RemotePath) -Headers (New-AzFileHeaders) -OutFile $DestinationPath
}}

function Receive-AzFileDirectory([string]$RemotePath, [string]$DestinationRoot) {{
    $entries = Get-AzFileDirectoryEntries $RemotePath
    $count = 0
    if ($entries.Directory) {{
        @($entries.Directory) | ForEach-Object {{
            $name = [string]$_.Name
            $count += Receive-AzFileDirectory (Join-AzFileRemotePath $RemotePath $name) (Join-Path $DestinationRoot $name)
        }}
    }}
    if ($entries.File) {{
        @($entries.File) | ForEach-Object {{
            $name = [string]$_.Name
            Receive-AzFileOne (Join-AzFileRemotePath $RemotePath $name) (Join-Path $DestinationRoot $name)
            $count++
        }}
    }}
    if ($count -eq 0) {{ New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null }}
    return $count
}}

function Download-AzFilePath {{
    if (Test-AzFileRemoteFile $RemoteRoot) {{
        $destination = $LocalPath
        if ((Test-Path -LiteralPath $destination -PathType Container) -or $LocalPathIsDirectoryTarget) {{
            $leaf = (($RemoteRoot -split '/') | Where-Object {{ $_ -ne '' }})[-1]
            $destination = Join-Path $destination $leaf
        }}
        Receive-AzFileOne $RemoteRoot $destination
    }} else {{
        Receive-AzFileDirectory $RemoteRoot $LocalPath | Out-Null
    }}
    Write-Output "[download] /$Share/$RemoteRoot -> $LocalPath"
}}

try {{
    if ({_ps_quote(command.action)} -eq 'upload') {{
        Upload-AzFilePath
    }} else {{
        Download-AzFilePath
    }}
}} catch {{
    $message = $_.Exception.Message
    $message = $message.Replace($Sas, '***')
    Write-Error $message
    exit 1
}}
"""


def _service_client(config: AzureFilesConfig, factory: ServiceClientFactory | None) -> object:
    try:
        return (factory or create_service_client)(config)
    except AzureFilesError:
        raise
    except Exception as exc:
        raise AzureFilesError(mask_secret(str(exc), config.sas)) from exc


def _configured_service(
    config_path: str | os.PathLike[str] | None,
    service_client_factory: ServiceClientFactory | None,
) -> object:
    config = load_azure_files_config(config_path)
    return _service_client(config, service_client_factory)


def _upload_one(share_client: object, source: Path, remote_path: str, *, overwrite: bool) -> None:
    try:
        if not overwrite and _remote_file_exists(share_client.get_file_client(remote_path)):
            raise AzureFilesError(f"remote file already exists: {remote_path}")
        parent = str(PurePosixPath(remote_path).parent)
        if parent != ".":
            _ensure_remote_directories(share_client, parent)
        with source.open("rb") as handle:
            share_client.get_file_client(remote_path).upload_file(handle)
    except AzureFilesError:
        raise
    except Exception as exc:
        raise AzureFilesError(str(exc)) from exc


def _download_one(file_client: object, destination: Path, *, overwrite: bool) -> None:
    if destination.exists() and not overwrite:
        raise AzureFilesError(f"local file already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = file_client.download_file()
        data = stream.readall()
    except Exception as exc:
        raise AzureFilesError(str(exc)) from exc
    destination.write_bytes(data)


def _download_directory(share_client: object, remote_dir: str, destination: Path, *, overwrite: bool) -> int:
    count = 0
    try:
        directory_client = share_client.get_directory_client(remote_dir)
        for item in directory_client.list_directories_and_files():
            name = _listed_name(item)
            if not name:
                continue
            child_remote = _remote_join(remote_dir, name)
            if _listed_is_directory(item):
                count += _download_directory(share_client, child_remote, destination / name, overwrite=overwrite)
            else:
                _download_one(share_client.get_file_client(child_remote), destination / name, overwrite=overwrite)
                count += 1
    except AzureFilesError:
        raise
    except Exception as exc:
        raise AzureFilesError(str(exc)) from exc
    if count == 0:
        destination.mkdir(parents=True, exist_ok=True)
    return count


def _ensure_remote_directories(share_client: object, directory_path: str) -> None:
    current: list[str] = []
    for part in PurePosixPath(directory_path).parts:
        if part in {"", "."}:
            continue
        current.append(part)
        client = share_client.get_directory_client("/".join(current))
        try:
            client.create_directory()
        except Exception as exc:
            if _is_exists_error(exc):
                continue
            raise


def _remote_file_exists(file_client: object) -> bool:
    try:
        file_client.get_file_properties()
        return True
    except Exception as exc:
        if _is_missing_error(exc):
            return False
        raise


def _remote_join(base: str, child: str) -> str:
    return str(PurePosixPath(base) / PurePosixPath(child))


def _split_transfer_args(arg: str) -> list[str]:
    lexer = shlex.shlex(arg, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return [_strip_outer_quotes(token) for token in lexer]


def _strip_outer_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _remote_path_is_directory_target(value: str) -> bool:
    return value.endswith(("/", "\\"))


def _local_path_is_directory_target(value: str | os.PathLike[str]) -> bool:
    text = os.fspath(value)
    return text.endswith(("/", "\\"))


def _yaml_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _listed_name(item: object) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or "")
    return str(getattr(item, "name", "") or "")


def _listed_is_directory(item: object) -> bool:
    if isinstance(item, dict):
        if "is_directory" in item:
            return bool(item["is_directory"])
        kind = item.get("type") or item.get("kind")
        if kind:
            return str(kind).lower() == "directory"
        properties = item.get("properties")
        if isinstance(properties, dict) and "content_length" in properties:
            return False
        return False
    return getattr(item, "is_directory", False) or getattr(item, "type", None) == "directory"


def _list_item(item: object) -> AzureFilesListItem:
    name = _listed_name(item)
    item_type = "directory" if _listed_is_directory(item) else "file"
    size = "-"
    if item_type == "file":
        size_value = None
        if isinstance(item, dict):
            properties = item.get("properties") or {}
            if isinstance(properties, dict):
                size_value = properties.get("content_length") or properties.get("size")
            size_value = size_value or item.get("size")
        else:
            properties = getattr(item, "properties", None)
            size_value = getattr(properties, "content_length", None) if properties is not None else None
            size_value = size_value or getattr(item, "size", None)
        size = str(size_value) if size_value is not None else "-"
    return AzureFilesListItem(item_type, name, size)


def _is_missing_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    error_code = str(getattr(exc, "error_code", "")).lower()
    text = str(exc).lower()
    return status_code == 404 or "resourcenotfound" in error_code or "not found" in text


def _is_exists_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    error_code = str(getattr(exc, "error_code", "")).lower()
    text = str(exc).lower()
    return status_code == 409 or "resourcealreadyexists" in error_code or "already exists" in text


def _parse_share_command(tokens: list[str]) -> AzureFilesManagementCommand:
    if not tokens:
        raise AzureFilesError("usage: /azure-files share <list|create|remove> [share-name]")
    verb = tokens[0].lower()
    if verb == "list":
        if len(tokens) != 1:
            raise AzureFilesError("usage: /azure-files share list")
        return AzureFilesManagementCommand("share-list")
    if verb == "create":
        if len(tokens) != 2:
            raise AzureFilesError("usage: /azure-files share create <share-name>")
        return AzureFilesManagementCommand("share-create", share=_normalize_share_name(tokens[1]))
    if verb == "remove":
        if len(tokens) != 2:
            raise AzureFilesError("usage: /azure-files share remove <share-name>")
        return AzureFilesManagementCommand("share-remove", share=_normalize_share_name(tokens[1]))
    raise AzureFilesError("usage: /azure-files share <list|create|remove> [share-name]")


def _parse_directory_command(tokens: list[str]) -> AzureFilesManagementCommand:
    if not tokens:
        raise AzureFilesError("usage: /azure-files directory <create|remove> --share <share-name> <directory>")
    verb = tokens[0].lower()
    if verb not in {"create", "remove"}:
        raise AzureFilesError("usage: /azure-files directory <create|remove> --share <share-name> <directory>")
    options, positional = _parse_options_and_positionals(tokens[1:], allowed={"share"})
    share = options.get("share", "")
    if not share or len(positional) != 1:
        raise AzureFilesError(f"usage: /azure-files directory {verb} --share <share-name> <directory>")
    action: AzureFilesManagementAction = "directory-create" if verb == "create" else "directory-remove"
    return AzureFilesManagementCommand(action, share=_normalize_share_name(share), path=_normalize_directory_path(positional[0]))


def _parse_storage_account_management_command(tokens: list[str]) -> AzureFilesManagementCommand:
    if not tokens:
        raise AzureFilesError("usage: /azure-files storage-account <list|create|remove|key>")
    verb = tokens[0].lower()
    if verb not in {"list", "create", "remove", "key"}:
        raise AzureFilesError("usage: /azure-files storage-account <list|create|remove|key>")
    options, positional = _parse_options_and_positionals(
        tokens[1:],
        allowed={"account", "name", "n", "g", "resource-group", "l", "location", "sku", "kind"},
    )
    if positional:
        if "account" not in options and "name" not in options and "n" not in options:
            options["name"] = positional.pop(0)
        if positional:
            raise AzureFilesError(f"unexpected argument: {positional[0]}")
    if verb == "list":
        if options:
            raise AzureFilesError("usage: /azure-files storage-account list")
        return AzureFilesManagementCommand("storage-account-list")
    name = options.get("account") or options.get("name") or options.get("n")
    resource_group = options.get("g") or options.get("resource-group")
    if not name:
        raise AzureFilesError("missing required option: --account <storage-account>")
    if verb == "key":
        return AzureFilesManagementCommand("storage-account-key", share=name, resource_group=resource_group or "")
    if not resource_group:
        raise AzureFilesError("missing required option: -g <resource-group>")
    if verb == "create":
        location = options.get("l") or options.get("location")
        if not location:
            raise AzureFilesError("missing required option: -l <location>")
        return AzureFilesManagementCommand(
            "storage-account-create",
            share=name,
            resource_group=resource_group,
            location=location,
            sku=options.get("sku") or "Standard_LRS",
            kind=options.get("kind") or "StorageV2",
        )
    return AzureFilesManagementCommand("storage-account-remove", share=name, resource_group=resource_group)


def _parse_options_and_positionals(tokens: list[str], *, allowed: set[str]) -> tuple[dict[str, str], list[str]]:
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
            raise AzureFilesError(f"unexpected option: {token}")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
            raise AzureFilesError(f"missing value for option: {token}")
        options[key] = tokens[index + 1]
        index += 2
    return options, positional


def _parse_positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise AzureFilesError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise AzureFilesError(f"{name} must be a positive integer")
    return parsed


def _normalize_share_name(value: str) -> str:
    share = value.strip().strip("/")
    if "/" in share or not share:
        raise AzureFilesError(f"invalid share name: {value}")
    return share


def _normalize_directory_path(value: str) -> str:
    path = str(PurePosixPath(value.replace("\\", "/").strip("/")))
    if not path or path == ".":
        raise AzureFilesError("directory path is required")
    return path


def _usage(action: Action) -> str:
    if action == "upload":
        return "usage: /upload [--target local|agent] [--overwrite] <local-path> /<share-name>/<remote-path>"
    return "usage: /download [--target local|agent] [--overwrite] /<share-name>/<remote-path> <local-path>"


def _azure_files_usage() -> str:
    return (
        "usage: /azure-files <storage-account|share|directory|list|sas>. "
        "Run /azure-files storage-account key --account <account>, /azure-files share list, /azure-files list --share /<share>[/directory], or /azure-files sas [--permissions rwld] [--hours 24]."
    )


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
