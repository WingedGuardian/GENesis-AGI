# Memory Resilience — swap + systemd-oomd, the adaptive way

## The invariant

**A memory spike must degrade into swap pressure and a userspace OOM kill of
the greedy process tree — never into load-100 D-state thrash that wedges the
whole machine.**

Genesis is a process-heavy system: a cognitive server, per-session Claude Code
trees, MCP servers, indexers. On a box with no swap and no userspace OOM
killer, the first component to over-allocate doesn't fail alone — the kernel
enters direct-reclaim thrash, load climbs into the tens or hundreds, SSH stops
answering, and *everything* on the machine dies together. One incident series
(2026-07) produced exactly this failure mode four times from four unrelated
causes; the common factor was the missing relief valve, not any single leak.

The fingerprint, if you're diagnosing it live:

- load average 50–130 with mostly-idle CPUs (processes stuck in `D` state)
- SSH connections drop or hang; `incus exec` hangs from the host
- inside a container: `cat /sys/fs/cgroup/memory.swap.max` shows `0`
- `swapon --show` empty on the host/VM

## What Genesis sets up (and what it only warns about)

`scripts/lib/memory_resilience.sh` runs from `bootstrap.sh` on fresh installs
and from every `update.sh` (which re-runs bootstrap), so existing installs
retrofit automatically. It is idempotent and **adaptive — every threshold is a
pressure percentage, never an absolute byte value**, so the same config
right-sizes from a small VPS to a large workstation:

- `/etc/systemd/system/user.slice.d/genesis-oomd.conf` —
  `ManagedOOMMemoryPressure=kill` at **60%** memory-pressure (PSI) on the
  user slice: the backstop monitor.
- `/etc/systemd/system/user@.service.d/genesis-oomd.conf` — `kill` for the
  per-user manager. In practice this is the operative monitor (many distros
  ship a 50% default limit for it; ours guarantees `kill` where the distro
  ships `auto` or nothing).
- `/etc/systemd/oomd.conf.d/genesis.conf` — swap-use limit 90%, default
  pressure limit 60%, 20s duration.
- `genesis-server.service` carries `ManagedOOMPreference=avoid` (plus the
  kernel-side `OOMScoreAdjust=-500`), so oomd prefers killing the greedy
  session tree over the cognitive core. `avoid` is honored by the per-user
  monitor (same-UID cgroup ownership) — see systemd.resource-control(5).

Graceful degradation: no systemd, no `systemd-oomd`, no kernel PSI, or no
non-interactive sudo each produce a one-line skip note, never a failure.

**Swap itself is verified, not created.** The setup warns — with the exact
remediation for the detected vantage — because the knob is never local:

- **Inside a container** (LXC/Incus): swap capability is granted by the host.
  Fix on the host: `incus config set <container> limits.memory.swap true`,
  and make sure the host itself has swap. `host-setup.sh` does both for
  managed installs (and warns if the host is swapless).
- **Bare metal / VM**: create a swapfile or LV sized to taste. Even a few
  GiB turns the OOM cliff into a ramp.

## How the body schema surfaces it

The infrastructure profile (`INFRASTRUCTURE.md`, `infra_profile` package)
records the invariant as facts, so drift is observed and the annotation layer
flags unprotected installs:

| Fact | Plane | Healthy | Wedge-defect |
|---|---|---|---|
| `cgroup_memory_swap_max` | container | `"max"` (or an int) | `0` |
| `oomd_user_slice_kill` | container | `true` | `false` |
| `swap_total_kb` | host | > 0 | `0` |

## Notes

- Inside a container, systemd-oomd's swap-based kills are inert (the
  container can't see swap devices); the **pressure** path is the active
  mechanism. The swap limit line matters on bare/VM installs.
- These kills are a last line of defense, not a workload manager. If oomd
  ever kills `genesis-server` itself, the pressure limit is mis-tuned for
  that machine — raise the percentage or investigate what drove sustained
  PSI, and file the incident.
- Quality-over-cost still applies: nothing here throttles or degrades
  Genesis under pressure; it only decides *what dies first* when the machine
  is already out of headroom.
