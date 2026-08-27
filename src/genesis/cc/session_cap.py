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
- Origin-aware: a direct LAN/tailscale SSH login (the operator "let me in" path,
  distinct from the dashboard/magic-link "normal method") gets an emergency slot
  ABOVE the safe cap and is NEVER hard-denied — when the box is full/tight it is
  offered an interactive RECLAIM (pick a session to end, or reattach), so the
  operator always has a path in without the OOM-killer choosing the casualty.

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
from dataclasses import dataclass

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

        def _int(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                val = int(str(raw).strip())
            except ValueError:
                return default
            # Guard against a nonsensical override that would break the model
            # (e.g. per_session 0 → division error). Fall back to the default.
            return val if val > 0 else default

        return cls(
            system_reserve_mb=_int("GENESIS_CC_SYSTEM_RESERVE_MB", _DEF_SYSTEM_RESERVE_MB),
            per_session_mb=_int("GENESIS_CC_PER_SESSION_MB", _DEF_PER_SESSION_MB),
            oom_floor_mb=_int("GENESIS_CC_OOM_FLOOR_MB", _DEF_OOM_FLOOR_MB),
            # emergency_slots may legitimately be 0 (disable the emergency lever),
            # so it uses a >=0 guard rather than the >0 guard above.
            emergency_slots=max(0, _emergency_from_env(env)),
        )


def _emergency_from_env(env: dict) -> int:
    raw = env.get("GENESIS_CC_EMERGENCY_SLOTS")
    if raw is None or str(raw).strip() == "":
        return _DEF_EMERGENCY_SLOTS
    try:
        return int(str(raw).strip())
    except ValueError:
        return _DEF_EMERGENCY_SLOTS


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

    Returns tailscale/lan for the OPERATOR-at-console (emergency-eligible),
    public for a non-private remote, and none for no SSH (dashboard/manual/local
    "normal method"). Unparseable → public (safe: no emergency, still reattachable
    and still fail-open in the bash caller).
    """
    if not ssh_connection or not ssh_connection.strip():
        return "none"
    client = ssh_connection.split()[0]
    try:
        ip = ipaddress.ip_address(client)
    except ValueError:
        return "public"
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
    ram_ok = mem_available_mb >= config.oom_floor_mb

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
                f"RAM low ({mem_available_mb}MB free, need >= {config.oom_floor_mb}MB "
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
            f"RAM tight ({mem_available_mb}MB free, need >= {config.oom_floor_mb}MB). "
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
        mem_total, mem_available = read_meminfo()
        decision = decide(
            mem_total_mb=mem_total,
            mem_available_mb=mem_available,
            existing=args.existing,
            cpu_count=os.cpu_count() or 1,
            ssh_connection=os.environ.get("SSH_CONNECTION"),
            config=CapConfig.from_env(),
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
