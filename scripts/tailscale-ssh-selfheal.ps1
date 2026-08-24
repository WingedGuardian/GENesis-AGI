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
  Scheduled task name. Default 'GenesisTailscaleSelfHeal'.

.PARAMETER Uninstall
  Remove the task and its generated files instead of installing.

.NOTES
  Windows-only. Run in an ELEVATED PowerShell (registering a SYSTEM task needs admin).
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
#   rest — it hides nothing. That task runs as SYSTEM with no console, so it logs
#   EVERY outcome (successes and failures, with the real error text) to
#   ts-selfheal.log; the log is the surface. Existence checks here use
#   -ErrorAction SilentlyContinue by design (a not-yet-created task/file/log is a
#   normal state, reported clearly). The installer itself fails loud ('Stop').
[CmdletBinding()]
param(
    [ValidateRange(0, 3600)]
    [int]$DelaySeconds = 45,
    [string]$TaskName = 'GenesisTailscaleSelfHeal',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$dir        = Join-Path $env:ProgramData 'GenesisNet'
$scriptPath = Join-Path $dir 'ts-selfheal.ps1'

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $scriptPath -Force -ErrorAction SilentlyContinue
    # Also remove the generated log and (if now empty) the GenesisNet directory,
    # so uninstall genuinely removes "its generated files" as documented.
    Remove-Item (Join-Path $dir 'ts-selfheal.log') -Force -ErrorAction SilentlyContinue
    Remove-Item $dir -Force -ErrorAction SilentlyContinue  # no -Recurse: only removes it if empty
    Write-Host "Removed task '$TaskName' and its generated files under $dir."
    return
}

# Elevation is required to register a task that runs as SYSTEM.
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in an ELEVATED PowerShell (needed to register a SYSTEM scheduled task)."
}

New-Item -ItemType Directory -Path $dir -Force | Out-Null

# The self-heal payload, run by the task as SYSTEM a short delay after each login.
# Single-quoted here-string: nothing below is expanded at install time.
$selfHeal = @'
# ts-selfheal.ps1 (generated) — warm Tailscale tunnels + re-apply MagicDNS after login.
#
# Runs headless as SYSTEM with no console, so the log IS the surface: every branch
# below records its outcome (success AND failure, with the real error text). We set
# SilentlyContinue only so a single transient peer error cannot abort warming the
# rest — NOT to swallow problems. Nothing is hidden; failures are logged explicitly.
$ErrorActionPreference = 'SilentlyContinue'
$dir = Join-Path $env:ProgramData 'GenesisNet'
$log = Join-Path $dir 'ts-selfheal.log'
function Log($m) { Add-Content -Path $log -Value ((Get-Date -Format o) + '  ' + $m) }
# Cap the log so it can never grow without bound.
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 1MB) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
Log 'self-heal run'

$ts = (Get-Command tailscale.exe -ErrorAction SilentlyContinue).Source
if (-not $ts) { $ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe' }
if (-not (Test-Path $ts)) { Log "ABORT: tailscale.exe not found (looked at Get-Command and '$ts'). Is Tailscale installed?"; return }

# 1. Re-apply MagicDNS. Bounded in a job so a DNS-config lock cannot hang the task.
$j = Start-Job { param($t) & $t set --accept-dns=true 2>&1 } -ArgumentList $ts
if (Wait-Job $j -Timeout 20) {
    $out = (Receive-Job $j | Out-String).Trim()
    if ($out) { Log "accept-dns re-applied (output: $out)" } else { Log 'accept-dns re-applied' }
} else {
    Log 'accept-dns did NOT complete within 20s (DNS config likely locked by another process); left as-is'
    Stop-Job $j
}
Remove-Job $j -Force

# 2. Warm every online peer so IP-keyed aliases connect immediately. Peers are
#    read live from `tailscale status`, so nothing about the tailnet is hardcoded.
$peers = @()
try {
    # Do NOT merge stderr (2>&1) into the JSON stream: a cosmetic tailscale
    # warning line would break ConvertFrom-Json and zero out warming. stdout is
    # pure JSON; stderr goes to the discarded error stream.
    $status = & $ts status --json | ConvertFrom-Json
    $peers  = @($status.Peer.PSObject.Properties.Value | Where-Object { $_.Online })
    Log ("found {0} online peer(s) to warm" -f $peers.Count)
} catch {
    Log ("could not read peer list from 'tailscale status --json': " + $_.Exception.Message)
}
foreach ($p in $peers) {
    $ip = ($p.TailscaleIPs | Where-Object { $_ -notmatch ':' } | Select-Object -First 1)
    if (-not $ip) { Log ("no IPv4 for peer {0}; skipped" -f $p.HostName); continue }
    $k = Start-Job { param($t, $i) & $t ping --timeout 3s --c 1 $i 2>&1 } -ArgumentList $ts, $ip
    if (Wait-Job $k -Timeout 8) {
        $r = (Receive-Job $k | Select-Object -First 1)
        Log ("warm {0} ({1}): {2}" -f $p.HostName, $ip, $r)
    } else {
        Log ("warm {0} ({1}): no response within 8s" -f $p.HostName, $ip)
        Stop-Job $k
    }
    Remove-Job $k -Force
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
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Installed '$TaskName' — runs $DelaySeconds s after each login (battery-safe)."
Write-Host "Self-heal script: $scriptPath"
Write-Host "Log:              $(Join-Path $dir 'ts-selfheal.log')"
Write-Host "Test now:         Start-ScheduledTask -TaskName '$TaskName'   (then read the log)"
Write-Host "Remove:           .\tailscale-ssh-selfheal.ps1 -Uninstall"
