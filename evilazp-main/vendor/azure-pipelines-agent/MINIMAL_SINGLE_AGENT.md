# Minimal Single-Binary Azure Pipelines Agent

This fork builds a script-only Azure Pipelines agent as a single executable.

## Scope

Supported:

- Register/connect to Azure DevOps with command-line arguments
- Join a specified agent pool
- Receive YAML jobs
- Run `checkout: none` jobs
- Run inline `PowerShell`, `CmdLine`, and Linux/macOS `Bash` script steps
- Upload logs, timeline updates, and job result

Not supported:

- Repository checkout
- Marketplace/Node task download and execution
- Pipeline/build artifacts
- Pipeline cache
- Test result publishing
- Code coverage publishing
- Release/deployment jobs
- Service/autologon install
- Agent self-update

## Build

Set `PackageRuntime` to the target runtime.

Windows x64:

```sh
dotnet msbuild src/dir.proj /t:SingleBinary \
  /p:BUILDCONFIG=Debug \
  /p:PackageRuntime=win-x64 \
  /p:TargetFramework=net8.0 \
  /p:RuntimeFrameworkVersion=8.0.27 \
  /p:AgentVersion=$(cat src/agentversion) \
  /p:LayoutRoot=$PWD/_layout/win-x64-single \
  /p:RestoreSources=https://api.nuget.org/v3/index.json \
  /m:1
```

Output:

```text
_layout/win-x64-single/single/Agent.Listener.exe
```

Linux x64:

```sh
dotnet msbuild src/dir.proj /t:SingleBinary \
  /p:BUILDCONFIG=Debug \
  /p:PackageRuntime=linux-x64 \
  /p:TargetFramework=net8.0 \
  /p:RuntimeFrameworkVersion=8.0.27 \
  /p:AgentVersion=$(cat src/agentversion) \
  /p:LayoutRoot=$PWD/_layout/linux-x64-single \
  /p:RestoreSources=https://api.nuget.org/v3/index.json \
  /m:1
```

Output:

```text
_layout/linux-x64-single/single/Agent.Listener
```

## Run

Windows:

```powershell
.\azp-single.exe --url https://dev.azure.com/<org> --auth pat --token <PAT> --pool <pool> --agent <agent-name> --work _work --replace
```

Linux:

```sh
./azp-single --url https://dev.azure.com/<org> --auth pat --token "$AZP_TOKEN" --pool <pool> --agent <agent-name> --work _work --replace
```

Use YAML with `checkout: none`:

```yaml
pool:
  name: <pool>
  demands:
  - Agent.Name -equals <agent-name>

steps:
- checkout: none
- powershell: |
    Write-Host "hello from minimal agent"
```

## Repository Hygiene

Do not commit runtime state or build outputs:

- `_layout/`
- `bin/`
- `obj/`
- `_diag/`
- `.agent`
- `.credentials`
- `.credentials_rsaparams`
- PATs, passwords, logs

These paths are ignored by `.gitignore`.
