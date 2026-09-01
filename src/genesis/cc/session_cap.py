"""Interactive-slot capacity decision — `python -m genesis.cc.session_cap`.

`scripts/cc-slot.sh` runs this on slot CREATE (never on reattach) to decide
whether a NEW `cc-N` session may start. It replaces the old free-RAM formula
(`(MemAvailable - reserve) / per_session`) that *collapsed*: because it sampled
instantaneous FREE RAM after sessions were already running, each running session
lowered MemAvailable and thus lowered the cap BELOW the running count (3 running →
computed cap 2 → the operator locked out of a 4th, sometimes the 3rd).

The model here is CAPACITY, not free-RAM:

- The cap is a function of the box's own TOTAL resources (MemTotal) → stable, does
  NOT shrink as sessions run, and portable to any install (a bigger box yields a
  bigger cap with no hardcoded per-install number). Background apps (Kimi/codex,
  bg daemons) no longer shrink the foreground cap.
- Live free RAM (MemAvailable) is consulted ONLY as a low OOM circuit-breaker —
  a floor to refuse a NEW session when the swapless box is genuinely near
  exhaustion — never as the cap.
- Origin-aware: ANY interactive SSH login (the operator "let me in" path — a slot
  hostname OR a plain shell that runs `claude`, keyed on SSH_CONNECTION, distinct
  from the dashboard/local-console "normal method") gets an emergency slot ABOVE
  the safe cap. `decide()` NEVER returns DENY for an operator — when the box is
  full/tight it returns RECLAIM (pick a session to end, or reattach), so the
  operator always has a path in without the OOM-killer choosing the casualty. (The
  interactive reclaim in cc-slot.sh can still decline in two honest corners the
  pure gate can't see: no controlling TTY to prompt on, or an OOM-floor breach with
  no slot to trade — both guide the user to reattach rather than risk an OOM.)

Contract (mirrors `genesis.cc.login_gate`): `main()` reads /proc/meminfo, cpu
count, `SSH_CONNECTION`, and the `GENESIS_CC_*` config levers, takes the current
`cc-N` count via `--existing N`, then prints a two-part protocol to STDOUT:

    line 1: ``ALLOW`` | ``RECLAIM`` | ``DENY``   (the action)
    line 2+: a human-facing explanation (cc-slot.sh echoes it to the operator)

Exit 0 whenever a decision was produced (even DENY). Exit non-zero ONLY on an
internal error — cc-slot.sh treats that as fail-OPEN (its own MemTotal-based
static fallback), because the one thing this gate must never do is lock the
operator out. It is a resource governor, not a security gate.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
from dataclasses import dataclass

# Lever grammars — kept BYTE-IDENTICAL to the bash fallback's validation in
# scripts/cc-slot.sh so the same ~/.genesis/cc-slot.env yields the same cap on
# the Python path and the degraded static path. Positive levers: no leading zero
# (bash reads that as octal), 1–7 digits (bash arithmetic can't overflow a
# ≤9,999,999 value). Emergency: 0–99 (0 disables the emergency slot).
_LEVER_POSITIVE = re.compile(r"[1-9][0-9]{0,6}")
_LEVER_EMERGENCY = re.compile(r"0|[1-9][0-9]?")

# --- Config levers (documented, tunable via ~/.genesis/cc-slot.env) -----------
# Explicit, honest defaults measured on the reference 18GB swapless box
# (2026-08-27): a fully-loaded cc session (claude + serena + memory/health/
# outreach/recon MCPs + gitnexus + pyright) measured ~2.9GB RSS, so PER_SESSION
# is sized NEAR that max (conservative — a swapless box OOM-KILLS a resident
# spike, it cannot page). On this box these yield SAFE_CAP = (18432-4096)//3072
# = 4 (matching the 3-4 comfortably run); a bigger MemTotal scales the cap up,
# a smaller one down (clamped to >=1 so a small box is never locked out).
_DEF_SYSTEM_RESERVE_MB = 4096  # OS + genesis-server + qdrant + bg-spares + swapless margin
_DEF_PER_SESSION_MB = 3072  # measured loaded-session RSS ceiling, conservative
_DEF_OOM_FLOOR_MB = 1536  # min free RAM to START a new session (circuit-breaker)
_DEF_EMERGENCY_SLOTS = 1  # extra slots an operator-origin login may claim above SAFE_CAP

# Tailscale's v4 space is RFC 6598 CGNAT (100.64/10) — checked explicitly so
# classification does not depend on `is_private`'s version-dependent treatment of
# 100.64/10. Tailscale's v6 space is a ULA (fc00::/7), which `ip.is_private`
# ALREADY classifies as operator ("lan") — so no explicit v6 tailnet range is
# needed (and hardcoding one would embed an install-adjacent literal).
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")

_ACTIONS = ("ALLOW", "RECLAIM", "DENY")


@dataclass(frozen=True)
class CapConfig:
    system_reserve_mb: int = _DEF_SYSTEM_RESERVE_MB
    per_session_mb: int = _DEF_PER_SESSION_MB
    oom_floor_mb: int = _DEF_OOM_FLOOR_MB
    emergency_slots: int = _DEF_EMERGENCY_SLOTS

    @classmethod
    def from_env(cls, environ: dict | None = None) -> CapConfig:
        env = os.environ if environ is None else environ

        def _lever(name: str, default: int, pattern: re.Pattern) -> int:
            # Accept ONLY the exact bash grammar (fullmatch) — a value the bash
            # fallback would reject (e.g. "3072mb", "+3072", "2_000", a leading
            # zero, or an oversized string) falls back to the default here too,
            # so the two paths never disagree on a config file.
            raw = env.get(name)
            if raw is None:
                return default
            s = str(raw).strip()
            return int(s) if pattern.fullmatch(s) else default

        return cls(
            system_reserve_mb=_lever(
                "GENESIS_CC_SYSTEM_RESERVE_MB", _DEF_SYSTEM_RESERVE_MB, _LEVER_POSITIVE
            ),
            per_session_mb=_lever(
                "GENESIS_CC_PER_SESSION_MB", _DEF_PER_SESSION_MB, _LEVER_POSITIVE
            ),
            oom_floor_mb=_lever("GENESIS_CC_OOM_FLOOR_MB", _DEF_OOM_FLOOR_MB, _LEVER_POSITIVE),
            # emergency_slots may legitimately be 0 (disable the emergency lever) —
            # the emergency grammar admits 0; anything malformed → the default.
            emergency_slots=_lever(
                "GENESIS_CC_EMERGENCY_SLOTS", _DEF_EMERGENCY_SLOTS, _LEVER_EMERGENCY
            ),
        )


@dataclass(frozen=True)
class Decision:
    action: str  # ALLOW | RECLAIM | DENY
    safe_cap: int  # the resource-derived normal limit
    effective_cap: int  # safe_cap (+ emergency for operator origin)
    origin: str  # tailscale | lan | public | none
    reason: str  # machine tag: ok | emergency_slot | cap_reached | cap_full | oom_floor
    message: str  # human-facing explanation


def classify_origin(ssh_connection: str | None) -> str:
    """Classify the SSH client origin from $SSH_CONNECTION.

    Returns tailscale/lan for the OPERATOR (any interactive SSH login from a
    private/tailscale IP — a slot hostname OR a plain shell that then runs
    `claude`), public for a non-private remote, and none when there is no
    SSH_CONNECTION at all (the dashboard web terminal / local console — the
    "normal method" held to the plain cap). Unparseable → public (safe: no
    emergency, still reattachable and still fail-open in the bash caller).
    """
    if not ssh_connection or not ssh_connection.strip():
        return "none"
    client = ssh_connection.split()[0]
    try:
        ip = ipaddress.ip_address(client)
    except ValueError:
        return "public"
    # A dual-stack sshd can report an IPv4-mapped v6 client (::ffff:a.b.c.d);
    # unwrap it so a mapped tailscale/LAN operator is not misread as "public"
    # (ip.is_private is False on the mapped v6 form, and it misses _TAILSCALE_V4).
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    if ip in _TAILSCALE_V4:
        return "tailscale"
    # ULA (fc00::/7, incl. tailscale's v6 range) + RFC1918 + loopback → operator.
    if ip.is_loopback or ip.is_private:
        return "lan"
    return "public"


def decide(
    *,
    mem_total_mb: int,
    mem_available_mb: int,
    existing: int,
    cpu_count: int,
    ssh_connection: str | None,
    config: CapConfig,
) -> Decision:
    """Pure decision: may a NEW cc-N session start? (reattach is handled upstream)."""
    origin = classify_origin(ssh_connection)
    is_operator = origin in ("lan", "tailscale")

    # SAFE_CAP from TOTAL memory — stable, does NOT collapse as sessions run.
    raw = (mem_total_mb - config.system_reserve_mb) // config.per_session_mb
    cpu = cpu_count if cpu_count and cpu_count > 0 else 1
    safe_cap = max(1, min(raw, cpu))
    emergency = config.emergency_slots if is_operator else 0
    effective_cap = safe_cap + emergency

    # OOM circuit-breaker: enough live RAM to safely START one more session?
    # The threshold must cover a FULL per-session footprint (a fresh session
    # grows toward per_session_mb; starting one with less free than that can OOM
    # a swapless box), plus the operator's absolute floor — whichever is larger.
    need_mb = max(config.per_session_mb, config.oom_floor_mb)
    ram_ok = mem_available_mb >= need_mb

    if not is_operator:
        # Normal method (dashboard/magic-link/public): held to SAFE_CAP.
        if existing >= safe_cap:
            return Decision(
                "DENY",
                safe_cap,
                effective_cap,
                origin,
                "cap_reached",
                f"Session cap reached ({existing}/{safe_cap}). Reattach an existing "
                f"session, or SSH in over LAN/tailscale for an emergency slot.",
            )
        if not ram_ok:
            return Decision(
                "DENY",
                safe_cap,
                effective_cap,
                origin,
                "oom_floor",
                f"RAM low ({mem_available_mb}MB free, need >= {need_mb}MB "
                f"to start safely). Reattach an existing session or free memory.",
            )
        return Decision(
            "ALLOW",
            safe_cap,
            effective_cap,
            origin,
            "ok",
            f"Slot available ({existing + 1}/{safe_cap}).",
        )

    # Operator origin (LAN/tailscale): NEVER hard-denied.
    if existing < effective_cap and ram_ok:
        if existing >= safe_cap:
            return Decision(
                "ALLOW",
                safe_cap,
                effective_cap,
                origin,
                "emergency_slot",
                f"Emergency slot granted (operator origin: {origin}) — "
                f"{existing + 1}/{safe_cap}+{emergency}.",
            )
        return Decision(
            "ALLOW",
            safe_cap,
            effective_cap,
            origin,
            "ok",
            f"Slot available ({existing + 1}/{safe_cap}).",
        )

    # Full (past the emergency slot) OR RAM genuinely tight → offer RECLAIM,
    # never a hard "no". cc-slot.sh runs the interactive pick-a-session-to-end UI.
    if not ram_ok:
        return Decision(
            "RECLAIM",
            safe_cap,
            effective_cap,
            origin,
            "oom_floor",
            f"RAM tight ({mem_available_mb}MB free, need >= {need_mb}MB). "
            f"Reattach a session, or reclaim one to free memory and start fresh.",
        )
    return Decision(
        "RECLAIM",
        safe_cap,
        effective_cap,
        origin,
        "cap_full",
        f"At the emergency limit ({existing}/{safe_cap}+{emergency}). "
        f"Reattach a session, or reclaim one to start fresh.",
    )


def read_meminfo(path: str = "/proc/meminfo") -> tuple[int, int]:
    """Return (MemTotal, MemAvailable) in MB. Raises on unreadable/absent fields
    so main() can fail-OPEN (cc-slot.sh falls back to its bash static cap)."""
    total = avail = None
    with open(path, encoding="ascii") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) // 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) // 1024
            if total is not None and avail is not None:
                break
    if total is None or avail is None:
        raise ValueError("MemTotal/MemAvailable not found in meminfo")
    return total, avail


def _cpu_count() -> int:
    """Process-aware CPU count — respects the cpuset/CPU affinity (like `nproc`),
    NOT os.cpu_count() which returns HOST cores inside a container and would
    inflate the clamp. (It does not read a cpu.max bandwidth quota, but memory is
    the binding constraint here, and this only ever CLAMPS the RAM-derived cap.)"""
    try:
        return len(os.sched_getaffinity(0)) or 1
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def effective_memory() -> tuple[int, int, bool]:
    """(total_mb, available_mb, available_known), each capped by the container's
    cgroup memory limit. procfs can expose HOST values inside a container — a
    16GiB container on a 32GiB host would otherwise be sized for 32GiB and trigger
    a cgroup OOM. Reuses genesis.runtime.cgroup; degrades to procfs-only if cgroup
    is absent.

    ``available_known`` is False in exactly one case: a FINITE cgroup limit was
    found but current usage could not be read. procfs MemAvailable is then
    untrustworthy (it may describe HOST headroom, not the container's), so the
    caller must substitute a conservative estimate rather than trust it — otherwise
    a partial cgroup read would let the gate ALLOW a session the container cannot
    hold. total is still clamped to the limit; only availability is unknown."""
    total_mb, avail_mb = read_meminfo()
    available_known = True
    try:
        from genesis.runtime import cgroup

        cg_max = cgroup.read_container_memory_max()  # bytes, or None if unlimited
        cg_cur = cgroup.read_container_memory_current() if cg_max else None
        cg_file = cgroup.read_container_memory_reclaimable() if cg_max else None
    except Exception:  # noqa: BLE001 — cgroup unreadable → procfs values stand
        cg_max = cg_cur = cg_file = None
    if cg_max:
        total_mb = min(total_mb, cg_max // (1024 * 1024))
        if cg_cur is not None:
            # Available = limit − usage + reclaimable page cache (current counts
            # cache as used; add it back so this matches MemAvailable's meaning).
            cg_avail = max(0, cg_max - cg_cur + (cg_file or 0))
            avail_mb = min(avail_mb, cg_avail // (1024 * 1024))
        else:
            # Finite limit but usage unreadable → procfs avail cannot be trusted.
            available_known = False
    return total_mb, avail_mb, available_known


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="session_cap")
    parser.add_argument(
        "--existing",
        type=int,
        required=True,
        help="Current count of live cc-N tmux sessions (from cc-slot.sh).",
    )
    try:
        args = parser.parse_args(argv)
        config = CapConfig.from_env()
        mem_total, mem_available, available_known = effective_memory()
        if not available_known:
            # cgroup limit known but usage unreadable (see effective_memory):
            # estimate free RAM from the capacity model itself rather than trust
            # host procfs — free ≈ total − reserve − (running sessions × per).
            # Conservative (leans toward RECLAIM when loaded), never a hard lockout
            # at 0 sessions. User decision 2026-08-27.
            mem_available = max(
                0,
                mem_total - config.system_reserve_mb - args.existing * config.per_session_mb,
            )
        decision = decide(
            mem_total_mb=mem_total,
            mem_available_mb=mem_available,
            existing=args.existing,
            cpu_count=_cpu_count(),
            ssh_connection=os.environ.get("SSH_CONNECTION"),
            config=config,
        )
    except Exception:  # noqa: BLE001 — fail-OPEN: any error → cc-slot.sh static fallback
        return 1

    # STDOUT protocol (cc-slot.sh parses by line):
    #   line 1: action  (ALLOW | RECLAIM | DENY)
    #   line 2: human-facing message (echoed to the operator)
    #   line 3: machine reason tag (ok|emergency_slot|cap_reached|cap_full|oom_floor)
    #           — lets the shell distinguish "trade a slot" (cap_full) from
    #           "genuine RAM exhaustion, nothing to trade" (oom_floor) on RECLAIM.
    print(decision.action)
    print(decision.message)
    print(decision.reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
