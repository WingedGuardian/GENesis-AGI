"""Tests for the session-charter MCP tools (living fields + ledger).

Invariant under test at the tool layer: origin is not addressable (there is
no parameter that can reach origin_prompt/origin_ts), stubs precede the
first compaction, and every mutation regenerates the charter.md mirror.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.db.crud import session_charters as crud
from genesis.mcp.health import session_charter_tools as tools

pytestmark = pytest.mark.asyncio

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_SESSIONS_DIR", tmp_path)
    return tmp_path


async def test_charter_update_creates_stub_and_sets_mission(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_charter_update(SID, mission="Ship PR-2a")
    assert res.get("updated") == ["mission"], res
    row = await crud.get(db, SID)
    assert row["mission"] == "Ship PR-2a"
    assert row["origin_prompt"] is None  # stub — the hook fills origin later
    md = (sessions_dir / SID / "charter.md").read_text()
    assert "Ship PR-2a" in md


async def test_charter_update_pointer_add_remove_dedup(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        await tools._impl_session_charter_update(SID, add_pointer="a.md")
        await tools._impl_session_charter_update(SID, add_pointer="a.md")
        res = await tools._impl_session_charter_update(SID, add_pointer="b.md")
        assert res["pointers"] == ["a.md", "b.md"]
        res = await tools._impl_session_charter_update(SID, remove_pointer="a.md")
        assert res["pointers"] == ["b.md"]


async def test_charter_update_nothing_to_do(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_charter_update(SID)
    assert "error" in res


async def test_ledger_add_auto_added_by(db, sessions_dir, monkeypatch):
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID, "build the modal")
    item = await crud.get_ledger_item(db, res["id"])
    assert item["added_by"] == "foreground"
    assert res["open_items"] == 1

    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    # Explicit, not accidental: this asserts the CROSS-session ambient write, so
    # the caller's own id must differ from the target. Left to the ambient
    # environment it passes only by luck.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "99998888-7777-6666-5555-444433332222")
    with patch.object(tools, "_get_db", return_value=db):
        res2 = await tools._impl_session_ledger_add(SID, "dispatched item")
    item2 = await crud.get_ledger_item(db, res2["id"])
    assert item2["added_by"] == "ambient"


async def test_ledger_add_explicit_invalid_added_by(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID, "x", added_by="martian")
    assert "error" in res
    assert "added_by" in res["error"]


async def test_ledger_add_writes_mirror(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        await tools._impl_session_ledger_add(SID, "durable agreement")
    md = (sessions_dir / SID / "charter.md").read_text()
    assert "- [ ] durable agreement" in md


async def test_ledger_update_status_and_mirror(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        created = await tools._impl_session_ledger_add(SID, "close me")
        res = await tools._impl_session_ledger_update(
            created["id"], status="done", evidence="PR #1234"
        )
    assert res["status"] == "done"
    assert res["evidence"] == "PR #1234"
    md = (sessions_dir / SID / "charter.md").read_text()
    assert "- [x] close me" in md


async def test_ledger_update_invalid_status(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        created = await tools._impl_session_ledger_add(SID, "x")
        res = await tools._impl_session_ledger_update(created["id"], status="finished")
    assert "error" in res
    assert "status" in res["error"]


async def test_ledger_update_unknown_id(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_update("deadbeef", status="done")
    assert "error" in res


async def test_charter_read_with_truncated_sid(db, sessions_dir):
    await crud.import_charter(db, session_id=SID, origin_prompt="the original ask", origin_ts="t0")
    with patch.object(tools, "_get_db", return_value=db):
        await tools._impl_session_ledger_add(SID, "one open thing")
        res = await tools._impl_session_charter(SID[:8])
    assert res["session_id"] == SID
    assert res["origin_prompt"] == "the original ask"
    assert res["ledger_counts"] == {"open": 1}
    assert res["ledger"][0]["text"] == "one open thing"


async def test_charter_read_missing(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_charter("nope-1234")
    assert "error" in res


async def test_all_tools_error_when_db_unavailable(sessions_dir):
    with patch.object(tools, "_get_db", return_value=None):
        for res in [
            await tools._impl_session_charter(SID),
            await tools._impl_session_charter_update(SID, mission="m"),
            await tools._impl_session_ledger_add(SID, "x"),
            await tools._impl_session_ledger_update("id1", status="done"),
        ]:
            assert res == {"error": "Database not available"}


async def test_empty_session_id_rejected(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        assert "error" in await tools._impl_session_charter("  ")
        assert "error" in await tools._impl_session_charter_update(" ", mission="m")
        assert "error" in await tools._impl_session_ledger_add("", "x")


async def test_writes_refuse_unresolved_short_id(db, sessions_dir):
    """A truncated id that resolves nowhere must NOT create a stub — rows
    under a short prefix are orphaned when the hook writes the full id
    (Codex P2, PR #1053)."""
    with patch.object(tools, "_get_db", return_value=db):
        res1 = await tools._impl_session_charter_update("deadbeef", mission="m")
        res2 = await tools._impl_session_ledger_add("deadbeef", "x")
    assert "did not resolve" in res1["error"]
    assert "did not resolve" in res2["error"]
    assert await crud.get(db, "deadbeef") is None  # no stub created


async def test_prefix_resolves_via_cc_sessions_before_first_compaction(db, sessions_dir):
    """Pre-compaction (no charter row), the [Session: xxxxxxxx] prefix must
    resolve through cc_sessions.cc_session_id so the stub lands under the
    FULL id the hook will later write."""
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, started_at,"
        " last_activity_at, cc_session_id)"
        " VALUES ('g-1', 'foreground', 'test-model',"
        " '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00', ?)",
        (SID,),
    )
    await db.commit()
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID[:8], "captured via prefix")
    assert res["session_id"] == SID
    assert await crud.get(db, SID) is not None
    assert await crud.get(db, SID[:8]) is None


# --- Dispatched self-write refusal (foreground-only charter) ----------------
# A dispatched session (GENESIS_CC_SESSION=1) writing to its OWN charter
# produces a row nothing will ever re-inject: the SessionStart emission block
# and the PreCompact maintainer both skip that session class. Measured
# 2026-09-02 — a Telegram DM session wrote a ledger row and promised the user
# it would survive to Friday.

OTHER_SID = "11112222-3333-4444-5555-666677778888"


async def test_dispatched_self_write_refused_and_creates_nothing(
    db, sessions_dir, monkeypatch
):
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    with patch.object(tools, "_get_db", return_value=db):
        res1 = await tools._impl_session_ledger_add(SID, "survives to Friday")
        res2 = await tools._impl_session_charter_update(SID, mission="m")
    assert "never re-injected" in res1["error"].lower(), res1
    assert "never re-injected" in res2["error"].lower(), res2
    # The refusal must precede every write — no stub, no ledger row, no mirror.
    assert await crud.get(db, SID) is None
    assert await crud.ledger_counts(db, SID) == {}
    assert not (sessions_dir / SID).exists()


async def test_dispatched_write_to_another_session_still_allowed(
    db, sessions_dir, monkeypatch
):
    """The gate is NARROW on purpose: `added_by='ambient'` exists so a
    dispatched session can contribute to a FOREGROUND session's charter, and
    that row IS re-injected for its owner. Only the self-write is inert."""
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OTHER_SID)
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID, "ambient contribution")
    assert "error" not in res, res
    item = await crud.get_ledger_item(db, res["id"])
    assert item["added_by"] == "ambient"


async def test_foreground_self_write_allowed(db, sessions_dir, monkeypatch):
    """Both conditions are required — an interactive session writing to its own
    charter is the primary supported case and must never be refused."""
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID, "foreground item")
    assert "error" not in res, res
    # The row must actually LAND — absence of an error is also true of a tool
    # that silently did nothing.
    assert await crud.get_ledger_item(db, res["id"]) is not None
    assert "every post-compaction window" in res["message"], res


async def test_dispatched_self_write_refused_via_short_prefix(
    db, sessions_dir, monkeypatch
):
    """The guard runs AFTER id resolution, so the 8-char prefix form the
    session sees in its own [Session: xxxxxxxx] tag is refused too."""
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, started_at,"
        " last_activity_at, cc_session_id)"
        " VALUES ('g-tg', 'foreground', 'sonnet',"
        " '2026-09-02T00:00:00+00:00', '2026-09-02T00:00:00+00:00', ?)",
        (SID,),
    )
    await db.commit()
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID[:8], "prefix form")
    assert "never re-injected" in res["error"].lower(), res
    assert await crud.get(db, SID) is None


async def test_gate_fails_open_without_a_known_session_id(
    db, sessions_dir, monkeypatch
):
    """Truthfulness gate, not a security boundary: with no id to compare, it
    degrades to the previous behaviour rather than refusing legitimate work.

    NOTE this test cannot fail if the gate is deleted — it is an INTENT PIN
    against a future fail-CLOSED rewrite, not a guard on the mechanism. The
    branch is near-dead in production (CC sets CLAUDE_CODE_SESSION_ID on every
    stdio-MCP spawn), which is precisely why the intent needs pinning in a test
    rather than left to be re-derived."""
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID, "unknown own id")
    assert "error" not in res, res
    # Failing open must not UPGRADE to a confidently false claim. With no own
    # id, sid may be this very session, so the cross-session wording would be
    # unsound — the message must claim nothing about persistence.
    assert "THAT session's" not in res["message"], res["message"]
    assert "could NOT be verified" in res["message"], res["message"]


async def test_dispatched_cross_session_message_names_the_beneficiary(
    db, sessions_dir, monkeypatch
):
    """The success message is the sentence that produced "survives to Friday".
    On the path this gate deliberately leaves open — dispatched writing to a
    FOREGROUND charter — it must say WHOSE windows the row re-injects into, or
    the natural reading is "mine", and it is not."""
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OTHER_SID)
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID, "ambient contribution")
    assert "error" not in res, res
    msg = res["message"]
    assert SID[:8] in msg, msg
    assert "NOT" in msg and "this one" in msg, msg
    # The unqualified promise must NOT appear on this path.
    assert "re-inject into every post-compaction window" not in msg, msg


async def test_every_charter_tool_description_states_the_foreground_limit():
    """The @mcp.tool descriptions are injected into the system prompt of every
    session holding genesis-health — including dispatched ones. They are what
    INDUCED the bad call: the ledger description promised the row "re-injects
    into every post-compaction window ... no summary can erase it".

    Gating the write while leaving the inducement unqualified just converts a
    silent false promise into refusal + friction, so every description that
    makes a persistence claim must carry the limit.
    """
    from genesis.mcp.health import mcp

    tools_by_name = await mcp.get_tools()
    for name in ("session_charter", "session_charter_update", "session_ledger_add"):
        desc = (tools_by_name[name].description or "")
        assert "FOREGROUND SESSIONS ONLY" in desc, f"{name} description: {desc[:200]}"
        assert "GENESIS_CC_SESSION=1" in desc, name
