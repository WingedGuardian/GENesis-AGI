<#
.SYNOPSIS
  Perform ONE desktop action, then exit. Reads a request file, writes a result.

.DESCRIPTION
  GROUNDWORK. Nothing in Genesis calls this. It is the act leg of the
  desktop-takeover design and is inert until its approval gate exists.

  One action per invocation, by design. The container triggers a scheduled
  task; this runs, acts once, and exits. Nothing on the machine holds any
  authority between actions, so abort is the absence of a running process
  rather than a message something must choose to honour.

  Every check below refuses a case that would otherwise fail SILENTLY:
  session 0 sees an empty desktop rather than erroring, an elevated window
  returns an empty automation tree rather than access-denied, and SendInput
  returns 0 rather than raising. See docs/reference/windows-remote-execution.md

.PARAMETER Install    Register the scheduled task and exit.
.PARAMETER Uninstall  Remove the task and exit.
.PARAMETER StateDir   Where request.json / result.json / abort.flag live.
#>
param(
    [switch]$Install,
    [switch]$Uninstall,
    [string]$StateDir = "",
    [int]$WatcherStaleSeconds = 30,
    [int]$DriftTolerancePx = 4
)

$ErrorActionPreference = "Stop"
$TaskName = "GenesisAct"
if ([string]::IsNullOrWhiteSpace($StateDir)) {
    $StateDir = [System.IO.Path]::Combine($env:LOCALAPPDATA, "Genesis", "desktop")
}
$RequestPath = [System.IO.Path]::Combine($StateDir, "request.json")
$ResultPath  = [System.IO.Path]::Combine($StateDir, "result.json")
$FlagPath    = [System.IO.Path]::Combine($StateDir, "abort.flag")
$BeatPath    = [System.IO.Path]::Combine($StateDir, "watcher.beat")

if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }

if ($Uninstall) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Ignore
    if (-not $t) { "NOT_REGISTERED $TaskName"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "UNREGISTERED $TaskName"
    exit 0
}

if ($Install) {
    . (Join-Path $PSScriptRoot "genesis-win-common.ps1")
    Install-GenesisHiddenTask -TaskName $TaskName -ScriptPath $PSCommandPath
    "  state: $StateDir"
    exit 0
}

# ── act ─────────────────────────────────────────────────────────────────────
. (Join-Path $PSScriptRoot "genesis-win-common.ps1")

$result = [ordered]@{
    status        = "ERROR"
    reason        = ""
    action        = $null
    session_id    = $null
    intended      = $null
    landed        = $null
    drift_px      = $null
    target_seen   = $null
    sendinput_ret = $null
    at            = (Get-Date).ToUniversalTime().ToString("o")
}
function Write-Result {
    $result.at = (Get-Date).ToUniversalTime().ToString("o")
    $result | ConvertTo-Json -Depth 6 | Set-Content -Path $ResultPath -Encoding utf8
    "$($result.status) $($result.reason)"
}
function Deny { param([string]$Why) $result.status = "REFUSED"; $result.reason = $Why; Write-Result; exit 2 }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GenesisInput {
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
    [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT {
        public int dx; public int dy; public uint mouseData; public uint dwFlags;
        public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
        public ushort wVk; public ushort wScan; public uint dwFlags;
        public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Explicit)] public struct INPUTUNION {
        [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
    [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public INPUTUNION u; }

    [DllImport("user32.dll", SetLastError=true)]
    public static extern uint SendInput(uint n, INPUT[] pInputs, int cbSize);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
    [DllImport("user32.dll")] public static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr h);

    public const uint MOVE = 0x0001, ABSOLUTE = 0x8000, VIRTUALDESK = 0x4000;
    public const uint LDOWN = 0x0002, LUP = 0x0004, RDOWN = 0x0008, RUP = 0x0010;
    public const uint WHEEL = 0x0800, KEYUP = 0x0002, UNICODE = 0x0004;

    public static POINT Cursor() { POINT p; GetCursorPos(out p); return p; }

    static uint Send(INPUT[] inp) { return SendInput((uint)inp.Length, inp, Marshal.SizeOf(typeof(INPUT))); }

    public static uint MoveAbs(int x, int y) {
        int w = GetSystemMetrics(78), h = GetSystemMetrics(79);   // VIRTUALSCREEN
        int vx = GetSystemMetrics(76), vy = GetSystemMetrics(77);
        if (w <= 0) { w = GetSystemMetrics(0); h = GetSystemMetrics(1); vx = 0; vy = 0; }
        INPUT[] i = new INPUT[1];
        i[0].type = 0;
        i[0].u.mi.dx = (int)((x - vx) * 65535.0 / w);
        i[0].u.mi.dy = (int)((y - vy) * 65535.0 / h);
        i[0].u.mi.dwFlags = MOVE | ABSOLUTE | VIRTUALDESK;
        return Send(i);
    }
    public static uint MouseFlags(uint flags) {
        INPUT[] i = new INPUT[1]; i[0].type = 0; i[0].u.mi.dwFlags = flags; return Send(i);
    }
    public static uint Wheel(int delta) {
        INPUT[] i = new INPUT[1]; i[0].type = 0;
        i[0].u.mi.mouseData = (uint)delta; i[0].u.mi.dwFlags = WHEEL; return Send(i);
    }
    public static uint TypeChar(char c) {
        INPUT[] i = new INPUT[2];
        i[0].type = 1; i[0].u.ki.wScan = c; i[0].u.ki.dwFlags = UNICODE;
        i[1].type = 1; i[1].u.ki.wScan = c; i[1].u.ki.dwFlags = UNICODE | KEYUP;
        return Send(i);
    }
    public static uint TapVk(ushort vk) {
        INPUT[] i = new INPUT[2];
        i[0].type = 1; i[0].u.ki.wVk = vk;
        i[1].type = 1; i[1].u.ki.wVk = vk; i[1].u.ki.dwFlags = KEYUP;
        return Send(i);
    }
    // A desktop we cannot open is the secure desktop (UAC prompt, lock screen).
    public static bool OnReachableDesktop() {
        IntPtr h = OpenInputDesktop(0, false, 0x0100 /* DESKTOP_SWITCHDESKTOP */);
        if (h == IntPtr.Zero) return false;
        CloseDesktop(h); return true;
    }
}
'@ -ErrorAction Stop

$buttonsHeld = $false
try {
    # ── refusals, cheapest and most fundamental first ───────────────────────
    $refusal = Assert-GenesisInteractiveSession
    if ($refusal) { Deny $refusal }

    if (-not [GenesisInput]::OnReachableDesktop()) {
        Deny "secure desktop is active (UAC prompt or lock screen) - a user-mode process cannot reach it"
    }

    # The abort signal is ALWAYS written by the watcher, which knows nothing
    # about sessions and is better for it. Whether that signal MEANS anything
    # is decided here, where the session context lives: an Esc pressed before
    # this session was granted aborted nothing.
    #
    # This matters because Esc is one of the most-pressed keys on a keyboard.
    # A sticky global flag made any incidental Esc - closing a dialog, leaving
    # a menu - block Genesis permanently. MEASURED 2026-09-06: a verification
    # run had every action after the first refused, because the operator was
    # using his own computer at the time. During a session the property is
    # unchanged: Esc stops everything, instantly.
    if (-not (Test-Path $RequestPath)) { Deny "no request file at $RequestPath" }
    $req = Get-Content $RequestPath -Raw | ConvertFrom-Json
    $result.action     = $req.action
    $result.session_id = $req.session_id

    if (Test-Path $FlagPath) {
        $abortedAt = $null
        $flagText = (Get-Content $FlagPath -Raw).Trim()
        if ($flagText -match 'aborted_at=(\S+)') {
            try {
                $abortedAt = [datetime]::Parse(
                    $Matches[1],
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::RoundtripKind
                ).ToUniversalTime()
            } catch { $abortedAt = $null }
        }
        # A flag we cannot read the time of is treated as CURRENT. Fail closed:
        # an unparseable abort is still an abort.
        if ($null -eq $abortedAt) {
            Deny "abort signal present but its timestamp is unreadable - treating it as live and refusing"
        }
        $sessionStart = $null
        if ($req -and ($req.PSObject.Properties.Name -contains "session_started_at")) {
            try {
                $sessionStart = [datetime]::Parse(
                    [string]$req.session_started_at,
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::RoundtripKind
                ).ToUniversalTime()
            } catch { $sessionStart = $null }
        }
        # No session start declared means we cannot tell an old Esc from a new
        # one, so the signal stands. Fail closed again.
        if (($null -eq $sessionStart) -or ($abortedAt -ge $sessionStart)) {
            Deny "aborted at $($abortedAt.ToString('o')) - Esc was pressed during this session"
        }
    }

    # No live watcher means Esc does nothing and the operator has no abort.
    # Fail closed: an actuator with no working abort must not act.
    if (-not (Test-Path $BeatPath)) {
        Deny "abort watcher has never run - no abort available, refusing to act"
    }
    $beatAge = ((Get-Date).ToUniversalTime() - [datetime]::Parse((Get-Content $BeatPath -Raw).Trim()).ToUniversalTime()).TotalSeconds
    if ($beatAge -gt $WatcherStaleSeconds) {
        Deny ("abort watcher heartbeat is {0:N0}s stale (limit {1}s) - no working abort, refusing to act" -f $beatAge, $WatcherStaleSeconds)
    }


    if ($req.PSObject.Properties.Name -contains "expires_at") {
        # Parse with the INVARIANT culture and RoundtripKind. [datetime]::Parse
        # with no culture uses the machine's current culture, which rejects an
        # ISO-8601 round-trip string on some locales - and ConvertFrom-Json may
        # hand back either a string or an already-converted [datetime]
        # depending on the PowerShell version. Both are handled explicitly
        # because the failure is a thrown exception on the SAFETY check that
        # stops stale actions, and a safety check that throws is a safety check
        # that is not running.
        $rawExp = $req.expires_at
        try {
            $expUtc = if ($rawExp -is [datetime]) { $rawExp.ToUniversalTime() }
                      else {
                          [datetime]::Parse(
                              [string]$rawExp,
                              [Globalization.CultureInfo]::InvariantCulture,
                              [Globalization.DateTimeStyles]::RoundtripKind
                          ).ToUniversalTime()
                      }
        } catch {
            Deny "expires_at '$rawExp' is not a parseable timestamp - refusing rather than acting on an unbounded request"
        }
        if ((Get-Date).ToUniversalTime() -gt $expUtc) {
            Deny "request expired - a stale action aims at a screen that has moved"
        }
    }

    # ── resolve the target window, and refuse anything but the granted one ──
    # An absent or empty target is a REFUSAL, never a wildcard. The grant is
    # per-window by design, so "no window named" means "no grant" - and an
    # empty string here becomes the match pattern "**", quietly selecting
    # whichever window enumeration happened to return first. A permissive
    # default in the one field that defines the grant's scope is the wrong
    # direction; caught 2026-09-06 when an empty target matched an elevated
    # window and was refused for the wrong reason.
    if (($req.PSObject.Properties.Name -notcontains "target_window") -or
        [string]::IsNullOrWhiteSpace($req.target_window)) {
        Deny "no target_window in the request - the grant is per-window, so an unnamed target is not granted"
    }

    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $cond = [System.Windows.Automation.Condition]::TrueCondition
    $win = $null
    foreach ($c in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)) {
        $n = $null
        try { $n = $c.Current.Name } catch { continue }
        if ($n -and $n -like "*$($req.target_window)*") { $win = $c; break }
    }
    if (-not $win) { Deny "granted window '$($req.target_window)' is not open" }

    # Elevated windows return an EMPTY automation tree rather than an error, so
    # acting on one means clicking coordinates we cannot verify. Refuse.
    try {
        $wpid = $win.Current.ProcessId
        $wproc = Get-Process -Id $wpid -ErrorAction Stop
        if ([string]::IsNullOrEmpty($wproc.Path)) {
            Deny "target window belongs to an ELEVATED process - its automation tree is unreadable (UIPI), refusing"
        }
    } catch [System.ComponentModel.Win32Exception] {
        Deny "target window belongs to an ELEVATED process - access denied reading it, refusing"
    }

    $rect = $win.Current.BoundingRectangle
    if ($rect.Width -le 0 -or $rect.Height -le 0) { Deny "target window has a zero-size rect (minimised?)" }

    # ── work out where to act ───────────────────────────────────────────────
    $tx = $null; $ty = $null; $targetName = $null
    if ($req.PSObject.Properties.Name -contains "element" -and $req.element) {
        $byName = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty, $req.element.name)
        $el = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $byName)
        if (-not $el) { Deny "element '$($req.element.name)' not found in the granted window" }
        # Typing into a password field is refused HERE, at the device, whatever
        # the caller believed it was doing.
        if ($req.action -eq "type") {
            $isPw = $false
            try { $isPw = $el.Current.IsPassword } catch { $isPw = $false }
            if ($isPw) { Deny "target is a password field - refusing to type into it" }
        }
        $er = $el.Current.BoundingRectangle
        if ($er.Width -le 0 -or $er.Height -le 0) { Deny "target element has a zero-size rect (offscreen or blocked)" }
        $tx = [int]($er.X + $er.Width / 2); $ty = [int]($er.Y + $er.Height / 2)
        $targetName = $el.Current.Name
    }
    elseif ($req.PSObject.Properties.Name -contains "x") {
        $tx = [int]$req.x; $ty = [int]$req.y
    }

    # ── position, then VERIFY, then act ─────────────────────────────────────
    if ($null -ne $tx) {
        $result.intended = "$tx,$ty"
        $moved = [GenesisInput]::MoveAbs($tx, $ty)
        if ($moved -ne 1) { Deny "SendInput move returned $moved (0 = the OS refused the injection)" }
        Start-Sleep -Milliseconds 120

        $now = [GenesisInput]::Cursor()
        $result.landed = "$($now.X),$($now.Y)"
        $drift = [math]::Abs($now.X - $tx) + [math]::Abs($now.Y - $ty)
        $result.drift_px = $drift
        if ($drift -gt $DriftTolerancePx) {
            Deny "pointer landed at $($now.X),$($now.Y) but $tx,$ty was intended (drift ${drift}px) - coordinate space mismatch, refusing to act"
        }

        # What is ACTUALLY under the pointer? A computation cannot notice it is
        # wrong; this can.
        try {
            $under = [System.Windows.Automation.AutomationElement]::FromPoint(
                (New-Object System.Windows.Point($now.X, $now.Y)))
            $result.target_seen = $under.Current.Name
            if ($targetName -and $under.Current.Name -ne $targetName) {
                Deny "pointer is over '$($under.Current.Name)' but '$targetName' was the target - refusing"
            }
        } catch {
            Deny "could not identify what is under the pointer - refusing rather than acting blind"
        }
    }

    switch ($req.action) {
        "move"  { $result.sendinput_ret = 1 }
        "click" {
            $buttonsHeld = $true
            $result.sendinput_ret = [GenesisInput]::MouseFlags([GenesisInput]::LDOWN)
            Start-Sleep -Milliseconds 40
            [void][GenesisInput]::MouseFlags([GenesisInput]::LUP)
            $buttonsHeld = $false
        }
        "rightclick" {
            $buttonsHeld = $true
            $result.sendinput_ret = [GenesisInput]::MouseFlags([GenesisInput]::RDOWN)
            Start-Sleep -Milliseconds 40
            [void][GenesisInput]::MouseFlags([GenesisInput]::RUP)
            $buttonsHeld = $false
        }
        "scroll" { $result.sendinput_ret = [GenesisInput]::Wheel([int]$req.delta) }
        "type" {
            $sent = 0
            foreach ($ch in $req.text.ToCharArray()) {
                $sent += [GenesisInput]::TypeChar($ch)
                Start-Sleep -Milliseconds 12
            }
            $result.sendinput_ret = $sent
        }
        # [uint16], not [ushort]: the latter is a C# alias and is NOT a
        # PowerShell type accelerator, so it fails at RUNTIME with "Unable to
        # find type" rather than at parse time. Caught by an E2E run; a syntax
        # check will not find it.
        "key"   { $result.sendinput_ret = [GenesisInput]::TapVk([uint16]$req.vk) }
        default { Deny "unsupported action '$($req.action)'" }
    }

    if ($result.sendinput_ret -eq 0) {
        # SendInput reports the number of events injected. Zero is a refusal by
        # the OS, not an exception — a caller that ignores it sees success.
        $result.status = "ERROR"; $result.reason = "SendInput injected 0 events - the OS refused"
        Write-Result; exit 1
    }

    $result.status = "OK"
    Write-Result
}
catch {
    $result.status = "ERROR"; $result.reason = $_.Exception.Message
    Write-Result
    exit 1
}
finally {
    # An abort or a throw mid-gesture must never leave a button down: the next
    # pointer move would drag something unpredictably. Unconditional.
    if ($buttonsHeld) {
        try { [void][GenesisInput]::MouseFlags([GenesisInput]::LUP) } catch { }
        try { [void][GenesisInput]::MouseFlags([GenesisInput]::RUP) } catch { }
    }
}
