<#
.SYNOPSIS
  Shared preamble for Genesis's Windows-side helpers. Dot-source it; do not
  run it directly.

.DESCRIPTION
  Exists so DPI awareness and the session check live in ONE place. Both are
  preconditions that fail SILENTLY when omitted - a DPI-unaware process is
  told the screen is smaller than it is, and a session-0 process sees an empty
  desktop rather than an error. Two copies of a silent precondition is two
  chances to omit one and never find out.

  Background and measurements: docs/reference/windows-remote-execution.md
#>

Set-StrictMode -Version Latest

# ── DPI ──────────────────────────────────────────────────────────────────────
# MUST run before ANY measurement. On a 1920x1080 display at 125% scaling,
# Windows reports 1536x864 to a DPI-unaware process, and every subsequent
# reading — screen bounds, UI Automation BoundingRectangle, the bitmap we
# allocate — comes back in that virtualised space. SendInput's absolute mode
# addresses the PHYSICAL desktop, so a coordinate measured before this call is
# wrong by the scale factor with nothing to indicate it.
# MEASURED 2026-09-06: 1536x864 -> 1920x1080 across this single call, DPI 120.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GenesisWin {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
}
'@ -ErrorAction Stop

$script:GenesisDpiAware = [GenesisWin]::SetProcessDPIAware()

# ── console ──────────────────────────────────────────────────────────────────
# Task Scheduler launches powershell.exe WITH A CONSOLE regardless of
# -WindowStyle Hidden, so every helper run pops a black window onto the
# operator's desktop. For a one-shot capture that is a flash; for an actuator
# invoked once per action it is a window every few seconds, and for a resident
# watcher it is a window that never goes away.
#
# This lives in the shared preamble deliberately. It was first added to the
# watcher alone, which left the actuator flashing a console on every single
# action — the same "fixed it in one of the two places" mistake DPI awareness
# is here to prevent. One copy, inherited by everything that dot-sources this.
$script:GenesisConsole = [GenesisWin]::GetConsoleWindow()
if ($script:GenesisConsole -ne [IntPtr]::Zero) {
    [void][GenesisWin]::ShowWindow($script:GenesisConsole, 0)   # SW_HIDE
}

function Get-GenesisSessionInfo {
    <#
      .SYNOPSIS
        Session and desktop reachability, as facts rather than assumptions.
    #>
    [CmdletBinding()]
    param()
    [pscustomobject]@{
        SessionId   = (Get-Process -Id $PID).SessionId
        Interactive = [System.Environment]::UserInteractive
        DpiAware    = $script:GenesisDpiAware
        ScreenW     = [GenesisWin]::GetSystemMetrics(0)
        ScreenH     = [GenesisWin]::GetSystemMetrics(1)
    }
}

function Assert-GenesisInteractiveSession {
    <#
      .SYNOPSIS
        Refuse, loudly, when we cannot reach the desktop.
      .DESCRIPTION
        An SSH login lands in SessionId 0, whose window station has no desktop.
        Capture there yields a valid all-black PNG; UI Automation enumerates
        zero windows; SendInput returns 0. Every one of those reads as an
        ordinary empty result, so this must be an explicit refusal rather than
        something the caller is trusted to notice.
      .OUTPUTS
        $null when reachable; a refusal reason string when not.
    #>
    [CmdletBinding()]
    param()
    $info = Get-GenesisSessionInfo
    if ($info.SessionId -eq 0) {
        return "session 0 has no desktop - run via a scheduled task with an Interactive logon type"
    }
    if (-not $info.Interactive) {
        return "process is not interactive - cannot reach the desktop"
    }
    return $null
}


# ── task registration ────────────────────────────────────────────────────────

function Install-GenesisHiddenTask {
    <#
      .SYNOPSIS
        Register a scheduled task that runs a script in the interactive session
        WITHOUT ever showing a console window.

      .DESCRIPTION
        Two things every Genesis helper task needs, in one place so they cannot
        diverge:

        1. LogonType Interactive. A ServiceAccount/SYSTEM principal runs in
           session 0, where it cannot see or touch the desktop at all.

        2. No console. Task Scheduler launches powershell.exe WITH a console
           and `-WindowStyle Hidden` does not suppress it. Hiding it from
           inside the script is too late — the window is already up while
           Add-Type compiles. So the action runs `wscript.exe` against a tiny
           generated shim, whose Run(..., 0, False) starts PowerShell hidden
           from the outset. No window is ever created.

        The shim is GENERATED here rather than shipped, so there is one file
        per helper in the repo and nothing to keep in sync.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$TaskName,
        [Parameter(Mandatory)][string]$ScriptPath,
        [switch]$AtLogon
    )

    $shimPath = [System.IO.Path]::ChangeExtension($ScriptPath, ".hidden.vbs")
    # Run(cmd, 0, False): 0 = hidden window, False = do not wait.
    $shim = @"
' Generated by Install-GenesisHiddenTask. Do not edit; it is rewritten on
' every install. Exists so Task Scheduler never creates a console window.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""$ScriptPath""", 0, False
"@
    Set-Content -Path $shimPath -Value $shim -Encoding ASCII

    $id  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $act = New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument ('//nologo "{0}"' -f $shimPath)
    $pri = New-ScheduledTaskPrincipal -UserId $id.User.Value `
        -LogonType Interactive -RunLevel Limited
    # Battery flags or a laptop task sits in Queued and never runs. Zero time
    # limit or a long-lived helper is killed at the default cap.
    $setArgs = @{
        AllowStartIfOnBatteries    = $true
        DontStopIfGoingOnBatteries = $true
        StartWhenAvailable         = $true
        ExecutionTimeLimit         = [TimeSpan]::Zero
        MultipleInstances          = "IgnoreNew"
    }
    $set = New-ScheduledTaskSettingsSet @setArgs

    $reg = @{
        TaskName = $TaskName; Action = $act; Principal = $pri
        Settings = $set; Force = $true
    }
    if ($AtLogon) { $reg["Trigger"] = New-ScheduledTaskTrigger -AtLogOn }
    Register-ScheduledTask @reg | Out-Null

    $t = Get-ScheduledTask -TaskName $TaskName
    "REGISTERED $($t.TaskName) logon=$($t.Principal.LogonType) host=wscript (no console)"
}
