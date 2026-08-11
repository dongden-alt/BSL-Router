<#
.SYNOPSIS
    One-way sanitized export: working repo  ->  public repo.

.DESCRIPTION
    The public repo is a DERIVED ARTIFACT, not a parallel codebase. Fix bugs once
    in the working repo, then run this script to re-export. This replaces manual
    per-repo porting, which is what allowed the two trees to drift apart.

    Three categories of files are handled differently:

      1. SOURCE  - copied from working -> public (app/, scripts/, tests/, etc.)
      2. SECRETS - never copied (config.yaml, .bsl_key, .venv, runtime logs)
      3. PUBLIC-ONLY - never overwritten (README, CHANGELOG, CONTRIBUTING,
         SECURITY, docs/ARCHITECTURE.md, config.example.yaml, .github/)

    Category 3 matters: those files are authored FOR the public repo and have no
    counterpart in the working tree. A naive mirror would delete them.

.PARAMETER DryRun
    Report what would change without writing anything. Always start here.

.PARAMETER Force
    Perform the export. Required for any actual writes.

.EXAMPLE
    .\scripts\export-public.ps1 -DryRun
    .\scripts\export-public.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Force,
    [string]$Source,
    [string]$Target = "D:\Projects\BSL Router Public"
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot is not populated during parameter binding, so the repo root has
# to be resolved here in the body rather than as a param default.
if (-not $Source) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Source = Split-Path -Parent $scriptDir
}

if (-not $DryRun -and -not $Force) {
    Write-Host ""
    Write-Host "  Refusing to run without an explicit mode." -ForegroundColor Yellow
    Write-Host "  Preview first:  .\scripts\export-public.ps1 -DryRun"
    Write-Host "  Then execute:   .\scripts\export-public.ps1 -Force"
    Write-Host ""
    exit 1
}

# -- Validate both ends before touching anything ------------------------------
if (-not (Test-Path (Join-Path $Source 'app\main.py'))) {
    Write-Error "Source does not look like the BSL Router repo: $Source"
    exit 1
}
if (-not (Test-Path (Join-Path $Target '.git'))) {
    Write-Error "Target is not a git repository: $Target`nRefusing to write to a non-repo directory."
    exit 1
}

# -- NEVER copy these. Secrets, machine-bound state, runtime artifacts. -------
# config.yaml holds live API keys, and config.backup.* snapshots are FULL
# COPIES of it (same keys) — the 2026-08-11 dry-run caught two 229KB backups
# queued for public export. .bsl_key is the machine-bound encryption key
# and makes every encrypted value in config.yaml readable.
$SecretPatterns = @(
    'config.yaml',
    'config.yaml.*',
    'config.backup.*',
    '.bsl_key',
    '.bsl_key.dpapi',
    '*.log'
)

# -- NEVER overwrite these. Authored for the public repo only. ----------------
$PublicOnly = @(
    'README.md',
    'CHANGELOG.md',
    'CONTRIBUTING.md',
    'SECURITY.md',
    'LICENSE',
    'config.example.yaml',
    'docs/ARCHITECTURE.md',
    '.github/CODE_OF_CONDUCT.md'
)

# -- NEVER copy these. Private working-repo files. ---------------------------
# GEMINI.md holds agent operating rules; artifacts/ holds internal design notes.
# The three root scripts are one-off internal maintenance helpers from config
# cleanup sessions — not part of the product, not safe/useful for consumers.
$PrivateToWorking = @(
    'GEMINI.md',
    'artifacts',
    'detect_deleted.py',
    'remove_deleted_providers.py',
    'verify_integrity.py'
)

# -- Directories excluded wholesale -----------------------------------------
$ExcludeDirs = @(
    '.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache',
    '.brain', 'scratch', 'data', '.agents'
)

$SourceExtensions = @('.py', '.js', '.css', '.html', '.md', '.yaml', '.yml',
                      '.ps1', '.txt', '.bat', '.json', '.cfg', '.toml', '.ico',
                      '.svg', '.png')

function Test-Excluded {
    param([string]$RelPath)

    $normalized = $RelPath -replace '\\', '/'

    foreach ($dir in $ExcludeDirs) {
        if ($normalized -eq $dir -or $normalized -like "$dir/*" -or $normalized -like "*/$dir/*") {
            return $true
        }
    }
    foreach ($priv in $PrivateToWorking) {
        if ($normalized -eq $priv -or $normalized -like "$priv/*") { return $true }
    }
    $leaf = Split-Path $RelPath -Leaf
    foreach ($pattern in $SecretPatterns) {
        if ($leaf -like $pattern) { return $true }
    }
    return $false
}

function Test-PublicOnly {
    param([string]$RelPath)
    $normalized = $RelPath -replace '\\', '/'
    return $PublicOnly -contains $normalized
}

Write-Host ""
Write-Host "BSL Router  ->  public export" -ForegroundColor Cyan
Write-Host "  source: $Source"
Write-Host "  target: $Target"
Write-Host "  mode:   $(if ($DryRun) { 'DRY RUN (no writes)' } else { 'EXPORT' })"
Write-Host ""

# -- Refuse to overwrite uncommitted work in the target ---------------------
Push-Location $Target
try {
    $dirty = git status --porcelain 2>$null
} finally {
    Pop-Location
}
if ($dirty -and -not $DryRun) {
    Write-Host "  Target has uncommitted changes:" -ForegroundColor Yellow
    $dirty -split "`n" | Select-Object -First 15 | ForEach-Object { Write-Host "    $_" }
    Write-Host ""
    Write-Host "  Commit or stash them first so this export stays reviewable." -ForegroundColor Yellow
    exit 1
}

# -- Walk the source tree ---------------------------------------------------
$copied = @()
$skippedSecret = @()
$skippedPublicOnly = @()
$unchanged = 0

Get-ChildItem -Path $Source -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
    $rel = $_.FullName.Substring($Source.Length + 1)

    if ($_.Extension -notin $SourceExtensions) { return }

    if (Test-Excluded $rel) {
        $leaf = Split-Path $rel -Leaf
        foreach ($pattern in $SecretPatterns) {
            if ($leaf -like $pattern) { $script:skippedSecret += $rel; break }
        }
        return
    }

    if (Test-PublicOnly $rel) {
        $script:skippedPublicOnly += $rel
        return
    }

    $destPath = Join-Path $Target $rel

    # Skip byte-identical files so the report shows only real changes.
    if (Test-Path $destPath) {
        $srcHash = (Get-FileHash -Path $_.FullName -Algorithm SHA256).Hash
        $dstHash = (Get-FileHash -Path $destPath -Algorithm SHA256).Hash
        if ($srcHash -eq $dstHash) { $script:unchanged++; return }
    }

    $script:copied += $rel

    if (-not $DryRun) {
        $destDir = Split-Path $destPath -Parent
        if (-not (Test-Path $destDir)) {
            New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        }
        Copy-Item -Path $_.FullName -Destination $destPath -Force
    }
}

# -- Report ----------------------------------------------------------------
Write-Host "  Files to sync: $($copied.Count)" -ForegroundColor $(if ($copied.Count) { 'Green' } else { 'Gray' })
$copied | Select-Object -First 40 | ForEach-Object { Write-Host "    ~ $_" }
if ($copied.Count -gt 40) { Write-Host "    ... and $($copied.Count - 40) more" }

Write-Host ""
Write-Host "  Unchanged (skipped): $unchanged"
Write-Host "  Public-only (preserved): $($skippedPublicOnly.Count)"
$skippedPublicOnly | ForEach-Object { Write-Host "    = $_" }

if ($skippedSecret.Count) {
    Write-Host ""
    Write-Host "  Secrets blocked: $($skippedSecret.Count)" -ForegroundColor Yellow
    $skippedSecret | ForEach-Object { Write-Host "    ! $_" -ForegroundColor Yellow }
}

# -- Post-export safety check: no secret may exist in the target -----------
Write-Host ""
Write-Host "  Verifying target has no secrets..." -ForegroundColor Cyan
$leaked = @()
# Get-ChildItem -Filter handles wildcards, which Join-Path+Test-Path do not —
# the old scan matched only the literal name 'config.yaml' and would have let
# config.backup.* leaks through the gate.
foreach ($pattern in @('config.yaml', 'config.backup.*', '.bsl_key', '.bsl_key.dpapi')) {
    $hits = @(Get-ChildItem -Path $Target -Filter $pattern -ErrorAction SilentlyContinue)
    if ($hits.Count) { $leaked += $pattern }
}
if ($leaked.Count) {
    Write-Host "  SECRET PRESENT IN TARGET: $($leaked -join ', ')" -ForegroundColor Red
    Write-Host "  Remove it before committing or pushing." -ForegroundColor Red
    exit 1
}
Write-Host "  Clean: no config.yaml or key files in target." -ForegroundColor Green

Write-Host ""
if ($DryRun) {
    Write-Host "  Dry run complete. Nothing was written." -ForegroundColor Cyan
    Write-Host "  Re-run with -Force to apply."
} else {
    Write-Host "  Export complete." -ForegroundColor Green
    Write-Host "  Review with:  git -C `"$Target`" status --short"
    Write-Host "                git -C `"$Target`" diff"
}
Write-Host ""
