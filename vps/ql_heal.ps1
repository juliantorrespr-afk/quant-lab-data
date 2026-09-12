# QUANT_LAB - self-healing compile + relaunch for the 1 GB box.
# Runs detached (survives RDP drops). Order matters: close MT5 FIRST so metaeditor
# gets the RAM, compile P7VT3 only if the source is newer than the .ex5 (bounded so it
# can never hang forever), then launch MT5 clean on the FTMO config. ASCII only (PS 5.1).
# Writes every step to C:\ql\heal_status.txt so a dropped session can read the result.
$r  = 'C:\Program Files\MetaTrader 5'
$ex = "$r\MQL5\Experts\P7VT3.mq5"
$e5 = "$r\MQL5\Experts\P7VT3.ex5"
$st = 'C:\ql\heal_status.txt'
function Log($m){ "$(Get-Date -Format 'HH:mm:ss') $m" | Tee-Object $st -Append }
Set-Content $st "=== ql_heal $(Get-Date) ==="

# 1) Close MT5 cleanly (persists servers.dat + saved password), then kill any hung metaeditor.
$t = Get-Process terminal64 -ErrorAction SilentlyContinue
if ($t) { $t.CloseMainWindow() | Out-Null; Log 'terminal64 CloseMainWindow sent' }
for ($i=0; $i -lt 20 -and (Get-Process terminal64 -ErrorAction SilentlyContinue); $i++){ Start-Sleep 1 }
if (Get-Process terminal64 -ErrorAction SilentlyContinue){ Stop-Process -Name terminal64 -Force; Log 'terminal64 forced (did not close in 20s)' } else { Log 'terminal64 closed' }
Get-Process metaeditor64 -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 2

# 2) Compile only if source newer than the compiled binary; bound the wait to 90s.
$need = -not (Test-Path $e5) -or ((Get-Item $ex).LastWriteTime -gt (Get-Item $e5).LastWriteTime)
if ($need) {
  Log 'compiling P7VT3 (source newer than ex5)'
  $p = Start-Process "$r\metaeditor64.exe" -ArgumentList '/portable',"/compile:`"$ex`"" -PassThru
  if (-not $p.WaitForExit(90000)) { $p.Kill(); Log 'compile TIMEOUT 90s - killed metaeditor' }
  else { Log ("compile exit=" + $p.ExitCode) }
} else { Log 'ex5 already up to date - skip compile' }
if (Test-Path $e5){ $g=Get-Item $e5; Log ("ex5 size=" + $g.Length + " ts=" + $g.LastWriteTime) }

# 3) Park the chart profile so /config makes exactly one chart, then launch on the FTMO config.
$prof = "$r\MQL5\Profiles\Charts\Default"
if (Test-Path $prof){ Rename-Item $prof ("Default_" + (Get-Date -Format 'yyyyMMdd_HHmmss')); Log 'parked chart profile' }
Start-Process "$r\terminal64.exe" -ArgumentList '/portable',"/config:$r\p7_ftmo.ini"
Log 'launched terminal64 with p7_ftmo.ini'
Log 'DONE'
