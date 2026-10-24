# Register (or remove) a per-user scheduled task that starts the bot supervisor at logon.
# Usage:  pwsh scripts\install_service.ps1            # install and start now
#         pwsh scripts\install_service.ps1 -Uninstall # stop and remove
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$name = 'discord-dfy'
$root = Split-Path -Parent $PSScriptRoot

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Output "Removed scheduled task '$name'."
    return
}

$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw; run 'uv sync --locked' first." }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m discord_dfy.supervisor' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# No time limit; Task Scheduler also restarts the supervisor itself if it ever fails.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description 'discord-dfy bot supervisor (restarts the bot on crash or !restart)' -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Output "Installed and started scheduled task '$name'. Logs: $root\data\logs"
