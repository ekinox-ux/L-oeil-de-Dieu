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

# Les verrous vivent dans CACHE_DIR, que OEIL_CACHE_DIR (backend\.env compris)
# peut deplacer hors de backend\ : on le demande a settings.py, comme le backend.
$backendDir = Join-Path $projectRoot "backend"
$lockDirs = @($backendDir)
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    # Pas de Select-Object -First ici : il couperait le pipeline et
    # $LASTEXITCODE vaudrait -1 meme quand Python reussit.
    $output = & $expectedPython -I -c "import sys; sys.path.insert(0, sys.argv[1]); import settings; print(settings.CACHE_DIR)" $backendDir 2>$null
    $cacheDir = @($output)[0]
    if ($LASTEXITCODE -eq 0 -and $cacheDir -and (Test-Path -LiteralPath $cacheDir -PathType Container)) {
        $lockDirs += [string]$cacheDir
    }
}
catch {
    # settings.py illisible : on garde au moins backend\.
}
finally {
    $ErrorActionPreference = $previousPreference
}

$seenDirs = @{}
$lockFiles = foreach ($dir in $lockDirs) {
    $key = [System.IO.Path]::GetFullPath($dir).TrimEnd('\').ToLowerInvariant()
    if ($seenDirs.ContainsKey($key)) {
        continue
    }
    $seenDirs[$key] = $true
    Get-ChildItem -LiteralPath $dir -Filter "analyzer_*.lock" -File -ErrorAction SilentlyContinue
}

$lockFiles |
    ForEach-Object {
        try {
            $data = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json
            if (-not $data.worker_pid) {
                # Worker pas encore inscrit : le backend gere ce cas lui-meme.
                return
            }
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
