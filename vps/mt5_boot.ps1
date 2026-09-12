# QUANT_LAB - idempotent MT5 launcher (scheduled task QL, runs at Administrator logon on the VPS).
# Deterministic boot: park the chart profile, then launch terminal64 /portable with the FTMO startup
# config, which logs into the saved account and creates exactly ONE XAUUSD.sim D1 chart with P7VT3
# (inputs from MQL5\Presets\P7VT3.set). Starts only if terminal64 is not already running.
# Never kills a running terminal. No secrets here (password is in MT5's own saved store).
$r = 'C:\Program Files\MetaTrader 5'
$log = 'C:\ql\mt5_boot.log'
if (Get-Process terminal64 -ErrorAction SilentlyContinue) {
  "already running $(Get-Date)" | Out-File $log -Append
  return
}
$p = "$r\MQL5\Profiles\Charts\Default"
if (Test-Path $p) { Rename-Item $p ("Default_" + (Get-Date -Format 'yyyyMMdd_HHmmss')) }
Start-Process "$r\terminal64.exe" -ArgumentList '/portable', "/config:$r\p7_ftmo.ini"
"started with p7_ftmo.ini $(Get-Date)" | Out-File $log -Append
