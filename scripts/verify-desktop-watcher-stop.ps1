$d = Join-Path $env:LOCALAPPDATA 'Genesis\desktop'
$pass = 0; $fail = 0
function Check { param($n,$got,$want)
  if ("$got" -like "*$want*") { $script:pass++; "  PASS  $n" }
  else { $script:fail++; "  FAIL  $n`n          wanted ~ '$want'`n          got     '$got'" } }
function HelperCount {
  $n = 0
  foreach ($p in @(Get-Process powershell -ErrorAction SilentlyContinue)) {
    $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue).CommandLine
    if ($cl -like '*genesis-abort-watch*') { $n++ }
  }
  $n
}
"baseline helper procs: $(HelperCount)"
Enable-ScheduledTask -TaskName GenesisAbortWatch | Out-Null
Start-ScheduledTask -TaskName GenesisAbortWatch
Start-Sleep -Seconds 8
Check "watcher started" (HelperCount) "1"

"-Stop result: " + (& C:\scripts\genesis-abort-watch.ps1 -Stop)
Start-Sleep -Seconds 2
Check "watcher actually stopped" (HelperCount) "0"

# And prove -Uninstall does not leave one behind.
& C:\scripts\genesis-abort-watch.ps1 -Install | Out-Null
Enable-ScheduledTask -TaskName GenesisAbortWatch | Out-Null
Start-ScheduledTask -TaskName GenesisAbortWatch
Start-Sleep -Seconds 8
Check "watcher restarted" (HelperCount) "1"
& C:\scripts\genesis-abort-watch.ps1 -Uninstall | Out-Null
Start-Sleep -Seconds 2
Check "uninstall left nothing running" (HelperCount) "0"
""
"RESULT: $pass passed, $fail failed"
