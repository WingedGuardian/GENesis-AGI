"""Worktree board — every piece of committed work in flight, and its fate.

This is the human view of what ``scripts/worktree_lifecycle.py`` decided. It
deliberately does NOT re-derive anything: the reaper classifies, writes its
answer to a cache, and this route renders it. A second implementation could
disagree with the first, and a board that disagrees with the reaper is worse
than no board — someone would trust it.

Distinct from the zero-drop detector by design (owner ruling): zero-drop reports
REPO state (unpushed branches, PR-less pushes). This reports WORKTREES — code
someone already wrote, sitting on disk, with a countdown on it.

The refresh endpoint exists because the cache is only as fresh as the daily
timer. It runs the reaper in report-only mode, which mutates nothing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import sys
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint

logger = logging.getLogger(__name__)

BOARD_CACHE = Path.home() / ".genesis" / "worktree-board.json"
TRASH_DIR = Path.home() / ".genesis" / "worktree-trash"
TOMBSTONE_INDEX = Path.home() / ".genesis" / "worktree-tombstones.jsonl"

# MEASURED 2026-09-10 on this install: a full classification of 191 worktrees
# took 48s with the network check and 20s without. 180s is ~3.7x the measured
# worst case — enough headroom for a slower box or a larger tree, and bounded
# because this runs inside a synchronous HTTP request that a browser will not
# wait on forever. The failure mode it guards is a hung `git` or `gh` child.
_REFRESH_TIMEOUT_S = 180

# Human-facing meaning for each state the reaper can assign. Kept here rather
# than in the template so the vocabulary has one home.
_STATE_LABELS = {
    "in_use": "in use — a live process is sitting in it",
    "protected": "protected — locked, mid-operation, or nested",
    "fresh": "fresh — touched recently",
    "at_risk": "at risk — unmerged and going cold",
    "reap_merged": "due for archiving — already in main",
    "reap_unmerged": "due for archiving — NOT in main",
}


def _repo_root() -> Path:
    """The checkout this dashboard serves."""
    return Path(__file__).resolve().parents[4]


def _read_cache() -> dict:
    try:
        raw = json.loads(BOARD_CACHE.read_text())
    except (OSError, ValueError, TypeError):  # JSONDecodeError is a ValueError
        return {}
    return raw if isinstance(raw, dict) else {}


def _trash_summary() -> dict:
    """What the archive holds. Counted, never opened."""
    entries = 0
    archived = 0
    total_bytes = 0
    try:
        for e in TRASH_DIR.iterdir():
            if e.name.startswith(".") or e.name.endswith(".meta.json"):
                continue
            entries += 1
            if e.is_file() and e.name.endswith(".tar.gz"):
                archived += 1
                with contextlib.suppress(OSError):
                    total_bytes += e.stat().st_size
    except OSError:
        return {"entries": 0, "archived": 0, "bytes": 0, "available": False}

    tombstones = 0
    try:
        with TOMBSTONE_INDEX.open(encoding="utf-8") as fh:
            tombstones = sum(1 for line in fh if line.strip())
    except OSError:
        pass

    return {
        "entries": entries,
        "archived": archived,
        "bytes": total_bytes,
        "tombstones": tombstones,
        "available": True,
    }


def _relative_location(path: str) -> str:
    """A worktree's location with the home prefix removed.

    GET /api/genesis/worktrees is unauthenticated by the dashboard's design (the
    blueprint gate exempts /api/, and the mutation gate exempts GET), and the
    dashboard is reachable from any IP. Publishing 191 absolute paths would put
    the OS username and the full directory layout on an open endpoint — a new
    class of data, against the standing rule about home paths that embed a
    username. The parent directory is the part with operational value, so keep
    that and drop the prefix.
    """
    try:
        return str(Path(path).relative_to(Path.home()))
    except (ValueError, OSError, TypeError):
        return Path(str(path)).name


def _payload() -> dict:
    cache = _read_cache()
    raw_rows = cache.get("worktrees")
    raw_rows = raw_rows if isinstance(raw_rows, list) else []
    rows = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        row = dict(r)
        row["location"] = _relative_location(str(row.pop("path", "")))
        rows.append(row)

    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r.get("state", "?"))] = counts.get(str(r.get("state", "?")), 0) + 1

    return {
        "generated_at": cache.get("generated_at"),
        "available": bool(cache),
        "worktrees": rows,
        "counts": counts,
        "total": len(rows),
        "due_for_archiving": sum(1 for r in rows if r.get("action") == "trash"),
        "state_labels": _STATE_LABELS,
        "trash": _trash_summary(),
    }


@blueprint.route("/api/genesis/worktrees")
def worktree_board():
    """The cached board. Fast — one file read, no git."""
    return jsonify(_payload())


@blueprint.route("/api/genesis/worktrees/refresh", methods=["POST"])
def worktree_board_refresh():
    """Recompute the board by running the reaper in REPORT-ONLY mode.

    ``--report-json`` classifies and writes the cache without touching a single
    worktree, so this is safe to expose: there is no argument this endpoint can
    be given that causes a reap. ``--no-network`` is offered because the one
    network call (``gh pr list``) is most of the runtime; skipping it can only
    under-report a branch as unmerged, never the reverse.
    """
    skip_network = bool((request.get_json(silent=True) or {}).get("no_network"))
    script = _repo_root() / "scripts" / "worktree_lifecycle.py"
    if not script.exists():
        return jsonify({"ok": False, "error": f"reaper not found at {script}"}), 500

    cmd = [sys.executable, str(script), "--report-json"]
    if skip_network:
        cmd.append("--no-network")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(_repo_root()),
            timeout=_REFRESH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning("worktree board refresh timed out after %ss", _REFRESH_TIMEOUT_S)
        return jsonify(
            {
                "ok": False,
                "error": f"classification exceeded {_REFRESH_TIMEOUT_S}s",
                **_payload(),
            }
        ), 504
    except OSError as e:  # FileNotFoundError is an OSError
        return jsonify({"ok": False, "error": str(e), **_payload()}), 500

    if proc.returncode != 0:
        return jsonify(
            {
                "ok": False,
                "error": (proc.stderr or "").strip()[:500] or "reaper exited non-zero",
                **_payload(),
            }
        ), 500

    return jsonify({"ok": True, **_payload()})
