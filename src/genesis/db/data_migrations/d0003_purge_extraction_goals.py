"""d0003 — purge extraction-derived garbage rows from ``user_goals``.

The keyword goal-signal matcher (``memory/goal_tracker.py``) was ~95% false
positive — it fired on any conversation snippet containing a goal-ish keyword,
including Genesis's OWN status commentary, and wrote them into ``user_goals``
as ``origin='user'`` directives. It produced ~277 garbage goals before being
disabled at the ``extraction_job.py`` call site (explicit MCP/foreground goal
creation replaced it). The residue still poisons downstream readers: e.g.
``ego/computed_focus.py`` surfaces active user goals into ``ego_focus_summary``,
so a row titled "Migration 0020 applied to add goal_id column to proposals"
leaks a migration-log string into the ego's factual focus line.

This purges the whole extraction-derived cohort. It is safe because the ONLY
writer of ``evidence_source LIKE 'extraction:%'`` goals was that disabled
matcher — legitimate goals created via ``ego_goal_create`` (MCP) or a
foreground session carry ``evidence_source=NULL`` and are never matched. Being
a data migration, it also cleans any peer install still carrying the residue on
its next pull+restart, with no per-install hand-fix.

migrate()/verify() are SYNC (framework contract, cf. d0001/d0002); the runner
offloads via ``asyncio.to_thread``. Own connections only — never the runtime's
async ``rt._db``.
"""

from __future__ import annotations

import sqlite3

from genesis.env import genesis_db_path

requires_operator = False

# The WHERE clause (inlined as a literal in both statements — no interpolation,
# so no injection surface): origin='user' scopes to the user lane (ego-owned
# goals are origin='genesis_ego' and were never written by the matcher);
# evidence_source LIKE 'extraction:%' is the matcher's exclusive signature
# (goal_tracker.py stamps f"extraction:{session_id}") — explicit-creation paths
# (ego_goal_create / foreground) leave evidence_source NULL and are never matched.


def migrate() -> dict:
    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    try:
        cur = db.execute(
            "DELETE FROM user_goals WHERE origin = 'user' AND evidence_source LIKE 'extraction:%'"
        )
        db.commit()
        return {"purged": cur.rowcount}
    finally:
        db.close()


def verify() -> bool:
    """Complete when no extraction-derived user goal remains."""
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM user_goals "
            "WHERE origin = 'user' AND evidence_source LIKE 'extraction:%'"
        ).fetchone()
        return row[0] == 0
    finally:
        db.close()
