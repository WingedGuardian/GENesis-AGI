"""Known secrets.env values never reach what proactive recall and the session observer emit or store.

Both are places Claude Code keeps a hook's text as its own record, out of the secret-redaction plugin's reach
(measured on 2.1.299), so the hooks replace the values themselves through ``scripts/hooks/secret_values.py``,
which reuses the plugin's loader. Canary values only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "hooks"))
import proactive_memory_hook as pmh  # noqa: E402
import secret_values  # noqa: E402

KEY = "ck_" + "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
OBSERVER = REPO / "scripts" / "hooks" / "session_observer_hook.py"


@pytest.fixture
def canary_secrets(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text(f"CANARY_API_KEY={KEY}\nPLAIN_SETTING=not-a-secret-but-long-enough\n")
    monkeypatch.setenv("SECRETS_PATH", str(path))
    monkeypatch.setattr(secret_values, "_pairs", None)  # load afresh for this test's file
    yield path
    secret_values._pairs = None


def test_redact_replaces_every_spelling_of_a_known_value(canary_secrets):
    import base64

    encoded = base64.b64encode(f"user:{KEY}".encode()).decode()
    text = secret_values.redact(
        f"raw {KEY} and basic {encoded} and plain not-a-secret-but-long-enough"
    )
    assert KEY not in text and "[secrets.env:CANARY_API_KEY]" in text
    assert encoded not in text
    assert "not-a-secret-but-long-enough" in text


def test_redact_passes_text_through_when_values_cannot_load(
    canary_secrets, tmp_path, monkeypatch, capsys
):
    # The secrets file holds the canary, so only a failed load can leave it in place.
    monkeypatch.setattr(secret_values, "_LOADER", tmp_path / "missing.py")
    monkeypatch.setattr(secret_values, "_pairs", None)
    assert secret_values.redact(f"value {KEY}") == f"value {KEY}"
    assert "known values not loaded" in capsys.readouterr().err


def test_recall_output_carries_no_known_value(canary_secrets, capsys):
    out = pmh._writer()
    assert (
        pmh._emit_tracked(out, f"[Memory | 1d | infra | id:abc] the key is {KEY}", "server-recall")
        is True
    )
    printed = capsys.readouterr().out
    assert KEY not in printed and "[secrets.env:CANARY_API_KEY]" in printed


def _observe(payload: dict, home: Path, secrets: Path) -> str:
    env = {
        **os.environ,
        "HOME": str(home),
        "GENESIS_DB_PATH": str(home / "isolated-never-created.db"),
        "SECRETS_PATH": str(secrets),
    }
    env.pop("GENESIS_CC_SESSION", None)
    subprocess.run(
        [sys.executable, str(OBSERVER)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    f = home / ".genesis" / "sessions" / "test-known-values" / "tool_observations.jsonl"
    return f.read_text() if f.exists() else ""


def test_observer_key_info_carries_no_known_value(tmp_path, canary_secrets):
    payload = {
        "tool_name": "Bash",
        "session_id": "test-known-values",
        "tool_input": {"command": f"curl -s -d key={KEY} http://service.example"},
        "tool_response": {"stdout": "ok", "stderr": ""},
    }
    stored = _observe(payload, tmp_path, canary_secrets)
    assert stored and KEY not in stored and "[secrets.env:CANARY_API_KEY]" in stored


def test_observer_stores_no_known_value_even_across_the_truncation_cut(tmp_path, canary_secrets):
    from session_observer_hook import _OUTPUT_CAP

    # Put the value across the truncation point: a cut first would leave a prefix no exact match can find. The
    # observer truncates json.dumps(tool_response), so the cut is measured in that text: 20 characters of the value
    # land before it.
    head = 20
    prefix = len(json.dumps({"stdout": ""})) - 2  # '{"stdout": "'
    output = "x" * (_OUTPUT_CAP - prefix - head) + KEY + "y" * 100
    payload = {
        "tool_name": "Bash",
        "session_id": "test-known-values",
        "tool_input": {"command": "cat app.log"},
        "tool_response": {"stdout": output, "stderr": ""},
    }
    assert (
        json.dumps(payload["tool_response"]).index(KEY) == _OUTPUT_CAP - head
    )  # the fixture straddles the cut
    stored = _observe(payload, tmp_path, canary_secrets)
    assert stored, "the observer stored nothing; the test exercised nothing"
    assert KEY not in stored
    assert KEY[:head] not in stored


@pytest.mark.parametrize("cut", [11, 20, 38])
def test_recall_clipped_through_a_value_shows_none_of_it(canary_secrets, capsys, cut):
    out = pmh._writer()
    pmh._emit_tracked(
        out, f"[Memory | 1d | infra | id:abc] the key is {KEY[:cut]}...", "server-recall"
    )
    emitted = capsys.readouterr().out
    assert KEY[:cut] not in emitted and "[secrets.env:CANARY_API_KEY]" in emitted


def test_the_partial_backstop_leaves_ordinary_text_alone(canary_secrets):
    text = "ck_ is a prefix and 0f1e2d3c is hex, and nothing here is long enough to be a value"
    assert secret_values.redact(text, partial=True) == text


def test_a_value_inside_json_text_in_a_tool_response_is_not_stored(tmp_path, canary_secrets):
    # An MCP result carries JSON text; encoding it before the filter would escape a quoted value twice.
    secrets = canary_secrets
    secrets.write_text(secrets.read_text() + 'QUOTED_SECRET="pa\\"ss-0123456789-abc"\n')
    secret_values._pairs = None
    value = 'pa"ss-0123456789-abc'
    payload = {
        "tool_name": "mcp__example__lookup",
        "session_id": "test-known-values",
        "tool_input": {"name": "x"},
        "tool_response": [{"type": "text", "text": json.dumps({"note": value})}],
    }
    stored = _observe(payload, tmp_path, secrets)
    assert stored, "the observer stored nothing; the test exercised nothing"
    # The tail has no character JSON escapes, so it survives any escaping: it must not be stored at all.
    assert value not in stored and "0123456789-abc" not in stored


def test_the_partial_backstop_skips_a_url_shaped_value(canary_secrets):
    # A URL's start is a scheme and a user other URLs share; only token-like values are matched by their start.
    canary_secrets.write_text(
        canary_secrets.read_text() + "DATABASE_URL=postgres://app:longpassword123456@db.example/x\n"
    )
    secret_values._pairs = None
    text = "connect with postgres://app:other@host.example/y"
    assert secret_values.redact(text, partial=True) == text
