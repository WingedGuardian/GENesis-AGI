# Running Work on a Remote Windows Machine

How Genesis reaches a Windows host over SSH, why half of it fails silently,
and the cheap way around that almost everyone misses.

Everything here is MEASURED on real Windows 11 machines (2026-09-06 and
2026-09-15), not inferred from documentation. The numbers and error strings are
what those machines actually produced.

## The short version

SSH to a Windows box works and is enough for **filesystem, registry, service
and process work**. It is **useless for anything involving the desktop** —
screen capture, window enumeration, input injection — because an SSH login
lands in a different Windows *session* from the desktop, with its own window
station. Worse, it does not fail loudly. It returns an empty, well-formed,
entirely wrong result.

The instinct at that point is to get your own code into the desktop session, by
registering a scheduled task with an interactive logon. That works, and the
escape hatch is documented below, but it is the expensive road: it drags in a
request/response protocol (a triggered task cannot be passed arguments), a
console-window problem, and per-invocation result correlation.

**The cheap road is to stop trying to cross the boundary and instead talk to
something already on the other side.** The session boundary isolates *window
stations*, not *sockets*. A process in session 0 can open a loopback connection
to a process in session 1 and ask it to do the desktop work. If the machine
already runs a resident agent in the interactive session — and an operator's
daily-driver machine usually runs several — the capability you want may already
be a local HTTP call away.

## Why: session isolation

Windows puts services and non-interactive logons in **session 0**, and each
interactive desktop logon in **session 1 or higher** — not reliably 1. A
machine with a reconnected RDP session, a switched user, or several interactive
logons will number them differently, so assert the session you *find the
desktop in*, never a constant.

Confirm the split before debugging anything else:

From a POSIX shell (the Genesis container):

```bash
ssh user@host "powershell -NoProfile -Command \
  '(Get-Process -Id \$PID).SessionId; [Environment]::UserInteractive; (Get-Process explorer).SessionId'"
```

The inner command is **single-quoted, and `$PID` is escaped, on purpose.** Both
local shells would otherwise substitute it before anything is sent: PowerShell
expands `$PID` inside double quotes to its own process id, and bash expands it
to nothing at all. Either way the remote machine is asked the wrong question
and answers it confidently. Watch the fence language too — the outer shell here
is bash, and a PowerShell backtick continuation pasted into bash leaves an open
quote rather than an error.

Measured output over SSH:

```
0        <- the SSH session
False    <- not interactive
1        <- where the actual desktop lives on this machine
```

If you see that, session isolation is the problem, not your code.

## The failure mode that costs you a day

**Every one of these failures presents as empty success.** None raises anything
a caller would notice if it wraps the call in a `try`.

| what failed | what it returned |
|---|---|
| UI Automation from session 0 | **0 top-level windows** — reads as an empty desktop |
| `CopyFromScreen` from session 0 | throws `The handle is invalid`, **but still leaves a valid all-black PNG** on disk |
| UIPI blocking an **elevated** window | **empty element tree** — reads as an app with no accessibility structure |
| `Get-Process ... MainWindowTitle` from session 0 | **every title empty** — reads as a machine with no windows open |

Most of these produce artifacts that look completely normal. A 3 KB PNG opens
fine and is simply black. An empty UI Automation tree is indistinguishable from
a genuinely featureless application.

One more empty result that is not evidence: the **Task Scheduler Operational
log is disabled by default**, so `Get-WinEvent` returns zero task-launch events
on a machine where tasks are firing constantly. Check
`(Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational').IsEnabled`
before reading anything into an empty history.

Session 0 also reports a **phantom display**: `Screen.PrimaryScreen.Bounds`
returned `1024x768` where the real desktop was `1536x864`. Even the metadata
lies, plausibly.

**Therefore: validate the content, never the envelope.** A saved file is not
evidence of a capture; a returned tree is not evidence of enumeration. Sample a
grid of pixels and count distinct colours. Assert the session the desktop is
actually in. Check the resolution is plausible. Refuse rather than returning a
confident blank.

And never publish "X has no Y" from an empty Windows result without a positive
control proving the same probe *can* see something. In testing, an elevated
window returned 3 elements and 0% actionable — which read as "native Windows
apps expose no structure" until a non-elevated app of the same generation
returned 47 elements and 53% actionable.

## The pattern that works: ask something already inside the session

**Sockets are not session-isolated.** This is the whole trick, and it is easy to
miss because everything else about the boundary is so absolute.

MEASURED 2026-09-15: from an SSH login confirmed to be in session 0,
`Invoke-WebRequest http://localhost:<port>/` returned a normal `200` from a
service running in session 1 — the same session `explorer` was in, and the same
session that can see the desktop. Nothing about the loopback call is degraded
by the boundary that makes a capture from the same login impossible.

So the question to ask first is not *"how do I get my code into the interactive
session?"* but *"is something already there that can do this for me?"* The same
applies to Chrome DevTools Protocol, which is why CDP against a browser on a
Windows host works perfectly from session 0 while direct capture does not.

Two conditions the claim carries, neither obvious. A **packaged (UWP/Store)**
agent runs in an AppContainer with network isolation, and is NOT reachable over
loopback without a `CheckNetIsolation LoopbackExempt` entry — the one common
case where this fails. And the agent's **auth secret usually lives under the
interactive user's profile**, so the SSH principal has to be that same user for
the one-liner below to be able to read it.

**The contract to look for.** Any resident agent is usable this way if it
offers: a local HTTP (or other socket) surface, an operation that does the
desktop-side work, and some way to authenticate. A useful one for desktop
perception exposes roughly:

```
POST http://127.0.0.1:<port>/<capture-endpoint>
Authorization: <whatever that agent uses>
  -> { "image": "<base64>", ... }   # or a path on the remote machine
```

**Reaching it from Genesis.** A resident agent is usually bound to loopback
only, and it should stay that way — binding it to a tailnet or LAN address to
make it reachable trades a genuine security property for convenience Genesis
does not need. Instead, make the call *on the remote machine*, from the SSH
session that is already authenticated:

```bash
# Run on the Genesis side; the inner command runs on the Windows machine.
ssh "$user@$host" "powershell -NoProfile -Command \
  \"Invoke-RestMethod -Uri http://localhost:$port/$endpoint -Method POST\""
```

`localhost` rather than `127.0.0.1` on purpose: that is the spelling the
measurement above used, and an agent that validates `Host` (see below) may
allowlist one and not the other. If you get a 4xx that is clearly not an
authentication failure, trying the other spelling is the cheapest first move.

Or forward the port for the life of a session with `ssh -L`. Either way the
service keeps its loopback binding and nothing new is exposed.

**This is a capability boundary, not just a convenience.** Reaching a resident
agent makes desktop work dramatically cheaper to invoke, and cheap invocation is
exactly what the desktop-takeover gate (`src/genesis/autonomy/desktop_gate.py`,
`config/desktop_takeover.yaml`) exists to prevent for desktop ACTUATION. Keep
the same line here: a route like this belongs behind an operator-run step or a
foreground session, and must NOT become something Genesis can fire on its own —
an MCP tool, a scheduled job, an autonomous caller — without going through that
gate first. The one real advantage of wiring it as a tool is autonomous
callability, and that is the single property this capability should not have
yet. Perception is not actuation, but a screen is the operator's, and a route
nothing can call is the posture to keep until the gate covers it.

**Where the specifics live.** Port, endpoint path, auth scheme and capability
set are properties of whichever agent a given install runs, so they are
**install-local configuration, never values in this repo** — the same rule that
keeps device details out of any tracked script. Follow the
convention the rest of the codebase uses for a subsystem's local settings —
tracked defaults in `config/<name>.yaml`, a gitignored overlay at
`~/.genesis/config/<name>.local.yaml`, deep-merged by
`src/genesis/_config_overlay.py::merge_local_overlay`, which 44 modules already
read. Do **not** put this in `~/.genesis/config/genesis.yaml`: that file is
machine identity and network, read through `env.py::_local_section`, which
resolves exactly one top-level key to a dict and has no nested-path accessor —
so a three-deep key there fails silently, which is the failure mode this whole
document is about.

Note the `desktop` namespace is already taken: `config/desktop_takeover.yaml`
plus its `.local.yaml` overlay belong to the takeover gate. Either extend that
file or pick a distinct name; a second, differently-shaped `desktop:` loaded by
different machinery is a trap for whoever meets it second.

Two properties of the agent worth checking before depending on it, because both
are common and neither is advertised: whether it binds loopback only (good, and
handled above), and whether it validates the `Host` header against an allowlist
— a service that does will reject a forwarded request whose `Host` is not the
name it expects, which presents as a puzzling 400 rather than a connection
error.

## If nothing is resident: the scheduled-task escape hatch

Only worth it when no process in the interactive session can be asked. Put the
real work in a `.ps1` on the target — do not inline it through nested SSH
quoting — then:

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

Six details, each of which costs a debugging cycle if missed:

- **Register against the token's SID, not a constructed name.** On a workgroup
  machine `$env:USERDOMAIN` reads `WORKGROUP` while the real principal is
  `<COMPUTER>\<user>`. Passing the constructed name fails with
  `No mapping between account names and security IDs was done.`
- **The battery flags are not optional on a laptop.** Without
  `-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries`, the task sits in
  `Queued` and never runs, silently.
- **`-ExecutionTimeLimit ([TimeSpan]::Zero)`** or a long-running body is killed
  at the default cap.
- **`Start-ScheduledTask` cannot pass arguments**, so the registered action
  runs with whatever was baked in at registration. This is a limit of that
  cmdlet, not of Task Scheduler: the COM interface `IRegisteredTask::Run`
  accepts up to 32 values that substitute into the action as `$(Arg0)` …
  `$(Arg31)`, reachable from PowerShell via
  `New-Object -ComObject Schedule.Service`. Either way you still own RESULT
  correlation, which is the harder half — so anything per-invocation has to
  travel out of band — a request
  file written before the trigger, read by the body — and the reply has to be
  keyed to that request. Polling for "a new output file" cannot tell this run's
  artifact from a concurrent run's, or from one produced moments before the
  trigger, and a wrong result is accepted in silence because it is a perfectly
  valid result of something else.
- **A Windows profile directory is NOT reliably `C:\Users\<username>`.** It can
  be renamed, redirected, or on another drive. Ask the machine
  (`$env:USERPROFILE`) and build paths from the answer; constructing it from
  the login name works on every machine you tested and then does not.
- **The trigger returns immediately.** The body is asynchronous, so have the
  script write its own evidence — the session it ran in, what it saw, a content
  check — and a terminal status on *every* exit path including the ones that
  produce no output. A body that can exit without writing a status strands the
  caller until its timeout, which is indistinguishable from an unreachable
  machine.
- **A console app launched by Task Scheduler gets a visible console, and
  `-WindowStyle Hidden` on the action does not suppress it.** For a one-shot
  that is a flash on the operator's screen every single time; for anything that
  runs per-action it is a window popping up every few seconds, which makes the
  machine unusable while the tool works. Hiding it from inside the script
  (`GetConsoleWindow` + `ShowWindow SW_HIDE`) still leaves a brief flash,
  because the console exists for some milliseconds first. A `wscript.exe` shim
  that launches with window style `0` avoids it entirely — as does choosing a
  console-less interpreter binary where one exists, which is the same fix in a
  different spelling.
- **The shim is not free: it costs you ownership of the process.** Under a
  wscript shim the launcher exits immediately, so Task Scheduler considers the
  task finished while the real work is still running — and `Stop-ScheduledTask`
  can no longer stop it. MEASURED: six orphaned pollers accumulated in one
  afternoon of install/start cycles, none of them visible as a running task.
  Anything resident therefore needs a stop signal of its own (a sentinel file
  it polls, a named event), and anything one-shot should have the shim WAIT so
  the task still owns its lifetime.

## DPI: measurements taken while unaware are wrong, and nothing notices

Windows lies to a DPI-unaware process: on a 1920x1080 display at 125% scaling
it reports 1536x864, and every subsequent measurement — virtual screen bounds,
UI Automation bounding rectangles, an allocated bitmap — comes back in that
virtualised space. The work then SUCCEEDS and produces a downscaled result
while reporting the scaled size as though it were real.

MEASURED 2026-09-06: before and after `SetProcessDPIAware` on the same machine,
`GetSystemMetrics` went 1536x864 -> 1920x1080 at monitor DPI 120.

Two rules follow:

- **Set awareness before the first measurement, not before the first use.**
  Anything measured beforehand is already in the wrong space.
- **The return value is not the state.** The documented already-set behaviour
  belongs to the shcore call `SetProcessDpiAwareness`, which returns
  `E_ACCESSDENIED` when awareness was already set by an earlier call or by the
  application manifest — a success reported as a failure code. The legacy
  user32 `SetProcessDPIAware` documents only nonzero-on-success, so its false
  is ambiguous between "already set" and "could not set" rather than
  documented as either. Do not infer the state from either return: read it
  back with `GetProcessDpiAwareness` and refuse to measure while unaware.

## Elevation is a separate wall

A Limited-integrity process cannot read an **elevated** window's UI Automation
tree. UIPI denies it and returns an empty tree.

Detect it two ways, because each misses cases the other catches:
`Get-Process -Id <pid>` on an elevated process returns an **empty `Path`** from
a non-elevated caller; and a window whose UI Automation `BoundingRectangle`
comes back **zero-sized** is either minimised or elevated-and-blocked. Neither
is targetable, and a zero-size rect read as a real measurement produces a
zero-size bitmap rather than an error.

**Refuse the target rather than falling back.** Driving a window you cannot
introspect, using coordinates you cannot verify, is worse than declining. And
running the agent elevated to "solve" this is the wrong trade: it grants
administrative reach over the whole machine to fix a narrow visibility problem.

## Deploying to another install

There is no automated path: a user's own Windows machine is not reachable by
the container's deploy scripts. Prefer, in order: an agent the machine already
runs and can be asked, then a helper the operator installs deliberately once.
If it comes to a helper, `scripts/tailscale-ssh-selfheal.ps1` is the reference
shape — self-installing, with an `-Uninstall` switch.

## See also

- `docs/reference/tailscale-ssh-access.md` — the *inbound* direction: reaching
  Genesis's tmux slots from a client device.
- `scripts/tailscale-ssh-selfheal.ps1` — the reference shape for a
  self-installing Windows helper with an `-Uninstall` switch.
