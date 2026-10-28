"""Round-6 review regressions: agent-ID scrub failures reach the row,
option-bearing ``env`` hook commands name the real script, and compact
token counts reject negative values."""

import json

import pytest

from genesis.transcript_analytics import extract
from genesis.transcript_analytics.classify import parse_hook_block
from genesis.transcript_analytics.extract import extract_source

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret


def _write(tmp_path, records):
    path = tmp_path / "source.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return extract_source(path, path.name)


def _spawn_result(structured, content, *, calls=1):
    return [
        {
            "type": "assistant",
            "sessionId": "parent",
            "uuid": "call",
            "message": {
                "id": "turn",
                "content": [{"type": "tool_use", "id": "spawn", "name": "Agent", "input": {}}]
                * calls,
            },
        },
        {
            "type": "user",
            "sessionId": "parent",
            "uuid": "result",
            "toolUseResult": structured,
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "spawn", "content": content}]
            },
        },
    ]


def _result_rows(result):
    return [r for r in result.tables["tool_calls"] if r.get("has_result")]


# --- agent-ID candidates: a scrubber failure is recorded, a drop is not -------


@pytest.mark.parametrize("calls", [1, 2])
def test_agent_id_scrubber_failure_marks_the_row(tmp_path, monkeypatch, calls):
    def failing(text):
        if text == "a05de3b28c4029fea":
            return None, True
        return text, False

    monkeypatch.setattr(extract, "scrub_text", failing)
    result = _write(tmp_path, _spawn_result({"agentId": "a05de3b28c4029fea"}, "done", calls=calls))
    rows = _result_rows(result)
    assert len(rows) == calls
    for row in rows:
        assert row["result_agent_ids"] == []
        assert row["scrub_failed"] is True


def test_dropped_credential_shaped_agent_id_is_not_a_scrub_failure(tmp_path):
    result = _write(tmp_path, _spawn_result({"agentId": TOKEN}, "done"))
    (row,) = _result_rows(result)
    assert row["result_agent_ids"] == []
    assert row["scrub_failed"] is False


def test_clean_agent_id_leaves_scrub_failed_false(tmp_path):
    result = _write(tmp_path, _spawn_result({"agentId": "a05de3b28c4029fea"}, "done"))
    (row,) = _result_rows(result)
    assert row["result_agent_ids"] == ["a05de3b28c4029fea"]
    assert row["scrub_failed"] is False


# --- env options before the hook command --------------------------------------


@pytest.mark.parametrize(
    "command,script",
    [
        ("env -u GH_TOKEN /x/guard.sh", "guard.sh"),
        ("env --unset=GH_TOKEN /x/guard.sh", "guard.sh"),
        ("env --unset GH_TOKEN /x/guard.sh", "guard.sh"),
        ("env -uGH_TOKEN /x/guard.sh", "guard.sh"),
        ("env -C /tmp/work bash /x/guard.sh", "guard.sh"),
        ("env --chdir=/tmp/work python3 /x/hook.py", "hook.py"),
        ("env -i -0 -v /x/guard.sh", "guard.sh"),
        ("env - PATH=/bin /x/guard.sh", "guard.sh"),
        ("env - /x/guard.sh", "guard.sh"),
        ("env -iu GH_TOKEN /x/guard.sh", "guard.sh"),
        ("env --ignore-environment --debug /x/guard.sh", "guard.sh"),
        ("env -u A -u B X=1 /x/guard.sh --flag", "guard.sh"),
        ("env -- /x/guard.sh", "guard.sh"),
        ("env -- X=1 /x/guard.sh", "guard.sh"),
        ("env --block-signal=PIPE /x/guard.sh", "guard.sh"),
        ("/usr/bin/env -u GH_TOKEN /x/guard.sh", "guard.sh"),
        ('env -S "bash /x/guard.sh --mode x"', "guard.sh"),
        ("env --split-string='python3 /x/hook.py'", "hook.py"),
        ("env -S'/x/guard.sh'", "guard.sh"),
        ("env -u GH_TOKEN", "<empty>"),
        ("GH_TOKEN=[REDACTED] env -u OTHER /x/guard.sh", "guard.sh"),
    ],
)
def test_env_options_are_not_the_hook_script(command, script):
    h = parse_hook_block(f"PreToolUse:Bash hook error: [{command}]: msg")
    assert h["hook_script"] == script


# --- compact token counts -------------------------------------------------------


@pytest.mark.parametrize(
    "pre,post,expected",
    [
        (-1, -5, (None, None)),
        (-1, 10, (None, 10)),
        (0, 0, (0, 0)),
        (170098, 44010, (170098, 44010)),
        (2**63, -(2**63), (None, None)),
    ],
)
def test_compact_token_counts_are_nonnegative(tmp_path, pre, post, expected):
    record = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "e",
        "sessionId": "s",
        "timestamp": "2026-10-01T00:00:03Z",
        "compactMetadata": {"trigger": "auto", "preTokens": pre, "postTokens": post},
    }
    (ev,) = [e for e in _write(tmp_path, [record]).tables["events"] if e["uuid"] == "e"]
    assert (ev["pre_tokens"], ev["post_tokens"]) == expected
