"""Extractor tests on synthetic transcripts shaped like the measured corpus
(plan §2-§14). No real transcript content is used."""

import json

import pytest

from genesis.transcript_analytics.extract import extract_source

SID = "11111111-2222-3333-4444-555555555555"


def _write(path, records, *, trailing_newline=True, raw_tail=None):
    lines = [json.dumps(r) for r in records]
    text = "\n".join(lines) + ("\n" if trailing_newline else "")
    if raw_tail is not None:
        text += raw_tail
    path.write_text(text)
    return path


def _asst(mid, blocks, *, ts, stop=None, out=0, uuid=None, model="claude-x", **extra):
    rec = {
        "type": "assistant",
        "uuid": uuid or f"u-{mid}-{ts}",
        "sessionId": SID,
        "timestamp": ts,
        "requestId": f"req-{mid}",
        "entrypoint": "cli",
        "cwd": "/w",
        "gitBranch": "main",
        "version": "2.1.280",
        "message": {
            "id": mid,
            "model": model,
            "stop_reason": stop,
            "content": blocks,
            "usage": {
                "input_tokens": 3,
                "output_tokens": out,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 50,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 10,
                    "ephemeral_1h_input_tokens": 40,
                },
                "output_tokens_details": {"thinking_tokens": 7},
            },
        },
    }
    rec.update(extra)
    return rec


def _result(tool_use_id, content, *, ts, is_error="absent", tur=None, denial=None, uuid=None):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error != "absent":
        block["is_error"] = is_error
    rec = {
        "type": "user",
        "uuid": uuid or f"r-{tool_use_id}",
        "sessionId": SID,
        "timestamp": ts,
        "message": {"role": "user", "content": [block]},
    }
    if tur is not None:
        rec["toolUseResult"] = tur
    if denial:
        rec["toolDenialKind"] = denial
    return rec


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d / f"{SID}.jsonl"


def test_bash_error_pairs_with_exit_code_latency_and_no_redundant_tur(src):
    _write(
        src,
        [
            _asst(
                "msg_1",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_A",
                        "name": "Bash",
                        "input": {"command": "make check", "description": "run checks"},
                    }
                ],
                ts="2026-10-01T00:00:00.000Z",
                stop="tool_use",
                out=40,
            ),
            _result(
                "toolu_A",
                "Exit code 2\nboom",
                ts="2026-10-01T00:00:01.500Z",
                is_error=True,
                tur="Error: Exit code 2\nboom",
            ),
        ],
    )
    r = extract_source(src, "proj/x.jsonl")
    (tc,) = r.tables["tool_calls"]
    assert tc["tool_use_id"] == "toolu_A" and tc["tool"] == "Bash" and tc["message_id"] == "msg_1"
    assert (
        tc["is_error"] is True
        and tc["error_source"] == "flag"
        and tc["error_class"] == "exit_nonzero"
    )
    assert tc["exit_code"] == 2 and tc["error_text"] == "Exit code 2\nboom"
    assert tc["tool_use_result_text"] is None  # merely "Error: " + content
    assert tc["latency_ms"] == 1500 and tc["has_result"] is True
    assert tc["command"] == "make check" and tc["description"] == "run checks"
    assert tc["line_no_call"] == 1 and tc["line_no_result"] == 2


def test_tool_use_result_kept_when_it_adds_information(src):
    _write(
        src,
        [
            _asst(
                "msg_1",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_A",
                        "name": "Edit",
                        "input": {"file_path": "/w/a.py"},
                    }
                ],
                ts="2026-10-01T00:00:00Z",
                stop="tool_use",
            ),
            _result(
                "toolu_A",
                [
                    {
                        "type": "text",
                        "text": "<tool_use_error>String to replace not found</tool_use_error>",
                    }
                ],
                ts="2026-10-01T00:00:01Z",
                is_error=True,
                tur="Error: String to replace not found in file x",
            ),
        ],
    )
    (tc,) = extract_source(src, "p").tables["tool_calls"]
    assert tc["error_class"] == "tool_use_error"
    assert tc["tool_use_result_text"] == "Error: String to replace not found in file x"
    assert tc["file_path"] == "/w/a.py"


def test_hook_block_fields_and_denial(src):
    text = "PreToolUse:Bash hook error: [${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook git_push_guard.py]: BLOCKED"
    _write(
        src,
        [
            _asst(
                "msg_1",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_H",
                        "name": "Bash",
                        "input": {"command": "git push"},
                    }
                ],
                ts="2026-10-01T00:00:00Z",
                stop="tool_use",
            ),
            _result(
                "toolu_H",
                text,
                ts="2026-10-01T00:00:00.2Z",
                is_error=True,
                denial="permission-rule",
            ),
        ],
    )
    (tc,) = extract_source(src, "p").tables["tool_calls"]
    assert tc["error_class"] == "hook_block" and tc["denial_kind"] == "permission-rule"
    assert (
        tc["hook_event"] == "PreToolUse"
        and tc["hook_tool"] == "Bash"
        and tc["hook_script"] == "git_push_guard.py"
    )


def test_absent_flag_mcp_json_error_is_text_detected(src):
    _write(
        src,
        [
            _asst(
                "msg_1",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_M",
                        "name": "mcp__genesis-health__follow_up_list",
                        "input": {"status": "x"},
                    }
                ],
                ts="2026-10-01T00:00:00Z",
                stop="tool_use",
            ),
            _result("toolu_M", '{"error":"Invalid status"}', ts="2026-10-01T00:00:01Z"),
        ],
    )
    (tc,) = extract_source(src, "p").tables["tool_calls"]
    assert (
        tc["is_error"] is None and tc["error_source"] == "text" and tc["error_class"] == "mcp_error"
    )
    assert tc["mcp_server"] == "genesis-health" and tc["mcp_tool"] == "follow_up_list"
    assert tc["error_text"] == '{"error":"Invalid status"}'


def test_successful_result_stores_length_not_text(src):
    _write(
        src,
        [
            _asst(
                "msg_1",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_R",
                        "name": "Read",
                        "input": {"file_path": "/w/b"},
                    }
                ],
                ts="2026-10-01T00:00:00Z",
                stop="tool_use",
            ),
            _result(
                "toolu_R",
                "line one\nline two",
                ts="2026-10-01T00:00:01Z",
                is_error=False,
                tur={"stdout": "", "stderr": "", "interrupted": False},
            ),
        ],
    )
    (tc,) = extract_source(src, "p").tables["tool_calls"]
    assert tc["is_error"] is False and tc["error_class"] is None and tc["error_text"] is None
    assert tc["result_len"] == len("line one\nline two") and tc["interrupted"] is False


def test_fragments_line_order_streaming_usage_and_synthetic_excluded(src):
    # Subagent-style: partial output counts climb, stop_reason only on the last fragment.
    _write(
        src,
        [
            _asst(
                "msg_S",
                [{"type": "thinking", "thinking": ""}],
                ts="2026-10-01T00:00:00Z",
                out=1,
                uuid="f1",
            ),
            _asst(
                "msg_S",
                [{"type": "text", "text": "hi"}],
                ts="2026-10-01T00:00:01Z",
                out=2,
                uuid="f2",
            ),
            _asst(
                "msg_S",
                [{"type": "tool_use", "id": "toolu_Z", "name": "Read", "input": {}}],
                ts="2026-10-01T00:00:02Z",
                out=305,
                stop="tool_use",
                uuid="f3",
            ),
            _asst(
                "6f9619ff-8b86-d011-b42d-00cf4fc964ff",
                [{"type": "text", "text": "x"}],
                ts="2026-10-01T00:00:03Z",
                model="<synthetic>",
                uuid="syn",
            ),
        ],
    )
    fr = extract_source(src, "p").tables["fragments"]
    assert [f["uuid"] for f in fr] == ["f1", "f2", "f3"]
    assert [f["line_no"] for f in fr] == [1, 2, 3]
    assert [f["output_tokens"] for f in fr] == [1, 2, 305]
    assert fr[2]["stop_reason"] == "tool_use" and fr[0]["stop_reason"] is None
    assert (
        fr[0]["cache_create_5m"] == 10
        and fr[0]["cache_create_1h"] == 40
        and fr[0]["thinking_tokens"] == 7
    )
    assert fr[2]["n_tool_use"] == 1 and fr[1]["has_text"] is True and fr[0]["has_thinking"] is True


def test_torn_tail_and_stop_at_are_respected(src):
    good = [_asst("msg_1", [{"type": "text", "text": "a"}], ts="2026-10-01T00:00:00Z", uuid="a1")]
    _write(
        src, good, raw_tail='{"type":"assistant","uuid":"torn"'
    )  # no newline: still being written
    r = extract_source(src, "p")
    assert [f["uuid"] for f in r.tables["fragments"]] == ["a1"]
    assert r.stats["bytes_read"] == len(json.dumps(good[0])) + 1
    # stop_at below the first newline reads nothing
    r2 = extract_source(src, "p", stop_at=5)
    assert r2.tables["fragments"] == [] and r2.stats["bytes_read"] == 0


def test_malformed_lines_are_counted_not_fatal(src):
    _write(
        src,
        [_asst("msg_1", [{"type": "text", "text": "a"}], ts="2026-10-01T00:00:00Z")],
        raw_tail="not json at all\n[1,2]\n",
    )
    r = extract_source(src, "p")
    assert len(r.tables["fragments"]) == 1 and r.stats["malformed"] == 2 and r.stats["lines"] == 3


def test_hooks_events_and_session_meta(src):
    _write(
        src,
        [
            {
                "type": "attachment",
                "uuid": "h1",
                "sessionId": SID,
                "timestamp": "2026-10-01T00:00:00Z",
                "attachment": {
                    "type": "hook_success",
                    "hookName": "PreToolUse:Bash",
                    "hookEvent": "PreToolUse",
                    "toolUseID": "toolu_A",
                    "command": "python3 x.py",
                    "exitCode": 0,
                    "durationMs": 391,
                    "content": "ok",
                    "stdout": "abc",
                    "stderr": "",
                },
            },
            {
                "type": "attachment",
                "uuid": "h2",
                "sessionId": SID,
                "timestamp": "2026-10-01T00:00:01Z",
                "attachment": {
                    "type": "hook_cancelled",
                    "hookName": "UserPromptSubmit",
                    "hookEvent": "UserPromptSubmit",
                    "command": "slow.sh",
                    "durationMs": 8034,
                    "timedOut": True,
                    "timeoutMs": 8000,
                },
            },
            {
                "type": "attachment",
                "uuid": "nh",
                "sessionId": SID,
                "attachment": {"type": "date", "date": "x"},
            },
            {
                "type": "system",
                "subtype": "turn_duration",
                "uuid": "e1",
                "sessionId": SID,
                "timestamp": "2026-10-01T00:00:02Z",
                "durationMs": 4117,
                "messageCount": 38,
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "e2",
                "sessionId": SID,
                "timestamp": "2026-10-01T00:00:03Z",
                "compactMetadata": {"trigger": "auto", "preTokens": 170098, "postTokens": 44010},
            },
            {"type": "ai-title", "aiTitle": "Fix the thing", "sessionId": SID},
            {
                "type": "pr-link",
                "sessionId": SID,
                "prNumber": 42,
                "prRepository": "o/r",
                "timestamp": "2026-10-01T00:00:04Z",
            },
            {"type": "last-prompt", "lastPrompt": "private words", "sessionId": SID},
        ],
    )
    t = extract_source(src, "p").tables
    hooks = {h["uuid"]: h for h in t["hooks"]}
    assert set(hooks) == {"h1", "h2"}
    assert (
        hooks["h1"]["duration_ms"] == 391
        and hooks["h1"]["exit_code"] == 0
        and hooks["h1"]["stdout_len"] == 3
    )
    assert hooks["h2"]["timed_out"] is True and hooks["h2"]["kind"] == "hook_cancelled"
    ev = {e["uuid"]: e for e in t["events"]}
    assert ev["e1"]["duration_ms"] == 4117 and ev["e1"]["message_count"] == 38
    assert (
        ev["e2"]["pre_tokens"] == 170098
        and ev["e2"]["post_tokens"] == 44010
        and ev["e2"]["trigger"] == "auto"
    )
    meta = {(m["kind"], m["value"]) for m in t["session_meta"]}
    assert ("ai-title", "Fix the thing") in meta and ("pr-link", "42") in meta
    assert not any(
        m["kind"] == "last-prompt" for m in t["session_meta"]
    )  # prompt text is not copied


def test_unmatched_call_and_orphan_result(src):
    _write(
        src,
        [
            _asst(
                "msg_1",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_open",
                        "name": "Bash",
                        "input": {"command": "sleep"},
                    }
                ],
                ts="2026-10-01T00:00:00Z",
                stop="tool_use",
            ),
            _result("toolu_orphan", "hello", ts="2026-10-01T00:00:01Z"),
        ],
    )
    rows = {r["tool_use_id"]: r for r in extract_source(src, "p").tables["tool_calls"]}
    assert rows["toolu_open"]["has_result"] is False and rows["toolu_open"]["is_error"] is None
    assert rows["toolu_orphan"]["tool"] is None and rows["toolu_orphan"]["has_result"] is True


def test_secrets_are_scrubbed_in_command_and_error_text(src):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret — synthetic fixture
    _write(
        src,
        [
            _asst(
                "msg_1",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_S",
                        "name": "Bash",
                        "input": {"command": f"GH_TOKEN={token} gh api user"},
                    }
                ],
                ts="2026-10-01T00:00:00Z",
                stop="tool_use",
            ),
            _result(
                "toolu_S",
                f"Exit code 1\nbad credentials for {token}",
                ts="2026-10-01T00:00:01Z",
                is_error=True,
            ),
        ],
    )
    (tc,) = extract_source(src, "p").tables["tool_calls"]
    assert token not in (tc["command"] or "") and token not in (tc["error_text"] or "")
    assert tc["scrub_failed"] is False


def test_agent_meta_sidecar(tmp_path):
    sub = tmp_path / SID / "subagents"
    sub.mkdir(parents=True)
    f = sub / "agent-abc123.jsonl"
    _write(
        f,
        [
            {
                "type": "user",
                "uuid": "x",
                "sessionId": SID,
                "agentId": "abc123",
                "isSidechain": True,
                "message": {"role": "user", "content": "go"},
            }
        ],
    )
    (sub / "agent-abc123.meta.json").write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "description": "find it",
                "toolUseId": "toolu_P",
                "spawnDepth": 1,
                "requestShape": "foreground",
                "requestNonInteractive": True,
            }
        )
    )
    (a,) = extract_source(f, f"{SID}/subagents/agent-abc123.jsonl").tables["agents"]
    assert a == {
        "source_file": f"{SID}/subagents/agent-abc123.jsonl",
        "agent_id": "abc123",
        "session_id": SID,
        "agent_type": "Explore",
        "description": "find it",
        "tool_use_id": "toolu_P",
        "spawn_depth": 1,
        "request_shape": "foreground",
        "non_interactive": True,
        "scrub_failed": False,
    }


def test_malformed_field_types_do_not_discard_later_valid_records(tmp_path):
    from genesis.transcript_analytics.extract import extract_source

    records = [
        {"type": ["user"]},
        {"type": "system", "subtype": {}},
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "bad",
                        "content": [{"type": "text", "text": None}],
                    }
                ]
            },
        },
        {"type": "assistant", "message": {"id": "valid", "model": "example", "content": []}},
    ]
    path = tmp_path / "source.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    result = extract_source(path, path.name)
    assert [row["message_id"] for row in result.tables["fragments"]] == ["valid"]


def test_user_controlled_labels_are_scrubbed_but_identity_stays_exact(tmp_path):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret — synthetic fixture
    path = tmp_path / "agent-example.jsonl"
    path.with_suffix(".meta.json").write_text(
        json.dumps({"agentType": token, "requestShape": token})
    )
    records = [
        {"type": "ai-title", "aiTitle": token, "sessionId": "session-example"},
        {
            "type": "assistant",
            "cwd": token,
            "gitBranch": token,
            "message": {
                "id": "message-example",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-example",
                        "name": "Bash",
                        "input": {"file_path": token, "subagent_type": token, "skill": token},
                    }
                ],
            },
        },
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    result = extract_source(path, path.name)
    assert token not in json.dumps(result.tables)
    assert result.tables["tool_calls"][0]["tool_use_id"] == "tool-example"
