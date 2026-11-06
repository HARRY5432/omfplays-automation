# install_autostart.ps1 — register the omfplays pipeline as a Windows task.
#
# Triggers
#   * at logon  — the automation starts with the PC, as requested
#   * daily 07:05, repeating every 60 min for 12 h (07:05-19:05)
#
# The pipeline itself is idempotent and takes a lock, so extra runs are cheap
# and can never double-post or overlap.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_autostart.ps1
#   ... -Status
#   ... -Remove
param(
    [switch]$Remove,
    [switch]$Status,
    [string]$TaskName = "omfplays-pipeline",
    [string]$StartTime = "07:05"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $root "run_local.bat"
$vbs = Join-Path $root "run_hidden.vbs"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    }
    else {
        Write-Host "Scheduled task '$TaskName' is not installed."
    }
    return
}

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Scheduled task '$TaskName' is not installed."
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task      : $TaskName"
    Write-Host "State     : $($task.State)"
    Write-Host "Last run  : $($info.LastRunTime)"
    Write-Host "Last result: $($info.LastTaskResult)"
    Write-Host "Next run  : $($info.NextRunTime)"
    return
}

foreach ($f in @($bat, $vbs)) {
    if (-not (Test-Path $f)) { throw "Missing required file: $f" }
}

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`"" -WorkingDirectory $root

$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$daily = New-ScheduledTaskTrigger -Daily -At $StartTime
$daily.Repetition = (New-ScheduledTaskTrigger `
        -Once -At $StartTime `
        -RepetitionInterval (New-TimeSpan -Minutes 60) `
        -RepetitionDuration (New-TimeSpan -Hours 12)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($logon, $daily) `
    -Settings $settings `
    -Description "omfplays pipeline: build shorts, publish the local slot, push to GitHub" `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "  * at logon for $env:USERNAME"
Write-Host "  * daily $StartTime, every 60 min for 12 h"
Write-Host "  * runs hidden via wscript + run_hidden.vbs"
Write-Host ""
Write-Host "Check it with:  install_autostart.bat -Status"
Write-Host "Remove it with: install_autostart.bat -Remove"
Write-Host "Dry run now:    python run_local_pipeline.py --dry-run"
