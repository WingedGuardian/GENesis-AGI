"""A RESUMED CC turn's reported cost is session-cumulative from CC 2.1.277.

CC 2.1.277's changelog: a headless resume no longer starts "the session's cost
and usage totals at zero; headless sessions now save their totals at exit". So a
resumed `-p` call reports the running totals of the whole CC session.

MEASURED on CC 2.1.280, one CC session resumed across a model switch
(haiku -> haiku -> sonnet, `claude -p --resume <id> --model <m>`):

    call  total_cost_usd  usage.output_tokens  modelUsage outputTokens
    1     0.0382509       188                  haiku 188
    2     0.0420894        39                  haiku 227
    3     0.1474972         3                  haiku 227, sonnet 3

`total_cost_usd` is a running total across the switch (0.1474972 = 0.0420894 +
sonnet's 0.1054078), the earlier model's entry is restored, the session id is
unchanged and `num_turns` is 1 every time. A detector that compared the main
(highest-tier) model's tokens with this call's could not see call 3 — sonnet's
entry starts at this call's own 3 tokens — so it re-added the whole session.
The flag is therefore keyed on the CC VERSION, which is what defines the
behaviour. The payloads below are those three real results, trimmed to the
fields the parser reads.
"""

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.cc.invoker import (
    _CC_VERSION_RETRY_S,
    CCInvoker,
    cost_is_cumulative_for,
    parse_cc_version,
)
from genesis.cc.types import CCInvocation, CCModel
from genesis.db.crud import cc_sessions
from genesis.db.crud.cc_sessions import _COST_CURSOR_LIMIT

_REPO = Path(__file__).resolve().parents[2]

_HAIKU = "claude-haiku-4-5-20251001"
_SONNET = "claude-sonnet-5"
_SID = "5c121a34-8208-49e9-914b-c333aa08323b"


def _probe_call(n: int) -> dict:
    """The n-th (1-based) real result of the measured model-switch session."""
    usage_by_call = {
        1: (0.0382509, 188, {_HAIKU: (188, 0.0382509)}),
        2: (0.0420894, 39, {_HAIKU: (227, 0.0420894)}),
        3: (0.1474972, 3, {_HAIKU: (227, 0.0420894), _SONNET: (3, 0.1054078)}),
    }
    total, out_call, models = usage_by_call[n]
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": _SID,
        "result": "ok",
        "total_cost_usd": total,
        "num_turns": 1,
        "usage": {"input_tokens": 10, "output_tokens": out_call},
        "modelUsage": {
            name: {"inputTokens": 10, "outputTokens": out, "costUSD": cost}
            for name, (out, cost) in models.items()
        },
    }


# ── the rule: the CC version decides ────────────────────────────────────


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ((2, 1, 246), False),  # the pin this fix lands under: totals restart per call
        ((2, 1, 276), False),
        ((2, 1, 277), True),  # the release that started restoring totals
        ((2, 1, 280), True),
        ((2, 2, 0), True),
        ((3, 0, 0), True),
        (None, False),  # unknown: keep additive (see CCInvoker._cc_version)
    ],
)
def test_cumulative_follows_the_cc_version(version, expected):
    assert cost_is_cumulative_for(version) is expected


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("2.1.280 (Claude Code)\n", (2, 1, 280)),
        ("2.1.246 (Claude Code)", (2, 1, 246)),
        ("  10.20.30\n", (10, 20, 30)),
        ("", None),
        ("Claude Code", None),
        ("2.1", None),
    ],
)
def test_parse_cc_version(stdout, expected):
    assert parse_cc_version(stdout) == expected


# ── reading the version from the real binary ─────────────────────────────


def _fake_claude(tmp_path: Path, version_line: str, *, exit_code: int = 0) -> Path:
    """A real executable that answers `--version` and counts its calls."""
    counter = tmp_path / "calls"
    script = tmp_path / "claude"
    tmp = tmp_path / "claude.new"
    tmp.write_text(
        f"#!/bin/sh\necho x >> \"{counter}\"\necho '{version_line}'\nexit {exit_code}\n",
        encoding="utf-8",
    )
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR)
    os.replace(tmp, script)  # a fresh inode, as an npm reinstall produces
    return script


def _calls(tmp_path: Path) -> int:
    counter = tmp_path / "calls"
    return len(counter.read_text().splitlines()) if counter.exists() else 0


async def test_version_is_read_from_the_binary_the_invoker_runs(tmp_path):
    inv = CCInvoker(claude_path=str(_fake_claude(tmp_path, "2.1.280 (Claude Code)")))
    assert await inv._cc_version() == (2, 1, 280)


async def test_version_is_read_once_per_binary(tmp_path):
    inv = CCInvoker(claude_path=str(_fake_claude(tmp_path, "2.1.280 (Claude Code)")))
    for _ in range(3):
        assert await inv._cc_version() == (2, 1, 280)
    assert _calls(tmp_path) == 1


async def test_a_replaced_binary_is_read_again(tmp_path):
    """An update replaces the binary under a running server; the cached answer
    for the old file must not be reused for the new one."""
    path = _fake_claude(tmp_path, "2.1.246 (Claude Code)")
    inv = CCInvoker(claude_path=str(path))
    assert await inv._cc_version() == (2, 1, 246)
    _fake_claude(tmp_path, "2.1.280 (Claude Code)")
    assert await inv._cc_version() == (2, 1, 280)
    assert _calls(tmp_path) == 2


@pytest.mark.parametrize(("line", "code"), [("2.1.280 (Claude Code)", 1), ("no version here", 0)])
async def test_an_unreadable_version_is_unknown_and_retried_after_a_cooldown(tmp_path, line, code):
    """A failed read is retried, but not on every call: a CLI that hangs on
    `--version` would otherwise hold every reply for the full timeout. And it
    is not cached for good: one bad moment must not pin a long-lived server to
    the additive path."""
    inv = CCInvoker(claude_path=str(_fake_claude(tmp_path, line, exit_code=code)))
    now = [1000.0]
    inv._clock = lambda: now[0]
    assert await inv._cc_version() is None
    assert await inv._cc_version() is None
    assert _calls(tmp_path) == 1  # inside the cooldown: no second subprocess
    now[0] += _CC_VERSION_RETRY_S + 1
    assert await inv._cc_version() is None
    assert _calls(tmp_path) == 2


async def test_a_version_that_recovers_is_used_after_the_cooldown(tmp_path):
    inv = CCInvoker(claude_path=str(_fake_claude(tmp_path, "x", exit_code=1)))
    now = [1000.0]
    inv._clock = lambda: now[0]
    assert await inv._cc_version() is None
    _fake_claude(tmp_path, "2.1.280 (Claude Code)")  # a NEW file: a new key
    assert await inv._cc_version() == (2, 1, 280)


async def test_a_missing_binary_is_unknown(tmp_path):
    inv = CCInvoker(claude_path=str(tmp_path / "absent"))
    assert await inv._cc_version() is None


# ── both run paths stamp the flag (the measured model switch) ─────────────


def _mock_json_proc(result: dict) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(json.dumps(result).encode(), b""))
    proc.returncode = 0
    return proc


def _mock_stream_proc(result: dict) -> AsyncMock:
    data = (
        json.dumps({"type": "system", "subtype": "init", "session_id": _SID}).encode()
        + b"\n"
        + json.dumps(result).encode()
        + b"\n"
    )

    class _Stdout:
        def __init__(self) -> None:
            self._lines = data.splitlines(keepends=True)

        async def readline(self) -> bytes:
            return self._lines.pop(0) if self._lines else b""

    class _Stderr:
        async def read(self) -> bytes:
            return b""

    proc = AsyncMock()
    proc.stdout = _Stdout()
    proc.stderr = _Stderr()
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.returncode = 0
    return proc


def _resumed() -> CCInvocation:
    return CCInvocation(prompt="three", model=CCModel.SONNET, resume_session_id=_SID)


@pytest.mark.parametrize(("version", "expected"), [("2.1.280", True), ("2.1.246", False)])
async def test_run_flags_the_model_switch_turn_by_version(tmp_path, version, expected):
    inv = CCInvoker(claude_path=str(_fake_claude(tmp_path, f"{version} (Claude Code)")))
    with patch("asyncio.create_subprocess_exec", return_value=_mock_json_proc(_probe_call(3))):
        out = await inv.run(_resumed())
    assert out.model_used == _SONNET  # guard: this IS the switched-to model's turn
    assert out.cost_is_cumulative is expected


@pytest.mark.parametrize(("version", "expected"), [("2.1.280", True), ("2.1.246", False)])
async def test_run_streaming_flags_the_model_switch_turn_by_version(tmp_path, version, expected):
    inv = CCInvoker(claude_path=str(_fake_claude(tmp_path, f"{version} (Claude Code)")))
    with patch("asyncio.create_subprocess_exec", return_value=_mock_stream_proc(_probe_call(3))):
        out = await inv.run_streaming(_resumed())
    assert out.model_used == _SONNET
    assert out.cost_is_cumulative is expected


def test_the_parse_point_no_longer_guesses_from_tokens():
    """The token comparison was the defect; it must not come back as a second
    opinion that the run paths then have to reconcile with the version."""
    inv = CCInvocation(prompt="x", model=CCModel.HAIKU)
    out = CCInvoker()._parse_result_dict(_probe_call(2), inv, 1000)
    assert out.cost_is_cumulative is False  # stamped by the run paths, not here


# ── recording ───────────────────────────────────────────────────────────


@pytest.fixture
async def row(db):
    await cc_sessions.create(
        db,
        id="s1",
        session_type="foreground",
        model="haiku",
        effort="medium",
        status="active",
        user_id="u",
        channel="telegram",
        started_at="2026-09-25T00:00:00",
        last_activity_at="2026-09-25T00:00:00",
    )
    return "s1"


async def _record(db, row_id, total, *, cumulative=True, sid="cc-A", out=10):
    await cc_sessions.record_turn_cost(
        db,
        row_id,
        cc_session_id=sid,
        reported_cost_usd=total,
        cumulative=cumulative,
        input_tokens=10,
        output_tokens=out,
    )


async def test_the_measured_model_switch_session_records_its_true_total(db, row):
    """Replays the three real results. The old path ADDED every report and
    stored 0.0382509 + 0.0420894 + 0.1474972 = 0.2278, 54% over the truth; a
    token-comparison detector read the third call as per-call and stored
    0.1896."""
    for n in (1, 2, 3):
        r = _probe_call(n)
        await _record(
            db, row, r["total_cost_usd"], sid=r["session_id"], out=r["usage"]["output_tokens"]
        )
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.1474972)
    assert got["output_tokens"] == 188 + 39 + 3  # tokens were always per call


async def test_pre_2_1_277_resumed_turns_still_add(db, row):
    for total in (0.03, 0.004, 0.005):
        await _record(db, row, total, cumulative=False)
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.039)


async def test_a_fresh_cc_session_on_the_same_row_restarts_the_cursor(db, row):
    """_reconstruct_resume can degrade to a FRESH CC session on the same row.
    That session's running total starts again from its own first call; it must
    not be diffed against the previous session's total."""
    await _record(db, row, 0.03, sid="cc-A")
    await _record(db, row, 0.05, sid="cc-A")  # +0.02
    await _record(db, row, 0.01, sid="cc-B")  # fresh: +0.01
    await _record(db, row, 0.018, sid="cc-B")  # +0.008
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.05 + 0.018)


async def test_a_cumulative_report_from_another_cc_session_is_not_diffed_against_it(db, row):
    """The cursor still names CC session A when B's first recorded report
    arrives. Diffing B against A's larger total would clamp B's turn to zero."""
    await _record(db, row, 0.05, sid="cc-A")
    await _record(db, row, 0.02, sid="cc-B")
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.05 + 0.02)


async def test_another_cc_sessions_larger_report_is_not_diffed_against_the_cursor(db, row):
    """The case the drop guard cannot cover: B's first report is ABOVE A's
    cursor, so only the session-id match stops B being recorded as B - A."""
    await _record(db, row, 0.02, sid="cc-A")
    await _record(db, row, 0.03, sid="cc-B")
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.02 + 0.03)


async def test_returning_to_an_earlier_cc_session_diffs_against_its_own_cursor(db, row):
    """A row can go A -> B -> A: a failed resume reconstruction runs a fresh
    session B, and a later turn resumes A again. A's report must be diffed
    against A's last total, not added whole because the latest cursor is B's."""
    await _record(db, row, 0.03, sid="cc-A")
    await _record(db, row, 0.05, sid="cc-A")  # +0.02
    await _record(db, row, 0.01, sid="cc-B")  # +0.01
    await _record(db, row, 0.06, sid="cc-A")  # +0.01 against A's 0.05
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.07)


async def test_a_report_below_its_cursor_adds_nothing_and_resets_it(db, row):
    """CC saves a session's totals only when its process EXITS. If a process is
    killed after reporting T(k), the next resume restores the older T(k-1) and
    reports T(k-1) + c, below the cursor. Adding that whole would count the
    earlier session a second time; adding nothing under-counts one call. Take
    the smaller error, and diff later reports against the restored figure."""
    await _record(db, row, 0.05)
    await _record(db, row, 0.02)  # restored older totals: + 0
    await _record(db, row, 0.03)  # + 0.01 against the reset cursor
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.06)


async def test_the_cursor_map_is_bounded_and_evicts_the_oldest(db, row):
    """One CC session per row is normal; the bound only stops a pathological
    fallback loop growing metadata without limit. An evicted session that
    returns is added whole, which is the reading before this fix."""
    for i in range(_COST_CURSOR_LIMIT + 1):
        await _record(db, row, 0.01, sid=f"cc-{i}")
    cursors = json.loads((await cc_sessions.get_by_id(db, row))["metadata"])["cost_cursors"]
    assert len(cursors) == _COST_CURSOR_LIMIT
    assert "cc-0" not in cursors and f"cc-{_COST_CURSOR_LIMIT}" in cursors
    await _record(db, row, 0.015, sid="cc-1")  # still held: +0.005
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.01 * (_COST_CURSOR_LIMIT + 1) + 0.005)


async def test_a_non_dict_cursor_map_is_tolerated(db, row):
    await cc_sessions.merge_metadata(db, row, {"cost_cursors": ["junk"]})
    await _record(db, row, 0.03)
    await _record(db, row, 0.05)
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.05)


async def test_a_cumulative_report_seen_without_a_cursor_is_added_whole(db, row):
    """No cursor for this CC session (its first call on this version, or the
    first recorded turn was lost): the reported total is the only reading."""
    await _record(db, row, 0.05)
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.05)


async def test_an_empty_cc_session_id_never_writes_or_matches_a_cursor(db, row):
    await _record(db, row, 0.05, sid="")
    await _record(db, row, 0.06, sid="")
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.11)
    assert "cost_cursors" not in json.loads(got["metadata"] or "{}")


async def test_corrupt_metadata_is_tolerated_and_kept_recoverable(db, row):
    await db.execute("UPDATE cc_sessions SET metadata = 'not json' WHERE id = ?", (row,))
    await db.commit()
    await _record(db, row, 0.03)
    await _record(db, row, 0.05)
    got = await cc_sessions.get_by_id(db, row)
    assert got["cost_usd"] == pytest.approx(0.05)
    assert json.loads(got["metadata"])["cost_cursors"] == {"cc-A": 0.05}


async def test_other_metadata_keys_survive(db, row):
    await cc_sessions.merge_metadata(db, row, {"roster_model": "claude"})
    await _record(db, row, 0.03)
    assert (
        json.loads((await cc_sessions.get_by_id(db, row))["metadata"])["roster_model"] == "claude"
    )


# ── wiring ──────────────────────────────────────────────────────────────


def test_the_foreground_turn_paths_record_through_the_cumulative_aware_writer():
    """Both foreground turn paths (non-streaming and streaming) resume, so both
    must use record_turn_cost. A bare increment_cost there re-adds the session."""
    tree = ast.parse((_REPO / "src/genesis/cc/conversation.py").read_text())
    called = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"increment_cost", "record_turn_cost"}
    ]
    assert called.count("record_turn_cost") == 2, called
    assert "increment_cost" not in called, called


def test_both_turn_paths_pass_the_invokers_flag_and_session_id():
    """Counting the calls is not enough: a call site that hardcodes
    `cumulative=False` brings the over-count straight back. Each call must pass
    the CCOutput's own flag and CC session id."""
    tree = ast.parse((_REPO / "src/genesis/cc/conversation.py").read_text())
    sites = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "record_turn_cost"
    ]
    assert len(sites) == 2
    for call in sites:
        kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
        assert kw.get("cumulative") == "output.cost_is_cumulative", (call.lineno, kw)
        assert kw.get("cc_session_id", "").startswith("output.session_id"), (call.lineno, kw)
        assert kw.get("reported_cost_usd", "").startswith("output.cost_usd"), (call.lineno, kw)
