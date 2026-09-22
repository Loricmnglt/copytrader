# copytrader auto-restart launcher (PowerShell).
#
#   Usage:  right-click > "Run with PowerShell", or from a terminal:
#             .\run.ps1              # runs the paper engine
#             .\run.ps1 report       # any subcommand works
#
# It relaunches the bot automatically if it ever exits/crashes, so a
# multi-day (or 24h) run survives transient errors and reboots-of-the-bot.
# Open positions + cash are restored from state_<mode>.json on each restart.
#
# To STOP: close this window (or press Ctrl+C twice).

param([string]$Cmd = "paper")

Set-Location -Path $PSScriptRoot
$log = Join-Path $PSScriptRoot "copytrader.out.log"

Write-Host "copytrader launcher -> 'python -m copytrader $Cmd' (auto-restart)"
Write-Host "Logs: $log"
Write-Host "Stop: close this window or Ctrl+C."
Write-Host ""

while ($true) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "[$ts] starting: python -m copytrader $Cmd"
    python -m copytrader $Cmd 2>&1 | Tee-Object -FilePath $log -Append
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "[$ts] exited (code $LASTEXITCODE); restart in 10s"
    Write-Host "`n[copytrader] process exited; restarting in 10s (Ctrl+C to stop)...`n"
    Start-Sleep -Seconds 10
}
