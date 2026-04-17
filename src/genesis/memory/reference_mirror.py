"""Markdown mirror of the reference store at ~/.genesis/known-to-genesis.md.

Read-only human view. Regenerated on reference_store / reference_delete /
reference_export operations. NOT a source of truth — the database is.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.db.crud import knowledge as knowledge_crud

logger = logging.getLogger(__name__)

_MIRROR_PATH = Path.home() / ".genesis" / "known-to-genesis.md"
_REFERENCE_PROJECT = "reference"

# Map domain keys to display headings (order matters for the output).
_DOMAIN_ORDER = [
    ("reference.credentials", "Credentials"),
    ("reference.url", "URLs"),
    ("reference.network", "Network"),
    ("reference.persona_pointer", "Personas"),
    ("reference.account", "Accounts"),
    ("reference.fact", "Facts"),
]
_DOMAIN_HEADING = dict(_DOMAIN_ORDER)


async def regenerate_mirror(db: aiosqlite.Connection) -> Path:
    """Query the reference store and write the markdown mirror file.

    Returns the path written to. Raises on query failure but absorbs
    file-write errors (caller should wrap in try/except for non-critical
    use sites).
    """
    grouped = await knowledge_crud.list_by_domain(
        db, project_type=_REFERENCE_PROJECT,
    )

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "# Genesis Reference Store",
        f"_Auto-generated. Last updated: {now}. "
        "DO NOT EDIT — changes here are not read back._",
        "",
    ]

    total = 0
    for domain_key, heading in _DOMAIN_ORDER:
        entries = grouped.pop(domain_key, [])
        if not entries:
            continue
        total += len(entries)
        lines.append(f"## {heading}")
        lines.append("")
        for entry in entries:
            lines.append(f"### {entry['concept']}")
            # Body already contains the formatted value, description, tags.
            for body_line in (entry.get("body") or "").splitlines():
                lines.append(f"- {body_line}" if body_line.strip() else "")
            if entry.get("ingested_at"):
                lines.append(f"- **Stored**: {entry['ingested_at'][:10]}")
            lines.append("")

    # Any domains not in _DOMAIN_ORDER (future-proofing).
    for domain_key, entries in sorted(grouped.items()):
        total += len(entries)
        heading = domain_key.removeprefix("reference.").replace("_", " ").title()
        lines.append(f"## {heading}")
        lines.append("")
        for entry in entries:
            lines.append(f"### {entry['concept']}")
            for body_line in (entry.get("body") or "").splitlines():
                lines.append(f"- {body_line}" if body_line.strip() else "")
            if entry.get("ingested_at"):
                lines.append(f"- **Stored**: {entry['ingested_at'][:10]}")
            lines.append("")

    if total == 0:
        lines.append("_No reference entries stored yet._")
        lines.append("")

    _MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MIRROR_PATH.write_text("\n".join(lines))
    logger.info("reference_mirror: wrote %d entries to %s", total, _MIRROR_PATH)
    return _MIRROR_PATH
