# Running Work on a Remote Windows Machine

How Genesis reaches a Windows host over SSH — and the one thing that makes
half of it fail silently.

Everything here is MEASURED on a real Windows 11 machine (2026-09-06), not
inferred from documentation. The numbers and error strings are what that
machine actually produced.

## The short version

SSH to a Windows box works and is enough for **filesystem, registry, service
and process work**. It is **useless for anything involving the desktop** —
screen capture, window enumeration, input injection — because an SSH login
lands in a different Windows *session* from the desktop, with its own window
station.

Worse, it does not fail loudly. It returns an empty, well-formed result.

To reach the desktop, register a **Scheduled Task with an Interactive logon
type** and trigger it from the SSH session. The task body runs in the user's
session and can see everything.

## Why: session isolation

Windows puts services and non-interactive logons in **session 0**, and each
interactive desktop logon in **session 1** (or higher). Sessions have separate
window stations and desktops, and session 0's has no visible desktop at all.

Confirm the split before debugging anything else:

```powershell
ssh user@host powershell -NoProfile -Command `
  "(Get-Process -Id $PID).SessionId; [Environment]::UserInteractive; (Get-Process explorer).SessionId"
```

Measured output over SSH:

```
0        <- the SSH session
False    <- not interactive
1        <- where the actual desktop lives
```

If you see that, session isolation is the problem, not your code.

## The failure mode that costs you a day

**Every one of these failures presents as empty success.** None of them raises
anything a caller would notice if it wraps the call in a `try`.

| what failed | what it returned |
|---|---|
| UI Automation from session 0 | **0 top-level windows** — reads as an empty desktop |
| `CopyFromScreen` from session 0 | throws `The handle is invalid`, **but still leaves a valid all-black PNG** on disk |
| UIPI blocking an **elevated** window | **empty element tree** — reads as an app with no accessibility structure |

Two of these produce artifacts that look completely normal. A 3 KB PNG opens
fine and is simply black. An empty UI Automation tree is indistinguishable from
a genuinely featureless application.

Session 0 also reports a **phantom display**: `Screen.PrimaryScreen.Bounds`
returned `1024x768` where the real desktop was `1536x864`. So even the metadata
lies, plausibly.

**Therefore: validate the content, never the envelope.** A saved file is not
evidence of a capture; a returned tree is not evidence of enumeration. Sample a
grid of pixels and count distinct colours. Assert the expected `SessionId`.
Check the resolution is plausible. Refuse rather than returning a confident
blank.

And never publish "X has no Y" from an empty Windows result without a positive
control proving the same probe *can* see something. In testing, an elevated
window returned 3 elements and 0% actionable — which read as "native Windows
apps expose no structure" until a non-elevated app of the same generation
returned 47 elements and 53% actionable.

## The working pattern

Put the real work in a `.ps1` on the target — do not inline it through nested
SSH quoting — then:

```powershell
$id = [Security.Principal.WindowsIdentity]::GetCurrent()

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\path\to\work.ps1"

# InteractiveToken is the load-bearing choice. ServiceAccount and SYSTEM run
# in session 0 and fail exactly as silently as SSH does.
$principal = New-ScheduledTaskPrincipal -UserId $id.User.Value `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "MyTask" -Action $action `
    -Principal $principal -Settings $settings -Force
```

Then from the SSH session: `Start-ScheduledTask -TaskName MyTask`.

Four details, each of which costs a debugging cycle if missed:

- **Register against the token's SID, not a constructed name.** On a workgroup
  machine `$env:USERDOMAIN` reads `WORKGROUP` while the real principal is
  `<COMPUTER>\<user>`. Passing the constructed name fails with
  `No mapping between account names and security IDs was done.`
- **The battery flags are not optional on a laptop.** Without
  `-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries`, the task sits in
  `Queued` and never runs, silently.
- **`-ExecutionTimeLimit ([TimeSpan]::Zero)`** or a long-running body is killed
  at the default cap.
- **The trigger returns immediately.** The body is asynchronous, so poll the
  task's `LastTaskResult` *and* wait on the output file. Have the script write
  its own evidence — session id, the rect it saw, a content check — and read
  that back rather than trusting an exit code.

## Elevation is a separate wall

A Limited-integrity process cannot read an **elevated** window's UI Automation
tree. UIPI denies it and returns an empty tree.

Detect it: `Get-Process -Id <pid>` on an elevated process returns an **empty
`Path`** from a non-elevated caller.

**Refuse the target rather than falling back.** Driving a window you cannot
introspect, using coordinates you cannot verify, is worse than declining. And
running the agent elevated to "solve" this is the wrong trade: it grants
administrative reach over the whole machine to fix a narrow visibility problem.

## What this does NOT affect

**Chrome DevTools Protocol is immune.** CDP is a TCP connection to a browser
that is already running in the interactive session, so a client in session 0
talks to it perfectly well. If you are driving a browser on a Windows host over
CDP, none of this applies — which is exactly why that path works while direct
capture does not.

## Deploying to another install

There is no automated path: a user's own Windows machine is not reachable by
the container's deploy scripts. Anything here ships as a script in `scripts/`
plus a documented operator step the user runs once, following the same shape as
`scripts/tailscale-ssh-selfheal.ps1`.

## See also

- `docs/reference/tailscale-ssh-access.md` — the *inbound* direction: reaching
  Genesis's tmux slots from a client device.
- `scripts/tailscale-ssh-selfheal.ps1` — the reference shape for a
  self-installing Windows helper with an `-Uninstall` switch.
