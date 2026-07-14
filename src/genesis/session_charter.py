"""Session-charter markdown mirror — the canonical renderer.

The charter's canonical store is the DB (``session_charters`` +
``session_ledger``, migration 0058); ``~/.genesis/sessions/<sid>/charter.md``
is the human-readable mirror regenerated after every write. This module is
the runtime-side renderer used by the ledger MCP tools and the backfill
script.

NOTE: ``scripts/genesis_precompact.py`` carries an intentionally duplicated
``_charter_md`` — the PreCompact hook is deliberately stdlib-only (fail-open
under a 5s budget) and must not import the genesis package, and runtime code
must not import from ``scripts/``. A parity test
(tests/test_scripts/test_precompact_charter.py) pins both renderers to
byte-identical output, so drift fails CI immediately.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_STATUS_MARKS = {
    "open": " ",
    "in_progress": "~",
    "done": "x",
    "absorbed": "a",
    "dropped": "d",
}


def charter_md(charter: dict, ledger: list[dict] | None = None) -> str:
    """Render charter.md from a charter row + optional ledger rows.

    Stub rows (origin not yet filled — an MCP write preceded the session's
    first compaction) render with an empty Origin section rather than the
    string "None".
    """
    lines = [
        f"# Session Charter — {charter.get('session_id', '')}",
        "",
        f"- **Born:** {charter.get('origin_ts') or 'unknown'}",
        f"- **Compactions:** {charter.get('compaction_count', 0)}",
        f"- **Charter created:** {charter.get('created_at', '')}",
        "",
        "## Origin (immutable)",
        "",
        str(charter.get("origin_prompt") or ""),
    ]
    mission = charter.get("mission")
    if mission:
        lines += ["", "## Mission", "", str(mission)]
    pointers = charter.get("pointers") or []
    if pointers:
        lines += ["", "## Pointers", ""]
        lines += [f"- {p}" for p in pointers]
    if ledger:
        lines += ["", "## Ledger", ""]
        for item in ledger:
            mark = _STATUS_MARKS.get(str(item.get("status", "open")), " ")
            lines.append(f"- [{mark}] {item.get('text', '')}")
    return "\n".join(lines) + "\n"


def write_charter_md(
    sessions_dir: Path,
    session_id: str,
    charter: dict,
    ledger: list[dict] | None = None,
) -> None:
    """Best-effort mirror write: the DB is canonical, a failed mirror only
    means charter.md goes stale until the next write regenerates it."""
    try:
        session_dir = sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "charter.md").write_text(charter_md(charter, ledger), encoding="utf-8")
    except OSError as exc:
        logger.warning("charter.md mirror write failed for %s: %s", session_id, exc)
