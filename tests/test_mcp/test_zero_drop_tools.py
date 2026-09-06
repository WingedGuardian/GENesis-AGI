"""The read and disposition surfaces — without these the detector is inert.

A board nobody can read is not an answer to "what fell through the cracks?",
and a board nobody can disposition dies of alarm fatigue: the design has no
branch-name denylist by decision, so acknowledgement is the ONLY way to say
"this one is meant to sit there". The first build of this subsystem had a
detector, a store, a lifecycle and an alert that told the reader to run a tool
that did not exist.
"""

import json

import pytest

from genesis.db.crud import zero_drop as zd
from genesis.mcp.health.zero_drop_tools import (
    STALE_AFTER_S,
    _impl_zero_drop_ack,
    _impl_zero_drop_status,
)

CLS = "unpushed_branch"


def _f(branch="feat/x", tip="aaa111"):
    return {"branch": branch, "tip_sha": tip, "ahead_count": 4}


@pytest.fixture
def last_run(monkeypatch, tmp_path):
    """Point read_last_run at a temp home so the tests own the run record."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))

    def _write(record: dict) -> None:
        from genesis.session_awareness.zero_drop_worker import last_run_path

        path = last_run_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

    return _write


async def test_status_lists_findings_with_their_denominator(db, last_run):
    from datetime import UTC, datetime

    await zd.apply_sweep(db, class_=CLS, present=[_f("a"), _f("b"), _f("c")], run_id="r1")
    await zd.ack(db, class_=CLS, branch="b", reason="deliberate")
    last_run({"computed_at": datetime.now(UTC).isoformat(), "coverage": "all classes swept"})

    out = await _impl_zero_drop_status(db, now=datetime.now(UTC), limit=2)

    assert out["counts_by_status"] == {"open": 2, "acked": 1, "resolved": 0}
    assert out["listed"] == 2
    assert out["listed_of"] == 3, "a paged listing must still report the total"
    assert out["detector"]["stale"] is False


async def test_a_zero_from_a_stale_detector_is_labelled_unverified(db, last_run):
    """The whole point of the freshness block: an empty board and a dead
    detector look identical from the outside, and only one of them is good
    news."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    last_run({"computed_at": (now - timedelta(seconds=STALE_AFTER_S + 60)).isoformat()})

    out = await _impl_zero_drop_status(db, now=now)

    assert out["open"] == 0
    assert out["detector"]["stale"] is True
    assert "UNVERIFIED" in out["detector"]["verdict"]


async def test_a_never_run_detector_says_so(db, last_run):
    from datetime import UTC, datetime

    out = await _impl_zero_drop_status(db, now=datetime.now(UTC))
    assert out["detector"]["stale"] is True
    assert "NEVER RUN" in out["detector"]["verdict"]


async def test_status_surfaces_a_blind_leg(db, last_run):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    last_run(
        {
            "computed_at": now.isoformat(),
            "coverage": "FROZEN: unpushed_branch,pushed_no_pr",
            "frozen_classes": ["unpushed_branch", "pushed_no_pr"],
            "degraded": {"branches": "pr history: gh auth failed"},
        }
    )
    out = await _impl_zero_drop_status(db, now=now)

    assert out["detector"]["blind"] is True
    assert out["detector"]["frozen_classes"] == ["unpushed_branch", "pushed_no_pr"]
    assert "FROZEN" in out["detector"]["coverage"]


async def test_ack_records_the_tip_it_was_granted_at(db, last_run):
    await zd.apply_sweep(db, class_=CLS, present=[_f(tip="aaa111")], run_id="r1")

    out = await _impl_zero_drop_ack(
        db,
        class_=CLS,
        branch="feat/x",
        reason="backup branch, keeping it",
        now="2026-01-01T00:00:00+00:00",
    )

    assert out["status"] == "ok"
    assert out["acked_tip_sha"] == "aaa111"
    assert (await zd.get(db, class_=CLS, branch="feat/x"))["status"] == "acked"


@pytest.mark.parametrize("reason", ["", "   "])
async def test_ack_refuses_an_empty_reason(db, reason):
    """An unexplained suppression is indistinguishable from a forgotten one."""
    await zd.apply_sweep(db, class_=CLS, present=[_f()], run_id="r1")
    out = await _impl_zero_drop_ack(
        db, class_=CLS, branch="feat/x", reason=reason, now="2026-01-01T00:00:00+00:00"
    )
    assert out["status"] == "error"
    assert (await zd.get(db, class_=CLS, branch="feat/x"))["status"] == "open"


async def test_ack_rejects_an_unknown_class_rather_than_writing_nothing(db):
    """A typo'd class would otherwise return not_found, which reads as 'that
    finding is already handled' rather than 'you named a class that cannot
    exist'."""
    out = await _impl_zero_drop_ack(
        db, class_="made_up", branch="feat/x", reason="x", now="2026-01-01T00:00:00+00:00"
    )
    assert out["status"] == "error"
    assert "valid" in out["message"]


async def test_ack_on_an_unknown_branch_says_so(db):
    out = await _impl_zero_drop_ack(
        db, class_=CLS, branch="never-seen", reason="x", now="2026-01-01T00:00:00+00:00"
    )
    assert out["status"] == "not_found"


async def test_the_read_tool_is_in_the_reflection_allowlist():
    """A read-only health tool that is not in the allowlist fails closed for
    reflection sessions — which is where an autonomous 'what is outstanding?'
    question actually gets asked."""
    from genesis.cc.session_config import _REFLECTION_READ_MCP

    assert "zero_drop_status" in _REFLECTION_READ_MCP
    assert "zero_drop_ack" not in _REFLECTION_READ_MCP, "a write tool is not a read tool"


def test_both_tools_are_registered_on_the_health_server():
    """Built != wired: an _impl with no @mcp.tool wrapper reaches nobody."""
    import genesis.mcp.health as health

    assert hasattr(health, "_impl_zero_drop_status")
    assert hasattr(health, "_impl_zero_drop_ack")
    source = __import__("pathlib").Path(health._zero_drop_tools.__file__).read_text()
    assert source.count("@mcp.tool()") == 2
