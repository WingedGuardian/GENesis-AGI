"""Characterization of every caller-identity reader (MCP P1, no behaviour change).

Each reader used to read the process environment directly. These tests pin what
each one returns across representative environment combinations, so routing the
reads through ``genesis.caller_context`` can be shown to change nothing. The
expected value in every case is the ORIGINAL expression, written out literally
against the same environment, so the oracle does not depend on the accessor.

The combinations deliberately include the near-misses a refactor gets wrong:
``"0"``, ``"true"``, ``""``, and whitespace-padded ids.
"""

from __future__ import annotations

import itertools
import os
from unittest.mock import AsyncMock, patch

import pytest

_IDENTITY_VARS = (
    "GENESIS_CC_SESSION",
    "GENESIS_SESSION_SUPERVISED",
    "GENESIS_SESSION_ID",
    "GENESIS_SESSION_ORIGIN",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
)

# None means "unset".
_MARKER_VALUES = (None, "1", "0", "true", "", " 1")
_ID_VALUES = (None, "", "   ", "sess-a", "  sess-b  ")


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch):
    """The runner is itself a CC session; never let its identity leak in."""
    for var in _IDENTITY_VARS:
        monkeypatch.delenv(var, raising=False)


def _apply(monkeypatch, **values):
    for var, value in values.items():
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)


# ── is_dispatched_session_env (and so guard_human_gate and its 10 callers) ──


@pytest.mark.parametrize(("cc", "sup"), list(itertools.product(_MARKER_VALUES, _MARKER_VALUES)))
def test_is_dispatched_session_env_matrix(monkeypatch, cc, sup):
    from genesis.security.immunity_shadow import (
        DispatchGateRefused,
        guard_human_gate,
        is_dispatched_session_env,
    )

    _apply(monkeypatch, GENESIS_CC_SESSION=cc, GENESIS_SESSION_SUPERVISED=sup)
    expected = os.environ.get("GENESIS_CC_SESSION") == "1" and (
        os.environ.get("GENESIS_SESSION_SUPERVISED") != "1"
    )
    assert is_dispatched_session_env() is expected
    if expected:
        with pytest.raises(DispatchGateRefused):
            guard_human_gate("characterization")
    else:
        guard_human_gate("characterization")


# ── session_origin_from_env (every memory/observation write) ────────────────


@pytest.mark.parametrize(
    "origin", [None, "", " ", "first_party", "external_untrusted", "bogus", " first_party"]
)
def test_session_origin_from_env(monkeypatch, origin):
    from genesis.memory.provenance import ORIGIN_CLASSES, session_origin_from_env

    _apply(monkeypatch, GENESIS_SESSION_ORIGIN=origin)
    raw = os.environ.get("GENESIS_SESSION_ORIGIN")
    expected = raw if raw and raw in ORIGIN_CLASSES else None
    assert session_origin_from_env() == expected


# ── session_charter_tools helpers ────────────────────────────────────────────


@pytest.mark.parametrize("cc", _MARKER_VALUES)
def test_charter_dispatch_helpers(monkeypatch, cc):
    from genesis.mcp.health import session_charter_tools as tools

    _apply(monkeypatch, GENESIS_CC_SESSION=cc)
    dispatched = os.environ.get("GENESIS_CC_SESSION") == "1"
    assert tools._is_dispatched() is dispatched
    assert tools._default_added_by() == ("ambient" if dispatched else "foreground")


@pytest.mark.parametrize(
    ("transcript", "genesis_sid"), list(itertools.product(_ID_VALUES, _ID_VALUES))
)
def test_charter_own_session_ids(monkeypatch, transcript, genesis_sid):
    from genesis.mcp.health import session_charter_tools as tools

    _apply(monkeypatch, CLAUDE_CODE_SESSION_ID=transcript, GENESIS_SESSION_ID=genesis_sid)
    expected: set[str] = set()
    for var in ("CLAUDE_CODE_SESSION_ID", "GENESIS_SESSION_ID"):
        value = (os.environ.get(var) or "").strip()
        if value:
            expected.add(value)
    assert tools._own_session_ids() == expected


# ── follow_up_tools: source attribution and the promotion demotion ──────────


@pytest.mark.parametrize("cc", _MARKER_VALUES)
async def test_follow_up_create_source_and_lane(db, monkeypatch, cc):
    from genesis.db.crud import follow_ups
    from genesis.mcp.health import follow_up_tools

    _apply(monkeypatch, GENESIS_CC_SESSION=cc)
    dispatched = os.environ.get("GENESIS_CC_SESSION") == "1"
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="characterization item", reason="r", strategy="ego_judgment", work_state="ready"
        )
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["source"] == ("ego_dispatch" if dispatched else "foreground_session")
    assert row["kind"] == ("tabled" if dispatched else "follow_up")


@pytest.mark.parametrize("cc", _MARKER_VALUES)
async def test_follow_up_update_promotion(db, monkeypatch, cc):
    from genesis.db.crud import follow_ups
    from genesis.mcp.health import follow_up_tools

    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="cold item", reason="r", strategy="ego_judgment", work_state="deferred_cold"
        )
        _apply(monkeypatch, GENESIS_CC_SESSION=cc)
        dispatched = os.environ.get("GENESIS_CC_SESSION") == "1"
        await follow_up_tools._impl_follow_up_update(created["id"], work_state="ready")
    row = await follow_ups.get_by_id(db, created["id"])
    assert row["kind"] == ("tabled" if dispatched else "follow_up")


# ── direct_session_run(deliver_to_origin=True): which id becomes the origin ─


@pytest.mark.parametrize("genesis_sid", _ID_VALUES)
async def test_direct_session_origin_id(monkeypatch, genesis_sid):
    from genesis.mcp.health import direct_session_tools

    _apply(monkeypatch, GENESIS_SESSION_ID=genesis_sid)
    expected = os.environ.get("GENESIS_SESSION_ID") or None
    enqueue = AsyncMock(return_value="q-1")
    monkeypatch.setattr(direct_session_tools, "_db", object())
    with patch("genesis.db.crud.direct_session_queue.enqueue", enqueue):
        res = await direct_session_tools._impl_direct_session_run("p", deliver_to_origin=True)
    assert res.get("queue_id") == "q-1", res
    kwargs = enqueue.await_args.kwargs
    assert kwargs["origin_session_id"] == expected
    assert kwargs["delivery_mode"] == ("result" if expected else None)


# ── approval: the attribution session id it looks up ────────────────────────


@pytest.mark.parametrize("ctx_sid", [None, "ctx-sid"])
@pytest.mark.parametrize("genesis_sid", _ID_VALUES)
async def test_approval_attribution_session_id(monkeypatch, ctx_sid, genesis_sid):
    from genesis.autonomy.approval import ApprovalManager
    from genesis.observability import session_context

    _apply(monkeypatch, GENESIS_SESSION_ID=genesis_sid)
    lookup = AsyncMock(return_value=None)
    token = session_context._current_session_id.set(ctx_sid)
    try:
        expected = session_context.get_session_id() or os.environ.get("GENESIS_SESSION_ID")
        with (
            patch("genesis.db.crud.cc_sessions.get_by_id", lookup),
            patch("genesis.db.crud.approval_requests.create_chained", AsyncMock()),
        ):
            await ApprovalManager(db=object()).request_approval(
                action_type="t", action_class="c", description="d", timeout_seconds=1
            )
    finally:
        session_context._current_session_id.reset(token)
    if expected:
        assert lookup.await_args.args[1] == expected
    else:
        lookup.assert_not_awaited()


# ── j9 hooks: the session id an eval event is attributed to ─────────────────


@pytest.mark.parametrize("ctx_sid", [None, "ctx-sid"])
@pytest.mark.parametrize("genesis_sid", _ID_VALUES)
def test_j9_resolve_session_id(monkeypatch, ctx_sid, genesis_sid):
    from genesis.eval.j9_hooks import _resolve_session_id
    from genesis.observability import session_context

    _apply(monkeypatch, GENESIS_SESSION_ID=genesis_sid)
    token = session_context._current_session_id.set(ctx_sid)
    try:
        assert _resolve_session_id("explicit") == "explicit"
        # Raw env value, "" included: j9 does not coalesce an empty id to None.
        expected = session_context.get_session_id() or os.environ.get("GENESIS_SESSION_ID")
        assert _resolve_session_id(None) == expected
    finally:
        session_context._current_session_id.reset(token)


# ── trash: the session id recorded on a tombstone ───────────────────────────


@pytest.mark.parametrize(("legacy", "genesis_sid"), list(itertools.product(_ID_VALUES, _ID_VALUES)))
def test_trash_tombstone_session_id(tmp_path, monkeypatch, legacy, genesis_sid):
    from genesis.trash import trash

    _apply(monkeypatch, CLAUDE_SESSION_ID=legacy, GENESIS_SESSION_ID=genesis_sid)
    expected = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("GENESIS_SESSION_ID")
    item = tmp_path / "victim.txt"
    item.write_text("x")
    stone = trash(item, reason="characterization", caller="test")
    assert stone.session_id == expected
