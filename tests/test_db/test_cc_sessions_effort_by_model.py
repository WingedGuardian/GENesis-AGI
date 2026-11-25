"""A model-only switch takes the channel's per-model default effort (#4854)."""

from __future__ import annotations

import pytest

from genesis.channels import config as channel_config
from genesis.db.crud import cc_sessions

TELEGRAM_MAP = {"telegram": {"default_effort_by_model": {"opus": "medium", "sonnet": "high"}}}


@pytest.fixture
def per_model(monkeypatch):
    def install(data):
        monkeypatch.setattr(channel_config, "_load_yaml_merged", lambda name: data)

    install(TELEGRAM_MAP)
    return install


async def _session(db, *, channel="telegram", model="sonnet", effort="high"):
    await cc_sessions.create(
        db,
        id="s1",
        session_type="foreground",
        model=model,
        effort=effort,
        started_at="2026-11-17T00:00:00+00:00",
        last_activity_at="2026-11-17T00:00:00+00:00",
        user_id="tg-1",
        channel=channel,
    )


def test_the_configured_effort_for_a_model(per_model):
    assert channel_config.default_effort_for_model("telegram", "opus").value == "medium"
    assert channel_config.default_effort_for_model("telegram", "haiku") is None
    assert channel_config.default_effort_for_model("web", "opus") is None


def test_an_invalid_configured_effort_is_ignored(per_model):
    per_model({"telegram": {"default_effort_by_model": {"opus": "turbo"}}})
    assert channel_config.default_effort_for_model("telegram", "opus") is None


def test_no_map_means_no_default(per_model):
    per_model({"telegram": {"default_model": "sonnet"}})
    assert channel_config.default_effort_for_model("telegram", "opus") is None


@pytest.mark.asyncio
async def test_a_model_only_switch_takes_the_models_default(db, per_model):
    await _session(db)
    assert await cc_sessions.update_model_effort(db, "s1", model="opus")
    row = await cc_sessions.get_by_id(db, "s1")
    assert (row["model"], row["effort"]) == ("opus", "medium")


@pytest.mark.asyncio
async def test_an_explicit_effort_wins(db, per_model):
    await _session(db)
    await cc_sessions.update_model_effort(db, "s1", model="opus", effort="max")
    assert (await cc_sessions.get_by_id(db, "s1"))["effort"] == "max"


@pytest.mark.asyncio
async def test_another_channel_keeps_its_effort(db, per_model):
    await _session(db, channel="web")
    await cc_sessions.update_model_effort(db, "s1", model="opus")
    assert (await cc_sessions.get_by_id(db, "s1"))["effort"] == "high"


@pytest.mark.asyncio
async def test_an_unmapped_model_keeps_its_effort(db, per_model):
    await _session(db, effort="low")
    await cc_sessions.update_model_effort(db, "s1", model="haiku")
    assert (await cc_sessions.get_by_id(db, "s1"))["effort"] == "low"


@pytest.mark.asyncio
async def test_a_config_failure_never_blocks_the_switch(db, monkeypatch):
    def boom(name):
        raise OSError("unreadable")

    monkeypatch.setattr(channel_config, "_load_yaml_merged", boom)
    await _session(db)
    assert await cc_sessions.update_model_effort(db, "s1", model="opus")
    row = await cc_sessions.get_by_id(db, "s1")
    assert (row["model"], row["effort"]) == ("opus", "high")


def test_the_shipped_config_sets_no_per_model_effort():
    """A fresh install behaves as before: the map lives only in a local overlay."""
    from pathlib import Path

    import yaml

    shipped = yaml.safe_load((Path(__file__).parents[2] / "config/channels.yaml").read_text())
    assert not shipped["telegram"].get("default_effort_by_model")


@pytest.mark.asyncio
async def test_restating_the_current_model_keeps_the_effort(db, per_model):
    """Only a switch takes the default: `/model opus` while already on opus keeps an
    effort the user set since."""
    await _session(db, model="opus", effort="max")
    await cc_sessions.update_model_effort(db, "s1", model="opus")
    assert (await cc_sessions.get_by_id(db, "s1"))["effort"] == "max"


@pytest.mark.asyncio
async def test_session_config_reports_the_effort_a_model_switch_applied(db, per_model, monkeypatch):
    import types

    import genesis.mcp.health_mcp as health_mcp_mod
    from genesis.mcp.health import session_control

    await _session(db)
    monkeypatch.setattr(health_mcp_mod, "_service", types.SimpleNamespace(_db=db))
    persisted = {}
    monkeypatch.setattr(
        session_control, "_persist_session_config", lambda **kw: persisted.update(kw)
    )
    result = await session_control.session_config(session_id="s1", model="opus")
    assert result["success"] and result["effort"] == "medium"
    assert persisted == {"model": "opus", "effort": "medium"}


@pytest.mark.asyncio
async def test_a_concurrent_explicit_switch_is_not_overwritten(db, per_model, monkeypatch):
    """The switch test runs inside the UPDATE, so a write that lands between the
    config read and the update wins (review finding: stale check-then-act)."""
    await _session(db)
    real = cc_sessions._per_model_effort

    async def racing(db_, id_, model):
        default = await real(db_, id_, model)
        await db_.execute("UPDATE cc_sessions SET model = 'opus', effort = 'max' WHERE id = 's1'")
        await db_.commit()
        return default

    monkeypatch.setattr(cc_sessions, "_per_model_effort", racing)
    assert await cc_sessions.update_model_effort(db, "s1", model="opus")
    row = await cc_sessions.get_by_id(db, "s1")
    assert (row["model"], row["effort"]) == ("opus", "max")


@pytest.mark.asyncio
async def test_apply_returns_the_effort_the_write_stored(db, per_model):
    """The effort comes back from the UPDATE itself, so a caller never needs a
    later read that another writer could change (review finding: read-back race)."""
    await _session(db)
    assert await cc_sessions.apply_model_effort(db, "s1", model="opus") == "medium"
    assert await cc_sessions.apply_model_effort(db, "s1", effort="max") == "max"
    assert await cc_sessions.apply_model_effort(db, "missing", model="opus") is None
    assert await cc_sessions.apply_model_effort(db, "s1") is None
    assert not await cc_sessions.update_model_effort(db, "missing", model="opus")


@pytest.mark.asyncio
async def test_a_commit_landing_right_after_the_update_cannot_meet_its_open_statement(
    db, per_model
):
    """Another coroutine on the shared connection may commit between the UPDATE
    and the read of its RETURNING row (review finding: the lock is released when
    execute() returns). The row is consumed in the same locked operation, so that
    commit never meets a statement still in progress."""
    await _session(db)

    class OtherWriterCommitsAfterExecute:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def execute(self, sql, *args, **kwargs):
            cursor = await self._inner.execute(sql, *args, **kwargs)
            if "RETURNING" in sql:
                await self._inner.commit()  # another writer, between execute and fetch
            return cursor

    wrapped = OtherWriterCommitsAfterExecute(db)
    assert await cc_sessions.apply_model_effort(wrapped, "s1", model="opus") == "medium"
