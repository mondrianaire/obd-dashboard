# =====================================================================
#   One-time GitHub setup for obd-dashboard
#
#   Open PowerShell, then:
#     cd 'C:\Users\mondr\Documents\Claude\obd-dashboard'
#     powershell -ExecutionPolicy Bypass -File .\setup_github.ps1
#
#   This will:
#     1. Verify gh + git are installed
#     2. Authenticate gh with your GitHub account (browser flow)
#     3. Initialize a local git repo + initial commit
#     4. Create a public 'obd-dashboard' repo on GitHub and push
#     5. Enable GitHub Pages serving from main/root
#     6. Print the live URL
# =====================================================================

$ErrorActionPreference = 'Stop'

Write-Host "=== Step 1: checking gh + git ===" -ForegroundColor Cyan
gh --version | Select-Object -First 1
git --version

Write-Host "`n=== Step 2: gh auth ===" -ForegroundColor Cyan
gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Not logged in. Starting browser flow..." -ForegroundColor Yellow
    gh auth login -h github.com -p https -w
} else {
    Write-Host "  Already authenticated."
}
$me = (gh api user -q .login).Trim()
Write-Host "  Logged in as: $me"

Write-Host "`n=== Step 3: local git init + initial commit ===" -ForegroundColor Cyan
# Detect (a) a half-broken .git folder (left over from a sandbox attempt) and
# (b) raw data/ files baked into existing history (would blow past GitHub's
# 100 MB file limit on push). Either one means we should start fresh.
$brokenGit = (Test-Path '.git') -and (
    (Test-Path '.git\index.lock') -or
    (Test-Path '.git\config.lock') -or
    (-not (Test-Path '.git\objects'))
)
$dataInHistory = $false
if ((Test-Path '.git\HEAD') -and (-not $brokenGit)) {
    $null = git rev-parse HEAD 2>&1
    if ($LASTEXITCODE -eq 0) {
        $found = git log --all --pretty=format: --name-only 2>$null |
                 Where-Object { $_ -match '^data/' } |
                 Select-Object -First 1
        if ($found) { $dataInHistory = $true }
    }
}
if ($brokenGit -or $dataInHistory) {
    if ($dataInHistory) {
        Write-Host "  Resetting .git: raw data/ files exist in earlier commits (would exceed GitHub's 100 MB per-file limit on push)." -ForegroundColor Yellow
    } else {
        Write-Host "  Cleaning leftover .git folder..." -ForegroundColor Yellow
    }
    Remove-Item -Recurse -Force .git
}
if (-not (Test-Path '.git')) {
    git init -b main
}
git config user.name  'Jett'
git config user.email 'mondrianaire@gmail.com'
git add -A
$staged = (git diff --cached --name-only | Measure-Object).Count
if ($staged -gt 0) {
    git commit -m 'Initial commit: 15-drive OBD-II dashboard for the 2016 VW Golf GTI'
} else {
    Write-Host '  (nothing new to commit)'
}

Write-Host "`n=== Step 3.5: ensure raw data/ is not tracked ===" -ForegroundColor Cyan
# Architecture decision: raw drive logs live locally only. The repo carries the
# rendered dashboard + JSON, not the per-drive CSV/dlg sources.
$tracked = git ls-files data/ 2>$null
if ($tracked) {
    Write-Host "  Untracking data/ from git (keeping files on disk)..."
    git rm -r --cached --quiet data/
    git add .gitignore
    git commit --amend --no-edit | Out-Null
    Write-Host "  Amended initial commit; raw data no longer in repo."
} else {
    Write-Host "  Already not tracked. Good."
}

Write-Host "`n=== Step 4: create public repo + push ===" -ForegroundColor Cyan
# Relax error handling for native commands - stderr from gh shouldn't abort us.
$savedEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'

# Check if the repo exists. Redirect both streams to a variable so PowerShell
# can't promote the stderr text into a terminating error.
$null = gh repo view "$me/obd-dashboard" --json name 2>&1
$exists = ($LASTEXITCODE -eq 0)

if ($exists) {
    Write-Host "  Repo $me/obd-dashboard already exists. Adding remote + pushing..."
    $null = git remote remove origin 2>&1
    git remote add origin "https://github.com/$me/obd-dashboard.git"
    git push -u origin main
} else {
    gh repo create obd-dashboard --public --source=. --remote=origin --push --description 'Drive telemetry dashboard for a 2016 VW Golf GTI (15 drives, 390+ miles)'
}

Write-Host "`n=== Step 5: enable GitHub Pages (main / root) ===" -ForegroundColor Cyan
$pagesBody = '{"source":{"branch":"main","path":"/"}}'
$pagesPath = "/repos/$me/obd-dashboard/pages"
$pagesOut  = $pagesBody | gh api --method POST -H "Accept: application/vnd.github+json" $pagesPath --input - 2>&1
$pagesExit = $LASTEXITCODE
$pagesOut  | ForEach-Object { Write-Host "  $_" }
if ($pagesExit -ne 0) {
    Write-Host "  (Non-zero exit - Pages may already be enabled, that is fine.)" -ForegroundColor Yellow
}

$ErrorActionPreference = $savedEAP

Write-Host "`n=== Done ===" -ForegroundColor Green
$liveUrl = "https://$me.github.io/obd-dashboard/"
Write-Host "Repo:  https://github.com/$me/obd-dashboard"
Write-Host "Pages: $liveUrl  (allow ~1 minute for first deploy)"
Write-Host ""
Write-Host "Future refreshes:  .\refresh_dashboard.bat" -ForegroundColor Cyan
Write-Host "  ...will rebuild data, commit, and push automatically."
