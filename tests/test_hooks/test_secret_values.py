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


def test_redact_passes_text_through_when_values_cannot_load(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_values, "_LOADER", tmp_path / "missing.py")
    monkeypatch.setattr(secret_values, "_pairs", None)
    assert secret_values.redact(f"value {KEY}") == f"value {KEY}"


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
