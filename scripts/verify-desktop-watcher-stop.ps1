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

# ── PID REUSE: -Stop must never force-kill a process it cannot prove is ours ──
# Windows reuses PIDs. A watcher.pid record that outlived its process names
# whatever now holds that number, and the old code force-terminated it. These
# three cases use a REAL decoy process, so a regression kills the decoy and the
# check fails loudly rather than passing on a technicality.
""
"-- PID-reuse safety --"
$decoy = Start-Process powershell -PassThru -WindowStyle Hidden `
    -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 120'
Start-Sleep -Seconds 1
$pidf = Join-Path $d 'watcher.pid'

# (1) Legacy record: a bare PID carries no identity, so it cannot be verified.
Set-Content -Path $pidf -Value "$($decoy.Id)" -Encoding utf8
$r1 = & C:\scripts\genesis-abort-watch.ps1 -Stop
Start-Sleep -Milliseconds 500
$alive1 = if (Get-Process -Id $decoy.Id -ErrorAction SilentlyContinue) { "alive" } else { "KILLED" }
Check "legacy bare-PID record does not kill an unrelated process" $alive1 "alive"
Check "legacy record says why it refused" $r1 "REFUSING"

# (2) Reused PID: right number, wrong start time. Must discard, not kill.
Set-Content -Path $pidf -Value "$($decoy.Id)|1" -Encoding utf8
$r2 = & C:\scripts\genesis-abort-watch.ps1 -Stop
Start-Sleep -Milliseconds 500
$alive2 = if (Get-Process -Id $decoy.Id -ErrorAction SilentlyContinue) { "alive" } else { "KILLED" }
Check "reused PID does not kill the process now holding it" $alive2 "alive"
Check "reused PID is reported as a different process" $r2 "DIFFERENT process"

Stop-Process -Id $decoy.Id -Force -ErrorAction SilentlyContinue

# (3) POSITIVE CONTROL — the bar that must FLIP. A genuine watcher, with a
# correct record, still gets force-killed when it ignores the stop file.
# Without this, all of the above would also pass if -Stop simply never killed
# anything, which is the failure mode that would make this whole section inert.
& C:\scripts\genesis-abort-watch.ps1 -Install | Out-Null
Enable-ScheduledTask -TaskName GenesisAbortWatch | Out-Null
Start-ScheduledTask -TaskName GenesisAbortWatch
Start-Sleep -Seconds 8
Check "control: a real watcher is running" (HelperCount) "1"
$r3 = & C:\scripts\genesis-abort-watch.ps1 -Stop
Start-Sleep -Seconds 2
Check "control: a verified watcher IS stopped" (HelperCount) "0"
Check "control: the kill reports the identity was verified" $r3 "identity verified"
& C:\scripts\genesis-abort-watch.ps1 -Uninstall | Out-Null

""
"RESULT: $pass passed, $fail failed"
