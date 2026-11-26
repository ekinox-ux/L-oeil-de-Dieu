<#
.SYNOPSIS
    Recompile la lib Python tantivy avec la compression zstd du doc store.

.DESCRIPTION
    Le wheel PyPI de tantivy n'active pas la feature Rust `zstd-compression` :
    il refuse d'ouvrir un index dont le doc store est compresse en zstd (voir
    la commande `compress` de l'indexeur). Ce script telecharge les sources de
    la version epinglee dans backend/requirements.txt, active la feature, puis
    construit un wheel `X.Y.Z+zstd`.

    `tantivy==X.Y.Z` accepte la version locale `X.Y.Z+zstd` (PEP 440) : un
    `pip install -r backend/requirements.txt` ulterieur garde donc ce build.
    A relancer apres chaque montee de version de tantivy.

    Prerequis : la toolchain Rust (deja requise par l'indexeur) et un acces a
    PyPI. Arretez le backend avant -Install : Windows verrouille le module charge.

.PARAMETER Install
    Installe le wheel construit dans le venv du projet.
#>
param(
    [switch]$Install
)

$ErrorActionPreference = "Stop"

# pip, tar et maturin ecrivent leur progression sur stderr : sous PowerShell 5.1
# en mode "Stop", la premiere ligne arreterait le script. Seul le code de
# sortie fait foi.
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Failure,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )
    $Previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | ForEach-Object { "$_" }
    } finally {
        $ErrorActionPreference = $Previous
    }
    if ($LASTEXITCODE -ne 0) { throw "$Failure (code $LASTEXITCODE)" }
}
$ProjectRoot = $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$Requirements = Join-Path $ProjectRoot "backend\requirements.txt"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "venv introuvable ($VenvPython). Lancez d'abord .\setup.ps1"
}
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "cargo introuvable : installez la toolchain Rust (https://rustup.rs)."
}

$Pin = Select-String -LiteralPath $Requirements -Pattern '^tantivy==([0-9][0-9A-Za-z.]*)\s*$'
if (-not $Pin) {
    throw "Version de tantivy introuvable dans $Requirements (ligne 'tantivy==X.Y.Z' attendue)."
}
$Version = $Pin.Matches[0].Groups[1].Value
$LocalVersion = "$Version+zstd"

# Dossier de travail neuf : les sources telechargees ne se melangent a rien.
$Work = Join-Path $env:TEMP "oeil-tantivy-zstd-$Version"
if (Test-Path -LiteralPath $Work) {
    Remove-Item -LiteralPath $Work -Recurse -Force
}
$SdistDir = Join-Path $Work "sdist"
$SrcRoot = Join-Path $Work "src"
$DistDir = Join-Path $Work "dist"
New-Item -ItemType Directory -Force -Path $SdistDir, $SrcRoot, $DistDir | Out-Null

Write-Host "Telechargement des sources de tantivy $Version..."
Invoke-Native "Echec du telechargement des sources de tantivy $Version" {
    & $VenvPython -m pip download "tantivy==$Version" --no-binary ":all:" --no-deps -d $SdistDir
}
$Archive = Get-ChildItem -LiteralPath $SdistDir -Filter "tantivy-*.tar.gz" | Select-Object -First 1
if (-not $Archive) { throw "Archive source de tantivy introuvable dans $SdistDir." }
Invoke-Native "Echec de l'extraction de $($Archive.Name)" { & tar -xzf $Archive.FullName -C $SrcRoot }
$Src = Join-Path $SrcRoot "tantivy-$Version"

# Activation de la feature sur la dependance Rust `tantivy` du binding.
$CargoToml = Join-Path $Src "Cargo.toml"
$Cargo = [System.IO.File]::ReadAllText($CargoToml)
$DependencyPattern = '(?m)^tantivy = "([^"]+)"\s*$'
$Found = [regex]::Matches($Cargo, $DependencyPattern)
if ($Found.Count -ne 1) {
    throw "Dependance tantivy non reconnue dans $CargoToml : le format a change, adaptez ce script."
}
$Cargo = [regex]::Replace($Cargo, $DependencyPattern, 'tantivy = { version = "$1", features = ["zstd-compression"] }')
[System.IO.File]::WriteAllText($CargoToml, $Cargo)

# Version locale `+zstd` : `pip show tantivy` indique quel build est installe.
$PyProject = Join-Path $Src "pyproject.toml"
$Project = [System.IO.File]::ReadAllText($PyProject)
$VersionLine = '(?m)^version = "' + [regex]::Escape($Version) + '"\s*$'
if ([regex]::Matches($Project, $VersionLine).Count -ne 1) {
    throw "Version non reconnue dans $PyProject : adaptez ce script."
}
$Project = [regex]::Replace($Project, $VersionLine, "version = `"$LocalVersion`"")
[System.IO.File]::WriteAllText($PyProject, $Project)

Write-Host "Installation de maturin dans un venv de construction..."
$BuildVenv = Join-Path $Work "build-venv"
Invoke-Native "Echec de la creation du venv de construction" { & $VenvPython -m venv $BuildVenv }
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
Invoke-Native "Echec de l'installation de maturin" {
    & $BuildPython -m pip install --quiet --disable-pip-version-check "maturin>=1.9.3,<2.0"
}

Write-Host "Compilation (plusieurs minutes)..."
Push-Location $Src
try {
    Invoke-Native "Echec de la compilation de tantivy" {
        & (Join-Path $BuildVenv "Scripts\maturin.exe") build --release --interpreter $VenvPython --out $DistDir
    }
} finally {
    Pop-Location
}

$Wheel = Get-ChildItem -LiteralPath $DistDir -Filter "tantivy-*.whl" | Select-Object -First 1
if (-not $Wheel) { throw "Wheel introuvable dans $DistDir." }
$Target = Join-Path $ProjectRoot "build\wheels"
New-Item -ItemType Directory -Force -Path $Target | Out-Null
$Copied = Join-Path $Target $Wheel.Name
Copy-Item -LiteralPath $Wheel.FullName -Destination $Copied -Force
Write-Host "Wheel construit : $Copied"

if ($Install) {
    Invoke-Native "Installation impossible. Le backend est-il arrete ? (Windows verrouille le module charge)" {
        & $VenvPython -m pip install --force-reinstall --no-deps --disable-pip-version-check $Copied
    }
    Invoke-Native "Le tantivy installe ne lit pas le zstd" {
        & $VenvPython (Join-Path $ProjectRoot "backend\check_tantivy_zstd.py")
    }
    Write-Host "tantivy $LocalVersion installe : le backend lit les index compresses en zstd."
} else {
    Write-Host "Pour l'installer (backend arrete) : .\build_tantivy_zstd.ps1 -Install"
}
