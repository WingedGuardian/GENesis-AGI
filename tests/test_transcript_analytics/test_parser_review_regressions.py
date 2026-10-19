"""Independent checks for the provider-record and scrub-failure contracts."""

import io
import json

import pytest

from genesis.transcript_analytics import extract
from genesis.transcript_analytics.classify import classify


@pytest.mark.parametrize(
    "text",
    [
        "PreToolUse:Bash hook error: [gate.sh]: blocked",
        "<tool_use_error>private</tool_use_error>",
        "Exit code 2",
    ],
)
def test_explicit_success_never_becomes_failure(text):
    assert classify(text, is_error=False, denial_kind=None) == (None, None)


def test_synthetic_api_error_retained_without_provider_id(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s",
                "isApiErrorMessage": True,
                "error": "rate_limit",
                "message": {
                    "model": "<synthetic>",
                    "stop_reason": "end_turn",
                    "usage": {"output_tokens": 99},
                    "content": [{"type": "text", "text": "API Error: 429"}],
                },
            }
        )
        + "\n"
    )
    fragment = extract.extract_source(path, path.name).tables["fragments"][0]
    assert fragment["is_api_error"] is True
    assert fragment["message_id"] is None
    assert fragment["output_tokens"] is None and not fragment["has_usage"]
    assert fragment["stop_reason"] == "end_turn"  # observed assertion, never a usable usage vector


def test_read_never_crosses_captured_prefix(tmp_path, monkeypatch):
    class Reader(io.BytesIO):
        def readline(self, limit=-1):
            assert limit == 4
            return super().readline(limit)

    monkeypatch.setattr(extract, "open", lambda *args: Reader(b"a" * 100000), raising=False)
    assert list(extract._complete_lines(tmp_path / "x", 4, {"consumed": 0})) == []


def test_all_text_tables_preserve_scrub_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "scrub_text", lambda text: (None, bool(text)))
    path = tmp_path / "agent-example.jsonl"
    path.write_text(
        json.dumps({"type": "ai-title", "sessionId": "s", "aiTitle": "private"})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "sessionId": "s",
                "message": {"id": "m", "model": "private", "content": []},
            }
        )
        + "\n"
    )
    path.with_suffix(".meta.json").write_text(
        json.dumps({"description": "private", "agentType": "worker"})
    )
    result = extract.extract_source(path, path.name)
    for table in ("fragments", "session_meta", "agents"):
        assert result.tables[table][0]["scrub_failed"] is True


def test_binary_payload_length_is_not_empty(tmp_path):
    payload = [{"type": "image", "source": {"type": "base64", "data": "x" * 100}}]
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "is_error": False,
                            "content": payload,
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    row = extract.extract_source(path, path.name).tables["tool_calls"][0]
    assert row["result_len"] == len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    assert row["error_text"] is None
