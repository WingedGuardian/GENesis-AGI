# Full PR-1 verification, in ONE run to minimise desktop noise.
$ErrorActionPreference = "Stop"
$d    = Join-Path $env:LOCALAPPDATA 'Genesis\desktop'
$flag = Join-Path $d 'abort.flag'
$reqf = Join-Path $d 'request.json'
$resf = Join-Path $d 'result.json'
$pass = 0; $fail = 0
function Check { param($Name, $Got, $Want)
    if ($Got -like "*$Want*") { $script:pass++; "  PASS  $Name" }
    else { $script:fail++; "  FAIL  $Name`n          wanted ~ '$Want'`n          got     '$Got'" }
}
function New-Req { param($Action, $Window, $Extra)
    $h = @{ action=$Action; session_id='verify'; target_window=$Window
            session_started_at=$script:SessionStart
            expires_at=(Get-Date).ToUniversalTime().AddMinutes(2).ToString('o') }
    if ($Extra) { foreach ($k in $Extra.Keys) { $h[$k] = $Extra[$k] } }
    $h | ConvertTo-Json
}
$script:SessionStart = (Get-Date).ToUniversalTime().ToString('o')
function Act { Start-ScheduledTask -TaskName GenesisAct; Start-Sleep -Seconds 5
               Get-Content $resf -Raw | ConvertFrom-Json }

"== re-register watcher through the shared hidden-task path =="
& C:\scripts\genesis-abort-watch.ps1 -Uninstall | Out-Null
& C:\scripts\genesis-abort-watch.ps1 -Install
$wa = (Get-ScheduledTask -TaskName GenesisAbortWatch).Actions[0]
Check "watcher runs via wscript (no console)" $wa.Execute "wscript.exe"

# BOTH tasks must be enabled. A disabled task cannot be started, and
# Start-ScheduledTask does not complain about it - the previous run of this
# script forgot the watcher and then "proved" the abort loop was broken.
Enable-ScheduledTask -TaskName GenesisAct        | Out-Null
Enable-ScheduledTask -TaskName GenesisAbortWatch | Out-Null
Start-ScheduledTask -TaskName GenesisAbortWatch
Start-Sleep -Seconds 10
# Liveness is the HEARTBEAT, not the task State. Under the wscript shim the
# launcher exits immediately, so the task reads Ready while PowerShell runs on
# detached - which is exactly why the actuator checks the heartbeat instead.
$beatFile = Join-Path $d 'watcher.beat'
$beatAge = if (Test-Path $beatFile) {
    [int]((Get-Date).ToUniversalTime() - [datetime]::Parse((Get-Content $beatFile -Raw).Trim()).ToUniversalTime()).TotalSeconds
} else { 9999 }
Check "watcher is alive (heartbeat under 30s)" ($beatAge -lt 30) "True"

""
"== refusal suite =="
Remove-Item $flag -Force -ErrorAction Ignore

Set-Content $reqf (New-Req 'move' '' @{x=300;y=300}) -Encoding utf8
Check "empty target_window is refused, not a wildcard" (Act).reason "not granted"

Set-Content $reqf (New-Req 'move' 'ThisWindowDoesNotExist9x' @{x=300;y=300}) -Encoding utf8
Check "unknown window refused" (Act).reason "is not open"

Set-Content $reqf (New-Req 'move' 'Task Manager' @{x=300;y=300}) -Encoding utf8
Check "elevated window refused" (Act).reason "ELEVATED"

$expired = @{ action='move'; session_id='verify'; target_window='Notepad'; x=300; y=300
              expires_at=(Get-Date).ToUniversalTime().AddMinutes(-1).ToString('o') } | ConvertTo-Json
Set-Content $reqf $expired -Encoding utf8
Check "expired request refused" (Act).reason "expired"

""
"== a real action, with landing verified =="
Set-Content $reqf (New-Req 'move' 'Notepad' @{x=640;y=480}) -Encoding utf8
$r = Act
Check "move succeeded" $r.status "OK"
# Drift of a pixel or two is inherent to the 65535-space normalisation; the
# assertion is the TOLERANCE, not zero.
# NOT ($r.drift_px -le 4): a null drift from an errored action satisfies that,
# so the check passed while the action failed. Require a real number.
Check "landed within tolerance" (($null -ne $r.drift_px) -and ([int]$r.drift_px -le 4)) "True"
"        intended=$($r.intended) landed=$($r.landed) under=$($r.target_seen)"

""
"== an Esc from BEFORE the session must not block it =="
Set-Content $flag "aborted_at=$((Get-Date).ToUniversalTime().AddMinutes(-10).ToString('o'))" -Encoding utf8
Set-Content $reqf (New-Req 'move' 'Notepad' @{x=650;y=490}) -Encoding utf8
$stale = Act
Check "stale Esc ignored" $stale.status "OK"
if ($stale.status -ne 'OK') { "        reason: $($stale.reason)"; "        flag was: $((Get-Content $flag -Raw).Trim())" }
Remove-Item $flag -Force -ErrorAction Ignore

""
"== abort loop: inject Esc, watcher catches it, next action blocked =="
Set-Content $reqf (New-Req 'key' 'Notepad' @{vk=27}) -Encoding utf8
Check "Esc injected" (Act).status "OK"
Start-Sleep -Seconds 2
Check "watcher wrote the abort flag" (Test-Path $flag) "True"
Set-Content $reqf (New-Req 'move' 'Notepad' @{x=500;y=400}) -Encoding utf8
Check "next action blocked by abort" (Act).reason "Esc was pressed during this session"

""
"== leave the machine quiet =="
Remove-Item $flag -Force -ErrorAction Ignore
Stop-ScheduledTask -TaskName GenesisAbortWatch -ErrorAction Ignore
Disable-ScheduledTask -TaskName GenesisAct | Out-Null
Disable-ScheduledTask -TaskName GenesisAbortWatch | Out-Null
"  all Genesis tasks disabled again"
""
"RESULT: $pass passed, $fail failed"
