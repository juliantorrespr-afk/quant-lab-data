# QUANT_LAB - idempotent MT5 launcher (scheduled task QL, runs at Administrator logon on the VPS).
# Starts terminal64 in portable mode ONLY if it is not already running. NO /config: the saved profile
# (login, server, charts, the one P7VT3 chart) is restored by MT5 itself. /config is used only once,
# by hand, to (re)create the EA chart after a clean exit - every /config launch adds another chart+EA.
# Never kills a running terminal. No secrets here.
$r = 'C:\Program Files\MetaTrader 5'
if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
  Start-Process "$r\terminal64.exe" -ArgumentList '/portable'
  "started $(Get-Date)" | Out-File C:\ql\mt5_boot.log -Append
} else {
  "already running $(Get-Date)" | Out-File C:\ql\mt5_boot.log -Append
}
