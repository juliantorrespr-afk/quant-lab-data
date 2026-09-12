# QUANT_LAB - idempotent MT5 launcher (scheduled task, runs at Administrator logon on the VPS).
# Starts terminal64 in portable mode with the FTMO startup config ONLY if it is not already running.
# Never kills a running terminal. No secrets: the account password lives in MT5's own saved store.
$r = 'C:\Program Files\MetaTrader 5'
if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
  Start-Process "$r\terminal64.exe" -ArgumentList '/portable', "/config:$r\p7_ftmo.ini"
  "started $(Get-Date)" | Out-File C:\ql\mt5_boot.log -Append
} else {
  "already running $(Get-Date)" | Out-File C:\ql\mt5_boot.log -Append
}
