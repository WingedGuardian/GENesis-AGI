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

if ($Uninstall) {
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

while ($true) {
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
