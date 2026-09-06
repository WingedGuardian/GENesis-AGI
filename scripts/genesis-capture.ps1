<#
.SYNOPSIS
  Screen/window capture helper for a Windows machine Genesis can reach.

.DESCRIPTION
  GROUNDWORK. This script is deliberately INERT with respect to Genesis: no
  Genesis code calls it, and nothing registers it automatically. It exists so
  the capability, once approved, deploys from source instead of being
  hand-configured onto one machine.

  Why a scheduled task and not a plain SSH command: an SSH login on Windows
  lands in SessionId 0, while the desktop runs in SessionId 1. Session 0 has
  its own window station with no desktop, so a capture there fails — and
  fails SILENTLY, leaving a valid all-black PNG behind. Full background and
  the measurements: docs/reference/windows-remote-execution.md

.PARAMETER Install
  Register the scheduled task (Interactive logon) and exit.

.PARAMETER Uninstall
  Remove the scheduled task and exit. Captured files are left alone.

.PARAMETER Mode
  screen (whole virtual desktop) or window (one titled window).

.PARAMETER Match
  Window title substring. Required for -Mode window.

.PARAMETER Keep
  How many captures to retain. Older ones are pruned after a successful run.

.PARAMETER OutDir
  Capture directory. Defaults to <UserProfile>\Pictures\GenesisCaptures —
  deliberately NOT the user's own Screenshots folder, so Genesis output never
  mixes with theirs.

.EXAMPLE
  .\genesis-capture.ps1 -Install
  .\genesis-capture.ps1 -Mode window -Match "Notepad"
  .\genesis-capture.ps1 -Uninstall
#>
param(
    [switch]$Install,
    [switch]$Uninstall,
    [ValidateSet("screen", "window")]
    [string]$Mode  = "screen",
    [string]$Match = "",
    [int]$Keep     = 40,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "GenesisCapture"

if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = [System.IO.Path]::Combine($env:USERPROFILE, "Pictures", "GenesisCaptures")
}

# ── install / uninstall ─────────────────────────────────────────────────────
if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction Ignore
    if (-not $existing) { "NOT_REGISTERED $TaskName"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "UNREGISTERED $TaskName (captures in $OutDir were left in place)"
    exit 0
}

if ($Install) {
    # The token's SID, not "$env:USERDOMAIN\$env:USERNAME": on a workgroup
    # machine USERDOMAIN reads "WORKGROUP" while the real principal is
    # "<COMPUTER>\<user>", and the mismatch fails registration with
    # "No mapping between account names and security IDs was done."
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()

    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"{0}`"" -f $PSCommandPath)

    # Interactive, NOT ServiceAccount/SYSTEM. A service principal runs in
    # session 0 and cannot see the desktop at all.
    $principal = New-ScheduledTaskPrincipal -UserId $id.User.Value `
        -LogonType Interactive -RunLevel Limited

    # The battery flags are not optional on a laptop: without them the task
    # sits in Queued and never runs. Zero time limit so a long body is not
    # killed at the default cap.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Principal $principal -Settings $settings -Force | Out-Null

    $t = Get-ScheduledTask -TaskName $TaskName
    "REGISTERED name=$($t.TaskName) logon=$($t.Principal.LogonType) out=$OutDir"
    "Trigger it with: Start-ScheduledTask -TaskName $TaskName"
    exit 0
}

# ── capture ─────────────────────────────────────────────────────────────────
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$png   = [System.IO.Path]::Combine($OutDir, "cap-$stamp.png")
$meta  = [System.IO.Path]::Combine($OutDir, "cap-$stamp.txt")

function Write-Meta { param([string]$Line) Add-Content -Path $meta -Value $Line }

try {
    Add-Type -AssemblyName System.Drawing, System.Windows.Forms, UIAutomationClient, UIAutomationTypes

    # DPI AWARENESS MUST COME BEFORE ANY MEASUREMENT.
    #
    # Windows lies to a DPI-unaware process: on a 1920x1080 display at 125%
    # scaling it reports 1536x864 (1920 * 96/120), and every subsequent
    # measurement — VirtualScreen, UI Automation BoundingRectangle, the bitmap
    # we allocate — comes back in that virtualised space. The capture then
    # SUCCEEDS and produces a downscaled image while reporting the scaled size
    # as though it were the real one.
    #
    # That is not cosmetic. SendInput's absolute mode addresses the PHYSICAL
    # desktop, so a coordinate read from a virtualised capture or UIA tree is
    # off by the scale factor — up to ~125px at the screen edge on a 125%
    # display. MEASURED 2026-09-06: before/after SetProcessDPIAware on the same
    # machine, GetSystemMetrics went 1536x864 -> 1920x1080 at monitor DPI 120.
    Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
public static class GenesisDpi {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@ -ErrorAction Stop
    $dpiAware = [GenesisDpi]::SetProcessDPIAware()

    $sid = (Get-Process -Id $PID).SessionId
    Write-Meta "session_id=$sid"
    Write-Meta "dpi_aware=$dpiAware"
    Write-Meta "interactive=$([System.Environment]::UserInteractive)"
    Write-Meta "mode=$Mode"

    # Refuse rather than producing the all-black PNG that session 0 yields.
    if ($sid -eq 0) {
        Write-Meta "status=REFUSED"
        Write-Meta "error=session 0 cannot see the interactive desktop; run via the scheduled task"
        "REFUSED session-0"
        exit 3
    }

    if ($Mode -eq "window") {
        if ([string]::IsNullOrWhiteSpace($Match)) { throw "-Mode window requires -Match" }
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $cond = [System.Windows.Automation.Condition]::TrueCondition
        $win  = $null
        $unreadable = 0
        foreach ($child in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)) {
            $name = $null
            try { $name = $child.Current.Name }
            catch { $unreadable++ }   # closed mid-enumeration; counted, not hidden
            if ($name -and $name -like "*$Match*") { $win = $child; break }
        }
        if ($unreadable -gt 0) { Write-Meta "windows_unreadable=$unreadable" }
        if (-not $win) { throw "no window title matched '$Match'" }

        $r = $win.Current.BoundingRectangle
        # A zero-size rect means minimised, or an elevated window UIPI will not
        # let us read. Either way it cannot be targeted; say so.
        if ($r.Width -le 0 -or $r.Height -le 0) {
            throw "target window has a zero-size rect (minimised, or elevated and blocked by UIPI)"
        }
        $x = [int]$r.X; $y = [int]$r.Y; $w = [int]$r.Width; $h = [int]$r.Height
        Write-Meta "window_title=$($win.Current.Name)"
    }
    else {
        $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $x = $vs.X; $y = $vs.Y; $w = $vs.Width; $h = $vs.Height
        Write-Meta "monitors=$(([System.Windows.Forms.Screen]::AllScreens).Count)"
    }

    Write-Meta "rect=${x},${y},${w}x${h}"
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $gfx = [System.Drawing.Graphics]::FromImage($bmp)
    $gfx.CopyFromScreen($x, $y, 0, 0, $bmp.Size)

    # A saved file is NOT evidence of a capture — a dead capture writes a
    # perfectly valid all-black PNG. Sample a grid and count distinct colours.
    $seen = @{}
    for ($px = 0; $px -lt $w -and $seen.Count -lt 12; $px += 89) {
        for ($py = 0; $py -lt $h -and $seen.Count -lt 12; $py += 89) {
            $seen[$bmp.GetPixel($px, $py).ToArgb()] = 1
        }
    }
    Write-Meta "distinct_colors=$($seen.Count)"

    $bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
    $gfx.Dispose(); $bmp.Dispose()

    if ($seen.Count -le 1) {
        Write-Meta "status=BLANK"
        "BLANK $png"
        exit 4
    }

    Write-Meta "status=OK"
    Write-Meta "bytes=$((Get-Item $png).Length)"
    "OK $png"

    # Retention: this directory grows without bound otherwise.
    $stale = Get-ChildItem -Path $OutDir -Filter "cap-*" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip ($Keep * 2)
    $failed = 0
    foreach ($f in $stale) {
        try { Remove-Item $f.FullName -Force } catch { $failed++ }
    }
    # A prune that cannot delete is a slow disk leak. Report it rather than
    # swallowing it; it does not fail the capture.
    if ($failed -gt 0) { Write-Meta "retention_failed=$failed of $($stale.Count)" }
}
catch {
    Write-Meta "status=ERROR"
    Write-Meta "error=$($_.Exception.Message)"
    "ERROR $($_.Exception.Message)"
    exit 1
}
