# QUANT_LAB VPS bootstrap - installs MetaTrader 5 silently, drops the P7VT3 EA in, launches MT5 portable.
# Run on the VPS:  powershell -c "iwr -useb https://raw.githubusercontent.com/juliantorrespr-afk/quant-lab-data/main/vps_setup.ps1 | iex"
$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$u = 'https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe'
$f = "$env:TEMP\mt5setup.exe"
Write-Host "1/4 downloading MetaTrader 5 installer..."
Invoke-WebRequest $u -OutFile $f -UseBasicParsing
Write-Host "2/4 installing silently (about 1-2 min)..."
Start-Process $f -ArgumentList '/auto' -Wait
$root = 'C:\Program Files\MetaTrader 5'
$e = "$root\MQL5\Experts"
New-Item -ItemType Directory -Force -Path $e | Out-Null
Write-Host "3/4 fetching P7VT3.mq5 into $e"
Invoke-WebRequest 'https://raw.githubusercontent.com/juliantorrespr-afk/quant-lab-data/main/P7VT3.mq5' -OutFile "$e\P7VT3.mq5" -UseBasicParsing
Write-Host "4/4 launching MetaTrader 5 (portable mode)"
Start-Process "$root\terminal64.exe" -ArgumentList '/portable'
Write-Host "DONE - MT5 is starting. Next: File > Open an Account > MetaQuotes-Demo."
