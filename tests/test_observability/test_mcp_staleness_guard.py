"""Tests for the MCP stale-code guard (InstrumentationMiddleware freshness gate).

Covers: block/warn/off modes, guarded-vs-unguarded routing, the fail-open edges
(no db / no spawn identity / empty history), the short-vs-full commit prefix
compare (regression guard against the guard firing on EVERY deploy), timezone-
robust timestamp comparison, and the ~60s verdict cache.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

import genesis.db.crud.update_history as _uh
import genesis.observability.mcp_spawn_identity as _si
import genesis.observability.mcp_staleness_guard_config as _cfg
from genesis.observability.mcp_middleware import InstrumentationMiddleware, _same_commit

# Full 40-char spawn SHA vs the SHORT commit update_history stores.
SPAWN_FULL = "0123456789abcdef0123456789abcdef01234567"
DEPLOY_DIFF = "fedcba98"  # different commit (short)
DEPLOY_SAME = SPAWN_FULL[:8]  # prefix of spawn → same commit
T_SPAWN = "2026-08-01T00:00:00+00:00"
T_AFTER = "2026-08-02T00:00:00+00:00"
T_BEFORE = "2026-07-31T00:00:00+00:00"


class _FakeDB:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FakeTracker:
    def __init__(self) -> None:
        self.records: list = []

    def record(self, provider, **kw) -> None:
        self.records.append((provider, kw))


def _ctx(tool_name: str):
    msg = type("Msg", (), {"name": tool_name})()
    return type("Ctx", (), {"message": msg})()


def _mw(db=None) -> InstrumentationMiddleware:
    return InstrumentationMiddleware(_FakeTracker(), "memory", db=db)


def _set_spawn(monkeypatch, commit, at) -> None:
    monkeypatch.setattr(_si, "_spawn_commit", commit)
    monkeypatch.setattr(_si, "_spawn_at", at)


def _set_deploy(monkeypatch, row, counter=None) -> None:
    async def fake(db):
        if counter is not None:
            counter.append(1)
        return row

    monkeypatch.setattr(_uh, "last_successful_update", fake)


def _set_mode(monkeypatch, mode) -> None:
    monkeypatch.setattr(_cfg, "effective_mode", lambda: mode)


async def _run(mw, tool_name):
    sentinel = object()

    async def call_next(ctx):
        return sentinel

    result = await mw.on_call_tool(_ctx(tool_name), call_next)
    return result, sentinel


# ── _same_commit unit ────────────────────────────────────────────────────────


def test_same_commit_prefix_and_none():
    assert _same_commit(SPAWN_FULL, DEPLOY_SAME) is True  # full ⊃ short
    assert _same_commit(DEPLOY_SAME, SPAWN_FULL) is True  # short ⊂ full
    assert _same_commit(SPAWN_FULL, DEPLOY_DIFF) is False
    assert _same_commit(None, DEPLOY_SAME) is False
    assert _same_commit(SPAWN_FULL, "") is False


# ── block mode ───────────────────────────────────────────────────────────────


async def test_stale_guarded_block_raises_and_rolls_back(monkeypatch):
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    _set_deploy(monkeypatch, (T_AFTER, DEPLOY_DIFF))
    _set_mode(monkeypatch, "block")
    db = _FakeDB()
    mw = _mw(db)
    with pytest.raises(ToolError):
        await _run(mw, "procedure_store")
    # finally ran rollback (no write happened), never commit
    assert (db.rollbacks, db.commits) == (1, 0)


async def test_stale_unguarded_tool_passes_even_when_stale(monkeypatch):
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    _set_deploy(monkeypatch, (T_AFTER, DEPLOY_DIFF))
    _set_mode(monkeypatch, "block")
    db = _FakeDB()
    mw = _mw(db)
    result, sentinel = await _run(mw, "follow_up_create")  # not in GUARDED
    assert result is sentinel
    assert (db.commits, db.rollbacks) == (1, 0)


# ── warn / off modes ─────────────────────────────────────────────────────────


async def test_stale_guarded_warn_allows(monkeypatch):
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    _set_deploy(monkeypatch, (T_AFTER, DEPLOY_DIFF))
    _set_mode(monkeypatch, "warn")
    mw = _mw(_FakeDB())
    result, sentinel = await _run(mw, "procedure_store")
    assert result is sentinel


async def test_stale_guarded_off_allows(monkeypatch):
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    _set_deploy(monkeypatch, (T_AFTER, DEPLOY_DIFF))
    _set_mode(monkeypatch, "off")
    mw = _mw(_FakeDB())
    result, sentinel = await _run(mw, "procedure_store")
    assert result is sentinel


# ── not-stale paths (must NOT block) ─────────────────────────────────────────


async def test_same_commit_is_not_stale(monkeypatch):
    # Deploy commit is a PREFIX of the spawn SHA → same commit → not stale.
    # Regression guard: a naive != would fire on every deploy.
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    _set_deploy(monkeypatch, (T_AFTER, DEPLOY_SAME))
    _set_mode(monkeypatch, "block")
    result, sentinel = await _run(_mw(_FakeDB()), "procedure_store")
    assert result is sentinel


async def test_deploy_before_spawn_is_not_stale(monkeypatch):
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    _set_deploy(monkeypatch, (T_BEFORE, DEPLOY_DIFF))
    _set_mode(monkeypatch, "block")
    result, sentinel = await _run(_mw(_FakeDB()), "procedure_store")
    assert result is sentinel


async def test_empty_history_is_not_stale(monkeypatch):
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    _set_deploy(monkeypatch, None)
    _set_mode(monkeypatch, "block")
    result, sentinel = await _run(_mw(_FakeDB()), "procedure_store")
    assert result is sentinel


async def test_no_spawn_identity_fails_open(monkeypatch):
    _set_spawn(monkeypatch, None, None)  # git unreadable at spawn
    _set_deploy(monkeypatch, (T_AFTER, DEPLOY_DIFF))
    _set_mode(monkeypatch, "block")
    result, sentinel = await _run(_mw(_FakeDB()), "procedure_store")
    assert result is sentinel


async def test_none_db_fails_open(monkeypatch):
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    _set_deploy(monkeypatch, (T_AFTER, DEPLOY_DIFF))
    _set_mode(monkeypatch, "block")
    mw = _mw(db=None)  # no db → can't check → fail open
    result, sentinel = await _run(mw, "procedure_store")
    assert result is sentinel


# ── correctness details ──────────────────────────────────────────────────────


async def test_mixed_timezone_offsets_compared_by_instant(monkeypatch):
    # completed_at is lexically SMALLER than spawn_at but a LATER instant.
    # Parsed compare → stale; a string compare would wrongly say not-stale.
    _set_spawn(monkeypatch, SPAWN_FULL, "2026-08-02T00:00:00+00:00")
    _set_deploy(monkeypatch, ("2026-08-01T22:00:00-04:00", DEPLOY_DIFF))  # = 02T02:00Z
    _set_mode(monkeypatch, "block")
    with pytest.raises(ToolError):
        await _run(_mw(_FakeDB()), "procedure_store")


async def test_stale_verdict_latches(monkeypatch):
    # A confirmed-stale verdict latches → no repeat DB reads (staleness is
    # monotonic, so once true it stays true for the process's life).
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    calls: list = []
    _set_deploy(monkeypatch, (T_AFTER, DEPLOY_DIFF), counter=calls)
    mw = _mw(_FakeDB())
    assert await mw._is_stale() is True
    assert await mw._is_stale() is True
    assert len(calls) == 1  # second call served from the latch


async def test_negative_verdict_not_cached_across_deploy(monkeypatch):
    # Codex P2 regression: a not-stale verdict must NOT be cached — a deploy
    # landing right after a clean check must be seen on the very NEXT call, not
    # masked by a stale cached False (the exact window the guard must cover).
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)
    mw = _mw(_FakeDB())
    _set_deploy(monkeypatch, None)  # no qualifying deploy yet
    assert await mw._is_stale() is False
    _set_deploy(monkeypatch, (T_AFTER, DEPLOY_DIFF))  # deploy lands after spawn
    assert await mw._is_stale() is True  # seen immediately, no stale False cache


async def test_db_error_fails_open(monkeypatch):
    _set_spawn(monkeypatch, SPAWN_FULL, T_SPAWN)

    async def boom(db):
        raise RuntimeError("db down")

    monkeypatch.setattr(_uh, "last_successful_update", boom)
    _set_mode(monkeypatch, "block")
    result, sentinel = await _run(_mw(_FakeDB()), "procedure_store")
    assert result is sentinel


# ── effective_mode() decision ladder (config lever) ──────────────────────────


def _cfg_returns(monkeypatch, d):
    monkeypatch.setattr(_cfg, "load_config", lambda: d)


def test_effective_mode_env_kill_switch_forces_off(monkeypatch):
    monkeypatch.setenv("GENESIS_MCP_STALENESS_GUARD", "1")
    _cfg_returns(monkeypatch, {"enabled": True, "mode": "block"})
    assert _cfg.effective_mode() == "off"


def test_effective_mode_disabled_is_off(monkeypatch):
    monkeypatch.delenv("GENESIS_MCP_STALENESS_GUARD", raising=False)
    _cfg_returns(monkeypatch, {"enabled": False, "mode": "block"})
    assert _cfg.effective_mode() == "off"


def test_effective_mode_yaml_off_boolean_is_off(monkeypatch):
    # Hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
    monkeypatch.delenv("GENESIS_MCP_STALENESS_GUARD", raising=False)
    _cfg_returns(monkeypatch, {"enabled": True, "mode": False})
    assert _cfg.effective_mode() == "off"


def test_effective_mode_invalid_degrades_to_block(monkeypatch):
    # Fail-SAFE: an unknown mode must NOT silently drop the guard.
    monkeypatch.delenv("GENESIS_MCP_STALENESS_GUARD", raising=False)
    _cfg_returns(monkeypatch, {"enabled": True, "mode": "loud"})
    assert _cfg.effective_mode() == "block"


def test_effective_mode_valid_warn_passthrough(monkeypatch):
    monkeypatch.delenv("GENESIS_MCP_STALENESS_GUARD", raising=False)
    _cfg_returns(monkeypatch, {"enabled": True, "mode": "warn"})
    assert _cfg.effective_mode() == "warn"


def test_effective_mode_shipped_default_is_block(monkeypatch):
    # Real load_config reading the shipped config/mcp_staleness_guard.yaml.
    monkeypatch.delenv("GENESIS_MCP_STALENESS_GUARD", raising=False)
    assert _cfg.DEFAULTS["mode"] == "block"
    assert _cfg.effective_mode() == "block"
