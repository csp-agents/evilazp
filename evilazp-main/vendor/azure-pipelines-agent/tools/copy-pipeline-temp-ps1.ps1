param(
    [string]$Destination = "C:\azp-ps1-copies",
    [int]$Seconds = 300,
    [int]$IntervalMilliseconds = 100
)

$ErrorActionPreference = "Continue"

New-Item -ItemType Directory -Path $Destination -Force | Out-Null

$seen = New-Object 'System.Collections.Generic.HashSet[string]'
$deadline = (Get-Date).AddSeconds($Seconds)

function Add-Root {
    param([string]$Path)
    if ($Path -and (Test-Path -LiteralPath $Path)) {
        (Resolve-Path -LiteralPath $Path).Path
    }
}

function Add-UserTempRoots {
    $profileRoot = "C:\Users"
    if (-not (Test-Path -LiteralPath $profileRoot)) {
        return
    }

    Get-ChildItem -LiteralPath $profileRoot -Directory -ErrorAction SilentlyContinue |
        ForEach-Object {
            Add-Root (Join-Path $_.FullName "AppData\Local\Temp")
        }
}

function Copy-Ps1 {
    param([System.IO.FileInfo]$File)

    if (-not $File -or -not $File.Exists) {
        return
    }

    $key = $File.FullName.ToLowerInvariant()
    if ($seen.Contains($key)) {
        return
    }

    $seen.Add($key) | Out-Null

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $safeName = ($File.FullName -replace "^[A-Za-z]:\\", "" -replace "[\\/:*?`"<>|]", "_")
    $target = Join-Path $Destination "$stamp`_$safeName"

    try {
        Copy-Item -LiteralPath $File.FullName -Destination $target -Force
        Write-Host "COPIED $($File.FullName) -> $target"
    }
    catch {
        Write-Host "COPY_FAILED $($File.FullName): $($_.Exception.Message)"
    }
}

Write-Host "Watching .ps1 files until $($deadline.ToString('o'))"
Write-Host "Destination: $Destination"

while ((Get-Date) -lt $deadline) {
    $roots = @(
        Add-Root $env:TEMP
        Add-Root $env:TMP
        Add-Root $env:AGENT_TEMPDIRECTORY
        Add-Root "C:\Windows\Temp"
        Add-UserTempRoots
        Add-Root (Join-Path (Get-Location) "_work\_temp")
        Add-Root "C:\azp-standard\_work\_temp"
        Add-Root "C:\azp-single\_work\_temp"
    ) | Where-Object { $_ } | Select-Object -Unique

    foreach ($root in $roots) {
        Get-ChildItem -LiteralPath $root -Filter "*.ps1" -File -Recurse -ErrorAction SilentlyContinue |
            ForEach-Object { Copy-Ps1 $_ }
    }

    Start-Sleep -Milliseconds $IntervalMilliseconds
}

Write-Host "Done. Copied files are in: $Destination"
