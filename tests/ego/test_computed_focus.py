"""Tests for system-computed ego focus summary."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.ego.computed_focus import compute_focus_summary


@pytest.fixture
async def db():
    """In-memory DB with ego tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    # Minimal schema for the tables compute_focus_summary queries
    await conn.execute("""
        CREATE TABLE ego_directives (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            ego_target TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT,
            resolution TEXT,
            kind TEXT NOT NULL DEFAULT 'directive'
        )
    """)
    await conn.execute("""
        CREATE TABLE ego_proposals (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            rank INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT,
            user_response TEXT
        )
    """)
    # Canonical schema (not a hand-rolled subset): compute_focus_summary
    # filters on columns like origin, so a drifted inline DDL breaks silently
    # behind the function's fallback path.
    from genesis.db.schema import TABLES

    await conn.execute(TABLES["user_goals"])
    await conn.commit()
    yield conn
    await conn.close()


async def test_empty_db_returns_fallback(db):
    """No directives, proposals, or goals → fallback message."""
    result = await compute_focus_summary(db, "ego_focus_summary")
    assert result == "no active directives or proposals"


async def test_with_directives(db):
    """Active directives appear in computed focus."""
    await db.execute(
        "INSERT INTO ego_directives (id, content, ego_target) "
        "VALUES ('d1', 'Propose Suki CDP application dispatch', 'user_ego')"
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "1 directive(s)" in result
    assert "Suki" in result


async def test_with_board_proposals(db):
    """Pending proposals appear in computed focus."""
    await db.execute(
        "INSERT INTO ego_proposals (id, content, status) "
        "VALUES ('p1', 'Publish 5th Medium article on stateless agents', 'pending')"
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "1 pending proposal(s)" in result
    assert "Medium" in result


async def test_with_goals(db):
    """User goals appear for user ego but not genesis ego."""
    await db.execute(
        "INSERT INTO user_goals (id, title, category, status, priority) "
        "VALUES ('g1', 'Suki AI application', 'career', 'active', 'high')"
    )
    await db.commit()

    # User ego sees goals
    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "goals:" in result
    assert "Suki" in result

    # Genesis ego does NOT see goals
    result = await compute_focus_summary(db, "genesis_ego_focus_summary")
    assert "goals:" not in result


async def test_approved_proposals(db):
    """Approved proposals awaiting dispatch appear."""
    await db.execute(
        "INSERT INTO ego_proposals (id, content, status) "
        "VALUES ('p1', 'Deploy monitoring fix', 'approved')"
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "1 approved awaiting dispatch" in result


async def test_combined_state(db):
    """Multiple state elements combined with semicolons."""
    await db.execute(
        "INSERT INTO ego_directives (id, content, ego_target) "
        "VALUES ('d1', 'Apply to Suki AI', 'user_ego')"
    )
    await db.execute(
        "INSERT INTO ego_proposals (id, content, status) "
        "VALUES ('p1', 'Research infrastructure costs', 'pending')"
    )
    await db.execute(
        "INSERT INTO user_goals (id, title, category, status, priority) "
        "VALUES ('g1', 'Panel preparation', 'project', 'active', 'high')"
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "directive" in result
    assert "proposal" in result
    assert "goals:" in result
    # Parts joined by semicolons
    assert ";" in result


async def test_db_error_returns_fallback(db):
    """DB errors produce graceful fallback, not exception."""
    await db.close()  # Break the connection

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert result == "general system awareness"


# -- Word-boundary clipping ---------------------------------------------------
#
# The focus text feeds both egos' prompts, the dashboard and the essential
# knowledge block. Each field used to be a raw slice, which cut words in half
# ("nothing leaves Gen"). Fixtures below are synthetic.

_LONG_DIRECTIVE = (
    "Keep every outbound message inside the approval queue so nothing leaves "
    "Genesis without an explicit yes"
)
_LONG_PROPOSAL = (
    "Rewrite the onboarding guide around the three most common install paths "
    "and their recovery steps. Then measure it."
)
_LONG_GOAL = "Finish the long-running documentation overhaul for contributors"


def _segment(result: str, label: str) -> str:
    """The text after ``label`` up to the end of that part."""
    return result.split(label, 1)[1].split("; ", 1)[0]


async def test_long_directive_ends_at_a_word_boundary(db):
    await db.execute(
        "INSERT INTO ego_directives (id, content, ego_target) VALUES (?, ?, 'user_ego')",
        ("d1", _LONG_DIRECTIVE),
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    shown = _segment(result, "1 directive(s): ")
    assert shown.endswith("…")
    assert len(shown) <= 60
    body = shown[:-1]
    assert set(body.split()) <= set(_LONG_DIRECTIVE.split())


async def test_long_proposal_uses_its_first_sentence_clipped(db):
    await db.execute(
        "INSERT INTO ego_proposals (id, content, status) VALUES (?, ?, 'pending')",
        ("p1", _LONG_PROPOSAL),
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    shown = _segment(result, "1 pending proposal(s): ")
    assert shown.endswith("…")
    assert len(shown) <= 40
    assert set(shown[:-1].split()) <= set(_LONG_PROPOSAL.split(".")[0].split())


async def test_short_proposal_keeps_its_first_sentence_without_the_dot(db):
    await db.execute(
        "INSERT INTO ego_proposals (id, content, status) VALUES (?, ?, 'pending')",
        ("p1", "Ship v3.0 docs. Then rest."),
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    # A version number is not a sentence end, and no marker without a cut.
    assert result.startswith("1 pending proposal(s): Ship v3.0 docs")
    assert "…" not in result
    assert "docs." not in result


async def test_long_goal_title_ends_at_a_word_boundary(db):
    await db.execute(
        "INSERT INTO user_goals (id, title, category, status, priority) "
        "VALUES ('g1', ?, 'project', 'active', 'high')",
        (_LONG_GOAL,),
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    shown = _segment(result, "goals: ")
    assert shown.endswith("…")
    assert len(shown) <= 40
    assert set(shown[:-1].split()) <= set(_LONG_GOAL.split())


@pytest.mark.parametrize(
    "content",
    [
        _LONG_DIRECTIVE,
        _LONG_PROPOSAL,
        "Investigate why the quiet hours window drops alerts after midnight",
        "Review the backlog. No proposals were filed last week for voice work",
        "Document the fallow-period behaviour of the scheduler for operators",
        "Holding pattern analysis for the v2.1 release candidates and docs",
    ],
)
async def test_clipped_focus_does_not_newly_trip_the_behavioral_filter(db, content):
    """The essential-knowledge reader drops a focus that matches
    _BEHAVIORAL_FOCUS_RE. Clipping changes the text, so check that ordinary
    topical content still passes the same way the old slices did."""
    from genesis.memory.essential_knowledge import _BEHAVIORAL_FOCUS_RE

    await db.execute(
        "INSERT INTO ego_directives (id, content, ego_target) VALUES (?, ?, 'user_ego')",
        ("d1", content),
    )
    await db.execute(
        "INSERT INTO ego_proposals (id, content, status) VALUES (?, ?, 'pending')",
        ("p1", content),
    )
    await db.execute(
        "INSERT INTO user_goals (id, title, category, status, priority) "
        "VALUES ('g1', ?, 'project', 'active', 'high')",
        (content,),
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    old = _old_focus_shape(content)
    assert bool(_BEHAVIORAL_FOCUS_RE.search(result)) == bool(_BEHAVIORAL_FOCUS_RE.search(old)), (
        result,
        old,
    )


def _old_focus_shape(content: str) -> str:
    """The focus string the pre-clip slices produced for one row of each kind."""
    return (
        f"1 directive(s): {content[:60].split('.')[0]}; "
        f"1 pending proposal(s): {content[:40].split('.')[0]}; "
        f"goals: {content[:40]}"
    )
