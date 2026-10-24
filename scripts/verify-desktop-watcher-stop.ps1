$d = Join-Path $env:LOCALAPPDATA 'Genesis\desktop'
$pass = 0; $fail = 0
function Check { param($n,$got,$want)
  if ("$got" -like "*$want*") { $script:pass++; "  PASS  $n" }
  else { $script:fail++; "  FAIL  $n`n          wanted ~ '$want'`n          got     '$got'" } }

# Counts get EXACT equality, never Check's substring match. "*0*" matches 10 and
# "*1*" matches 10, 11, 21 - so a run that leaked nine pollers would satisfy a
# "must be zero" assertion written with Check.
function CheckCount { param($n,$got,[int]$want)
  if (($got -is [int]) -and ($got -eq $want)) { $script:pass++; "  PASS  $n (=$got)" }
  else { $script:fail++; "  FAIL  $n`n          wanted exactly $want`n          got     '$got'" } }

function HelperCount {
  # Returns an int or THROWS. It must never silently under-count: a CIM failure
  # returns $null, $null never matches, and every "must be zero" check in this
  # file would then pass - which is precisely the direction that turns a leaked
  # poller into a green run.
  $n = 0
  foreach ($p in @(Get-Process powershell -ErrorAction SilentlyContinue)) {
    if ($p.Id -eq $PID) { continue }   # a probe that matches itself is a lie
    $cim = $null
    try { $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction Stop }
    catch {
      # Exiting between the enumeration and the query is normal, and a dead
      # process is not one of ours. A LIVE one we cannot read is a real failure.
      if (Get-Process -Id $p.Id -ErrorAction SilentlyContinue) {
        throw "HelperCount: cannot read the command line of live pid $($p.Id); refusing to report a count that may under-count. $($_.Exception.Message)"
      }
      continue
    }
    if ($cim.CommandLine -like '*genesis-abort-watch*') { $n++ }
  }
  $n
}
"baseline helper procs: $(HelperCount)"
# Self-install: this script must run on a clean machine, not only on one where
# a previous run happened to leave the task behind.
& C:\scripts\genesis-abort-watch.ps1 -Install | Out-Null
Enable-ScheduledTask -TaskName GenesisAbortWatch | Out-Null
Start-ScheduledTask -TaskName GenesisAbortWatch
Start-Sleep -Seconds 8
CheckCount "watcher started" (HelperCount) 1

"-Stop result: " + (& C:\scripts\genesis-abort-watch.ps1 -Stop)
Start-Sleep -Seconds 2
CheckCount "watcher actually stopped" (HelperCount) 0

# And prove -Uninstall does not leave one behind.
& C:\scripts\genesis-abort-watch.ps1 -Install | Out-Null
Enable-ScheduledTask -TaskName GenesisAbortWatch | Out-Null
Start-ScheduledTask -TaskName GenesisAbortWatch
Start-Sleep -Seconds 8
CheckCount "watcher restarted" (HelperCount) 1
& C:\scripts\genesis-abort-watch.ps1 -Uninstall | Out-Null
Start-Sleep -Seconds 2
CheckCount "uninstall left nothing running" (HelperCount) 0
# The task is not the only artifact install created. MEASURED 2026-09-07: the
# generated .vbs shim outlived every uninstall, so "leaves nothing behind" was
# true of the task and false of the file the task actually launched.
Check "uninstall removed the generated shim" `
    ([bool](Test-Path 'C:\scripts\genesis-abort-watch.hidden.vbs')) "False"

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

# (3) A REAL watcher, stopped. This covers the polite path end-to-end against a
# genuinely task-launched watcher in the interactive session.
& C:\scripts\genesis-abort-watch.ps1 -Install | Out-Null
Enable-ScheduledTask -TaskName GenesisAbortWatch | Out-Null
Start-ScheduledTask -TaskName GenesisAbortWatch
Start-Sleep -Seconds 8
CheckCount "control: a real watcher is running" (HelperCount) 1
$r3 = & C:\scripts\genesis-abort-watch.ps1 -Stop
Start-Sleep -Seconds 2
CheckCount "control: a verified watcher IS stopped" (HelperCount) 0
# A healthy watcher honours the stop file, so this path must NOT report a kill.
# MEASURED 2026-09-07 on hardware: it exits well inside the 5s polite window.
Check "a healthy watcher stops politely, with no kill" $r3 "stopped pid"
& C:\scripts\genesis-abort-watch.ps1 -Uninstall | Out-Null

# (4) POSITIVE CONTROL — the bar that must FLIP, and the only one that reaches
# the force-kill branch. A WEDGED watcher ignores the stop file, so -Stop must
# fall through to Stop-Process and say the identity was verified.
#
# Case (3) cannot stand in for this: a healthy watcher always exits politely,
# so the one branch that TERMINATES a process would otherwise ship with zero
# hardware coverage — which is exactly what the first run of this script
# revealed (it asserted "identity verified" against the polite path and failed).
# The stand-in is faithful because -Stop cannot tell it from the real thing:
# same "<pid>|<ticks>" record, alive, deaf to watcher.stop.
""
"-- force-kill path (wedged watcher) --"
$wedged = Start-Process powershell -PassThru -WindowStyle Hidden `
    -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 120'
Start-Sleep -Seconds 1
$wticks = (Get-Process -Id $wedged.Id).StartTime.Ticks
Set-Content -Path $pidf -Value "$($wedged.Id)|$wticks" -Encoding utf8
$r4 = & C:\scripts\genesis-abort-watch.ps1 -Stop
Start-Sleep -Milliseconds 500
$alive4 = if (Get-Process -Id $wedged.Id -ErrorAction SilentlyContinue) { "alive" } else { "KILLED" }
Check "control: a wedged watcher IS force-killed" $alive4 "KILLED"
Check "control: the kill reports the identity was verified" $r4 "identity verified"
Stop-Process -Id $wedged.Id -Force -ErrorAction SilentlyContinue

# ── (5) An EMPTY record must take the "unreadable" branch, not throw ─────────
# MEASURED 2026-09-07: it threw. Get-Content -Raw returns $null for an empty
# file - and a file holding only a UTF-8 BOM is empty by that measure - so
# .Trim() died before the branch written for exactly this case could run.
""
"-- degenerate records --"
Set-Content -Path $pidf -Value '' -Encoding utf8 -NoNewline
$r5 = try { & C:\scripts\genesis-abort-watch.ps1 -Stop } catch { "THREW: " + $_.Exception.Message }
Check "an empty pid record is discarded, not fatal" $r5 "unreadable"

Set-Content -Path $pidf -Value 'not-a-pid' -Encoding utf8
$r6 = try { & C:\scripts\genesis-abort-watch.ps1 -Stop } catch { "THREW: " + $_.Exception.Message }
Check "a garbage pid record is discarded, not fatal" $r6 "unreadable"

# ── (6) A REFUSED stop must not be followed by unregistering the task ────────
# Unregistering on top of a refusal leaves the poller running AND removes the
# task naming it, so nothing is left to find it by. This is the leak the stop
# ordering exists to prevent, in its permanent form.
""
"-- refusal must block uninstall --"
& C:\scripts\genesis-abort-watch.ps1 -Install | Out-Null
$orphan = Start-Process powershell -PassThru -WindowStyle Hidden `
    -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 120'
Start-Sleep -Seconds 1
Set-Content -Path $pidf -Value "$($orphan.Id)" -Encoding utf8   # legacy: unverifiable
$r7 = & C:\scripts\genesis-abort-watch.ps1 -Uninstall
$stillThere = [bool](Get-ScheduledTask -TaskName GenesisAbortWatch -ErrorAction SilentlyContinue)
Check "uninstall refuses while an unverifiable poller is live" $r7 "REFUSED to unregister"
Check "the task survives a refused uninstall" $stillThere "True"
Check "the unverifiable process is left alone" ([bool](Get-Process -Id $orphan.Id -ErrorAction SilentlyContinue)) "True"
Stop-Process -Id $orphan.Id -Force -ErrorAction SilentlyContinue
Remove-Item $pidf -Force -ErrorAction Ignore
& C:\scripts\genesis-abort-watch.ps1 -Uninstall | Out-Null

""
"RESULT: $pass passed, $fail failed"
# A harness that reports failures and then exits 0 is green to everything that
# reads exit codes. MEASURED 2026-09-07: the 12/13 run exited 0.
if ($fail -gt 0) { exit 1 }
exit 0
