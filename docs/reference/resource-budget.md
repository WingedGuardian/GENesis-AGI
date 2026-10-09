# Resource budget: readings, preflight and run

Before a session launches a heavy command (a build, a parallel test run, a
stress run), ask whether it fits, or let `run` ask and then cap it:

```bash
~/genesis/.venv/bin/python -m genesis.hostmetrics status
~/genesis/.venv/bin/python -m genesis.hostmetrics preflight --name build --ram 4 --cpu 200 --disk ~/tmp=10
~/genesis/.venv/bin/python -m genesis.hostmetrics run --name build --ram 4 --cpu 200 -- make -j4
```

`genesis.hostmetrics` is stdlib only and never imports the Genesis runtime. Run it with
the Genesis venv's interpreter (`~/genesis/.venv/bin/python` on a standard install):
a session shell usually has no bare `python`, and system `python3` cannot import
`genesis` unless `src/` is on its path.

## What it measures

| Resource | Total (D) | Live use (L) | Source |
|---|---|---|---|
| Memory (container) | cgroup `memory.max`, else `/proc/meminfo` MemTotal | total − available, where available = limit − `memory.current` + file LRU, clamped by MemAvailable | cgroup v2, then v1, then procfs |
| Memory (host) | host MemTotal | host MemTotal − MemAvailable | the guardian's read-only `ram-status` verb |
| CPU | cgroup `cpu.max` quota, capped by the affinity mask | busy cores over a sampling window (default 3 s) | cgroup `cpu.stat`, else `/proc/stat` |
| Disk space | filesystem size | size − space free to unprivileged users | `statvfs` on the path you name |
| Pressure | — | PSI `some avg300` for cpu, memory, io | the cgroup's own `*.pressure` files, else `/proc/pressure` |

Inside a container, `/proc/meminfo` and `/proc/pressure` can describe the host,
so cgroup files are always read first. If the cgroup limit is finite but its
usage cannot be read, memory is reported as unreadable rather than falling back
to procfs, which could over-admit.

## Verdicts and exit codes

Per resource, with threshold t (default 80%), the budget line is t × D:

| Verdict | Exit | When |
|---|---|---|
| GO | 0 | live + estimate fits under the line |
| NO | 2 | the estimate alone exceeds the line (memory, disk) |
| WAIT | 3 | it fits alone, not with the current load; retry later |
| ASK | 4 | already over the line and not transient: memory with PSI above `GENESIS_RB_ASK_PSI`, or disk; or container memory or a named disk cannot be read |
| (usage error) | 64 | bad arguments |

The overall verdict is the worst across resources (NO > ASK > WAIT > GO). CPU is
compressible, so CPU is never worse than WAIT. An unreadable CPU sample or host leg
is reported in a note, not as ASK: CPU is then judged on the estimate alone, and
memory on the container alone. Memory is judged on the container
and, when the host is reachable, on the host as well; the output names each leg.

`--approved-over-line RESOURCE` records that the owner accepted running while a
resource is over the line. That resource's budget becomes t × what is free now.

A job must state its estimates: without `--ram` and `--cpu` the verdict is NO
("estimate required"). `--assume-default` substitutes the default shares below
and prints them as ASSUMED.

Units: `--ram` and disk in GiB, `--cpu` in core-percent (100 = one core).

`--disk PATH=GB` judges the filesystem that would hold PATH, so PATH may be a
directory the job has yet to create. Paths on one filesystem are summed and
judged once, since they share its free space.

## The host leg

Memory is also checked against the host, because a container's cgroup limit can
exceed what the host has free. The host is read through the guardian gateway's
read-only `ram-status` verb over SSH, configured in
`~/.genesis/guardian_remote.yaml`. A healthy call takes about a second; it is
bounded at 15 seconds and made once per process. A failure is reported as a
short classified reason, never the raw SSH text, which can contain the host's
address and login. With no host link (no guardian, no config, no PyYAML, or a failed
call), the verdict says "host leg unavailable: <reason>" and judges the container
alone. `--no-host` skips the call.

## Levers

| Variable | Default | Meaning |
|---|---|---|
| `GENESIS_RB_THRESHOLD_PCT` | 80 | the budget line, % of each total |
| `GENESIS_RB_ASK_PSI` | 10 | memory PSI `some avg300` % above which over-the-line memory is ASK, not WAIT |
| `GENESIS_RB_DEFAULT_RAM_PCT` | 25 | `--assume-default` RAM, % of memory total |
| `GENESIS_RB_DEFAULT_CPU_PCT` | 25 | `--assume-default` CPU, % of CPU capacity |

Set them in the process environment or in `~/.genesis/resource-budget.env`
(`KEY=VALUE` lines, `export` allowed); the environment wins. An invalid value
falls back to its default, and an unknown `GENESIS_RB_*` name is ignored; the
output says so in both cases.

## Example

On a container with a 16 GiB limit, 6 GiB in use, and no host link:

```text
$ ~/genesis/.venv/bin/python -m genesis.hostmetrics preflight --name build --ram 8 --cpu 200
WAIT build
  memory: WAIT — fits once current load drops (live 6.0 GiB, estimate 8.0 GiB, line 12.8 GiB, total 16.0 GiB)
  cpu: GO — fits under the line (live 50%, estimate 200%, line 320%, total 400%)
  note: host leg unavailable: no host link configured (guardian_remote.yaml absent); judged on the container only
```

## Running a job: `run`

`run` takes the same arguments as `preflight`, then `-- COMMAND [ARGS...]`:

1. It runs the preflight. On WAIT it exits 3 at once, unless
   `--wait-until-fits MIN` is given; it then re-checks every 30 seconds until
   the job fits or MIN minutes pass. On NO or ASK it exits with that code.
2. On GO it launches the command in a transient user scope named
   `genesis-job-<name>-<id>.scope`, with the estimates as hard caps:
   `MemoryMax` = `--ram`, `MemorySwapMax=0`, `CPUQuota` = `--cpu`, plus
   `IOWeight=50` (effective only where the io controller is delegated) and
   `OOMPolicy=continue`. The scope runs in `app-capped.slice` (a child of
   `app.slice`, so no ancestor limit changes) unless `--slice` names another,
   e.g. `genesis-workload.slice`. That slice outlives its scopes and keeps its own
   `oom_kill` count, which is how the disk guardian can tell a job killed at its
   own cap from any other OOM kill. The cbm MCP wrapper and GitNexus index batches
   use the same slice. A probe scope with the same properties runs first.
   If systemd rejects `OOMPolicy` for scopes (before systemd 253), the job runs
   without it, and a job killed at its cap then loses its exit report. If
   systemd refuses any other property (an estimate it cannot apply, a bad
   slice), or the probe cannot run or does not finish within 15 seconds, `run`
   exits 64 with the reason; it never falls back to running uncapped on a guess.
   `--ram` below 16 MiB is refused for the same reason. The probe also reads the
   limits the kernel applies inside its scope. Where systemd accepts a cap but
   the kernel is not applying it (the scope's `memory.max` or `cpu.max` reads
   `max`, or `memory.swap.max` reads anything but the requested 0), `run` warns
   that the cap is NOT enforced; where the limit file cannot be read (cgroup v1,
   a controller missing from the parent, no memcg swap accounting), it warns
   that the cap could not be verified. Swap is skipped when no swap device is
   active, since nothing can swap. Either way the job still runs in the scope,
   visible to other sessions, and also gets nice 19 and
   data limit.
3. The job's exit code is `run`'s exit code (128+N for signal N). On exit it
   prints, on stderr, the job's peak memory, its CPU seconds, and whether it was
   killed at its memory cap. The verdict also goes to stderr; stdout belongs to
   the job. A command that is a shell builtin (`exit`, `exec`) ends the
   in-scope reporter with it, so no report is printed. A kill at the cap is
   reported here, to the caller, and NOT paged: the job runs in
   `app-capped.slice`, whose own `memory.events` counters let the disk guardian
   see that the kill happened inside the slice AND that the slice's own limit
   fired (logged in `~/.genesis/logs/oom_events.log`). A kill in that slice
   caused by a limit outside it (the container's, or the host's) still pages.

The scope is the job's ledger entry. `status` lists live `genesis-job-*`
scopes, and every `preflight` counts their memory reservations: each job's
`MemoryMax` minus its current use (page cache excluded, as it is from live
use) is added to live memory use, on the container and host legs. So a second
session sees the first session's job before that job has grown into its
reservation. Two `run`s started within a few seconds of each other can both
be admitted, since neither scope exists yet when the other checks. CPU is
compressible and capped by the quota, so it is not reserved.

SIGINT, SIGTERM and SIGHUP to `run` request cooperative SIGTERM cleanup of
its process group and exact named scope, including descendants that stay in
that scope. The wrapper keeps waiting for verified scope completion even if
the launch process exits first. A second cancellation requests SIGKILL; there
is no added timed escalation after the first cancellation. Existing scope
runtime ceilings still apply. The cancellation exit code is 128 plus the last
delivered caller signal. Supervision requires the Linux main CLI thread,
default SIGCHLD disposition, and exclusive ownership of child wait status.
Unexpected external reaping or unverifiable scope cleanup is reported.
Once the direct child exits, its retained wait status is consumed before
independently checking scope completion. A failed scope readback does not leave
that exited child unreaped, and numeric signaling authority closes before reap.

The intended unit name alone does not authorize a scope signal or wait. A
private pipe acknowledgment from a helper running inside the registered scope
establishes that authority. Registration refusal, including a name collision,
leaves any pre-existing scope alone. Before acknowledgment, cancellation can
signal only the owned, unreaped launcher's process group. The helper runs with
isolated Python startup and no site hooks, closes the registration writer,
restores exec signal defaults and the incoming `LC_CTYPE` presence/value, then
executes the payload. This prevents isolated Python locale coercion from
changing a non-Python target's locale. Unit suffixes
contain 128 random bits; this prevents accidental reuse with high probability,
but does not provide atomic protection against deliberate same-user unit
replacement or process migration.

A watchdog checks every 10 seconds. If the box stays over the line for 60 seconds
(container memory or a disk named with `--disk`, in any combination), it requests
SIGTERM for this job, even when other work caused the pressure. A resource
approved with `--approved-over-line` is not watched. A watchdog request does not
count as a caller cancellation or certify that shutdown has completed.

Without `systemd-run` or a reachable systemd user manager, `run` refuses the job
before spawning it and exits 64 with the reason. It does not launch an invisible
uncapped fallback. Accepted scopes whose caps are unenforced or unverified keep
the warnings and additional nice 19/data-segment limit described above; this
policy change does not change those scoped launches.

`run` is for non-interactive jobs: the job runs in its own session, without a
controlling terminal, so password prompts (ssh, sudo, git credentials) fail.

Exit codes overlap: a job that itself exits 2, 3 or 4 looks like a verdict.
Read stderr: the verdict lines start with the verdict (or are a JSON object
under `--json`), and the job's own report lines start with `genesis-job`.

## Limits

- Advisory: a job launched without `run` is invisible to the reservations,
  except through the live readings.
- The CPU sample adds its window (default 3 s, minimum 0.5 s) to every call.
- `run` reads its limits and exit figures (peak memory, CPU time, cap kills)
  from cgroup v2 files. On cgroup v1 it warns that it cannot verify the caps and
  the exit report omits those figures.
- A job that ignores the first cooperative cancellation can keep the wrapper
  waiting until a second cancellation forces SIGKILL or an existing runtime
  ceiling applies. Scope inspection failures are reported as incomplete cleanup.
- CPU reads cgroup v2 only. On cgroup v1, capacity is the affinity count and use
  comes from `/proc/stat`, which inside a container may cover the whole host.
- Where the cgroup has no `*.pressure` files, PSI comes from `/proc/pressure`,
  which inside a container is host-wide, so memory ASK then reflects the host.
- The host leg reads only memory. GPU is out of scope.
- The memory line is the container's. A job started directly inside a
  session's own capped scope is also bound by that scope's `MemoryMax`, which
  preflight does not read. That is any heavy job started without `run`. A job `run`
  launches lands under the user manager (`systemd-run --user --scope`), not
  inside the caller's scope, so that cap does not apply to it.
- A live job's declared `--disk` estimate is checked when it is admitted but not
  reserved afterwards: a second job admitted while the first has not yet written
  its output sees that space as free. The watchdog stops only its own job, and
  only after the line has been held for 60 s (not at all with `--approved-over-line disk`),
  so a fast writer can fill the disk first (#2973).
