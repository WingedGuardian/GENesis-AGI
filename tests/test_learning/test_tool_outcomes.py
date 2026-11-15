"""Tool-call outcome rows from a transcript spine (issue #4712).

Drives the REAL parser (``build_spine_and_haystack``) into the REAL row builder
and the REAL insert, over a synthetic transcript holding one call of each
shape that decides a row: a success, a hook refusal, a user rejection, a
non-zero exit, a call whose result has not arrived, and a subagent call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import tool_call_outcomes as tco
from genesis.learning import tool_outcomes
from genesis.learning.procedural.struggle_detector import (
    build_spine_and_haystack,
    score_struggle,
)

SID = "11111111-2222-3333-4444-555555555555"
# Fixture calls are dated yesterday, so the retention horizons never drop them.
DAY = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
# A synthetic credential shape the shared scrubber redacts; not a real key.
FAKE_KEY = "sk-ant-api03-" + "Q" * 40


def _use(tid, name, inp, *, ts, sidechain=False):
    return {
        "type": "assistant",
        "sessionId": SID,
        "timestamp": ts,
        "isSidechain": sidechain,
        "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]},
    }


def _result(tid, text, *, ts, is_error=None, denial=None, sidechain=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}]}
    if is_error is not None:
        block["is_error"] = is_error
    rec = {
        "type": "user",
        "sessionId": SID,
        "timestamp": ts,
        "isSidechain": sidechain,
        "message": {"content": [block]},
    }
    if denial:
        rec["toolDenialKind"] = denial
    return rec


HOOK_TEXT = (
    "PreToolUse:Bash hook error: [${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook "
    "hooks/example_guard.py]: BLOCKED: this command cannot be parsed safely. "
    f"token={FAKE_KEY}"
)

RECORDS = [
    _use("toolu_ok", "Bash", {"command": "ls"}, ts=f"{DAY}T10:00:00Z"),
    _result("toolu_ok", "a\nb", ts=f"{DAY}T10:00:01Z"),
    _use("toolu_hook", "Bash", {"command": "cat <<EOF"}, ts=f"{DAY}T10:01:00Z"),
    _result("toolu_hook", HOOK_TEXT, ts=f"{DAY}T10:01:01Z", is_error=True),
    _use("toolu_reject", "ExitPlanMode", {"plan": "x"}, ts=f"{DAY}T10:02:00Z"),
    _result(
        "toolu_reject",
        "The user doesn't want to proceed with this tool use.",
        ts=f"{DAY}T10:02:01Z",
        is_error=True,
        denial="user-rejected",
    ),
    _use("toolu_exit", "Bash", {"command": "false"}, ts=f"{DAY}T10:03:00Z"),
    _result("toolu_exit", "Exit code 1", ts=f"{DAY}T10:03:01Z", is_error=True),
    _use("toolu_edit", "Edit", {"file_path": "/x/y.py"}, ts=f"{DAY}T10:04:00Z"),
    _result("toolu_edit", "The file has been updated.", ts=f"{DAY}T10:04:01Z"),
    _use("toolu_side", "Bash", {"command": "pwd"}, ts=f"{DAY}T10:05:00Z", sidechain=True),
    _result("toolu_side", "Exit code 2", ts=f"{DAY}T10:05:01Z", is_error=True, sidechain=True),
    # Still running when the transcript is read: no result record yet.
    _use("toolu_pending", "Bash", {"command": "sleep 600"}, ts=f"{DAY}T10:06:00Z"),
]


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n")
    return path


def _rows(path):
    spine, _ = build_spine_and_haystack(path)
    return spine, {r[6]: r for r in tool_outcomes.outcome_rows(spine, fallback_session_id="fb")}


def test_rows_are_exactly_the_recordable_calls(transcript):
    _spine, rows = _rows(transcript)
    # Oracle: the pending call, the sidechain call and the Edit (the Stop-hook
    # sensor's tool) are absent; the rest are present.
    assert set(rows) == {"toolu_ok", "toolu_hook", "toolu_reject", "toolu_exit"}
    success = {tid: row[3] for tid, row in rows.items()}
    assert success == {
        "toolu_ok": 1,
        "toolu_hook": 0,
        "toolu_reject": 0,
        "toolu_exit": 0,
    }
    assert rows["toolu_ok"][0] == SID  # the record's own sessionId
    assert rows["toolu_hook"][5] == f"{DAY}T10:01:01Z"  # the result's timestamp
    assert rows["toolu_ok"][4] is None  # no snippet on a success
    kinds = {tid: row[7] for tid, row in rows.items()}
    assert kinds == {
        "toolu_ok": None,
        "toolu_hook": "hook_block",
        "toolu_reject": "user_rejected",
        "toolu_exit": "exit_nonzero",
    }


def test_the_stored_class_holds_what_the_row_alone_could_not_rebuild(tmp_path):
    """The two cases #4722 exists for: a class that comes from toolDenialKind,
    which no column keeps, and a hook refusal whose prefix closes past the
    400-character snippet."""
    long_hook = (
        "PreToolUse:Bash hook error: [bash -c '" + "x" * 600 + "']: BLOCKED: refused."
    )
    records = [
        _use("toolu_denied", "Bash", {"command": "x"}, ts=f"{DAY}T10:00:00Z"),
        _result(
            "toolu_denied", "Not run.", ts=f"{DAY}T10:00:01Z",
            is_error=True, denial="permission-denied",
        ),
        _use("toolu_longhook", "Bash", {"command": "y"}, ts=f"{DAY}T10:01:00Z"),
        _result("toolu_longhook", long_hook, ts=f"{DAY}T10:01:01Z", is_error=True),
    ]
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    _, rows = _rows(path)
    assert rows["toolu_denied"][7] == "denied_permission-denied"
    assert rows["toolu_longhook"][7] == "hook_block"
    # Neither is recoverable from what the row stores.
    from genesis.transcript_analytics.classify import classify

    assert classify(rows["toolu_denied"][4] or "", is_error=True, denial_kind=None)[0] != (
        "denied_permission-denied"
    )
    assert classify(rows["toolu_longhook"][4] or "", is_error=True, denial_kind=None)[0] != (
        "hook_block"
    )


def test_a_call_with_no_result_is_never_a_row_but_still_scores_as_an_error(transcript):
    """The parser keeps marking an unpaired call ``error`` for the struggle score;
    only the outcome writer leaves it out, because it may still be running."""
    spine, rows = _rows(transcript)
    pending = next(e for e in spine if e.get("tool_use_id") == "toolu_pending")
    assert pending["outcome"] == "error"
    assert pending["has_result"] is False
    assert "toolu_pending" not in rows


def test_failure_text_is_scrubbed_before_it_is_cut(transcript):
    _spine, rows = _rows(transcript)
    snippet = rows["toolu_hook"][4]
    assert snippet is not None
    assert snippet.startswith("PreToolUse:Bash hook error:")
    assert FAKE_KEY not in snippet
    assert len(snippet) <= tool_outcomes.SNIPPET_CHARS


def test_a_scrub_that_withholds_text_stores_no_snippet_but_keeps_the_failure(
    transcript, monkeypatch
):
    from genesis.transcript_analytics import scrub

    monkeypatch.setattr(scrub, "scrub_text", lambda text: (None, True))
    _spine, rows = _rows(transcript)
    assert rows["toolu_hook"][3] == 0
    assert rows["toolu_hook"][4] is None


def test_no_failures_means_only_success_rows(tmp_path):
    path = tmp_path / "clean.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                _use("toolu_a", "Read", {"file_path": "/a"}, ts=f"{DAY}T11:00:00Z"),
                _result("toolu_a", "contents", ts=f"{DAY}T11:00:01Z"),
                _use("toolu_b", "Grep", {"pattern": "x"}, ts=f"{DAY}T11:01:00Z"),
                _result("toolu_b", "No matches", ts=f"{DAY}T11:01:01Z", is_error=False),
            )
        )
        + "\n"
    )
    spine, _ = build_spine_and_haystack(path)
    rows = tool_outcomes.outcome_rows(spine, fallback_session_id="fb")
    assert [r[3] for r in rows] == [1, 1]
    assert all(r[4] is None for r in rows)


def test_new_spine_fields_leave_the_struggle_score_unchanged(transcript):
    """The extra per-call fields are for the writer only; scoring reads the old ones."""
    spine, _ = build_spine_and_haystack(transcript)
    legacy = [
        {k: e[k] for k in ("turn", "type", "tool", "args_summary", "outcome", "error_text")}
        for e in spine
    ]
    assert score_struggle(spine) == score_struggle(legacy)


async def test_insert_is_idempotent_and_never_overwrites_an_existing_row(db, transcript):
    # A row for one call already exists (an earlier cycle, or another writer).
    await db.execute(
        "INSERT INTO tool_call_outcomes (session_id, tool_name, success, timestamp, tool_use_id) "
        "VALUES (?, 'Bash', 1, '2026-11-01T10:01:01Z', 'toolu_hook')",
        (SID,),
    )
    await db.commit()
    _spine, rows = _rows(transcript)
    await tco.insert_outcomes(db, list(rows.values()))
    await tco.insert_outcomes(db, list(rows.values()))  # a later cycle re-reads the transcript
    cur = await db.execute(
        "SELECT tool_use_id, success FROM tool_call_outcomes ORDER BY tool_use_id"
    )
    got = {r[0]: r[1] for r in await cur.fetchall()}
    assert got == {
        "toolu_exit": 0,
        "toolu_hook": 1,  # the existing row stands
        "toolu_ok": 1,
        "toolu_reject": 0,
    }


def test_edit_and_write_are_left_to_the_stop_hook_sensor(transcript):
    """Legacy sensor rows have no tool_use_id, so a second writer for Edit and
    Write would count re-parsed history twice."""
    spine, rows = _rows(transcript)
    edit = next(e for e in spine if e.get("tool_use_id") == "toolu_edit")
    assert edit["has_result"] is True  # recordable in principle...
    assert "toolu_edit" not in rows  # ...but not by this writer


def test_a_classifier_fault_leaves_the_spine_whole(transcript, monkeypatch):
    from genesis.learning.procedural import struggle_detector as sd

    def boom(*_a, **_k):
        raise RuntimeError("synthetic classifier fault")

    clean, _ = build_spine_and_haystack(transcript)
    monkeypatch.setattr(sd, "classify", boom)
    spine, _ = build_spine_and_haystack(transcript)
    assert [e["tool"] for e in spine] == [e["tool"] for e in clean]
    assert tool_outcomes.outcome_rows(spine, fallback_session_id="fb") == []


async def test_a_writer_failure_never_reaches_the_extraction_job(db, monkeypatch, caplog):
    from genesis.memory import extraction_job

    def boom(*_a, **_k):
        raise RuntimeError("synthetic writer fault")

    monkeypatch.setattr(tool_outcomes, "outcome_rows", boom)
    await extraction_job._record_tool_outcomes(db, [{"type": "tool"}], "sess")
    assert "Tool-outcome recording failed" in caplog.text


async def test_the_extraction_job_records_outcomes_from_its_spine(db, transcript):
    from genesis.memory import extraction_job

    spine, _ = build_spine_and_haystack(transcript)
    await extraction_job._record_tool_outcomes(db, spine, "sess")
    cur = await db.execute("SELECT COUNT(*) FROM tool_call_outcomes")
    assert (await cur.fetchone())[0] == 4


async def test_prune_drops_old_rows_and_old_failure_text_only(db):
    """Retention: rows past 180 days go; failure text past 90 days is cleared;
    both timestamp spellings the table holds are compared as instants."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 15, tzinfo=UTC)

    def ago(days, z=False):
        ts = (now - timedelta(days=days)).isoformat()
        return ts.replace("+00:00", "Z") if z else ts

    rows = [
        ("s", "Bash", None, 0, "old text", ago(200), "t_old", "other"),
        ("s", "Bash", None, 0, "mid text", ago(120, z=True), "t_mid", "other"),
        ("s", "Bash", None, 0, "new text", ago(10, z=True), "t_new", "other"),
        ("s", "Read", None, 1, None, ago(179), "t_keep", None),
    ]
    await tco.insert_outcomes(db, rows)
    removed, cleared = await tco.prune(db, now=now)
    cur = await db.execute(
        "SELECT tool_use_id, error_snippet FROM tool_call_outcomes ORDER BY tool_use_id"
    )
    left = {r[0]: r[1] for r in await cur.fetchall()}
    assert (removed, cleared) == (1, 1)
    assert left == {"t_keep": None, "t_mid": None, "t_new": "new text"}


async def test_an_extraction_cycle_records_tool_outcomes(db, tmp_path, monkeypatch):
    """The call site inside run_extraction_cycle, not just the helper."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from genesis.db.crud import cc_sessions
    from genesis.memory import extraction_job

    await cc_sessions.create(
        db,
        id=SID,
        session_type="foreground",
        model="sonnet",
        started_at="2026-11-01T00:00:00",
        last_activity_at="2026-11-01T00:00:00",
        source_tag="foreground",
        status="active",
    )
    text = {"type": "user", "message": {"content": "please list"}}
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [text, *RECORDS]) + "\n")

    async def fake_extract(**_kwargs):
        return SimpleNamespace(
            extractions=[], session_keywords=[], session_topic="", parse_error=None
        )

    monkeypatch.setattr(extraction_job, "_extract_chunk", fake_extract)
    await extraction_job.run_extraction_cycle(
        db=db, store=AsyncMock(), router=AsyncMock(), transcript_dir=tmp_path
    )
    cur = await db.execute("SELECT tool_use_id FROM tool_call_outcomes ORDER BY tool_use_id")
    assert [r[0] for r in await cur.fetchall()] == [
        "toolu_exit",
        "toolu_hook",
        "toolu_ok",
        "toolu_reject",
    ]
    cur = await db.execute(
        "SELECT tool_use_id, error_class FROM tool_call_outcomes"
        " WHERE tool_use_id IN ('toolu_ok', 'toolu_reject')"
    )
    assert dict(await cur.fetchall()) == {"toolu_ok": None, "toolu_reject": "user_rejected"}
    cur = await db.execute(
        "SELECT COUNT(*), COUNT(recorded_at), MIN(recorded_at) > MAX(timestamp)"
        " FROM tool_call_outcomes"
    )
    total, stamped, later = await cur.fetchone()
    # Every writer row carries its insert time, which postdates these calls.
    assert total == stamped == 4 and later == 1


def _ago(days):
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def test_calls_older_than_the_retention_horizons_are_not_rebuilt(tmp_path):
    """A resumed session replays its whole transcript. A call older than the row
    horizon must not come back after the prune deleted it, and failure text older
    than the snippet horizon must not come back either."""
    records = [
        _use("toolu_old", "Bash", {"command": "x"}, ts=_ago(200)),
        _result("toolu_old", "Exit code 1", ts=_ago(200), is_error=True),
        _use("toolu_mid", "Bash", {"command": "y"}, ts=_ago(100)),
        _result("toolu_mid", HOOK_TEXT, ts=_ago(100), is_error=True),
        _use("toolu_new", "Bash", {"command": "z"}, ts=_ago(1)),
        _result("toolu_new", HOOK_TEXT, ts=_ago(1), is_error=True),
    ]
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    _, rows = _rows(path)
    assert set(rows) == {"toolu_mid", "toolu_new"}
    assert rows["toolu_mid"][3] == 0 and rows["toolu_mid"][4] is None
    assert rows["toolu_new"][4] is not None


def test_failure_text_over_the_cap_is_withheld_not_cut(tmp_path):
    """Cutting a long result before scrubbing can leave a JSON string open, and
    the scrubber does not decode escapes in an open string. Over the cap, the
    spine keeps no text at all; the failure is still recorded."""
    from genesis.learning.procedural import struggle_detector as sd

    long_text = "Exit code 1 " + "p" * (sd._FAILURE_TEXT_CAP + 10)
    records = [
        _use("toolu_long", "Bash", {"command": "x"}, ts=f"{DAY}T10:00:00Z"),
        _result("toolu_long", long_text, ts=f"{DAY}T10:00:01Z", is_error=True),
    ]
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    spine, rows = _rows(path)
    entry = next(e for e in spine if e.get("tool_use_id") == "toolu_long")
    assert entry["failure_class"] is not None and entry["failure_text"] is None
    assert rows["toolu_long"][3] == 0 and rows["toolu_long"][4] is None


# The leading "s" of the key as a JSON escape: a backslash, "u", "0073".
_ESCAPED_KEY = "\\u0073" + FAKE_KEY[1:]


def test_an_escaped_credential_in_an_unclosed_quote_is_never_stored():
    text = 'npm ERR! "token=' + _ESCAPED_KEY + " and more words"
    assert "\\u0073" in text  # the escape itself, not a decoded "s"
    assert tool_outcomes._snippet(text) is None


def test_a_harmless_escape_keeps_its_snippet():
    text = 'Error: "caf\\u00e9 not found'
    assert tool_outcomes._snippet(text) == text


def test_an_unparseable_timestamp_is_stored_as_now_so_the_prune_can_reach_it():
    spine = [{
        "type": "tool", "tool": "Bash", "tool_use_id": "toolu_ts", "has_result": True,
        "is_sidechain": False, "session_id": SID, "timestamp": "not a time",
        "failure_class": None, "failure_text": None, "file_path": None,
    }]
    (row,) = tool_outcomes.outcome_rows(spine, fallback_session_id="fb")
    from datetime import datetime

    assert datetime.fromisoformat(row[5]).tzinfo is not None


async def test_a_result_appended_without_new_text_is_recorded(db, tmp_path, monkeypatch):
    """A tool result arriving with no new conversation text yields an empty
    message delta. The cycle must still record the now-finished call."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from genesis.db.crud import cc_sessions
    from genesis.memory import extraction_job

    await cc_sessions.create(
        db,
        id=SID,
        session_type="foreground",
        model="sonnet",
        started_at="2026-11-01T00:00:00",
        last_activity_at="2026-11-01T00:00:00",
        source_tag="foreground",
        status="active",
    )
    text = {"type": "user", "message": {"content": "please list"}}
    first = [text, _use("toolu_late", "Bash", {"command": "sleep 1"}, ts=f"{DAY}T10:00:00Z")]
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in first) + "\n")

    async def fake_extract(**_kwargs):
        return SimpleNamespace(
            extractions=[], session_keywords=[], session_topic="", parse_error=None
        )

    monkeypatch.setattr(extraction_job, "_extract_chunk", fake_extract)

    async def cycle():
        await extraction_job.run_extraction_cycle(
            db=db, store=AsyncMock(), router=AsyncMock(), transcript_dir=tmp_path
        )

    async def recorded():
        cur = await db.execute("SELECT tool_use_id FROM tool_call_outcomes")
        return [r[0] for r in await cur.fetchall()]

    await cycle()
    assert await recorded() == []  # still running: no result yet
    with path.open("a") as f:
        f.write(json.dumps(_result("toolu_late", "done", ts=f"{DAY}T10:00:05Z")) + "\n")
    await cycle()
    assert await recorded() == ["toolu_late"]
