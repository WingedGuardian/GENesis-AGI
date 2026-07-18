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
  managed installs (and warns if the host is swapless). On guardian-managed
  hosts the invariant is also **self-healing**: the guardian's swap
  reconciler (`guardian/swap_watch.py`) re-asserts the config knob *and*
  live-activates the cgroup (`memory.swap.max`) each tick — covering installs
  that advance via bare `git pull` and never re-run host-setup. A heal pages
  an INFO alert; opt a host out with `swap_reconcile_enabled: false` in the
  guardian config (an explicitly-false knob is otherwise reconciled back to
  true — swap-on is the install invariant).
- **Bare metal / VM**: create a swapfile or LV sized to taste. Even a few
  GiB turns the OOM cliff into a ramp. **On guardian hosts this is now
  mechanical** — see the zram layer below.

## The zram layer (guardian hosts)

`scripts/lib/host_swap.sh` gives guardian host VMs a **compressed-RAM-first
swap tier**: a zram device at swap priority 100, so memory pressure compresses
into fast RAM before touching disk swap (which stays as the lower-priority
fallback). On a host with *no* swap at all, this closes the wedge-defect gap
mechanically instead of leaving the warning above as the only output.

- **Size**: `min(MemTotal/2, 4 GiB)` — zram-generator's own upstream default
  formula, computed per machine at apply time (a 2 GiB VPS gets 1 GiB, big
  hosts cap at 4 GiB). Override the cap with `HOSTSWAP_CAP_GIB`.
- **Unit**: a self-contained root-level `zram-swap.service` (oneshot,
  `RemainAfterExit`) written to `/usr/local/lib/systemd/system/` (NOT /etc —
  `systemctl mask` needs the /etc path free to plant its /dev/null symlink,
  so the documented opt-out actually works) — fixed `/dev/zram0`,
  zstd compression with automatic fallback to the kernel default, no
  genesis-path dependencies. Idempotent: an unchanged unit produces no
  systemd churn; a RAM change re-renders it.
- **Applied from**: `install_guardian.sh` Step 9c (fresh installs) and the
  gateway `redeploy` verb (existing installs retrofit on their next update).
- **Degrades to a one-line skip** when the machine can't or shouldn't:
  not-systemd, container vantage (zram needs the real host kernel), no
  `zramctl`/`zram.ko`, no non-interactive sudo, `HOSTSWAP_DISABLE=1`, an
  external zram setup already active (never shadows zram-generator or an
  operator's own device), or a masked unit.
- **Opt out permanently**: `sudo systemctl mask zram-swap.service` — apply
  skips masked units. Remove cleanly with
  `sudo bash -c 'source scripts/lib/host_swap.sh && host_swap_remove'`.
- The host-plane `swap_total_kb` fact (table above) picks the device up
  automatically, and `_memres_swap_check` stops warning once it's active.

## How the body schema surfaces it

The infrastructure profile (`INFRASTRUCTURE.md`, `infra_profile` package)
records the invariant as facts, so drift is observed and the annotation layer
flags unprotected installs:

| Fact | Plane | Healthy | Wedge-defect |
|---|---|---|---|
| `cgroup_memory_swap_max` | container | `"max"` (or an int) | `0` |
| `oomd_user_slice_kill` | container | `true` | `false` |
| `swap_total_kb` | host | > 0 | `0` |
| `container_limits["limits.memory.swap"]` | host_virt | `"true"`/absent | `"false"` |

**The posture alert (active signal).** Facts and annotations alone proved
insufficient — `infra_profile` only emits `infrastructure_drift` on a fact
*change*, so a box that was *always* unprotected produced no signal at all
(observed live: a sibling install ran for weeks with swap disabled and no
systemd-oomd until a memory spike wedged it). The awareness loop's hourly
`_check_infra_protection_posture` (`awareness/loop.py`) closes that: any
wedge-defect value in the table above raises one non-paging `high`
`infrastructure_alert` (dashboard + morning report) naming the missing
protections and their remediation, auto-resolving when the profile shows them
restored. Only *explicit* defect values alert — absent/`None` facts stay
silent (no guardian host plane, cgroup v1, fresh install), so partial installs
never false-alarm. A profile older than 3 days raises a distinct
"posture UNKNOWN — refresh broken" alert instead of asserting from dead facts.
The same check also covers the **network plane** (KeepConfiguration + the
networkd watchdog), gated so it only fires on networkd-managed boxes — see
`docs/reference/network-resilience.md` → "The posture alert (active signal)".

## Notes

- This userspace layer is complementary to the **kernel-side** OOM tuning
  Genesis already applies on guardian hosts
  (`/etc/sysctl.d/99-genesis-oom-tuning.conf` via `install_guardian.sh` /
  `host-setup.sh`: `vm.min_free_kbytes`, `vm.watermark_scale_factor`,
  `vm.oom_kill_allocating_task`). The sysctls shape *kernel reclaim and the
  kernel OOM killer*; systemd-oomd acts *earlier*, on pressure, in userspace.
  Tune them as two layers of one defense, not competing knobs.
- Inside a container, systemd-oomd's swap-based kills are inert (the
  container can't see swap devices); the **pressure** path is the active
  mechanism. The swap limit line matters on bare/VM installs.
- These kills are a last line of defense, not a workload manager. If oomd
  ever kills `genesis-server` itself, the pressure limit is mis-tuned for
  that machine — raise the percentage or investigate what drove sustained
  PSI, and file the incident.

## What a live fire drill proved (2026-07)

The full stack was drilled on a production install (16 GiB container, 7.3 GiB
host swap) with deliberate memory balloons. Findings worth knowing before you
tune anything:

- **The swap layer is what absorbs almost everything.** An idle 7.6 GiB
  balloon was absorbed silently; a single 12 GiB *churning* balloon ran for
  hours without wedging the machine — the exact stimulus class that
  previously took the box down in minutes.
- **At hard exhaustion, the KERNEL OOM killer fires first, and that's fine.**
  A four-process churn storm (~13 GiB hot working set) exceeded RAM + swap
  and was killed by the kernel within ~2 minutes (`memory.events` `oom_kill`
  is the counter to check — inside a container you cannot see the host's
  dmesg, and `journalctl -u systemd-oomd` staying empty does NOT mean nothing
  fired). `genesis-server` survived on `OOMScoreAdjust=-500`.
- **systemd-oomd thresholds against the *full* PSI metric** — the fraction of
  time ALL tasks in the cgroup were stalled simultaneously (see
  systemd.oomd(5)) — not the `some` line most dashboards show. A single
  greedy process can push `some` past 60% while `full` stays in single
  digits: oomd correctly stays quiet because everything else is still making
  progress off swap. Its unique window — sustained all-tasks stall *without*
  hard memory.max exhaustion — is narrow by design. Don't lower its
  percentages chasing kills the kernel layer already delivers; that only buys
  collateral kills during legitimate heavy bursts.
- Net: the wedge fingerprint at the top of this doc is covered twice over —
  swap turns the cliff into a ramp, and whichever of kernel-OOM (hard
  exhaustion) or oomd (sustained all-stall) triggers first takes the greedy
  tree while the server survives.
- Quality-over-cost still applies: nothing here throttles or degrades
  Genesis under pressure; it only decides *what dies first* when the machine
  is already out of headroom.
