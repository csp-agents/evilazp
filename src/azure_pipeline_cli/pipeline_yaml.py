"""Azure Pipelines YAML template used by the interactive command shell."""

from __future__ import annotations

AZURE_PIPELINE_YAML = r"""trigger: none
pr: none

pool:
  name: $(targetPool)
  demands:
  - Agent.Name -equals $(targetAgent)

steps:
- checkout: none

- powershell: |
    $ErrorActionPreference = "Continue"
    $runId = $env:EVILAZP_RUN_ID
    $commandB64 = $env:EVILAZP_COMMAND_B64
    Write-Host "::EVILAZP_START::$runId"
    try {
      $bytes = [Convert]::FromBase64String($commandB64)
      $command = [Text.Encoding]::UTF8.GetString($bytes)
      $scriptBlock = [ScriptBlock]::Create($command)
      $global:LASTEXITCODE = $null
      $captured = & $scriptBlock *>&1
      $success = $?
      if ($null -ne $captured) {
        $captured | ForEach-Object { Write-Host $_ }
      }
      $nativeExitCode = $LASTEXITCODE
      if ($null -ne $nativeExitCode) {
        $exitCode = [int]$nativeExitCode
      } elseif ($success) {
        $exitCode = 0
      } else {
        $exitCode = 1
      }
    } catch {
      Write-Error $_
      $exitCode = 1
    }
    Write-Host "::EVILAZP_END::$runId::EXIT::$exitCode"
    exit $exitCode
  displayName: "evilazp command"
  workingDirectory: $(Agent.HomeDirectory)
  env:
    EVILAZP_RUN_ID: $(runId)
    EVILAZP_COMMAND_B64: $(commandB64)
"""


def render_pipeline_yaml() -> str:
    """Return the static pipeline template."""

    return AZURE_PIPELINE_YAML
