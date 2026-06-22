"""Add kind / domain / goal_id columns to follow_ups (four-axis foundation).

Adds the orthogonal axes the overloaded ``strategy`` enum was missing:
  - ``kind``    : ``follow_up`` | ``tabled``  (tabled = tracked-but-not-for-action)
  - ``domain``  : ``internal`` | ``user_world`` (Genesis-COO vs user-CEO
                  jurisdiction; nullable — NULL means "not yet classified")
  - ``goal_id`` : link target for future follow-up -> goal promotion (nullable)

``strategy`` and the surplus dispatcher are untouched; these axes are additive.

``kind`` backfills to ``follow_up`` automatically via the column DEFAULT.

``domain`` backfill is conservative / NULL-on-uncertainty:
  * ``recon_pipeline`` source -> internal (model benchmarking; single producer)
  * ``inbox_evaluation`` -> the evaluator's own per-item judgment, recovered from
    the ``[ACTION]`` tag stored in ``content``: ADAPT/WATCH/IGNORE (genesis vocab)
    -> internal; EXPLORE/BOOKMARK (user vocab) -> user_world; ADOPT (ambiguous,
    both vocabs) -> NULL
  * else an internal-keyword hit on ``content+reason`` -> internal
  * otherwise leave NULL — never guess ``user_world``. A wrong domain would hide
    a row from the ego that needs it once consumers are domain-scoped; NULL keeps
    it visible to both egos transitionally and reclassifiable in the cockpit.

Self-contained: a migration is a frozen artifact and must NOT import
``genesis.ego`` (the live classifier evolves independently). The keyword set is
copied here intentionally — a snapshot as of migration 0034.
"""

from __future__ import annotations

import re

import aiosqlite

# Frozen snapshot of genesis.ego.domain_classifier.GENESIS_INTERNAL_KEYWORDS.
_INTERNAL_KEYWORDS = frozenset({
    "surplus", "dream cycle", "dream_cycle", "genesis runtime",
    "routing config", "circuit breaker", "guardian", "sentinel", "qdrant",
    "awareness loop", "health check", "dead letter", "model_routing",
    "worktree", "genesis-development", "dashboard fix", "ego cycle",
    "model eval", "surplus_task", "provider fallback", "watchdog",
    "systemd", "genesis server", "eval batch", "j9 eval",
    "runtime init", "embedding chain", "embedding fallback",
})

# Inbox evaluator's own per-item judgment, recovered from the [ACTION] tag.
# Genesis vocab -> internal; user vocab -> user_world. ADOPT is in BOTH vocabs
# (case is lost once stored uppercase in content) so it stays uncertain -> NULL.
_INBOX_INTERNAL_ACTIONS = {"ADAPT", "WATCH", "IGNORE"}
_INBOX_USER_ACTIONS = {"EXPLORE", "BOOKMARK"}
_ACTION_RE = re.compile(r"^\[([A-Z_]+)\]")


def _classify_domain(source: str, content: str | None, reason: str | None) -> str | None:
    """Return 'internal' | 'user_world' | None for an existing row."""
    if source == "recon_pipeline":
        return "internal"
    if source == "inbox_evaluation":
        m = _ACTION_RE.match(content or "")
        act = m.group(1) if m else None
        if act in _INBOX_INTERNAL_ACTIONS:
            return "internal"
        if act in _INBOX_USER_ACTIONS:
            return "user_world"
        # ADOPT / unparseable -> fall through to the keyword check
    text = f"{content or ''} {reason or ''}".lower()
    if any(kw in text for kw in _INTERNAL_KEYWORDS):
        return "internal"
    return None


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='follow_ups'"
    )
    if not await cursor.fetchone():
        return

    col_cursor = await db.execute("PRAGMA table_info(follow_ups)")
    cols = {row[1] for row in await col_cursor.fetchall()}

    if "kind" not in cols:
        await db.execute(
            "ALTER TABLE follow_ups ADD COLUMN kind TEXT NOT NULL "
            "DEFAULT 'follow_up' CHECK (kind IN ('follow_up', 'tabled'))"
        )
    if "domain" not in cols:
        await db.execute(
            "ALTER TABLE follow_ups ADD COLUMN domain TEXT "
            "CHECK (domain IN ('internal', 'user_world'))"
        )
    if "goal_id" not in cols:
        await db.execute("ALTER TABLE follow_ups ADD COLUMN goal_id TEXT")

    # Backfill domain (NULL-on-uncertainty) for rows not yet classified.
    cur = await db.execute(
        "SELECT id, source, content, reason FROM follow_ups WHERE domain IS NULL"
    )
    updates: list[tuple[str, str]] = []
    for row in await cur.fetchall():
        dom = _classify_domain(row[1], row[2], row[3])
        if dom is not None:
            updates.append((dom, row[0]))
    if updates:
        await db.executemany(
            "UPDATE follow_ups SET domain = ? WHERE id = ?", updates
        )
