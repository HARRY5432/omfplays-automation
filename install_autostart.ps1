# install_autostart.ps1 — register the omfplays automation as Windows tasks.
#
# Two tasks, because they need different windows:
#
#   <TaskName>        hourly, HIDDEN   — daily 07:05, every 60 min for 12 h,
#                                        started via wscript + run_hidden.vbs
#   <TaskName>-logon  at logon, VISIBLE — opens a console that prints the
#                                        command centre (today's slots, node
#                                        health, clips left in each raw video)
#                                        and then runs the pipeline in it
#
# Both point at the same idempotent pipeline, which takes a lock, so an
# overlap is cheap and can never double-post.
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
$logonBat = Join-Path $root "run_logon.bat"
$vbs = Join-Path $root "run_hidden.vbs"

$logonTaskName = "$TaskName-logon"
$taskNames = @($TaskName, $logonTaskName)

if ($Remove) {
    foreach ($name in $taskNames) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed scheduled task '$name'." -ForegroundColor Green
        }
        else {
            Write-Host "Scheduled task '$name' is not installed."
        }
    }
    return
}

if ($Status) {
    foreach ($name in $taskNames) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Host "Scheduled task '$name' is not installed."
            continue
        }
        $info = Get-ScheduledTaskInfo -TaskName $name
        Write-Host "Task       : $name"
        Write-Host "State      : $($task.State)"
        Write-Host "Last run   : $($info.LastRunTime)"
        Write-Host "Last result: $($info.LastTaskResult)"
        Write-Host "Next run   : $($info.NextRunTime)"
        Write-Host ""
    }
    return
}

foreach ($f in @($bat, $logonBat, $vbs)) {
    if (-not (Test-Path $f)) { throw "Missing required file: $f" }
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

# ── hourly, hidden ────────────────────────────────────────────────────────
$hiddenAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`"" -WorkingDirectory $root

$daily = New-ScheduledTaskTrigger -Daily -At $StartTime
$daily.Repetition = (New-ScheduledTaskTrigger `
        -Once -At $StartTime `
        -RepetitionInterval (New-TimeSpan -Minutes 60) `
        -RepetitionDuration (New-TimeSpan -Hours 12)).Repetition

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $hiddenAction `
    -Trigger @($daily) `
    -Settings $settings `
    -Description "omfplays pipeline (hourly, hidden): build shorts, publish the local slot, push to GitHub" `
    -Force | Out-Null

# ── at logon, visible ─────────────────────────────────────────────────────
# cmd.exe /c ""<path>\run_logon.bat"" — the doubled quotes keep the space in
# "F:\...Content Engine\..." intact.
$logonAction = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"`"$logonBat`"`"" `
    -WorkingDirectory $root

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

Register-ScheduledTask `
    -TaskName $logonTaskName `
    -Action $logonAction `
    -Trigger @($logonTrigger) `
    -Settings $settings `
    -Description "omfplays startup report (visible console): command centre + one pipeline run" `
    -Force | Out-Null

Write-Host "Installed scheduled tasks." -ForegroundColor Green
Write-Host "  '$TaskName'"
Write-Host "    * daily $StartTime, every 60 min for 12 h"
Write-Host "    * runs hidden via wscript + run_hidden.vbs"
Write-Host "  '$logonTaskName'"
Write-Host "    * at logon for $env:USERNAME"
Write-Host "    * visible console: status block, then one pipeline run"
Write-Host ""
Write-Host "Check them with:  install_autostart.bat -Status"
Write-Host "Remove them with: install_autostart.bat -Remove"
Write-Host "Dry run now:      python run_local_pipeline.py --dry-run --no-push"
