$ErrorActionPreference = "Stop"

# Compile l'indexeur en release avec la toolchain de rust-toolchain.toml.
# Prerequis : Rust (rustup) et les outils C++ MSVC (Visual Studio ou Build
# Tools, toute version recente, charge de travail "Developpement Desktop C++").

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$cargo = Get-Command cargo -ErrorAction SilentlyContinue
if (-not $cargo) {
    $userCargo = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
    if (Test-Path -LiteralPath $userCargo) {
        $cargo = Get-Item -LiteralPath $userCargo
    } else {
        throw "cargo introuvable. Installez Rust via https://rustup.rs puis relancez ce script."
    }
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $vswhere)) {
    throw "Visual Studio Installer introuvable. Installez les Build Tools avec les outils C++ MSVC."
}
$vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsPath) {
    throw "Outils C++ MSVC introuvables. Ajoutez la charge de travail 'Developpement Desktop C++' dans Visual Studio Installer."
}
$vcvars = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat"
if (-not (Test-Path -LiteralPath $vcvars)) {
    throw "vcvars64.bat introuvable dans $vsPath."
}

$cargoPath = if ($cargo.Source) { $cargo.Source } else { $cargo.FullName }
$command = "call `"$vcvars`" >nul && `"$cargoPath`" build --release"
Push-Location $scriptDir
try {
    cmd.exe /c $command
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    Write-Host "Binaire : $(Join-Path $scriptDir 'target\release\l_oeil_de_dieu.exe')"
} finally {
    Pop-Location
}
