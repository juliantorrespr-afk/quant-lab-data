# QUANT_LAB - Lightsail launch script (runs once as SYSTEM at first boot of a fresh Windows instance).
# Installs MetaTrader 5, pulls P7VT3 + preset + FTMO startup config from the public repo, compiles the EA.
# No secrets here. Login to OANDA-Demo-1 is done once by hand (Jae types the password) after boot.
$r = 'C:\Program Files\MetaTrader 5'
$u = 'https://raw.githubusercontent.com/juliantorrespr-afk/quant-lab-data/main/'
$log = 'C:\ql_setup.log'
"start $(Get-Date)" | Out-File $log
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
curl.exe -sL -o C:\mt5setup.exe https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe
"setup bytes: $((Get-Item C:\mt5setup.exe).Length)" | Out-File $log -Append
Start-Process C:\mt5setup.exe -ArgumentList '/auto' -Wait
Start-Sleep 30
Get-Process terminal64, metaeditor64 -ErrorAction SilentlyContinue | Stop-Process -Force
"installed: $(Test-Path "$r\terminal64.exe")" | Out-File $log -Append
New-Item -ItemType Directory -Force "$r\MQL5\Experts", "$r\MQL5\Presets" | Out-Null
curl.exe -sL -o "$r\MQL5\Experts\P7VT3.mq5" ($u + 'P7VT3.mq5')
curl.exe -sL -o "$r\MQL5\Presets\P7VT3.set" ($u + 'vps/P7VT3.set')
curl.exe -sL -o "$r\p7_ftmo.ini" ($u + 'vps/p7_ftmo.ini')
curl.exe -sL -o "$r\vps_attach.ps1" ($u + 'vps/vps_attach.ps1')
Start-Process "$r\metaeditor64.exe" -ArgumentList '/portable', "/compile:`"$r\MQL5\Experts\P7VT3.mq5`"", '/log' -Wait
"compiled: $(Test-Path "$r\MQL5\Experts\P7VT3.ex5")" | Out-File $log -Append
"done $(Get-Date)" | Out-File $log -Append
