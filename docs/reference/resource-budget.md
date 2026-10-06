# Resource budget: readings and preflight

Before a session launches a heavy command (a build, a parallel test run, a
stress run), ask whether it fits:

```bash
python -m genesis.hostmetrics status
python -m genesis.hostmetrics preflight --name build --ram 4 --cpu 200 --disk ~/tmp=10
```

`genesis.hostmetrics` is stdlib only and never imports the Genesis runtime, so it
runs under the venv or under system `python3` with `src/` on the path.

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
$ python -m genesis.hostmetrics preflight --name build --ram 8 --cpu 200
WAIT build
  memory: WAIT — fits once current load drops (live 6.0 GiB, estimate 8.0 GiB, line 12.8 GiB, total 16.0 GiB)
  cpu: GO — fits under the line (live 50%, estimate 200%, line 320%, total 400%)
  note: host leg unavailable: no host link configured (guardian_remote.yaml absent); judged on the container only
```

## Limits

- Advisory only: nothing enforces the verdict yet. Jobs launched without it are
  invisible to it, except through the live readings.
- The CPU sample adds its window (default 3 s, minimum 0.5 s) to every call.
- CPU reads cgroup v2 only. On cgroup v1, capacity is the affinity count and use
  comes from `/proc/stat`, which inside a container may cover the whole host.
- Where the cgroup has no `*.pressure` files, PSI comes from `/proc/pressure`,
  which inside a container is host-wide, so memory ASK then reflects the host.
- The host leg reads only memory. GPU is out of scope.
- The memory line is the container's. A job started directly inside a
  session's own capped scope is also bound by that scope's `MemoryMax`, which
  preflight does not read. Until the `run` wrapper (#2926, PR 2) exists, that is
  every job a session starts. A `systemd-run --user --scope` job (what `run`
  launches) lands under the user manager, not inside the caller's scope, so that
  cap does not apply to it.
