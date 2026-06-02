# =====================================================================
#   auto_refresh.ps1
#
#   Called by Windows Task Scheduler twice a day. Runs the same refresh
#   as refresh_dashboard.bat but silently and logs every line to
#   refresh.log alongside this script. Does NOT pause at the end.
#
#   You can also run it manually from PowerShell:
#       powershell -ExecutionPolicy Bypass -File .\auto_refresh.ps1
# =====================================================================

$ErrorActionPreference = 'Continue'

# Resolve the repo folder relative to this script
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

$log = Join-Path $repo 'refresh.log'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
"`n========== $stamp ==========" | Out-File -Append -Encoding UTF8 $log

# Pick a Python interpreter: prefer the 'py' launcher, fall back to 'python'.
$pyExe = $null
$cmd = Get-Command py -ErrorAction SilentlyContinue
if ($cmd) { $pyExe = $cmd.Source; $pyArgs = @('-3', 'refresh_dashboard.py') }
else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $pyExe = $cmd.Source; $pyArgs = @('refresh_dashboard.py') }
}
if (-not $pyExe) {
    "ERROR: Python not found on PATH. Install Python 3 to enable auto refresh." | Out-File -Append -Encoding UTF8 $log
    exit 1
}

"Python: $pyExe"  | Out-File -Append -Encoding UTF8 $log
"Repo:   $repo"   | Out-File -Append -Encoding UTF8 $log

# Run it and capture stdout+stderr to the log
& $pyExe @pyArgs *>&1 | Out-File -Append -Encoding UTF8 $log
$ec = $LASTEXITCODE
"Exit code: $ec" | Out-File -Append -Encoding UTF8 $log

exit $ec
