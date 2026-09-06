<#
.SYNOPSIS
  Resident abort watcher: Esc halts any Genesis desktop action, any time.

.DESCRIPTION
  GROUNDWORK. Nothing in Genesis runs this yet.

  This is the ONE resident piece of the desktop-takeover design, and it is
  resident for a specific reason: actions run as task-per-action, so each is a
  fresh process, and Windows reports key state PER PROCESS. A brand-new
  process can see Esc held down right now, but cannot see that the operator
  tapped it two seconds ago between actions. Catching a tap needs something
  that was already watching.

  It can only OBSERVE. It has no ability to click, type, or move anything,
  which is why a resident watcher is an acceptable surface where a resident
  actuator would not be.

  It writes two files:
    abort.flag      - present means STOP. The actuator refuses while it exists.
    watcher.beat    - a heartbeat. The actuator refuses if this is STALE,
                      because a dead watcher means Esc does nothing and the
                      operator has no working abort. Fail closed: no watcher,
                      no actions.

.PARAMETER Install    Register as a logon task and exit.
.PARAMETER Uninstall  Remove the task and exit.
.PARAMETER Clear      Clear a set abort flag and exit (start of a new session).
.PARAMETER StateDir   Where the flag and heartbeat live.
#>
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Clear,
    [switch]$Stop,
    [string]$StateDir = "",
    [int]$PollMs = 40,
    [int]$BeatSeconds = 5
)

$ErrorActionPreference = "Stop"
$TaskName = "GenesisAbortWatch"
if ([string]::IsNullOrWhiteSpace($StateDir)) {
    $StateDir = [System.IO.Path]::Combine($env:LOCALAPPDATA, "Genesis", "desktop")
}
$FlagPath = [System.IO.Path]::Combine($StateDir, "abort.flag")
$BeatPath = [System.IO.Path]::Combine($StateDir, "watcher.beat")

if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }

if ($Clear) {
    if (Test-Path $FlagPath) { Remove-Item $FlagPath -Force; "CLEARED" } else { "NOT_SET" }
    exit 0
}

function Stop-GenesisWatcher {
    <#
      .SYNOPSIS
        Ask a running watcher to exit, then verify it did.
      .DESCRIPTION
        Stop-ScheduledTask cannot do this: the wscript launcher has already
        exited, so the task owns nothing. Signal politely via a stop file, then
        confirm by PID and kill if the loop is wedged. Verifying rather than
        assuming, because an orphaned poller is invisible in the task list.
    #>
    $stop = [System.IO.Path]::Combine($StateDir, "watcher.stop")
    $pidf = [System.IO.Path]::Combine($StateDir, "watcher.pid")
    if (-not (Test-Path $pidf)) { return "no watcher pid recorded" }
    $wpid = [int]((Get-Content $pidf -Raw).Trim())
    Set-Content -Path $stop -Value "stop" -Encoding utf8
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 250
        if (-not (Get-Process -Id $wpid -ErrorAction SilentlyContinue)) {
            Remove-Item $pidf -Force -ErrorAction Ignore
            return "stopped pid $wpid"
        }
    }
    Stop-Process -Id $wpid -Force -ErrorAction SilentlyContinue
    Remove-Item $pidf, $stop -Force -ErrorAction Ignore
    return "pid $wpid ignored the stop file and was killed"
}

if ($Stop) { Stop-GenesisWatcher; exit 0 }

if ($Uninstall) {
    # Stop FIRST. Unregistering a task does not kill a process it no longer
    # owns, so uninstalling without this silently leaves the poller running.
    Stop-GenesisWatcher
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Ignore
    if (-not $t) { "NOT_REGISTERED $TaskName"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "UNREGISTERED $TaskName"
    exit 0
}

if ($Install) {
    . (Join-Path $PSScriptRoot "genesis-win-common.ps1")
    Install-GenesisHiddenTask -TaskName $TaskName -ScriptPath $PSCommandPath -AtLogon
    "  state: $StateDir"
    "Start it now with: Start-ScheduledTask -TaskName $TaskName"
    exit 0
}

# ── watch ───────────────────────────────────────────────────────────────────
. (Join-Path $PSScriptRoot "genesis-win-common.ps1")

$refusal = Assert-GenesisInteractiveSession
if ($refusal) {
    # Not fatal to write it down: a watcher that cannot see the keyboard is
    # exactly what the actuator's staleness check needs to notice.
    "REFUSED $refusal"
    exit 3
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GenesisKeys {
    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);
}
'@ -ErrorAction Stop

$VK_ESCAPE = 0x1B
$lastBeat = [DateTime]::MinValue
$StopPath = [System.IO.Path]::Combine($StateDir, "watcher.stop")
Remove-Item $StopPath -Force -ErrorAction Ignore

# Own PID recorded so a stopper can fall back to killing us if the polite
# route is ignored (a wedged loop still needs to die).
Set-Content -Path ([System.IO.Path]::Combine($StateDir, "watcher.pid")) -Value $PID -Encoding utf8

while ($true) {
    # Under the wscript shim, Task Scheduler does NOT own this process: the
    # launcher exits immediately, the task reports finished, and
    # Stop-ScheduledTask therefore cannot stop us. Without a signal of our own
    # every install/start cycle leaves another poller behind - MEASURED, six of
    # them accumulated during one afternoon of testing, none visible as a
    # running task. This is the cost of the no-console shim, paid here.
    if (Test-Path $StopPath) {
        Remove-Item $StopPath -Force -ErrorAction Ignore
        Remove-Item ([System.IO.Path]::Combine($StateDir, "watcher.pid")) -Force -ErrorAction Ignore
        exit 0
    }

    # 0x8000 = down RIGHT NOW. 0x0001 = pressed since this process last asked.
    # Both are needed: a quick tap between polls is released before the next
    # poll sees it, so checking only the high bit would silently miss exactly
    # the gesture an operator makes to abort. The low bit is per-process, which
    # works here precisely because this watcher is resident and keeps asking.
    if (([GenesisKeys]::GetAsyncKeyState($VK_ESCAPE) -band 0x8001) -ne 0) {
        $stamp = (Get-Date).ToUniversalTime().ToString("o")
        Set-Content -Path $FlagPath -Value "aborted_at=$stamp" -Encoding utf8
    }

    $now = Get-Date
    if (($now - $lastBeat).TotalSeconds -ge $BeatSeconds) {
        Set-Content -Path $BeatPath -Value $now.ToUniversalTime().ToString("o") -Encoding utf8
        $lastBeat = $now
    }

    Start-Sleep -Milliseconds $PollMs
}
