"""WS-3 B1 gate-1 (procedure) — origin derivation + shadow emit contract.

Pure-unit coverage of the tool-name origin derivation (``origin_from_tool_names``
+ its spine projection ``derive_session_origin``), plus an integration check that
``record_would_block(gate="procedure", ...)`` honors the never-block invariant
and the live kill switch under the REAL ``config/ws3_immunity.yaml``.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import immunity_shadow as crud
from genesis.db.migrations.runner import MigrationRunner
from genesis.learning.procedural.struggle_detector import derive_session_origin
from genesis.memory.provenance import (
    ORIGIN_EXTERNAL_UNTRUSTED,
    ORIGIN_FIRST_PARTY,
    ORIGIN_OWNER,
    origin_from_tool_names,
)
from genesis.security import immunity, immunity_shadow


@pytest.fixture(autouse=True)
def _reset_caches():
    crud._table_verified = False
    crud._table_verified_sync = False
    yield
    crud._table_verified = False
    crud._table_verified_sync = False


# ── origin_from_tool_names (pure) ──────────────────────────────────────────


def test_origin_external_on_cc_builtin_web():
    assert origin_from_tool_names(["Read", "Edit", "WebFetch"]) == ORIGIN_EXTERNAL_UNTRUSTED


def test_origin_external_on_namespaced_mcp_tool():
    # MCP names arrive namespaced; matched on the final __ segment.
    assert (
        origin_from_tool_names(["Bash", "mcp__genesis-health__web_search"])
        == ORIGIN_EXTERNAL_UNTRUSTED
    )


def test_origin_external_on_knowledge_recall():
    assert (
        origin_from_tool_names(["mcp__genesis-memory__knowledge_recall"])
        == ORIGIN_EXTERNAL_UNTRUSTED
    )


def test_origin_first_party_on_internal_only():
    # memory_store is a first-party WRITE, not an external ingest — must not flip.
    assert (
        origin_from_tool_names(["Read", "Edit", "Bash", "mcp__genesis-memory__memory_store"])
        == ORIGIN_FIRST_PARTY
    )


def test_origin_first_party_on_empty_and_none():
    assert origin_from_tool_names([]) == ORIGIN_FIRST_PARTY
    assert origin_from_tool_names([None, ""]) == ORIGIN_FIRST_PARTY


# ── derive_session_origin (spine projection) ───────────────────────────────


def _spine(*tools):
    return [{"type": "tool", "tool": t, "args_summary": ""} for t in tools]


def test_derive_session_origin_external():
    spine = _spine("Read", "WebSearch")
    spine.append({"type": "user", "tool": None, "args_summary": "hi"})
    assert derive_session_origin(spine) == ORIGIN_EXTERNAL_UNTRUSTED


def test_derive_session_origin_first_party():
    assert derive_session_origin(_spine("Read", "Edit", "Bash")) == ORIGIN_FIRST_PARTY


def test_derive_session_origin_empty():
    assert derive_session_origin([]) == ORIGIN_FIRST_PARTY


def test_derive_session_origin_ignores_user_turns():
    # A user turn whose text mentions a tool name must not flip origin — only
    # actual tool entries (type == "tool") are scanned.
    spine = [{"type": "user", "tool": None, "args_summary": "please WebFetch it"}]
    assert derive_session_origin(spine) == ORIGIN_FIRST_PARTY


# ── emit contract (real migration + real config) ───────────────────────────


async def _migrated(path):
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await MigrationRunner(db).run_pending()
    return db


@pytest.mark.asyncio
async def test_procedure_gate_records_external_never_firstparty_or_owner(tmp_path):
    """gate='procedure' under the REAL config: external → a row; first_party and
    owner → never a row (never-block invariant)."""
    mode = immunity.gate_mode("procedure")
    assert mode in ("off", "shadow", "enforce")
    db = await _migrated(tmp_path / "g.db")

    wrote_ext = await immunity_shadow.record_would_block(
        gate="procedure",
        source_kind="procedure_promotion",
        source_ref="p1",
        process="server",
        blockable_count=1,
        origin_class=ORIGIN_EXTERNAL_UNTRUSTED,
        db=db,
    )
    wrote_fp = await immunity_shadow.record_would_block(
        gate="procedure",
        source_kind="procedure_promotion",
        source_ref="p2",
        process="server",
        blockable_count=1,
        origin_class=ORIGIN_FIRST_PARTY,
        db=db,
    )
    wrote_owner = await immunity_shadow.record_would_block(
        gate="procedure",
        source_kind="procedure_teach",
        source_ref="p3",
        process="server",
        blockable_count=1,
        origin_class=ORIGIN_OWNER,
        db=db,
    )
    assert wrote_fp is False
    assert wrote_owner is False
    if mode == "off":
        assert wrote_ext is False
        assert await crud.count(db) == 0
    else:
        assert wrote_ext is True
        rows = await crud.list_recent(db)
        assert len(rows) == 1
        assert rows[0]["gate"] == "procedure"
        assert rows[0]["origin_class"] == ORIGIN_EXTERNAL_UNTRUSTED
        assert rows[0]["mode"] == mode
    await db.close()


@pytest.mark.asyncio
async def test_procedure_gate_kill_switch_off_records_nothing(tmp_path, monkeypatch):
    """Live kill switch: gate 'procedure' → off short-circuits the emit in-process
    (no row), even for external origin — proving no boot-cache."""
    monkeypatch.setattr(immunity, "gate_mode", lambda gate: "off")
    db = await _migrated(tmp_path / "g.db")
    wrote = await immunity_shadow.record_would_block(
        gate="procedure",
        source_kind="procedure_promotion",
        source_ref="p1",
        process="server",
        blockable_count=1,
        origin_class=ORIGIN_EXTERNAL_UNTRUSTED,
        db=db,
    )
    assert wrote is False
    assert await crud.count(db) == 0
    await db.close()
