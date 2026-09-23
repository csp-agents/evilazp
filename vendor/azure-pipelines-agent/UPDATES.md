# EvilAzp-Agent — Modifications to Azure Pipeline Agent

## 2026-06-08 Update

### Fixed Job Polling Delay

- `/agent create`, `evilazp create-agent`, and the compatibility `evilazp-builder.py` wrapper accept `--polling <seconds>`.
- When set, the baked agent waits that fixed number of seconds after an empty job poll instead of using the default random 5-15 second delay.
- Omitting `--polling` keeps the upstream-style random 5-15 second no-job delay.

### Integrated Agent Builder

- Agent bake/build logic now lives in the `evilazp` Python package.
- Use `/agent create --pool <pool> --agent <name> --runtime <runtime>` from the shell to build from the current session.
- Use `evilazp create-agent --org <org> --pool <pool> --agent <name> --runtime <runtime>` from the terminal.
- `vendor/azure-pipelines-agent/evilazp-builder.py` remains as a thin compatibility wrapper only.

### Builder YAML Config

- `evilazp create-agent` and the compatibility `evilazp-builder.py` wrapper accept `--yaml`; a custom path can still be passed when needed.
- YAML config is the only source for operator secrets and build configuration.
- Required values must exist in that file; missing values produce a clear error before bake/build starts.
- `create-agent --yaml` re-bakes `BakedConfig.cs` from YAML before compiling.

Example `.env`:

```yaml
url: https://dev.azure.com/<org>
pat: <pat>
pool: <pool>
agent: winvm03
sp_tenant: <tenant-id>
sp_client: <client-id>
sp_secret: <client-secret>
tunnel_id: winvm03
tunnel_ports: 22
relay_connection_string: "Endpoint=sb://..."
relay_remote_forward:
  - ssh-relay:127.0.0.1:22
```

### Dev Tunnels

- YAML builds write a stable `TunnelId` into `BakedConfig`.
- `tunnel_id` sets the requested Dev Tunnel ID. `tunnel_name` is kept as a YAML compatibility alias.
- If `tunnel_id` is omitted while tunnel ports are configured, the agent name is used as the default tunnel ID.
- The tunnel plugin authenticates with the baked service-principal credentials before creating the tunnel.
- The plugin creates the tunnel through the Dev Tunnels SDK using `Tunnel.TunnelId`, not `Tunnel.Name`.
- If the requested tunnel ID is unavailable or already used, the plugin generates a derived ID such as `<requested-id>-<suffix>`, retries creation, and logs the generated ID and URL.
- Tunnel labels use the SDK-compatible form `evilazp=<id>` so helper tools can discover tunnels by alias.

Example:

```sh
evilazp create-agent --yaml .env --tunnel --runtime win-x64
```

At runtime the agent logs the actual tunnel URL, for example:

```text
Tunnel active: https://winvm03.jpe1.devtunnels.ms
```

If the requested ID was already in use, use the generated ID shown in the log.

### Azure Relay Bridge

- YAML builds can now store Azure Relay Bridge configuration:
  - `relay_connection_string`
  - `relay_local_forward`
  - `relay_remote_forward`
  - `relay_remote_http_forward`
- `evilazp create-agent --relay` includes the Azure Relay Bridge plugin.
- `--tunnel` and `--relay` can be combined in a single agent binary.
- Relay-only builds do not require Dev Tunnel SP credentials or `--tunnel-ports`. If SP credentials are provided without `--tunnel-ports`, they are stored but the Dev Tunnel plugin remains inactive.

Example:

```sh
evilazp create-agent --yaml .env --relay --runtime win-x64
```

Combined build:

```sh
evilazp create-agent --yaml .env --tunnel --relay --runtime win-x64
```

### Secret Handling

`src/Agent.Listener/BakedConfig.cs` is intentionally committed with empty placeholder values. Do not commit baked PATs, service-principal secrets, or Relay connection strings. Run `evilazp create-agent` locally before building an operator-specific binary.

## High Level

1. **Dev Tunnels plugin** — Optional plugin that creates a Microsoft Dev Tunnel on agent startup, forwarding local TCP ports through Microsoft's relay infrastructure (`*.devtunnels.ms`). Runs alongside the agent's message listener, independent of pipeline job lifecycle.

2. **Entra ID service principal auth** — Plugin authenticates to the Dev Tunnels API using `Azure.Identity.ClientSecretCredential`. Tokens auto-refresh (60-90min access token lifetime, transparent re-acquisition). Client secrets last up to 2 years.

3. **Build-time toggle** — Plugin is included only when built with `/p:EnableTunnel=true`. Without it, the agent is identical to vanilla. Zero tunnel code in the vanilla binary.

4. **Runtime activation** — Tunnel starts only when `EVILAZP_SP_*` and `EVILAZP_TUNNEL_PORTS` env vars are set. If env vars are absent or plugin assembly is missing, agent runs normally.

---

## Files Modified

### `src/Agent.Listener/Agent.cs`
- Added `#pragma warning disable CA2000` (line 4)
- Inserted tunnel plugin loading block after session creation (~line 472): reads env vars, loads `TunnelPlugin` via `Type.GetType()` reflection, calls `StartAsync()` as fire-and-forget task alongside message loop

### `src/Agent.Listener/Agent.Listener.csproj`
- Added conditional `<ProjectReference>` to `Agent.Plugins.Tunnel` gated on `$(EnableTunnel)==true`
- Removed direct Dev Tunnels NuGet refs (moved to plugin project)

### `src/Agent.Listener/NuGet.Config`
- Added `nuget.org` as package source (required for Dev Tunnels transitive deps that aren't in Microsoft's private feed)

### `src/NuGet.Config`
- Same — added `nuget.org` source

---

## Files Created

### `src/Agent.Sdk/ITunnelPlugin.cs`
- Interface: `StartAsync(tenantId, clientId, clientSecret, ports, cancellationToken)`, `StopAsync()`, `TunnelUrl` property
- Lives in Agent.Sdk so both Listener and plugin can reference it without circular deps

### `src/Agent.Plugins.Tunnel/Agent.Plugins.Tunnel.csproj`
- Separate project, references `Agent.Sdk` + NuGet packages: `Azure.Core`, `Azure.Identity`, `Microsoft.DevTunnels.Management`, `Microsoft.DevTunnels.Connections`
- `AssetTargetFallback` cleared to avoid namespace collision with `vss-api-netcore`

### `src/Agent.Plugins.Tunnel/NuGet.Config`
- Points to `nuget.org` only (plugin has no private feed deps)

### `src/Agent.Plugins.Tunnel/TunnelPlugin.cs`
- Implements `ITunnelPlugin`
- Creates `ClientSecretCredential` from SP creds
- Token callback: `credential.GetTokenAsync()` with scope `46da2f7e-b5ef-422a-88d4-2a7f9de6a0b2/.default` (Dev Tunnels API resource ID)
- Creates tunnel with anonymous connect access + specified ports
- Hosts tunnel via `TunnelRelayTunnelHost` with `ForwardConnectionsToLocalPorts = true`
- Cleanup: disposes host, deletes tunnel on stop

---

## Build Commands

```sh
# Vanilla (no tunnel)
dotnet msbuild src/dir.proj /t:SingleBinary \
  /p:BUILDCONFIG=Debug /p:PackageRuntime=linux-x64 \
  /p:TargetFramework=net8.0 /p:AgentVersion=3.999.999 \
  /p:LayoutRoot=$PWD/_layout/linux-x64-vanilla /m:1

# With tunnel plugin
dotnet msbuild src/dir.proj /t:SingleBinary \
  /p:BUILDCONFIG=Debug /p:PackageRuntime=linux-x64 \
  /p:TargetFramework=net8.0 /p:AgentVersion=3.999.999 \
  /p:LayoutRoot=$PWD/_layout/linux-x64-tunnel \
  /p:EnableTunnel=true /m:1
```

NuGet feeds required: `nuget.org` + `https://pkgs.dev.azure.com/mseng/PipelineTools/_packaging/nugetvssprivate/nuget/v3/index.json`

The single-file Linux/macOS configuration path skips the upstream TEE EULA file check, so `Agent.Listener` does not need a companion `license.html`.

## Runtime

Run the generated `Agent.Listener` with no arguments. Baked YAML values drive agent registration and optional tunnel/relay startup.
