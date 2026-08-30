"""Context-injection health: is the SessionStart payload actually reaching the model?

The Claude Code harness persists a hook's stdout above an UNDOCUMENTED size
threshold: the full output goes to a file under the session's
``tool-results/`` directory and the model receives a ~2 KB preview. Nothing
errors. The session simply runs without its identity, charter, and essential
knowledge — and nothing anywhere says so.

MEASURED on this install (2026-08-30): 143 such filings across 58 sessions
before anyone noticed, spanning a MONTH. The threshold also MOVES ACROSS
VERSIONS (it dropped sharply in a CC update mid-window, tripling the filing
rate overnight; whether it can move without a version bump is unverified), so
no budget constant in our hook can be trusted to stay correct indefinitely.
This collector therefore watches the GROUND TRUTH — the harness's own persisted
files — rather than our arithmetic: a fresh ``hook-*-stdout.txt`` IS the
harness saying "I withheld a hook's output from the model", independent of
every assumption in the emitter. It never reads the emitter's constant, which
is what keeps it right when that constant is wrong.

Shape mirrors ``deploy_health.py``: sync filesystem work behind an async
entry, pure facts -> findings derivation, no side effects.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# Our own cap-measurement probe writes deliberately oversized hook output (see
# GENESIS_CTX_PROBE_BYTES in scripts/genesis_session_context.py). Those filings
# are expected artifacts of measuring, not losses — 17 of 27 filings in one
# window were probe runs. Detected by content, because there is nothing else to
# distinguish them by.
_PROBE_SENTINEL = b"PROBE-START"
_PROBE_SNIFF_BYTES = 64

_MARKER_GLOB = "injection_over_budget*.json"

# The projects tree accumulates a directory per session forever, so the scan is
# bounded. Bounded, NOT silently: hitting it sets `scan_truncated` and
# derive_findings says so — a cap that hides what it dropped reads as "all
# clear", which is the failure this collector exists to catch. Generous enough
# that a real incident is never truncated away (one bad day was 27 filings).
_MAX_SCAN = 2_000


def _default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _default_sessions_dir() -> Path:
    return Path.home() / ".genesis" / "sessions"


def _default_legacy_marker_dir() -> Path:
    return Path.home() / ".genesis" / "session_awareness"


@dataclass
class InjectionHealth:
    """Facts gathered from the filesystem; findings derived separately."""

    fresh_filings: list[dict] = field(default_factory=list)  # {path, size, age_h}
    filing_sessions: int = 0
    probe_filings: int = 0  # excluded from findings; reported for honesty
    markers: list[dict] = field(default_factory=list)  # the emitter's own self-reports
    scan_truncated: bool = False  # hit _MAX_SCAN — reported, never silent
    error: str | None = None


def _is_probe_artifact(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return _PROBE_SENTINEL in fh.read(_PROBE_SNIFF_BYTES)
    except OSError:
        return False  # unreadable -> treat as a real filing (fail toward alerting)


def _assert_readable_dir(path: Path) -> str | None:
    """Return an error string if ``path`` is not a readable directory, else None.

    MEASURED (py3.12.3): ``Path.glob`` SWALLOWS PermissionError and returns an
    empty iterator, and a FILE used as a glob base also yields nothing. So a
    glob inside try/except OSError cannot tell "no filings" from "cannot look" —
    which is exactly the silent all-clear this whole watcher exists to kill.
    Only ``iterdir``/``scandir`` raise, so the base must be probed explicitly.
    An ABSENT directory is not an error: a fresh install has no sessions yet.
    """
    if not path.exists():
        return None
    try:
        next(iter(path.iterdir()), None)
    except OSError as exc:
        return f"{path} is not readable: {exc}"
    return None


def _collect_sync(
    projects_dir: Path,
    sessions_dir: Path,
    legacy_marker_dir: Path,
    lookback_hours: float,
    now: float,
) -> InjectionHealth:
    health = InjectionHealth()
    cutoff = now - lookback_hours * 3600

    health.error = _assert_readable_dir(projects_dir)
    if health.error is None:
        try:
            sessions: set[str] = set()
            for scanned, f in enumerate(projects_dir.glob("*/*/tool-results/hook-*-stdout.txt")):
                if scanned >= _MAX_SCAN:
                    health.scan_truncated = True
                    break
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue
                if _is_probe_artifact(f):
                    health.probe_filings += 1
                    continue
                health.fresh_filings.append(
                    {
                        "path": str(f),
                        "size": st.st_size,
                        "age_h": round((now - st.st_mtime) / 3600, 1),
                    }
                )
                sessions.add(f.parent.parent.name)
            health.filing_sessions = len(sessions)
            health.fresh_filings.sort(key=lambda d: d["age_h"])
        except OSError as exc:
            health.error = f"projects dir unreadable mid-scan: {exc}"

    # The emitter's own per-(session, part) self-reports. One file per writer —
    # see _write_marker in the hook for why this is not a single shared dict.
    for base in (sessions_dir, legacy_marker_dir):
        err = _assert_readable_dir(base)
        if err:
            health.error = health.error or err
            continue
        pattern = f"*/{_MARKER_GLOB}" if base == sessions_dir else _MARKER_GLOB
        try:
            for m in base.glob(pattern):
                try:
                    data = json.loads(m.read_text())
                except (OSError, ValueError) as exc:
                    health.markers.append({"part": m.stem, "error": str(exc)})
                    continue
                if isinstance(data, dict):
                    health.markers.append(data)
        except OSError as exc:
            health.error = health.error or f"marker dir unreadable: {exc}"
    return health


def derive_findings(health: InjectionHealth, *, max_listed: int = 5) -> list[str]:
    """Facts -> human-readable findings. Pure; empty list = healthy."""
    findings: list[str] = []
    if health.error:
        findings.append(f"context-injection watcher DEGRADED — {health.error}")
    if health.scan_truncated:
        findings.append(
            f"context-injection watcher scan STOPPED at {_MAX_SCAN} files — this "
            "reading is partial and the real filing count may be higher. Prune "
            "~/.claude/projects/*/*/tool-results/ or raise the cap."
        )
    if health.fresh_filings:
        total = len(health.fresh_filings)
        sizes = ", ".join(
            f"{d['size']} B ({d['age_h']} h ago)" for d in health.fresh_filings[:max_listed]
        )
        more = f" …and {total - max_listed} more" if total > max_listed else ""
        findings.append(
            f"the harness FILED {total} hook output(s) across "
            f"{health.filing_sessions} session(s) — those windows ran WITHOUT the "
            f"filed context (identity/charter/EK): {sizes}{more}. Sessions that "
            "started before a fix keep the old wiring until they RESTART, so this "
            "can persist after a deploy. The persistence threshold is undocumented "
            "and moves between CC versions; treat this as the injection exceeding "
            "the CURRENT cap regardless of what the emitter's budget constant says."
        )
    for m in health.markers:
        if "error" in m:
            findings.append(
                f"an over-budget marker could not be read ({m.get('part', '?')}): {m['error']}"
            )
            continue
        part = m.get("part", "?")
        if part == "wiring":
            findings.append(
                "the SessionStart hook is MIS-WIRED — "
                f"{m.get('reason', 'unknown reason')} (session "
                f"{str(m.get('session_id', ''))[:8]}, {str(m.get('ts', ''))[:16]}). Only "
                "the charter part was emitted; the rest of the context is missing from "
                "that session."
            )
            continue
        findings.append(
            f"the emitter's own self-audit reports part '{part}' OVER BUDGET: "
            f"{m.get('chars', '?')}/{m.get('budget', '?')} chars (session "
            f"{str(m.get('session_id', ''))[:8]}, {str(m.get('ts', ''))[:16]})."
        )
    return findings


async def context_injection(
    *,
    projects_dir: Path | None = None,
    sessions_dir: Path | None = None,
    legacy_marker_dir: Path | None = None,
    lookback_hours: float = 24.0,
    now: float | None = None,
) -> InjectionHealth:
    """Async entry: gather filesystem facts off the event loop."""
    return await asyncio.to_thread(
        _collect_sync,
        projects_dir or _default_projects_dir(),
        sessions_dir or _default_sessions_dir(),
        legacy_marker_dir or _default_legacy_marker_dir(),
        lookback_hours,
        now if now is not None else time.time(),
    )
