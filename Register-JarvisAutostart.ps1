# Phase 3 autostart: JARVIS on Windows logon, 30s delay (network + audio ready).
# Run once in an elevated PowerShell. To remove: schtasks /delete /tn "JARVIS MARK 3" /f
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $here "jarvis-start.bat"
schtasks /create /tn "JARVIS MARK 3" /tr "`"$bat`"" /sc onlogon /delay 0000:30 /rl limited /f
Write-Output "Registered. Verify: schtasks /query /tn `"JARVIS MARK 3`""
