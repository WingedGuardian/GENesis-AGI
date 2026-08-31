"""Context-injection health: is a hook's output actually reaching the model?

The Claude Code harness persists a hook's stdout above an UNDOCUMENTED size
threshold: the full output goes to a file under the session's ``tool-results/``
directory and the model receives a ~2 KB preview. Nothing errors. The session
simply runs without whatever that hook was carrying — and nothing anywhere
says so.

MEASURED on this install (2026-08-30): 143 such filings across 58 sessions
before anyone noticed, spanning a MONTH; 195/195 observed filings were
SessionStart, every one of them the identity/charter/EK injection. The
threshold also MOVES ACROSS VERSIONS (it dropped sharply in a CC update
mid-window, tripling the filing rate overnight; whether it can move without a
version bump is unverified), so no budget constant in any hook can be trusted
to stay correct indefinitely.

This collector therefore watches GROUND TRUTH — the harness's own persisted
files — rather than our arithmetic: a fresh ``hook-*-stdout.txt`` IS the
harness saying "I withheld a hook's output from the model", independent of
every assumption in every emitter. It never reads an emitter's constant, which
is what keeps it right when that constant is wrong.

Two things it deliberately does NOT do. It does not filter filings down to the
one hook we fixed — a filed proactive-memory or guard output is also a real
loss, with a different remedy, and reporting only what we recognise would
recreate the silence. And it holds no state about emitters: the per-part
markers this used to read were deleted when the emitters gained a hard
chokepoint, because a marker could only ever record a condition that is now
unreachable, while the two conditions it never covered (the cap moving, another
hook filing) are exactly what this sees.

Shape mirrors ``deploy_health.py``: sync filesystem work behind an async
entry, pure facts -> findings derivation, no side effects.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from genesis.env import repo_root

# Our own cap-measurement probe writes deliberately oversized hook output (see
# GENESIS_CTX_PROBE_BYTES in scripts/genesis_session_context.py). Those filings
# are expected artifacts of measuring, not losses — 17 of 27 filings in one
# window were probe runs. Detected by content, because there is nothing else to
# distinguish them by.
_PROBE_SENTINEL = b"PROBE-START"

#: Label for a filing we cannot attribute to a known producer. Kept as a
#: constant because the ALERT IDENTITY keys on the class (stable) while the
#: alert body carries the head excerpt (informative but per-prompt variable).
OTHER_HOOK = "other hook"

#: How much of the append-only mis-wire log to tail. Generous next to a line
#: (~90 bytes) and constant regardless of how long the file has been growing.
_MISWIRE_TAIL_BYTES = 64_000

#: A slug prefix shorter than this cannot meaningfully scope anything.
_MIN_SLUG_PREFIX = 8

#: How much of a filed file to read when attributing it to a producer.
_HEAD_BYTES = 240

#: The session-context emitter stamps this on the first line of every part, so
#: attribution is a CONTRACT rather than a guess about what a file starts with.
#: Anything else is reported by its head excerpt — never dropped, never assumed.
_PART_STAMP = re.compile(rb"\[genesis-ctx:([\w-]+)")

#: The pre-stamp session-context injection always began with this heading.
#: Kept ONLY so filings from sessions started before the fix are attributed
#: correctly during the restart window; new filings carry the stamp above.
_LEGACY_SESSION_CONTEXT_HEAD = b"## Session Configuration"

# The projects tree accumulates a directory per session forever, so the scan is
# bounded. Bounded, NOT silently: hitting it sets `scan_truncated` and
# derive_findings says so — a cap that hides what it dropped reads as "all
# clear", which is the failure this collector exists to catch. Applied AFTER
# the lookback filter, so a backlog of old files can never crowd out a fresh
# incident (which would be a silent all-clear wearing a truncation notice).
_MAX_FRESH = 2_000


def _default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _default_miswire_log() -> Path:
    return Path.home() / ".genesis" / "session_awareness" / "context_miswire.log"


@dataclass
class InjectionHealth:
    """Facts gathered from the filesystem; findings derived separately."""

    fresh_filings: list[dict] = field(default_factory=list)  # {path, size, age_h, producer}
    filing_sessions: int = 0
    probe_filings: int = 0  # excluded from findings; reported for honesty
    foreign_filings: int = 0  # outside any Genesis checkout; counted, not alerted
    miswires: list[str] = field(default_factory=list)  # fresh mis-wired invocations
    scan_truncated: bool = False  # hit _MAX_FRESH — reported, never silent
    error: str | None = None


def _slug(path: Path) -> str:
    """CC's project-directory slug for a filesystem path.

    VERIFIED against the real directory names CC creates:
    ``/srv/checkout/.claude/worktrees/x`` -> ``-srv-checkout--claude-worktrees-x``
    (every ``/`` and ``.`` becomes ``-``, so a worktree slug is the parent
    checkout's slug plus a suffix — which is what makes prefix matching work).
    """
    return str(path).replace("/", "-").replace(".", "-")


def _main_checkout(path: Path) -> Path:
    """Normalise a worktree path to the main checkout that contains it.

    ``repo_root()`` resolves relative to the importing code, so a process
    running from a worktree reports the WORKTREE as the root — and scoping on
    that alone would silently ignore every filing in the main checkout.
    MEASURED while building this: run from a worktree, the scan scored the 9
    live main-tree filings "foreign" and reported all-clear. Too broad is a
    counted extra; too narrow is the silence this watcher exists to break.
    """
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i : i + 2] == (".claude", "worktrees"):
            return Path(*parts[:i])
    return path


def _genesis_slug_prefixes() -> tuple[str, ...]:
    """Slug prefixes that belong to THIS install.

    Scoping matters: the projects tree holds every project the operator has
    ever opened, and an unscoped watcher pages `critical` because someone's
    unrelated repo filed a hook — an alarm that cries about other people's
    software gets muted, taking ours with it. Derived from paths rather than a
    subprocess (`git worktree list`) so the check stays side-effect-free: every
    Genesis worktree lives under the main checkout or under ~/.genesis, so the
    two prefixes cover all of them, background-sessions included.
    """
    prefixes = {_slug(Path.home() / ".genesis")}
    try:
        prefixes.add(_slug(_main_checkout(repo_root())))
    except Exception:
        prefixes.add(_slug(Path.home() / "genesis"))
    # Drop a degenerate prefix. `repo_root()` honours GENESIS_REPO_ROOT verbatim,
    # so a relative or `/` value can normalise to `Path()` whose slug is "-" —
    # and EVERY CC project slug starts with "-", which would silently widen the
    # scan to every repo the operator has ever opened and page critical about
    # someone else's software.
    return tuple(sorted(p for p in prefixes if len(p) >= _MIN_SLUG_PREFIX))


def producer_class(producer: str) -> str:
    """The STABLE class of a producer label, for alert identity.

    An unrecognised producer's label embeds a head excerpt that varies per
    prompt, so hashing the label would mint a fresh critical alert every tick.
    A helper rather than a string split at the call site: the label wording has
    already changed once, and a caller splitting on a literal drifts silently
    into exactly the churn this prevents.
    """
    return OTHER_HOOK if producer.startswith(OTHER_HOOK) else producer


def _in_scope(slug: str, prefixes: tuple[str, ...]) -> bool:
    """Does ``slug`` name a project inside this install?

    Boundary-aware: a bare ``startswith`` would pull in a sibling whose name
    merely EXTENDS ours (a ``~/.genesis-unrelated`` checkout slugs to
    ``…--genesis-unrelated``, which starts with the ``~/.genesis`` prefix), and
    that project's hook output would then be excerpted into a Genesis alert.
    The slug separator is ``-``, so require an exact match or a separator next.

    LIMIT, stated rather than papered over: CC's slug mapping is lossy — it
    replaces every ``/`` and ``.`` with ``-``, so a SIBLING at ``<root>-name``
    is indistinguishable from a child directory ``<root>/name``. This check
    therefore excludes only a slug that shares the prefix without a separator;
    a same-prefixed sibling still reads as in-scope. That is the safe
    direction: too broad costs a reported filing with a head excerpt (noise),
    too narrow costs silence, which is what this watcher exists to break.
    """
    return any(slug == p or slug.startswith(p + "-") for p in prefixes)


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


def _attribute(path: Path) -> tuple[str, bool]:
    """``(producer, is_probe)`` for a filed hook output, read from its head."""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError:
        return ("unreadable filing", False)  # fail toward alerting
    if _PROBE_SENTINEL in head:
        return ("cap-measurement probe", True)
    stamp = _PART_STAMP.search(head)
    if stamp:
        return (f"session-context part '{stamp.group(1).decode('ascii', 'replace')}'", False)
    if head.startswith(_LEGACY_SESSION_CONTEXT_HEAD):
        # Transitional, and deliberately the ONLY content signature here. Every
        # filing predating the stamp begins with this line (MEASURED: 9 of 9
        # live filings, and every non-probe filing in 387 transcripts), and a
        # session started before the fix keeps emitting the old shape until it
        # restarts — so without this the whole post-deploy window would be
        # attributed to "some other hook" and given the wrong remedy.
        return ("session-context (pre-stamp emitter — session predates the fix)", False)
    raw = head.split(b"\n", 1)[0][:80].decode("utf-8", "replace")
    # SANITISE: this is the first line of a file some OTHER hook wrote, and it
    # ends up in an observation the model later reads and in a Telegram message.
    # Genesis authored the OBSERVATION; it did not author this text, so strip
    # control/ANSI sequences and mark it verbatim-unverified rather than letting
    # a first-party-labelled row carry unframed third-party content.
    excerpt = "".join(c for c in raw if c.isprintable()).strip()
    return (f'{OTHER_HOOK} — verbatim head (unverified): "{excerpt}"', False)


def _read_miswires(path: Path, cutoff: float) -> list[str]:
    """Mis-wire lines newer than ``cutoff``. Append-only file; nothing clears it.

    A mis-wired hook emits only the charter part, which stays UNDER the cap by
    design — so the harness never files it and the filings scan above cannot
    see it. Without this the condition is observable only by a model reading
    its own window, which covers a foreground session and nothing else.
    """
    if not path.exists():
        return []
    fresh: list[str] = []
    try:
        # Tail-read: the log is append-only by design, so reading it whole would
        # grow with the file forever on a check that runs every hour.
        with path.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - _MISWIRE_TAIL_BYTES))
            blob = fh.read().decode("utf-8", errors="replace")
        for line in blob.splitlines()[-200:]:
            ts, _, reason = line.partition("\t")
            try:
                when = datetime.fromisoformat(ts).timestamp()
            except ValueError:
                continue
            if when >= cutoff:
                fresh.append(reason.strip() or "unknown reason")
    except OSError:
        return []
    return fresh


def _collect_sync(
    projects_dir: Path,
    lookback_hours: float,
    now: float,
    miswire_log: Path | None = None,
) -> InjectionHealth:
    health = InjectionHealth()
    cutoff = now - lookback_hours * 3600
    prefixes = _genesis_slug_prefixes()
    health.miswires = _read_miswires(miswire_log or _default_miswire_log(), cutoff)

    health.error = _assert_readable_dir(projects_dir)
    if health.error is not None:
        return health

    try:
        fresh: list[tuple[float, Path, str]] = []
        for f in projects_dir.glob("*/*/tool-results/hook-*-stdout.txt"):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue  # lookback FIRST — the cap must bound fresh files only
            slug = f.parent.parent.parent.name
            if not _in_scope(slug, prefixes):
                health.foreign_filings += 1
                continue
            fresh.append((st.st_mtime, f, slug))

        fresh.sort(key=lambda t: t[0], reverse=True)  # newest first
        if len(fresh) > _MAX_FRESH:
            health.scan_truncated = True
            fresh = fresh[:_MAX_FRESH]

        sessions: set[str] = set()
        for mtime, f, _slug_name in fresh:
            producer, is_probe = _attribute(f)
            if is_probe:
                health.probe_filings += 1
                continue
            health.fresh_filings.append(
                {
                    "path": str(f),
                    "size": st.st_size,
                    "age_h": round((now - mtime) / 3600, 1),
                    "producer": producer,
                }
            )
            sessions.add(f.parent.parent.name)
        health.filing_sessions = len(sessions)
    except OSError as exc:
        health.error = f"projects dir unreadable mid-scan: {exc}"
    return health


def derive_findings(health: InjectionHealth, *, max_listed: int = 5) -> list[str]:
    """Facts -> human-readable findings. Pure; empty list = healthy."""
    findings: list[str] = []
    if health.error:
        findings.append(f"context-injection watcher DEGRADED — {health.error}")
    if health.scan_truncated:
        findings.append(
            f"context-injection watcher stopped after {_MAX_FRESH} FRESH filings — this "
            "reading is partial and the real count may be higher."
        )
    if health.miswires:
        findings.append(
            f"the SessionStart hook was MIS-WIRED {len(health.miswires)} time(s) "
            f"({health.miswires[-1]}) — those sessions got the charter part ONLY, so "
            "identity and essential knowledge were missing from them. Fix the four "
            "`--part` entries in .claude/settings.json and restart those sessions."
        )
    if not health.fresh_filings:
        return findings

    total = len(health.fresh_filings)
    by_producer: dict[str, int] = {}
    for d in health.fresh_filings:
        by_producer[d["producer"]] = by_producer.get(d["producer"], 0) + 1
    listed = ", ".join(
        f"{d['producer']}: {d['size']} B ({d['age_h']} h ago)"
        for d in health.fresh_filings[:max_listed]
    )
    more = f" …and {total - max_listed} more" if total > max_listed else ""
    findings.append(
        f"the harness FILED {total} hook output(s) across {health.filing_sessions} "
        f"session(s) — those windows ran WITHOUT the filed content: {listed}{more}."
    )
    if any(p.startswith("session-context") for p in by_producer):
        findings.append(
            "session-context filings mean the injection crossed the harness cap: "
            "RESTART the affected sessions (a session started before a fix keeps the "
            "old wiring until it does), and if filings continue after a restart the "
            "cap itself has MOVED — re-measure it with the probe seam in "
            "scripts/genesis_session_context.py and re-fit the part budgets."
        )
    if any(not p.startswith(("session-context", "cap-measurement")) for p in by_producer):
        findings.append(
            "filings from other hooks mean THAT hook's contribution was withheld from "
            "the model for those turns (a recall injection, a guard advisory) — bound "
            "the producer's output via scripts/hooks/hook_output.py."
        )
    return findings


async def context_injection(
    *,
    projects_dir: Path | None = None,
    lookback_hours: float = 24.0,
    now: float | None = None,
    miswire_log: Path | None = None,
) -> InjectionHealth:
    """Async entry: gather filesystem facts off the event loop."""
    return await asyncio.to_thread(
        _collect_sync,
        projects_dir or _default_projects_dir(),
        lookback_hours,
        now if now is not None else time.time(),
        miswire_log,
    )
