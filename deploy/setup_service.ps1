# Service Desk Helper - Server Setup Script
# Run this script on the workstation in an ELEVATED PowerShell
# Usage: powershell -ExecutionPolicy Bypass -File setup_service.ps1

$TaskName = "ServiceDeskHelper"
$PythonPath = "C:\Program Files\PyManager\python.exe"
$ProjectDir = "C:\projects\service_desk_helper"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Create the scheduled task
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument "-m uvicorn src.main:app --host 0.0.0.0 --port 8000" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettings -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 365)
$principal = New-ScheduledTaskPrincipal -UserId "UPHS\AslanukA" -LogonType S4U -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force

Write-Host "Scheduled task '$TaskName' created successfully." -ForegroundColor Green
Write-Host "Starting the task now..."

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Task status: $($task.State)" -ForegroundColor Cyan

if ($task.State -eq "Running") {
    Write-Host "Server is running! Access it at http://localhost:8000/ui/" -ForegroundColor Green
} else {
    Write-Host "Task may have failed. Check with: Get-ScheduledTaskInfo -TaskName $TaskName" -ForegroundColor Yellow
}