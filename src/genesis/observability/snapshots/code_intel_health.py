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


def index_slug(path: Path) -> str:
    """CBM's on-disk name for a project: the FULL path with ``/`` -> ``-``.

    Verified against a live cache entry rather than assumed:
    ``/home/ubuntu/tmp/kimi_review`` -> ``home-ubuntu-tmp-kimi_review``.

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

    for entry in entries:
        data = _marker_data(entry)
        if data is None:
            continue
        health.requested = True
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
    if any(_marker_data(p) is not None for p in pending):
        health.requested = True

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
        findings.append(
            "code-intel health check DEGRADED — "
            + "; ".join(health.errors)
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

    # An absent index is only a fault if something ASKED for one; a fresh clone
    # legitimately has neither, and alerting there would fire on every install.
    if health.index_state == "corrupt" or (health.index_state == "absent" and health.requested):
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
    repos = ",".join(sorted(e["repo_path"] for e in health.euthanized))
    return (
        f"target:{health.target}"
        f":index:{health.index_state}"
        f":requested:{'yes' if health.requested else 'no'}"
        f":euthanized:{repos}"
        f":errors:{'|'.join(sorted(health.errors))}"
    )
