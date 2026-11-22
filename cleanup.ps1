param(
    [switch]$Execute,
    [switch]$BuildArtifacts,
    [switch]$Logs
)

# Apercu par defaut : rien n'est supprime sans -Execute.
#   (defaut)         caches Python/pytest/ruff, venv casses archives, copies
#                    .sqlite.tmp orphelines, locks laisses par les tests,
#                    fichiers temporaires du projet dans %TEMP%
#   -BuildArtifacts  indexer\target et node_modules
#   -Logs            journaux du backend (uvicorn*.log*, analyzer.log*)
# Jamais touches : venv, .env, index, progressions et resultats d'analyse.

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$TempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
$Targets = [Collections.Generic.List[string]]::new()

function Add-Matches([string]$Root, [string[]]$Names, [switch]$Recurse, [switch]$Directories, [switch]$Files) {
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return }
    $items = Get-ChildItem -LiteralPath $Root -Force -Recurse:$Recurse -Directory:$Directories -File:$Files -ErrorAction SilentlyContinue
    foreach ($item in $items) {
        foreach ($pattern in $Names) {
            if ($item.Name -like $pattern) { $Targets.Add($item.FullName); break }
        }
    }
}

# Caches Python, pytest et Ruff (hors venv, gere par pip).
foreach ($RelativeRoot in @('backend', 'bot', 'frontend', 'indexer\src')) {
    Add-Matches (Join-Path $ProjectRoot $RelativeRoot) @('__pycache__', '.pytest_cache', '.ruff_cache') -Recurse -Directories
}
Add-Matches $ProjectRoot @('__pycache__', '.pytest_cache', '.ruff_cache', '.codex_pytest_*', 'pytest-cache-files-*', 'venv.broken-*') -Directories

# Copies temporaires et verrous orphelins laisses par d'anciennes versions ou par les tests.
Add-Matches (Join-Path $ProjectRoot 'backend') @('*.sqlite.tmp', 'analyzer_*pytest*.lock', '*.lock.tmp') -Files

# Fichiers temporaires du projet dans %TEMP% (tests, staging de l'indexeur).
Add-Matches $TempRoot @('oeil-browser-check-*', 'oeil-backend-tests-*', 'pytest-of-*') -Directories
Add-Matches $TempRoot @('oeil-indexer-*.jsonl') -Files

if ($BuildArtifacts) {
    foreach ($RelativePath in @('indexer\target', 'node_modules')) {
        $Candidate = Join-Path $ProjectRoot $RelativePath
        if (Test-Path -LiteralPath $Candidate) { $Targets.Add($Candidate) }
    }
}
if ($Logs) {
    Add-Matches (Join-Path $ProjectRoot 'backend') @('uvicorn*.log*', 'analyzer.log*') -Files
}

# Supprime les doublons et les cibles incluses dans une autre cible.
$SortedTargets = $Targets | Sort-Object @{ Expression = { $_.Length } }, @{ Expression = { $_ } } -Unique
$RootTargets = [Collections.Generic.List[string]]::new()
foreach ($Candidate in $SortedTargets) {
    $IsNested = $false
    foreach ($Parent in $RootTargets) {
        if ($Candidate.StartsWith($Parent.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            $IsNested = $true
            break
        }
    }
    if (-not $IsNested) { $RootTargets.Add($Candidate) }
}
if (-not $RootTargets.Count) {
    Write-Host "Aucun artefact correspondant."
    exit 0
}

foreach ($Target in $RootTargets) {
    $FullTarget = [IO.Path]::GetFullPath($Target)
    $InProject = $FullTarget.StartsWith($ProjectRoot + '\', [StringComparison]::OrdinalIgnoreCase)
    $InTemp = $FullTarget.StartsWith($TempRoot + '\', [StringComparison]::OrdinalIgnoreCase)
    if (-not ($InProject -or $InTemp)) {
        throw "Cible refusee hors projet et hors TEMP: $FullTarget"
    }
    if ($Execute) {
        Remove-Item -LiteralPath $FullTarget -Recurse -Force
        Write-Host "Supprime: $FullTarget"
    } else {
        Write-Host "A supprimer: $FullTarget"
    }
}

if (-not $Execute) {
    Write-Host "Aucune suppression effectuee. Ajoutez -Execute apres verification."
}
