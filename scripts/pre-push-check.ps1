<#
.SYNOPSIS
    Pre-push secret verification gate for BSL Router.
    Run this BEFORE every git push origin <branch>.

.DESCRIPTION
    Implements the 4-gate checklist from .agents/AGENTS.md HARD RULE (2026-09-10).
    Exits 0 if all gates pass (safe to push), exits 1 if any gate fails (STOP).

    Gate 1: Tracked-tree secret sweep (partial key fragments + full key patterns)
    Gate 2: Machine-specific path sweep
    Gate 3: Unpushed-diff secret pattern scan (filters out dummy placeholders)
    Gate 4: Gitignore invariant check

    Self-exclusion: This script and .agents/AGENTS.md contain the search patterns
    as documentation/implementation. They are excluded from the scan to avoid
    self-referential false positives. Real leaks would be in OTHER files.

    Known-safe patterns (NOT flagged):
    - localhost:6969, 127.0.0.1:6969  (canonical dev port)
    - sk-bsl-YOUR_API_KEY_HERE         (dummy placeholder)
    - REDACTED-BSL-ADMIN-KEY           (redaction placeholder)

.EXAMPLE
    .\scripts\pre-push-check.ps1
    .\scripts\pre-push-check.ps1 -Branch timeout-fix

.NOTES
    This script is read-only: it does NOT modify, commit, or push anything.
#>
[CmdletBinding()]
param(
    [string]$Branch = ""
)

$ErrorActionPreference = 'Continue'
$exitCode = 0
$failures = @()

# Files that legitimately contain the search patterns as documentation/implementation.
# Excluded from the scan to avoid self-referential false positives.
$selfExclude = @(
    'scripts/pre-push-check.ps1',
    '.agents/AGENTS.md',
    '.gitignore'
)

function Test-SelfExclude {
    param([string]$Line)
    foreach ($path in $selfExclude) {
        if ($Line -like "$path*") { return $true }
    }
    return $false
}

function Write-GateHeader {
    param([string]$Number, [string]$Title)
    Write-Host ""
    Write-Host "  Gate $Number - $Title" -ForegroundColor Cyan
}

function Write-GatePass {
    param([string]$Message)
    Write-Host "  PASS: $Message" -ForegroundColor Green
}

function Write-GateFail {
    param([string]$Message)
    Write-Host "  FAIL: $Message" -ForegroundColor Red
    $script:failures += "Gate: $Message"
    $script:exitCode = 1
}

# -- Determine branch --------------------------------------------------------
if (-not $Branch) {
    $Branch = git branch --show-current 2>$null
    if (-not $Branch) {
        Write-Host "  Cannot determine current branch." -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "  BSL Router Pre-Push Verification" -ForegroundColor White
Write-Host "  Branch: $Branch" -ForegroundColor Gray
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')" -ForegroundColor Gray

# -- Gate 1: Tracked-tree secret sweep ---------------------------------------
Write-GateHeader "1" "Tracked-tree secret sweep"

$secretPatterns = @(
    'sk-bsl-LzAC',
    'LzACIxbnpt',
    'Q4jHjmqdyCy5',
    'Q4jHmqdyCy5'
)

$gate1Hits = @()
foreach ($pattern in $secretPatterns) {
    $results = git grep -I -n -e $pattern 2>$null
    if ($results) {
        foreach ($r in $results) {
            if (-not (Test-SelfExclude $r)) {
                $gate1Hits += $r
            }
        }
    }
}

if ($gate1Hits.Count -eq 0) {
    Write-GatePass "No secret fragments in tracked tree."
} else {
    Write-GateFail "$($gate1Hits.Count) secret fragment(s) found in tracked tree:"
    $gate1Hits | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    if ($gate1Hits.Count -gt 10) {
        Write-Host "    ... and $($gate1Hits.Count - 10) more" -ForegroundColor Yellow
    }
}

# -- Gate 2: Machine-specific path sweep -------------------------------------
Write-GateHeader "2" "Machine-specific path sweep"

$machinePatterns = @(
    'D:/Tools',
    'D:\Tools',
    'd:/Projects/BSL',
    'D:\Projects\BSL',
    'C:/Users/Admin',
    'C:\Users\Admin'
)

$gate2Hits = @()
foreach ($pattern in $machinePatterns) {
    $grepPattern = $pattern -replace '\\', '\\'
    $results = git grep -I -n -e $grepPattern 2>$null
    if ($results) {
        foreach ($r in $results) {
            if (-not (Test-SelfExclude $r)) {
                $gate2Hits += $r
            }
        }
    }
}

if ($gate2Hits.Count -eq 0) {
    Write-GatePass "No machine-specific paths in tracked tree."
} else {
    Write-GateFail "$($gate2Hits.Count) machine path(s) found:"
    $gate2Hits | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
}

# -- Gate 3: Unpushed-diff secret scan ----------------------------------------
Write-GateHeader "3" "Unpushed diff secret scan"

$originRef = "origin/$Branch"
$hasOrigin = git rev-parse --verify $originRef 2>$null

if ($hasOrigin) {
    $diffRange = "$originRef...HEAD"
} else {
    $originMain = git rev-parse --verify "origin/main" 2>$null
    if ($originMain) {
        $diffRange = "origin/main...HEAD"
    } else {
        $diffRange = "HEAD"
    }
}

$diffOutput = git diff $diffRange -- 2>$null
if (-not $diffOutput) {
    $diffOutput = git diff --cached 2>$null
}

$realKeyMatches = $diffOutput | Select-String -Pattern 'sk-bsl-[A-Za-z0-9]{10,}' -AllMatches 2>$null

$safePlaceholders = @(
    'sk-bsl-YOUR_API_KEY_HERE',
    'sk-bsl-your-api-key-here'
)

$realLeaks = @()
if ($realKeyMatches) {
    foreach ($match in $realKeyMatches.Matches) {
        $value = $match.Value
        $isSafe = $false
        foreach ($safe in $safePlaceholders) {
            if ($value -like "$safe*") { $isSafe = $true; break }
        }
        if (-not $isSafe) {
            $realLeaks += $value
        }
    }
}

if ($realLeaks.Count -eq 0) {
    Write-GatePass "No real API key patterns in unpushed diff."
} else {
    Write-GateFail "$($realLeaks.Count) potential real key(s) in unpushed diff:"
    $realLeaks | Select-Object -First 5 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
}

# -- Gate 4: Gitignore invariant ----------------------------------------------
Write-GateHeader "4" "Gitignore invariant"

$requiredIgnored = @(
    'config.yaml',
    '.bsl_key',
    '.bsl_key.dpapi',
    '.venv',
    '.mcp.json'
)

$gate4Fails = @()
foreach ($path in $requiredIgnored) {
    $result = git check-ignore $path 2>$null
    if (-not $result) {
        $gate4Fails += $path
    }
}

if ($gate4Fails.Count -eq 0) {
    Write-GatePass "All required paths are gitignored."
} else {
    Write-GateFail "These paths are NOT gitignored (should be):"
    $gate4Fails | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
}

# -- Summary -----------------------------------------------------------------
Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "  ================================================" -ForegroundColor Green
    Write-Host "  ALL GATES PASSED - safe to push." -ForegroundColor Green
    Write-Host "  ================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Next: git push origin $Branch" -ForegroundColor Gray
} else {
    Write-Host "  ================================================" -ForegroundColor Red
    Write-Host "  $($failures.Count) GATE(S) FAILED - DO NOT PUSH." -ForegroundColor Red
    Write-Host "  ================================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Fix the issues above, then re-run this script." -ForegroundColor Yellow
    Write-Host "  See .agents/AGENTS.md HARD RULE (2026-09-10)." -ForegroundColor Yellow
}

Write-Host ""
exit $exitCode
