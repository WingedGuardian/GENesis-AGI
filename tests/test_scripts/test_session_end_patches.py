"""Tests for session patch writing + topic extraction in the SessionEnd hook."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

# Load the script as a module (not a package — use importlib)
_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "genesis_session_end.py"
_spec = importlib.util.spec_from_file_location("genesis_session_end", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_append_session_patch = _mod._append_session_patch
_extract_topic = _mod._extract_topic


# ── _append_session_patch tests ────────────────────────────────────────────


def test_append_session_patch_creates_file(tmp_path):
    """First session creates the patches file."""
    patches_file = tmp_path / "session_patches.json"
    _append_session_patch(
        patches_file=patches_file,
        session_id="abc123",
        topic_hint="Fixed memory leak in awareness loop",
        message_count=8,
        ended_at="2026-03-26T05:00:00+00:00",
    )

    assert patches_file.exists()
    data = json.loads(patches_file.read_text())
    assert len(data) == 1
    assert data[0]["session_id"] == "abc123"
    assert data[0]["topic"] == "Fixed memory leak in awareness loop"
    assert data[0]["message_count"] == 8


def test_append_session_patch_accumulates(tmp_path):
    """Subsequent sessions append to existing file."""
    patches_file = tmp_path / "session_patches.json"

    _append_session_patch(
        patches_file=patches_file, session_id="s1",
        topic_hint="First", message_count=3,
        ended_at="2026-03-26T05:00:00+00:00",
    )
    _append_session_patch(
        patches_file=patches_file, session_id="s2",
        topic_hint="Second", message_count=5,
        ended_at="2026-03-26T06:00:00+00:00",
    )

    data = json.loads(patches_file.read_text())
    assert len(data) == 2
    assert data[0]["topic"] == "First"
    assert data[1]["topic"] == "Second"


def test_append_session_patch_caps_at_20(tmp_path):
    """Patches file never grows beyond 20 entries."""
    patches_file = tmp_path / "session_patches.json"

    for i in range(25):
        _append_session_patch(
            patches_file=patches_file, session_id=f"s{i}",
            topic_hint=f"Topic {i}", message_count=1,
            ended_at=f"2026-03-26T{i % 24:02d}:00:00+00:00",
        )

    data = json.loads(patches_file.read_text())
    assert len(data) == 20
    # Oldest 5 evicted — first entry should be "Topic 5"
    assert data[0]["topic"] == "Topic 5"


def test_append_session_patch_handles_corrupt_file(tmp_path):
    """Corrupt existing file gets overwritten with single new entry."""
    patches_file = tmp_path / "session_patches.json"
    patches_file.write_text("not valid json{{{")

    _append_session_patch(
        patches_file=patches_file, session_id="s1",
        topic_hint="Recovery", message_count=2,
        ended_at="2026-03-26T05:00:00+00:00",
    )

    data = json.loads(patches_file.read_text())
    assert len(data) == 1
    assert data[0]["topic"] == "Recovery"


def test_append_session_patch_records_empty_topic(tmp_path):
    """Sessions with no topic hint are still recorded."""
    patches_file = tmp_path / "session_patches.json"
    _append_session_patch(
        patches_file=patches_file, session_id="s1",
        topic_hint="", message_count=0,
        ended_at="2026-03-26T05:00:00+00:00",
    )

    data = json.loads(patches_file.read_text())
    assert len(data) == 1
    assert data[0]["topic"] == ""


def test_append_session_patch_deduplicates_by_session_id(tmp_path):
    """Crash+resume: second end for same session_id replaces, not duplicates."""
    patches_file = tmp_path / "session_patches.json"
    _append_session_patch(
        patches_file=patches_file, session_id="s1",
        topic_hint="First attempt", message_count=3,
        ended_at="2026-03-26T05:00:00+00:00",
    )
    _append_session_patch(
        patches_file=patches_file, session_id="s1",
        topic_hint="After resume", message_count=8,
        ended_at="2026-03-26T05:30:00+00:00",
    )

    data = json.loads(patches_file.read_text())
    assert len(data) == 1
    assert data[0]["topic"] == "After resume"
    assert data[0]["message_count"] == 8


# ── _extract_topic tests ──────────────────────────────────────────────────


def test_extract_topic_prefers_first_substantive_message():
    """Picks first real message, not filler."""
    messages = [
        {"text": "yeah"},
        {"text": "Fix the leaked opencode processes and check memory"},
        {"text": "ok commit that"},
    ]
    assert "Fix the leaked" in _extract_topic(messages)


def test_extract_topic_falls_back_when_all_filler():
    """Falls back to provided string when all messages are filler."""
    messages = [{"text": "yes"}, {"text": "ok"}, {"text": "thanks"}]
    assert _extract_topic(messages, fallback="fallback topic") == "fallback topic"


def test_extract_topic_truncates_at_200():
    """Long messages get truncated to 200 chars."""
    messages = [{"text": "x" * 500}]
    result = _extract_topic(messages)
    assert len(result) == 200


def test_extract_topic_empty_messages():
    """Empty message list returns fallback."""
    assert _extract_topic([], fallback="fb") == "fb"
    assert _extract_topic([]) == ""


# ── main() zero-message guard (RC-1: no ghost recent-session rows) ───────────


def _run_main(monkeypatch, tmp_path, *, session_id, messages=None):
    """Invoke the hook's main() with module paths redirected into tmp_path."""
    genesis_dir = tmp_path / ".genesis"
    (genesis_dir / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
    flag = genesis_dir / "cc_context_enabled"
    flag.write_text("1")
    patches = genesis_dir / "session_patches.json"
    if messages:
        (genesis_dir / "sessions" / session_id / "messages.jsonl").write_text(
            "\n".join(json.dumps(m) for m in messages)
        )
    monkeypatch.setattr(_mod, "_GENESIS_DIR", genesis_dir)
    monkeypatch.setattr(_mod, "_FLAG", flag)
    monkeypatch.setattr(_mod, "_PATCHES_FILE", patches)
    monkeypatch.setattr(_mod, "_LAST_SESSION_FILE", genesis_dir / "last.json")
    monkeypatch.setattr(_mod, "_PENDING_BOOKMARK_FILE", genesis_dir / "pending.json")
    monkeypatch.setattr(_mod, "_trigger_essential_knowledge_regen", lambda: None)
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": session_id, "reason": "clear"})),
    )
    _mod.main()
    return patches


def test_main_skips_patch_for_zero_message_session(tmp_path, monkeypatch):
    """A session that ended with no messages must NOT write a ghost patch row."""
    patches = _run_main(monkeypatch, tmp_path, session_id="ghost1", messages=None)
    assert not patches.exists()


def test_main_writes_patch_for_session_with_messages(tmp_path, monkeypatch):
    """A session with real messages still writes its patch (no regression)."""
    patches = _run_main(
        monkeypatch, tmp_path, session_id="real1",
        messages=[{"text": "fix the memory leak in the awareness loop"}],
    )
    assert patches.exists()
    data = json.loads(patches.read_text())
    assert len(data) == 1
    assert data[0]["session_id"] == "real1"
    assert data[0]["message_count"] == 1
