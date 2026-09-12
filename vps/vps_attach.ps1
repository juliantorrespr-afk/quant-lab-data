# QUANT_LAB — attach P7VT3 on the FTMO US demo by config (no GUI clicks).
# Run in an admin PowerShell on the VPS AFTER the first manual login to OANDA-Demo-1 saved the password.
$r = 'C:\Program Files\MetaTrader 5'
$u = 'https://raw.githubusercontent.com/juliantorrespr-afk/quant-lab-data/main/vps/'
New-Item -ItemType Directory -Force "$r\MQL5\Presets" | Out-Null
curl.exe -sL -o "$r\MQL5\Presets\P7VT3.set" ($u + 'P7VT3.set')
curl.exe -sL -o "$r\p7_ftmo.ini" ($u + 'p7_ftmo.ini')
Get-Process terminal64 -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 3
Start-Process "$r\terminal64.exe" -ArgumentList '/portable', "/config:$r\p7_ftmo.ini"
Write-Host "launched with p7_ftmo.ini — check Experts tab for [P7VT3] Init OK"
