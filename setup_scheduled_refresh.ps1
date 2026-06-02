# =====================================================================
#   setup_scheduled_refresh.ps1
#
#   Registers a Windows scheduled task that runs auto_refresh.ps1 twice
#   a day (default 7:00 AM and 7:00 PM). Re-running this script updates
#   the existing task instead of duplicating it.
#
#   Open PowerShell, then:
#       cd 'C:\Users\mondr\Documents\Claude\obd-dashboard'
#       powershell -ExecutionPolicy Bypass -File .\setup_scheduled_refresh.ps1
#
#   No admin rights needed - the task runs as the current user.
#
#   To remove later:
#       Unregister-ScheduledTask -TaskName 'OBD Dashboard Refresh' -Confirm:$false
#
#   To change the times, edit the $morning / $evening variables below and
#   re-run this script.
# =====================================================================

$ErrorActionPreference = 'Stop'

# --- Configuration -------------------------------------------------
$taskName = 'OBD Dashboard Refresh'
$morning  = '7:00am'
$evening  = '7:00pm'
# -------------------------------------------------------------------

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$wrapper = Join-Path $repo 'auto_refresh.ps1'

if (-not (Test-Path $wrapper)) {
    Write-Host "ERROR: $wrapper not found. Run this from the repo folder." -ForegroundColor Red
    exit 1
}

Write-Host "=== Registering Windows scheduled task ===" -ForegroundColor Cyan
Write-Host "  Name:     $taskName"
Write-Host "  Runs:     daily at $morning and $evening"
Write-Host "  Wrapper:  $wrapper"
Write-Host "  Repo:     $repo"
Write-Host "  As user:  $env:USERNAME (no admin needed)"
Write-Host ""

# What the task does: launch PowerShell hidden, executing auto_refresh.ps1
$psArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`""
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $psArgs `
    -WorkingDirectory $repo

# When it fires
$trigger1 = New-ScheduledTaskTrigger -Daily -At $morning
$trigger2 = New-ScheduledTaskTrigger -Daily -At $evening

# Run as current user, only when logged in (so git credential manager is accessible)
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Behaviour: don't be obnoxious about battery, allow catch-up if missed
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

# If the task already exists, remove it first (so re-running this script is idempotent).
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Existing task found, replacing..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$null = Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger1, $trigger2 `
    -Principal $principal `
    -Settings $settings `
    -Description "Auto-refresh the OBD dashboard from Dropbox CSVs and push to GitHub. Logs to $repo\refresh.log."

Write-Host "Registered." -ForegroundColor Green
Write-Host ""
Write-Host "Verify in Task Scheduler (taskschd.msc) under 'Task Scheduler Library'." -ForegroundColor Cyan
Write-Host "Watch live output: Get-Content -Wait $repo\refresh.log" -ForegroundColor Cyan
Write-Host ""
Write-Host "Want to test it now? Run:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Then check the tail of refresh.log."
