"""Tests for reflex_verdicts CRUD + the reflex_signal_resolve tool impl.

Covers the three dispositions (fixed / not_a_bug / wont_fix), taste-corpus
verdict writes, idempotency on terminal signals, and input validation.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import reflex_signals as signals_crud
from genesis.db.crud import reflex_verdicts as verdicts_crud
from genesis.mcp.health.reflex_resolve import _impl_reflex_signal_resolve

M70 = importlib.import_module("genesis.db.migrations.0070_reflex_arc")

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC).isoformat()


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        await M70.up(conn)
        await conn.commit()
        yield conn


async def _seed_signal(db, *, fingerprint="fp1", error_type="CCProcessError") -> dict:
    return await signals_crud.upsert_occurrence(
        db,
        fingerprint=fingerprint,
        class_key=f"{error_type}xcc",
        task_name="direct-session-x",
        subsystem="cc",
        error_type=error_type,
        error_message="You've hit your session limit",
        traceback_tail="cc/invoker.py:run",
        now=NOW,
    )


class TestVerdictsCrud:
    async def test_record_and_list(self, db):
        sig = await _seed_signal(db)
        vid = await verdicts_crud.record(
            db,
            signal_id=sig["id"],
            verdict_point="diagnose_card",
            verdict="dismiss_notbug",
            resolved_by="user",
            context_snapshot={"rationale": "noise"},
            now=NOW,
        )
        rows = await verdicts_crud.list_for_signal(db, sig["id"])
        assert len(rows) == 1
        assert rows[0]["id"] == vid
        assert rows[0]["verdict"] == "dismiss_notbug"
        assert json.loads(rows[0]["context_snapshot"]) == {"rationale": "noise"}


class TestResolveTool:
    async def test_fixed_sets_resolved_no_verdict(self, db):
        sig = await _seed_signal(db)
        out = await _impl_reflex_signal_resolve(
            db,
            signal_id=sig["id"],
            disposition="fixed",
            rationale="root cause fixed in PR-A0",
            now=NOW,
        )
        assert out["status"] == "ok"
        assert out["new_status"] == "resolved"
        assert out["verdict_id"] is None
        row = await signals_crud.get_by_id(db, sig["id"])
        assert row["status"] == "resolved"
        # 'fixed' is a lifecycle close, not a card judgment → no taste-corpus row
        assert await verdicts_crud.list_for_signal(db, sig["id"]) == []

    async def test_not_a_bug_dismisses_with_verdict(self, db):
        sig = await _seed_signal(db)
        out = await _impl_reflex_signal_resolve(
            db,
            signal_id=sig["id"],
            disposition="not_a_bug",
            rationale="environmental noise",
            now=NOW,
        )
        assert out["status"] == "ok"
        assert out["new_status"] == "dismissed_notbug"
        row = await signals_crud.get_by_id(db, sig["id"])
        assert row["status"] == "dismissed_notbug"
        verdicts = await verdicts_crud.list_for_signal(db, sig["id"])
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "dismiss_notbug"
        ctx = json.loads(verdicts[0]["context_snapshot"])
        assert ctx["rationale"] == "environmental noise"
        assert ctx["disposition"] == "not_a_bug"
        assert ctx["class_key"] == "CCProcessErrorxcc"

    async def test_wont_fix_dismisses_with_verdict(self, db):
        sig = await _seed_signal(db)
        out = await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="wont_fix", rationale="later", now=NOW
        )
        assert out["new_status"] == "dismissed_wontfix"
        verdicts = await verdicts_crud.list_for_signal(db, sig["id"])
        assert verdicts[0]["verdict"] == "dismiss_wontfix"

    async def test_idempotent_on_terminal_no_double_verdict(self, db):
        sig = await _seed_signal(db)
        await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="not_a_bug", rationale="x", now=NOW
        )
        # second call is a no-op — must NOT write a second verdict
        out = await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="not_a_bug", rationale="x", now=NOW
        )
        assert out["status"] == "noop"
        assert len(await verdicts_crud.list_for_signal(db, sig["id"])) == 1

    async def test_unknown_disposition_rejected(self, db):
        sig = await _seed_signal(db)
        out = await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="bogus", rationale="x", now=NOW
        )
        assert out["status"] == "error"
        assert (await signals_crud.get_by_id(db, sig["id"]))["status"] == "new"

    async def test_blank_rationale_rejected(self, db):
        sig = await _seed_signal(db)
        out = await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="fixed", rationale="   ", now=NOW
        )
        assert out["status"] == "error"
        assert (await signals_crud.get_by_id(db, sig["id"]))["status"] == "new"

    async def test_missing_signal_rejected(self, db):
        out = await _impl_reflex_signal_resolve(
            db, signal_id="nope", disposition="fixed", rationale="x", now=NOW
        )
        assert out["status"] == "error"
        assert "not found" in out["message"]

    async def test_failure_state_signal_stays_resolvable(self, db):
        # A stuck FAILURE terminal (e.g. card_expired) is NOT "already disposed"
        # — a human must still be able to close it (it's exactly what this tool
        # is for). It must resolve, not no-op.
        sig = await _seed_signal(db)
        await signals_crud.set_status(
            db, signal_id=sig["id"], expected_from="new", to="card_expired", now=NOW
        )
        out = await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="wont_fix", rationale="stale card", now=NOW
        )
        assert out["status"] == "ok"
        assert out["new_status"] == "dismissed_wontfix"
        assert (await signals_crud.get_by_id(db, sig["id"]))["status"] == "dismissed_wontfix"

    async def test_verdict_write_failure_returns_partial(self, db, monkeypatch):
        # If the status transition commits but the taste-corpus write raises, the
        # caller must NOT be told status=="ok" — it's "partial" + a flag, and the
        # transition still stands (no unwind).
        sig = await _seed_signal(db)

        async def _boom(*a, **k):
            raise RuntimeError("verdict store down")

        monkeypatch.setattr(verdicts_crud, "record", _boom)
        out = await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="not_a_bug", rationale="x", now=NOW
        )
        assert out["status"] == "partial"
        assert out["verdict_write_failed"] is True
        assert out["verdict_id"] is None
        # transition still committed
        assert (await signals_crud.get_by_id(db, sig["id"]))["status"] == "dismissed_notbug"
        assert await verdicts_crud.list_for_signal(db, sig["id"]) == []

    async def test_partial_then_retry_repairs_the_missing_verdict(self, db, monkeypatch):
        # A transient verdict-write failure must be recoverable via the same API:
        # the retry sees a terminal status with NO matching verdict and REPAIRS it
        # (writes the missing corpus row) instead of no-op'ing forever.
        sig = await _seed_signal(db)
        real_record = verdicts_crud.record
        calls = {"n": 0}

        async def _flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("verdict store down")
            return await real_record(*a, **k)

        monkeypatch.setattr(verdicts_crud, "record", _flaky)

        out1 = await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="not_a_bug", rationale="x", now=NOW
        )
        assert out1["status"] == "partial"
        assert await verdicts_crud.list_for_signal(db, sig["id"]) == []

        out2 = await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="not_a_bug", rationale="x", now=NOW
        )
        assert out2["status"] == "repaired"
        verdicts = await verdicts_crud.list_for_signal(db, sig["id"])
        assert len(verdicts) == 1 and verdicts[0]["verdict"] == "dismiss_notbug"

        # once the verdict exists, a further retry is a genuine no-op
        out3 = await _impl_reflex_signal_resolve(
            db, signal_id=sig["id"], disposition="not_a_bug", rationale="x", now=NOW
        )
        assert out3["status"] == "noop"
