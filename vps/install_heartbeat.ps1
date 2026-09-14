# One-time, on the VPS, in an ADMIN PowerShell, after C:\ql\token.txt exists (one line: the GitHub token).
# Installs the heartbeat as a scheduled task that runs every 15 minutes as SYSTEM, whether or not anyone is logged in.
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Force -Path C:\ql | Out-Null
if (-not (Test-Path C:\ql\token.txt)) { throw "C:\ql\token.txt is missing - put the GitHub token there first (one line)" }
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/juliantorrespr-afk/quant-lab-data/main/vps/heartbeat.ps1" -OutFile C:\ql\heartbeat.ps1
$act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\ql\heartbeat.ps1"
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$pri = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "QL_heartbeat" -Action $act -Trigger $trg -Principal $pri -Settings $set -Force | Out-Null
Start-ScheduledTask -TaskName "QL_heartbeat"
Start-Sleep 25
"---- last heartbeat log lines ----"
Get-Content C:\ql\heartbeat.log -Tail 3 -ErrorAction SilentlyContinue
"If you see 'pushed docs/vps.json', the Mind will show the trial equity within 15 minutes."
