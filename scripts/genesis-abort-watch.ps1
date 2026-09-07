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
        confirm by PID AND START TIME and kill if the loop is wedged. Verifying
        rather than assuming, because an orphaned poller is invisible in the
        task list.

        The identity check is the load-bearing part. Windows reuses PIDs, so a
        recorded PID that outlived its process names whatever now holds that
        number; force-terminating on the PID alone would kill an unrelated
        process. Anything that cannot be proven to be this watcher is left
        ALONE and the stale record discarded — a wedged watcher merely goes
        stale, and genesis-act.ps1 already refuses to act on a stale heartbeat,
        whereas killing the wrong process is unrecoverable.
    #>
    $stop = [System.IO.Path]::Combine($StateDir, "watcher.stop")
    $pidf = [System.IO.Path]::Combine($StateDir, "watcher.pid")
    if (-not (Test-Path $pidf)) { return "no watcher pid recorded" }

    # "<pid>|<start-time ticks>". The ticks are what make this safe: Windows
    # reuses PIDs, so a record that outlived its process names whatever now
    # holds that number. Killing on the PID alone would terminate an unrelated
    # process - the operator's editor, a build, anything.
    $raw   = (Get-Content $pidf -Raw).Trim()
    $parts = $raw -split '\|'
    $wpid  = 0
    if (-not [int]::TryParse($parts[0], [ref]$wpid) -or $wpid -le 0) {
        Remove-Item $pidf -Force -ErrorAction Ignore
        return "watcher pid record was unreadable ('$raw') - discarded, nothing killed"
    }
    $ticks = $null
    if ($parts.Count -ge 2) {
        $t = [long]0
        if ([long]::TryParse($parts[1], [ref]$t)) { $ticks = $t }
    }

    Set-Content -Path $stop -Value "stop" -Encoding utf8
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 250
        if (-not (Get-Process -Id $wpid -ErrorAction SilentlyContinue)) {
            Remove-Item $pidf -Force -ErrorAction Ignore
            return "stopped pid $wpid"
        }
    }

    # The polite route was ignored. Before force-terminating, PROVE the process
    # holding this PID is still the watcher we recorded.
    $proc = Get-Process -Id $wpid -ErrorAction SilentlyContinue
    if (-not $proc) {
        Remove-Item $pidf, $stop -Force -ErrorAction Ignore
        return "pid $wpid is gone - nothing to kill"
    }
    if ($null -eq $ticks) {
        # A legacy record (bare PID) carries no identity, so this PID cannot be
        # proven to be ours. Refusing is the safe failure: a wedged watcher goes
        # stale and genesis-act.ps1 already refuses to act on a stale heartbeat,
        # whereas killing the wrong process is unrecoverable. Restarting the
        # watcher rewrites the record in the verifiable format.
        Remove-Item $stop -Force -ErrorAction Ignore
        return "pid $wpid has a legacy record with no start time - REFUSING to force-kill an unverifiable process; restart the watcher to re-record it"
    }
    if ($proc.StartTime.Ticks -ne $ticks) {
        # Same number, different process: the watcher died and Windows handed
        # its PID to someone else. Discard the stale record; kill nothing.
        Remove-Item $pidf, $stop -Force -ErrorAction Ignore
        return "pid $wpid now belongs to a DIFFERENT process (started $($proc.StartTime.ToString('o')), recorded $([DateTime]::new($ticks).ToString('o'))) - stale record discarded, nothing killed"
    }

    Stop-Process -Id $wpid -Force -ErrorAction SilentlyContinue
    Remove-Item $pidf, $stop -Force -ErrorAction Ignore
    return "pid $wpid ignored the stop file and was killed (identity verified)"
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

# Own IDENTITY recorded so a stopper can fall back to killing us if the polite
# route is ignored (a wedged loop still needs to die).
#
# PID AND START TIME, not the PID alone. Windows reuses PIDs, so a bare PID that
# outlives its process names whatever now holds that number — and -Stop would
# force-terminate an unrelated process. A PID is only unique WHILE its process
# lives; (PID, start time) is unique for all time, because a reused PID
# necessarily starts later. Written as one line, "<pid>|<ticks>", so a partial
# write cannot look like a valid record.
# A StartTime read can fail (rare, but it is a privileged property). Degrade to
# a bare PID rather than refusing to start: -Stop then declines to force-kill an
# unverifiable process, which is the safe direction, and the watcher still runs.
$identity = "$PID"
try   { $identity = "$PID|$((Get-Process -Id $PID -ErrorAction Stop).StartTime.Ticks)" }
catch { $identity = "$PID" }
Set-Content -Path ([System.IO.Path]::Combine($StateDir, "watcher.pid")) -Value $identity -Encoding utf8

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
