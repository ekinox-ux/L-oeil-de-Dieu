[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [string]$PidDir
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$expectedPython = [System.IO.Path]::GetFullPath($PythonExe)
$taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
$failed = $false

foreach ($name in @("backend.pid", "frontend.pid", "bot.pid")) {
    $pidFile = Join-Path $PidDir $name
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
    }

    $rawPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $trackedPid = 0
    if (-not [int]::TryParse([string]$rawPid, [ref]$trackedPid)) {
        Write-Warning "Fichier PID invalide: $pidFile"
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        continue
    }

    $process = Get-Process -Id $trackedPid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        continue
    }

    try {
        $actualPath = [System.IO.Path]::GetFullPath($process.Path)
    }
    catch {
        $actualPath = ""
    }

    if (-not $actualPath -or $actualPath -ine $expectedPython) {
        Write-Warning "PID $trackedPid ignore: il n'appartient pas au venv de ce projet."
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        continue
    }

    & $taskkill /PID $trackedPid /T /F 2>$null | Out-Null
    Start-Sleep -Milliseconds 150
    if (Get-Process -Id $trackedPid -ErrorAction SilentlyContinue) {
        Write-Warning "Impossible d'arreter l'arbre du PID $trackedPid."
        $failed = $true
        continue
    }

    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# Les analyseurs normaux sont enfants du backend et sont deja fermes avec son arbre.
# Pour un ancien verrou orphelin, on ne tue le PID que si sa ligne de commande prouve
# qu'il s'agit bien de l'analyseur de ce projet. Si CIM est interdit, le verrou reste
# en place plutot que de risquer d'arreter un autre processus Python.
$rootPattern = [regex]::Escape($projectRoot)
Get-ChildItem -LiteralPath (Join-Path $projectRoot "backend") -Filter "analyzer_*.lock" -File -ErrorAction SilentlyContinue |
    ForEach-Object {
        try {
            $data = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json
            $workerPid = [int]$data.worker_pid
            $worker = Get-Process -Id $workerPid -ErrorAction SilentlyContinue
            if ($null -eq $worker) {
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
                return
            }

            $cimProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$workerPid" -ErrorAction Stop
            if ($cimProcess.CommandLine -match $rootPattern -and $cimProcess.CommandLine -match "analyzer\.py") {
                & $taskkill /PID $workerPid /T /F 2>$null | Out-Null
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
        }
        catch {
            # Refus CIM ou verrou illisible: ne jamais tuer un PID non verifie.
        }
    }

if ($failed) {
    exit 1
}

exit 0
