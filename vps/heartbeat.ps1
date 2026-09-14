# QUANT_LAB VPS heartbeat -> repo docs/vps.json. Runs every 15 minutes from Task Scheduler.
# Needs the GitHub token in C:\ql\token.txt (one line). The token never leaves this machine; the file never goes to the repo.
$ErrorActionPreference = "Continue"
$repo = "juliantorrespr-afk/quant-lab-data"; $path = "docs/vps.json"
$tok = (Get-Content "C:\ql\token.txt" -Raw).Trim()
$mt5 = "C:\Program Files\MetaTrader 5"
$hb = Get-ChildItem "$mt5\MQL5\Files\*heart*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$lines = @(); if ($hb) { $lines = @(Get-Content $hb.FullName -ErrorAction SilentlyContinue) }
$kv = [ordered]@{}
if ($lines.Count -gt 0) { foreach ($p in ($lines[0] -split ' ')) { $a = $p -split '=', 2; if ($a.Count -eq 2) { $kv[$a[0]] = $a[1] } } }
$sleeves = @(); if ($lines.Count -gt 1) { $sleeves = $lines[1..($lines.Count - 1)] }
$exp = Get-ChildItem "$mt5\MQL5\Logs\*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$explines = @(); if ($exp) { $explines = @(Get-Content $exp.FullName -Tail 12 -ErrorAction SilentlyContinue) }
$jr = Get-ChildItem "$mt5\logs\*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$jrlines = @(); if ($jr) { $jrlines = @(Get-Content $jr.FullName -Tail 8 -ErrorAction SilentlyContinue) }
$obj = [ordered]@{
  updated          = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm 'UTC'")
  terminal_running = [bool](Get-Process terminal64 -ErrorAction SilentlyContinue)
  heartbeat_file   = $(if ($hb) { $hb.Name } else { $null })
  heartbeat_age_s  = $(if ($hb) { [int]((Get-Date) - $hb.LastWriteTime).TotalSeconds } else { $null })
  heartbeat        = $kv
  sleeves          = $sleeves
  experts_tail     = $explines
  journal_tail     = $jrlines
}
$json = $obj | ConvertTo-Json -Depth 5
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
$hdr = @{ Authorization = "Bearer $tok"; Accept = "application/vnd.github+json"; "User-Agent" = "quantlab-vps" }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$sha = $null
try { $cur = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/contents/$path" -Headers $hdr; $sha = $cur.sha } catch {}
$body = @{ message = "vps heartbeat"; content = $b64 }; if ($sha) { $body.sha = $sha }
try {
  Invoke-RestMethod -Method Put -Uri "https://api.github.com/repos/$repo/contents/$path" -Headers $hdr -Body ($body | ConvertTo-Json) -ContentType "application/json" | Out-Null
  "$(Get-Date -Format u) pushed $path (equity $($kv['equity']))" | Add-Content "C:\ql\heartbeat.log"
} catch { "$(Get-Date -Format u) push failed: $($_.Exception.Message)" | Add-Content "C:\ql\heartbeat.log" }
