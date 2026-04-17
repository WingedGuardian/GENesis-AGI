"""Tests for the session observer hook and processor."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.memory.session_observer import (
    _find_observation_files,
    _format_observations_for_prompt,
    _infer_wing_from_files,
    _parse_notes,
    _read_observations,
    process_pending_observations,
)

# ── Hook script tests (pure functions from the hook) ───────────────────


class TestHookExtraction:
    """Test the hook's key info extraction logic."""

    def test_extract_key_info_read(self):
        """Import and test _extract_key_info from the hook script."""
        import importlib.util

        hook_path = Path(__file__).parents[2] / "scripts" / "hooks" / "session_observer_hook.py"
        spec = importlib.util.spec_from_file_location("session_observer_hook", hook_path)
        hook_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook_mod)

        info = hook_mod._extract_key_info("Read", {"file_path": "${HOME}/genesis/src/foo.py"})
        assert info["file_path"] == "${HOME}/genesis/src/foo.py"

    def test_extract_key_info_bash(self):
        import importlib.util

        hook_path = Path(__file__).parents[2] / "scripts" / "hooks" / "session_observer_hook.py"
        spec = importlib.util.spec_from_file_location("session_observer_hook", hook_path)
        hook_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook_mod)

        info = hook_mod._extract_key_info("Bash", {"command": "git status"})
        assert info["command"] == "git status"

    def test_extract_key_info_grep(self):
        import importlib.util

        hook_path = Path(__file__).parents[2] / "scripts" / "hooks" / "session_observer_hook.py"
        spec = importlib.util.spec_from_file_location("session_observer_hook", hook_path)
        hook_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook_mod)

        info = hook_mod._extract_key_info("Grep", {"pattern": "def foo", "path": "/src"})
        assert info["pattern"] == "def foo"
        assert info["path"] == "/src"

    def test_truncate_output_short(self):
        import importlib.util

        hook_path = Path(__file__).parents[2] / "scripts" / "hooks" / "session_observer_hook.py"
        spec = importlib.util.spec_from_file_location("session_observer_hook", hook_path)
        hook_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook_mod)

        assert hook_mod._truncate_output("short") == "short"

    def test_truncate_output_long(self):
        import importlib.util

        hook_path = Path(__file__).parents[2] / "scripts" / "hooks" / "session_observer_hook.py"
        spec = importlib.util.spec_from_file_location("session_observer_hook", hook_path)
        hook_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook_mod)

        long_output = "x" * 5000
        result = hook_mod._truncate_output(long_output)
        assert len(result) < 2200
        assert "truncated" in result


# ── Processor tests ────────────────────────────────────────────────────


class TestParseNotes:
    def test_parse_valid_json(self):
        response = """```json
{
  "notes": [
    {
      "title": "Fixed memory recall filtering",
      "type": "bugfix",
      "narrative": "Wing/room filter was broken.",
      "files": ["src/genesis/mcp/memory/core.py"],
      "concepts": ["memory", "filtering"]
    }
  ]
}
```"""
        notes = _parse_notes(response)
        assert len(notes) == 1
        assert notes[0].title == "Fixed memory recall filtering"
        assert notes[0].note_type == "bugfix"
        assert "core.py" in notes[0].files[0]

    def test_parse_empty_notes(self):
        notes = _parse_notes('```json\n{"notes": []}\n```')
        assert notes == []

    def test_parse_invalid_json(self):
        notes = _parse_notes("not json at all")
        assert notes == []

    def test_parse_missing_title_skipped(self):
        response = '```json\n{"notes": [{"type": "bugfix"}]}\n```'
        notes = _parse_notes(response)
        assert notes == []


class TestInferWing:
    def test_memory_wing(self):
        assert _infer_wing_from_files(["src/genesis/memory/store.py"]) == "memory"

    def test_infrastructure_wing(self):
        assert _infer_wing_from_files(["src/genesis/runtime/_core.py"]) == "infrastructure"

    def test_learning_wing(self):
        assert _infer_wing_from_files(["src/genesis/learning/triage.py"]) == "learning"

    def test_infrastructure_hooks(self):
        assert _infer_wing_from_files(["scripts/hooks/session_observer_hook.py"]) == "infrastructure"

    def test_infrastructure_config(self):
        assert _infer_wing_from_files(["${HOME}/genesis/config/model_routing.yaml"]) == "infrastructure"

    def test_no_match(self):
        assert _infer_wing_from_files(["README.md"]) is None

    def test_empty_files(self):
        assert _infer_wing_from_files([]) is None


class TestFormatObservations:
    def test_format_basic(self):
        obs = [
            {"tool_name": "Read", "key_info": {"file_path": "/foo/bar.py"}, "output_summary": "contents"},
            {"tool_name": "Bash", "key_info": {"command": "git status"}, "output_summary": ""},
        ]
        text = _format_observations_for_prompt(obs)
        assert "[Read]" in text
        assert "[Bash]" in text
        assert "file_path=/foo/bar.py" in text


class TestReadObservations:
    def test_read_valid_jsonl(self, tmp_path):
        obs_file = tmp_path / "test.jsonl"
        obs_file.write_text(
            json.dumps({"tool_name": "Read", "ts": 1.0}) + "\n"
            + json.dumps({"tool_name": "Edit", "ts": 2.0}) + "\n"
        )
        results = _read_observations(obs_file, limit=10)
        assert len(results) == 2

    def test_read_respects_limit(self, tmp_path):
        obs_file = tmp_path / "test.jsonl"
        lines = [json.dumps({"tool_name": f"Tool{i}", "ts": float(i)}) for i in range(20)]
        obs_file.write_text("\n".join(lines) + "\n")
        results = _read_observations(obs_file, limit=5)
        assert len(results) == 5

    def test_read_handles_bad_json(self, tmp_path):
        obs_file = tmp_path / "test.jsonl"
        obs_file.write_text(
            '{"tool_name": "Read"}\n'
            'not json\n'
            '{"tool_name": "Edit"}\n'
        )
        results = _read_observations(obs_file, limit=10)
        assert len(results) == 2


class TestFindObservationFiles:
    def test_finds_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "genesis.memory.session_observer._sessions_dir",
            lambda: tmp_path,
        )
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        obs_file = session_dir / "tool_observations.jsonl"
        obs_file.write_text('{"tool_name": "Read"}\n')

        files = _find_observation_files()
        assert len(files) == 1
        assert files[0] == obs_file

    def test_skips_empty_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "genesis.memory.session_observer._sessions_dir",
            lambda: tmp_path,
        )
        session_dir = tmp_path / "abc123"
        session_dir.mkdir()
        obs_file = session_dir / "tool_observations.jsonl"
        obs_file.write_text("")

        files = _find_observation_files()
        assert len(files) == 0


@pytest.mark.asyncio
async def test_process_pending_observations_no_files(monkeypatch):
    """No observation files → immediate return with zero counts."""
    monkeypatch.setattr(
        "genesis.memory.session_observer._find_observation_files",
        lambda: [],
    )
    store = AsyncMock()
    router = AsyncMock()
    result = await process_pending_observations(store=store, router=router)
    assert result.files_processed == 0
    assert result.observations_read == 0
    router.route_call.assert_not_called()


@pytest.mark.asyncio
async def test_process_pending_observations_stores_notes(tmp_path, monkeypatch):
    """Observations are read, LLM is called, notes are stored."""
    # Setup observation file in a realistic directory structure
    session_dir = tmp_path / "test-session"
    session_dir.mkdir()
    obs_file = session_dir / "tool_observations.jsonl"
    obs_file.write_text(
        json.dumps({"tool_name": "Read", "key_info": {"file_path": "/foo.py"}, "output_summary": "code", "ts": 1.0}) + "\n"
        + json.dumps({"tool_name": "Edit", "key_info": {"file_path": "/foo.py"}, "output_summary": "done", "ts": 2.0}) + "\n"
    )

    monkeypatch.setattr(
        "genesis.memory.session_observer._find_observation_files",
        lambda: [obs_file],
    )

    # Mock router to return a valid extraction
    llm_response = MagicMock()
    llm_response.success = True
    llm_response.content = json.dumps({
        "notes": [{
            "title": "Read and edited foo.py",
            "type": "feature",
            "narrative": "Read foo.py and made edits.",
            "files": ["/foo.py"],
            "concepts": ["editing"],
        }],
    })

    router = AsyncMock()
    router.route_call.return_value = llm_response

    store = AsyncMock()
    store.store.return_value = "mem-123"

    result = await process_pending_observations(store=store, router=router)

    assert result.observations_read == 2
    assert result.notes_extracted == 1
    assert result.notes_stored == 1
    assert result.llm_calls == 1
    router.route_call.assert_called_once()
    store.store.assert_called_once()

    # Check store was called with correct params
    call_kwargs = store.store.call_args
    assert "session_note" in call_kwargs.kwargs["tags"]
    assert call_kwargs.kwargs["source_pipeline"] == "session_observer"
    assert call_kwargs.kwargs["source"] == "session_observer"

    # Verify atomic rename: original file should be gone (renamed to .processing
    # then deleted after processing)
    assert not obs_file.exists()
    assert not obs_file.with_suffix(".jsonl.processing").exists()
