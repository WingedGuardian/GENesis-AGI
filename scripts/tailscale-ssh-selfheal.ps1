<#
.SYNOPSIS
  Install a battery-safe boot self-heal task for Tailscale SSH access (Windows client).

.DESCRIPTION
  On a Windows client with a contended DNS stack (several DNS-managing VPNs, a large
  hosts-file ad/malware blocker, and/or manually pinned DNS servers), Tailscale's
  MagicDNS/NRPT write can intermittently lose a race for the Windows DNS configuration
  at boot. The result: for a window after login, peer tunnels stay cold and MagicDNS
  names do not resolve, so SSH to your Genesis fleet times out until Tailscale settles.

  This installs a scheduled task that, a short delay after each login, re-applies
  MagicDNS and pings every ONLINE tailnet peer to warm the tunnels — closing that
  window automatically. It derives peers live from `tailscale status`, so it hardcodes
  nothing about your tailnet. Pair it with IP-keyed ssh aliases (generate-ssh-config.sh)
  so your SSH never depends on MagicDNS in the first place.

  See docs/reference/tailscale-ssh-access.md for the full picture.

.PARAMETER DelaySeconds
  Seconds to wait after login before healing. Default 45 (lets the boot storm settle).

.PARAMETER TaskName
  Scheduled task name. Default 'GenesisTailscaleSelfHeal'. Each distinct sanitized name
  gets its own payload + log file (<TaskName>.ps1 / <TaskName>.log under %ProgramData%\
  GenesisNet), so separate installs do not clobber one another.

.PARAMETER Uninstall
  Remove the task and its generated files instead of installing.

.NOTES
  Windows-only. Run in an ELEVATED PowerShell (a SYSTEM task needs admin to add/remove).
  Requires Tailscale installed on this client. No-op-safe to re-run (idempotent).

.EXAMPLE
  .\tailscale-ssh-selfheal.ps1
.EXAMPLE
  .\tailscale-ssh-selfheal.ps1 -DelaySeconds 30
.EXAMPLE
  .\tailscale-ssh-selfheal.ps1 -Uninstall
#>
# behavioral-lint: ignore no-hide-problems
#   Justification: the generated headless payload sets $ErrorActionPreference =
#   'SilentlyContinue' ONLY so one transient peer error cannot abort warming the
#   rest - it hides nothing. That task runs as SYSTEM with no console, so it logs
#   EVERY outcome (successes and failures, with the real error text) to its log
#   file; the log is the surface. Existence checks here use -ErrorAction
#   SilentlyContinue by design (a not-yet-created task/file is a normal state, and
#   the elevation gate below guarantees a real permission failure can't reach them).
#   The installer itself fails loud ('Stop').
[CmdletBinding()]
param(
    [ValidateRange(0, 3600)]
    [int]$DelaySeconds = 45,
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('[A-Za-z0-9]')]  # must contain at least one path-safe char
    [string]$TaskName = 'GenesisTailscaleSelfHeal',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$dir = Join-Path $env:ProgramData 'GenesisNet'
# Per-task payload/log names, keyed on a sanitized TaskName, so installs with distinct
# names get distinct files. (Names differing only in sanitized characters collapse to
# the same slug and would share files - avoid near-identical custom names.)
$slug       = ($TaskName -replace '[^A-Za-z0-9_.-]', '_')
$scriptPath = Join-Path $dir "$slug.ps1"
$logPath    = Join-Path $dir "$slug.log"

# Elevation is required to register OR remove a SYSTEM task. Enforce it BEFORE either
# path, so uninstall can't silently no-op (access-denied swallowed) and then falsely
# report success while the task keeps running.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in an ELEVATED PowerShell (adding/removing a SYSTEM scheduled task needs admin)."
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    # Only this task's own files (not a shared path), so uninstalling one custom
    # task never breaks another. -ErrorAction SilentlyContinue now only absorbs the
    # expected not-found case (elevation is already guaranteed above).
    Remove-Item $scriptPath -Force -ErrorAction SilentlyContinue
    Remove-Item $logPath    -Force -ErrorAction SilentlyContinue
    Remove-Item $dir -Force -ErrorAction SilentlyContinue  # no -Recurse: removed only if empty
    Write-Host "Removed task '$TaskName' and its generated files ($slug.ps1 / $slug.log)."
    return
}

New-Item -ItemType Directory -Path $dir -Force | Out-Null

# The self-heal payload, run by the task as SYSTEM a short delay after each login.
# Single-quoted here-string: nothing below is expanded at install time. The payload
# derives its own log path from $PSCommandPath at run time, so it matches this task's
# per-slug script name without the installer injecting a path. Kept pure ASCII because
# it is written with -Encoding ascii below.
$selfHeal = @'
# (generated self-heal payload) warm Tailscale tunnels + re-apply MagicDNS after login.
#
# Runs headless as SYSTEM with no console, so the log IS the surface: every branch
# below records its outcome (success AND failure, with the real error text). We set
# SilentlyContinue only so a single transient peer error cannot abort warming the
# rest - NOT to swallow problems. Nothing is hidden; failures are logged explicitly.
$ErrorActionPreference = 'SilentlyContinue'
$log = [System.IO.Path]::ChangeExtension($PSCommandPath, 'log')
function Log($m) { Add-Content -Path $log -Value ((Get-Date -Format o) + '  ' + $m) }
# Cap the log so it can never grow without bound.
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 1MB) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
Log 'self-heal run'

$ts = (Get-Command tailscale.exe -ErrorAction SilentlyContinue).Source
if (-not $ts) { $ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe' }
if (-not (Test-Path $ts)) { Log "ABORT: tailscale.exe not found (looked at Get-Command and '$ts'). Is Tailscale installed?"; return }

# 1. Re-apply MagicDNS. Bounded in a job so a DNS-config lock cannot hang the task.
#    Capture the CLI EXIT CODE - a job reaching a terminal state is not success; the
#    daemon may not be ready in this exact post-login window and exit nonzero.
$j = Start-Job { param($t) $o = & $t set --accept-dns=true 2>&1; [pscustomobject]@{ Code = $LASTEXITCODE; Out = ($o | Out-String) } } -ArgumentList $ts
if (Wait-Job $j -Timeout 20) {
    $r = Receive-Job $j
    $o = ($r.Out).Trim()
    if ($r.Code -eq 0) {
        if ($o) { Log "accept-dns re-applied (output: $o)" } else { Log 'accept-dns re-applied' }
    } else {
        Log "accept-dns FAILED (exit $($r.Code)): $o"
    }
} else {
    Log 'accept-dns did NOT complete within 20s (DNS config likely locked by another process); left as-is'
    Stop-Job $j
}
Remove-Job $j -Force

# 2. Warm online peers. Read live from `tailscale status` (nothing hardcoded). Warm
#    with BOUNDED concurrency: Start-Job spawns a child powershell per peer, so an
#    unthrottled fan-out would storm logon on a large tailnet. Process in chunks so at
#    most $chunk run at once; each chunk is time-bounded, so total time stays sane and
#    a big/slow tailnet cannot exceed the task's execution limit.
$peers = @()
try {
    # Do NOT merge stderr (2>&1) into the JSON stream: a cosmetic tailscale warning
    # line would break ConvertFrom-Json and zero out warming. stdout is pure JSON.
    $status = & $ts status --json | ConvertFrom-Json
    $peers  = @($status.Peer.PSObject.Properties.Value | Where-Object { $_.Online })
    Log ("found {0} online peer(s) to warm" -f $peers.Count)
} catch {
    Log ("could not read peer list from 'tailscale status --json': " + $_.Exception.Message)
}
$targets = @()
foreach ($p in $peers) {
    $ip = ($p.TailscaleIPs | Where-Object { $_ -notmatch ':' } | Select-Object -First 1)
    if (-not $ip) { Log ("no IPv4 for peer {0}; skipped" -f $p.HostName); continue }
    $targets += [pscustomobject]@{ Name = $p.HostName; IP = $ip }
}
$chunk = 8
for ($i = 0; $i -lt $targets.Count; $i += $chunk) {
    $batch   = @($targets[$i..([math]::Min($i + $chunk - 1, $targets.Count - 1))])
    $running = @()
    foreach ($t in $batch) {
        $job = Start-Job { param($exe, $ip) & $exe ping --timeout 3s --c 1 $ip 2>&1 } -ArgumentList $ts, $t.IP
        if ($job) { $running += [pscustomobject]@{ Name = $t.Name; IP = $t.IP; Job = $job } }
        else      { Log ("warm {0} ({1}): could not start job" -f $t.Name, $t.IP) }
    }
    if ($running.Count -gt 0) {
        $null = Wait-Job -Job @($running.Job) -Timeout 20
        foreach ($e in $running) {
            if ($e.Job.State -eq 'Completed') {
                $res = (Receive-Job $e.Job | Select-Object -First 1)
                Log ("warm {0} ({1}): {2}" -f $e.Name, $e.IP, $res)
            } else {
                Log ("warm {0} ({1}): no response within 20s" -f $e.Name, $e.IP)
                Stop-Job $e.Job
            }
            Remove-Job $e.Job -Force
        }
    }
}
Log 'done'
'@
Set-Content -Path $scriptPath -Value $selfHeal -Encoding ascii

$action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
                -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT${DelaySeconds}S"
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
# Battery-safe: laptops default to NOT running scheduled tasks on battery, which
# silently leaves the task Queued and never fired. These settings override that.
# IgnoreNew: overlapping logons within the delay window don't run two racing copies.
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Installed '$TaskName' - runs $DelaySeconds s after each login (battery-safe)."
Write-Host "Self-heal script: $scriptPath"
Write-Host "Log:              $logPath"
Write-Host "Test now:         Start-ScheduledTask -TaskName '$TaskName'   (then read the log)"
Write-Host "Remove:           .\tailscale-ssh-selfheal.ps1 -Uninstall"
