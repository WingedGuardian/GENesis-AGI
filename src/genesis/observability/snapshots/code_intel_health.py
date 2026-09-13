"""Code-intel index health: is the code index actually there, or quietly dead?

MEASURED on this install (2026-09-05), which is why this module exists:

  * the main repo's index request was euthanized to ``<hash>.failed.json`` after
    ``index_marker.MAX_ATTEMPTS`` genuine failures — a state that module's own
    docstring describes as "never retried";
  * its database sat as a 164 MB ``home-ubuntu-genesis.db.corrupt`` for two weeks;
  * ``~/.genesis/code-intelligence-runner.log`` carried 35 ``index failed`` lines,
    the last three ``rc=143`` (SIGTERM — killed under memory pressure);
  * and the live server reported exactly two indexed projects: a worktree whose
    root no longer exists, and a 26-node scratch dir.

Nothing surfaced any of it. No awareness check covered code-intel, and the only
``src/`` consumer of the marker system is a WRITER
(``surplus/jobs/gitnexus.py``). So it did not fail silently — it failed LOUDLY
INTO A LOG FILE, which is operationally identical. This is the same generator the
SessionStart-injection watcher was built for, one subsystem over: a failure
recorded where only the machine can see it.

Shape mirrors ``context_injection.py`` deliberately (facts -> pure
``derive_findings`` -> condition-keyed ``alert_identity``), because that module
is the proven template for this exact class.

**Ground truth only.** Every signal here is a file on disk written by the
indexer itself. Nothing asks CBM how it is doing: a tool that has crashed cannot
be trusted to report its own health, and the daemon may not even be running.

**Scope is the CONFIGURED target, deliberately narrow.** Worktrees are created
and destroyed constantly on this install — a live example right now is a
31,431-node index whose worktree root is gone. Alerting on those is how this
alarm would get muted, taking the real signal with it.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# Everything outside the shape a path takes. Marker JSON is written by our own
# runner, so this is defence in depth rather than a known attack path — but the
# value lands in an observation that `memory/provenance.py` stamps first_party
# and `crud/observations.py` stores verbatim, so it is escaped at ingestion like
# every other filesystem-derived value in this package.
_PATH_UNSAFE = re.compile(r"[^\w./~+-]")

#: Where CBM keeps its per-project index databases. `CBM_CACHE_DIR` is honoured
#: because the binary honours it — MEASURED 2026-09-05: setting it relocates the
#: whole cache, which is what makes an isolated probe possible.
_DEFAULT_CACHE = Path.home() / ".cache" / "codebase-memory-mcp"


def _safe_path(value: object) -> str:
    return _PATH_UNSAFE.sub("?", str(value))


#: How long a PENDING index request may wait before its unbuilt index counts as
#: a fault. Deliberately longer than `code_intel_runner.sh`'s own
#: RELAX_AFTER_S (86400s), after which the runner drops its idle gating and
#: indexes regardless of load: this check must not accuse the runner of failing
#: while it is still correctly waiting for a quiet moment. Two of those windows
#: means it has had an unconstrained chance and produced nothing.
_PENDING_GRACE_S = 2 * 86_400

#: How many read failures are listed, and hashed into the alert identity. The
#: sibling context-injection watcher bounds exactly this and says why: one entry
#: per unreadable path, and the whole list goes into the identity, so an
#: unbounded list is unbounded work AND a fresh identity — i.e. a re-page — for
#: every additional unreadable file. The total is always stated, so the bound
#: is an explicit omission rather than a silent cut.
_MAX_LISTED_ERRORS = 3


def index_slug(path: Path) -> str:
    """CBM's on-disk name for a project: the FULL path with ``/`` -> ``-``.

    Verified against a real cache entry rather than assumed:
    ``/home/<user>/tmp/scratch`` -> ``home-<user>-tmp-scratch``.

    That the slug carries the WHOLE path is load-bearing here, not a detail: it
    means indexing ``<repo>/src`` produces a DIFFERENT slug from indexing
    ``<repo>``. A check hardcoded to the repo root would therefore report
    "unusable" forever the moment code-intel is scoped to a subdirectory — a
    permanently-wrong alarm shipped by the very work meant to stop silent
    failures. Hence :func:`collect` takes the target as a parameter.
    """
    return str(path).strip("/").replace("/", "-")


def default_cache_dir() -> Path:
    env = os.environ.get("CBM_CACHE_DIR")
    return Path(env) if env else _DEFAULT_CACHE


def default_marker_dir() -> Path:
    """The indexer's request queue. Resolved through the marker helper that
    WRITES it, so this cannot drift from the producer."""
    base = os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis")
    return Path(base) / "index-requests"


@dataclass
class CodeIntelHealth:
    """Facts from disk; findings derived separately."""

    #: Index requests the runner permanently gave up on (``*.failed.json``).
    euthanized: list[dict] = field(default_factory=list)
    #: ``ok`` | ``absent`` | ``corrupt`` — state of the CONFIGURED target's index.
    index_state: str = "ok"
    #: The target this reading is about, escaped.
    target: str = ""
    #: Whether anything ever ASKED for this target to be indexed. Absence of an
    #: index only means something when an index was requested — a fresh clone
    #: has neither, and that is correct, not a fault.
    requested: bool = False
    #: Whether a request for this target reached a TERMINAL state (euthanized).
    #: `requested` alone cannot carry the alarm: `install.sh` and
    #: `setup_claude_config.py` both queue a marker at setup, so EVERY fresh
    #: install has a pending request within seconds of existing. Treating that
    #: as "something asked and the index is missing" fires a high alert on a
    #: perfectly healthy new clone — the permanently-wrong-alarm class this
    #: module exists to prevent, committed by the module itself.
    requested_terminal: bool = False
    #: Age in seconds of the OLDEST pending request naming this target, or None.
    #: A pending request is work queued, not work failed: the runner is
    #: idle-gated and only relaxes after RELAX_AFTER_S (86400s), so a young
    #: pending request plus no index is the normal in-progress state.
    pending_age_s: float | None = None
    #: Reads that FAILED. A check that cannot look must never read as all-clear.
    errors: list[str] = field(default_factory=list)


def collect(
    *,
    indexed_path: Path,
    marker_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> CodeIntelHealth:
    """Read the indexer's own artifacts. Never raises; failures are recorded."""
    health = CodeIntelHealth(target=_safe_path(indexed_path))
    markers = marker_dir if marker_dir is not None else default_marker_dir()
    cache = cache_dir if cache_dir is not None else default_cache_dir()
    target_slug = index_slug(indexed_path)

    # ── euthanized requests ───────────────────────────────────────────────
    entries: list[Path] = []
    try:
        if markers.exists():
            entries = sorted(markers.glob("*.failed.json"))
            # `glob` SWALLOWS a traversal OSError and yields nothing, so an
            # unreadable dir is indistinguishable from an empty one. Probe it —
            # inside the SAME try, so a permission change between the exists()
            # and the probe cannot raise out of collect(). `with`, because a
            # bare scandir leaks its iterator's fd on every hourly tick.
            with os.scandir(markers) as it:
                next(iter(it), None)
    except OSError as exc:
        health.errors.append(f"{_safe_path(markers)} is not readable: {exc.strerror}")
        entries = []

    def _marker_data(entry: Path) -> dict | None:
        """This marker's payload IF it names our target, else None.

        Returns None silently for another repo's marker; records an error for
        anything unreadable or structurally wrong, because a marker we cannot
        classify must not quietly pass as "not ours".
        """
        try:
            data = json.loads(entry.read_text())
        except OSError as exc:
            # `exc` is NOT interpolated: OSError.__str__ embeds the raw
            # filename, which would carry an UNESCAPED path into a first_party
            # observation right beside its escaped twin.
            health.errors.append(
                f"{_safe_path(entry)} could not be read: {_safe_path(exc.strerror)}"
            )
            return None
        except ValueError:
            health.errors.append(f"{_safe_path(entry)} is not valid JSON")
            return None
        if not isinstance(data, dict):
            # `json.loads` happily returns a list/str/int, and `.get` on those
            # raises AttributeError straight OUT of collect() — past both except
            # clauses into the caller's bare `except Exception: logger.warning`.
            # That is this module's own failure mode applied to itself: no
            # alert, no error entry, one log line nobody reads.
            health.errors.append(f"{_safe_path(entry)} is not a JSON object")
            return None
        raw = str(data.get("repo_path", ""))
        return data if index_slug(Path(raw)) == target_slug else None

    # The live index's mtime, read BEFORE the marker loops because a tombstone
    # older than a working index is history, not a live fault. `.failed.json` is
    # written once and NEVER deleted — enumerated, not spot-checked: the only
    # writer is index_marker's euthanize path, and no unlink/rm of it exists
    # anywhere in the repo. Meanwhile the request is recreated routinely (the
    # post-commit hook, the twice-weekly gitnexus reindex, disk_reclaim), each
    # time resetting attempts to 0. So a tombstone survives the successful
    # rebuild that answered it, and keying the alarm on its mere existence is
    # the SAME defect already fixed for `.db.corrupt` forty lines below —
    # third instance of one class.
    db_mtime: float | None = None
    try:
        db_mtime = (cache / f"{target_slug}.db").stat().st_mtime
    except OSError:
        db_mtime = None  # absent or unreadable; the index block below records it

    for entry in entries:
        data = _marker_data(entry)
        if data is None:
            continue
        health.requested = True
        try:
            if db_mtime is not None and entry.stat().st_mtime < db_mtime:
                # A later request succeeded and rebuilt the index after this
                # tombstone was written. Superseded — say nothing.
                continue
        except OSError as exc:
            health.errors.append(
                f"{_safe_path(entry)} could not be stat'd: {_safe_path(exc.strerror)}"
            )
        health.requested_terminal = True
        health.euthanized.append(
            {
                "repo_path": _safe_path(data.get("repo_path", "")),
                # `attempts` is escaped too: it is interpolated into the finding
                # text, so an unescaped newline forges a finding line exactly as
                # a path would.
                "attempts": _safe_path(data.get("attempts", "?")),
            }
        )

    # A PENDING (not yet euthanized) request also proves the target was ASKED
    # for — which is what makes an absent index meaningful rather than fresh.
    # Scoped by READING each marker: filenames are opaque hashes carrying no
    # repo identity, so an `any(*.json)` test would accept ANOTHER repo's
    # pending request — or a `.tmp-` file from index_marker's atomic write — as
    # proof that OUR target was requested.
    try:
        pending = [
            p
            for p in markers.iterdir()
            if p.name.endswith(".json")
            and not p.name.endswith(".failed.json")
            and not p.name.startswith(".tmp-")
        ]
    except OSError:
        pending = []  # an unreadable dir is already recorded above
    now = time.time()
    for p in pending:
        data = _marker_data(p)
        if data is None:
            continue
        health.requested = True
        # `requested_at` is the marker's own field, preserved across coalescing
        # as the EARLIEST request (index_marker.write_marker), which is exactly
        # the clock we want: how long this target has been waiting, not when it
        # was last touched. A malformed value must not read as "waiting
        # forever", so an unusable one degrades to age 0 (in progress).
        try:
            age = now - float(data.get("requested_at", now))
        except (TypeError, ValueError):
            age = 0.0
        age = max(age, 0.0)
        health.pending_age_s = (
            age if health.pending_age_s is None else max(health.pending_age_s, age)
        )

    # ── the configured target's index ─────────────────────────────────────
    try:
        db = cache / f"{target_slug}.db"
        corrupt = cache / f"{target_slug}.db.corrupt"
        if db.exists():
            # `.corrupt` is the indexer's RETAINED BACKUP of a previously-bad
            # database, NOT a flag meaning "the current index is broken". Its
            # own binary says `backing up corrupt db to .corrupt` and then
            # rebuilds `<slug>.db` in place; nothing ever unlinks the backup
            # (MEASURED: a 164 MB one on this box, retained for two weeks).
            # Treating its mere existence as failure fired the alarm FOREVER
            # after a successful rebuild — a permanently-wrong alarm, which is
            # the exact failure this module exists to prevent, committed by the
            # module itself. Only a backup NEWER than the live db means the
            # CURRENT index is the broken one.
            health.index_state = (
                "corrupt"
                if corrupt.exists() and corrupt.stat().st_mtime > db.stat().st_mtime
                else "ok"
            )
        else:
            # No live db: a backup alone means the rebuild never completed.
            health.index_state = "corrupt" if corrupt.exists() else "absent"
    except OSError as exc:
        health.errors.append(f"{_safe_path(cache)} is not readable: {exc.strerror}")

    return health


def derive_findings(health: CodeIntelHealth) -> list[str]:
    """Facts -> human-readable findings. Pure; empty list = healthy."""
    findings: list[str] = []

    if health.errors:
        listed = health.errors[:_MAX_LISTED_ERRORS]
        omitted = len(health.errors) - len(listed)
        findings.append(
            "code-intel health check DEGRADED — "
            + "; ".join(listed)
            + (f"; and {omitted} more read failures" if omitted else "")
            + ". It could not read everything it watches, so THIS READING CANNOT "
            "BE TREATED AS ALL-CLEAR."
        )

    if health.euthanized:
        named = ", ".join(
            f"{e['repo_path']} (after {e['attempts']} attempts)" for e in health.euthanized
        )
        findings.append(
            f"the code indexer GAVE UP on {named} — the request was euthanized to "
            "a .failed.json marker and is NEVER retried, so the index will stay "
            "stale until someone clears it. Check "
            "~/.genesis/code-intelligence-runner.log for the failure reason "
            "(rc=143 means it was killed, usually under the memory cap)."
        )

    # An absent index is only a fault if the request is DONE ASKING — either it
    # was euthanized, or it has waited past the point where waiting is normal.
    #
    # "Something asked for one" is not enough, and the earlier version of this
    # line proved it: `install.sh` and `setup_claude_config.py` each queue a
    # marker at setup, so `requested` is True within seconds of a fresh clone
    # existing, while the index legitimately takes hours — the runner is
    # idle-gated and only relaxes after RELAX_AFTER_S (86400s), and the first
    # build is a full one. That combination fired a high alert on every healthy
    # new install, under a remedy telling the operator to delete the very
    # pending marker that would eventually build the index. The grace window is
    # deliberately LONGER than the runner's own relax window, so this speaks
    # only once the runner has had its unconstrained chance and still produced
    # nothing.
    absent_is_a_fault = health.index_state == "absent" and (
        health.requested_terminal
        or (health.pending_age_s is not None and health.pending_age_s > _PENDING_GRACE_S)
    )
    if health.index_state == "corrupt" or absent_is_a_fault:
        findings.append(
            f"the code index for {health.target} is {health.index_state.upper()} — "
            "code-intelligence tools that read it are answering from nothing. "
            "Rebuild it, or narrow the indexed path if it no longer fits the "
            "memory cap."
        )

    return findings


def alert_identity(health: CodeIntelHealth) -> str:
    """A stable key over every state :func:`derive_findings` can report.

    Keyed on the CONDITION, never on a tally. Attempt counts and the number of
    dead repos drift while one standing incident sits unfixed, and an alarm that
    re-pages for a condition the operator has already seen is how the channel
    gets muted — which is precisely how this failure survived two weeks.
    """
    # De-duplicated: `index_slug` is many-to-one, so two markers can name paths
    # that slug identically. Letting the same repo appear twice would put a
    # COUNT back into a key whose whole purpose is to carry only the condition.
    repos = ",".join(sorted({e["repo_path"] for e in health.euthanized}))
    return (
        f"target:{health.target}"
        f":index:{health.index_state}"
        f":requested:{'yes' if health.requested else 'no'}"
        f":euthanized:{repos}"
        f":errors:{'|'.join(sorted(health.errors[:_MAX_LISTED_ERRORS]))}"
        f":errcount:{'many' if len(health.errors) > _MAX_LISTED_ERRORS else len(health.errors)}"
    )
