param(
    [switch]$Recreate,
    [switch]$Development
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$VenvPath = Join-Path $ProjectRoot "venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

function Test-VenvPython {
    if (-not (Test-Path -LiteralPath $VenvPython)) { return $false }
    try {
        & $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Test-SystemPython314 {
    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $Launcher) { return $false }
    try {
        & $Launcher.Source -3.14 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if ((Test-Path -LiteralPath $VenvPath) -and -not (Test-VenvPython)) {
    if (-not $Recreate) {
        throw "Le venv existant est invalide. Relancez avec: .\setup.ps1 -Recreate"
    }
    if (-not (Test-SystemPython314)) {
        throw "Python 3.14 est requis. Installez-le avant de reconstruire le venv. Aucun fichier n'a ete deplace."
    }
    $BackupName = "venv.broken-{0}" -f (Get-Date -Format "yyyyMMdd-HHmmss")
    $BackupPath = Join-Path $ProjectRoot $BackupName
    Move-Item -LiteralPath $VenvPath -Destination $BackupPath
    Write-Host "Ancien environnement conserve dans $BackupPath"
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if (-not (Test-SystemPython314)) {
        throw "Python 3.14 est requis. Installez-le puis relancez ce script."
    }
    & py -3.14 -m venv $VenvPath
}

& $VenvPython -m pip install --upgrade pip
$Requirements = @(
    (Join-Path $ProjectRoot "backend\requirements.txt"),
    (Join-Path $ProjectRoot "bot\requirements.txt")
)
if ($Development) {
    $Requirements = @(
        (Join-Path $ProjectRoot "backend\requirements-dev.txt"),
        (Join-Path $ProjectRoot "bot\requirements-dev.txt")
    )
}
foreach ($Requirement in $Requirements) {
    & $VenvPython -m pip install -r $Requirement
}

# Index compresses en zstd : le wheel PyPI de tantivy ne les lit pas. On
# reinstalle le build local s'il existe (venv recree), sinon on previent.
# "Continue" : sous PowerShell 5.1, stderr redirige d'un exe stopperait le script.
$ErrorActionPreference = "Continue"
& $VenvPython (Join-Path $ProjectRoot "backend\check_tantivy_zstd.py") *> $null
$ZstdCheck = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($ZstdCheck -ne 0) {
    $Pin = Select-String -LiteralPath (Join-Path $ProjectRoot "backend\requirements.txt") -Pattern '^tantivy==(\S+)'
    $Wheel = $null
    if ($Pin) {
        $Wheel = Get-ChildItem -Path (Join-Path $ProjectRoot "build\wheels") -Filter ("tantivy-{0}+zstd-*.whl" -f $Pin.Matches[0].Groups[1].Value) -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($Wheel) {
        & $VenvPython -m pip install --force-reinstall --no-deps $Wheel.FullName
    } else {
        Write-Host "tantivy sans zstd : pour des index compresses, lancez .\build_tantivy_zstd.ps1 -Install"
    }
}

Write-Host "Environnement Python 3.14 pret: $VenvPython"
Write-Host "Lancez ensuite .\start.bat"
