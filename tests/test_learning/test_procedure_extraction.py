"""Tests for the procedural extraction overhaul.

Covers:
- Stream 2: procedure_extraction.py (classify + post-processor)
- Stream 1: struggle_detector.py (action spine + scoring)
- Judge: judge.py (response parsing)
- Schema: scenario field threading
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from genesis.memory.extraction import Extraction
from genesis.memory.procedure_extraction import (
    classify_as_procedure,
)

# ── Stream 2: classify_as_procedure ──────────────────────────────────────────


class TestClassifyAsProcedure:
    """Tests for the procedure candidate classifier."""

    def _make_extraction(self, **overrides) -> Extraction:
        defaults = {
            "content": "When deploying to HA, you must fully uninstall the addon first because Docker caches layers",
            "extraction_type": "procedure_candidate",
            "confidence": 0.85,
            "entities": ["HA Supervisor", "Docker"],
            "scenario": "deploying updated code to a Home Assistant addon",
        }
        defaults.update(overrides)
        return Extraction(**defaults)

    def test_classifies_valid_procedure_candidate(self):
        ext = self._make_extraction()
        result = classify_as_procedure(ext)
        assert result is not None
        assert result["scenario"] == "deploying updated code to a Home Assistant addon"
        assert result["principle"] == ext.content
        assert "HA Supervisor" in result["tools_used"]

    def test_rejects_non_procedure_type(self):
        ext = self._make_extraction(extraction_type="entity")
        assert classify_as_procedure(ext) is None

    def test_rejects_missing_scenario(self):
        ext = self._make_extraction(scenario=None)
        assert classify_as_procedure(ext) is None

    def test_rejects_short_content(self):
        ext = self._make_extraction(content="Too short")
        assert classify_as_procedure(ext) is None

    def test_rejects_empty_scenario(self):
        ext = self._make_extraction(scenario="")
        # Extraction dataclass stores empty string; classify checks truthiness
        assert classify_as_procedure(ext) is None


# ── Stream 1: Struggle detection ────────────────────────────────────────────


class TestBuildActionSpine:
    """Tests for JSONL action spine parser."""

    def _write_jsonl(self, entries: list[dict]) -> Path:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
            return Path(f.name)

    def _tool_use_entry(self, name: str, args: dict, tool_id: str = "t1") -> dict:
        return {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": name,
                    "input": args,
                    "id": tool_id,
                }],
            },
        }

    def _tool_result_entry(self, tool_id: str, content: str, is_error: bool = False) -> dict:
        return {
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": content,
                    "is_error": is_error,
                }],
            },
        }

    def _user_text_entry(self, text: str) -> dict:
        return {
            "type": "user",
            "message": {
                "content": [{
                    "type": "text",
                    "text": text,
                }],
            },
        }

    def test_parses_tool_use_and_result(self):
        from genesis.learning.procedural.struggle_detector import build_action_spine

        path = self._write_jsonl([
            self._tool_use_entry("Bash", {"command": "ls"}, "t1"),
            self._tool_result_entry("t1", "/home/user"),
        ])
        spine = build_action_spine(path)
        assert len(spine) == 1
        assert spine[0]["tool"] == "Bash"
        assert spine[0]["outcome"] == "ok"

    def test_detects_error_result(self):
        from genesis.learning.procedural.struggle_detector import build_action_spine

        path = self._write_jsonl([
            self._tool_use_entry("Bash", {"command": "cat /missing"}, "t1"),
            self._tool_result_entry("t1", "No such file", is_error=True),
        ])
        spine = build_action_spine(path)
        assert len(spine) == 1
        assert spine[0]["outcome"] == "error"
        assert "No such file" in spine[0]["error_text"]

    def test_captures_user_messages(self):
        from genesis.learning.procedural.struggle_detector import build_action_spine

        path = self._write_jsonl([
            self._user_text_entry("that didn't work, try again"),
        ])
        spine = build_action_spine(path)
        assert len(spine) == 1
        assert spine[0]["type"] == "user"

    def test_empty_file(self):
        from genesis.learning.procedural.struggle_detector import build_action_spine

        path = self._write_jsonl([])
        spine = build_action_spine(path)
        assert spine == []

    def test_missing_file(self):
        from genesis.learning.procedural.struggle_detector import build_action_spine

        spine = build_action_spine(Path("/nonexistent/path.jsonl"))
        assert spine == []


class TestScoreStruggle:
    """Tests for struggle scoring heuristics."""

    def _make_spine(self, tools: list[tuple[str, str, str]]) -> list[dict]:
        """Create spine from (tool, args, outcome) tuples."""
        spine = []
        for i, (tool, args, outcome) in enumerate(tools, 1):
            spine.append({
                "turn": i,
                "type": "tool",
                "tool": tool,
                "args_summary": args,
                "outcome": outcome,
                "error_text": "error" if outcome == "error" else "",
            })
        return spine

    def test_no_struggle_clean_session(self):
        from genesis.learning.procedural.struggle_detector import score_struggle

        spine = self._make_spine([
            ("Bash", "ls", "ok"),
            ("Read", "/path", "ok"),
            ("Edit", "file.py", "ok"),
        ])
        assert score_struggle(spine) < 0.3

    def test_high_error_rate_scores_high(self):
        from genesis.learning.procedural.struggle_detector import score_struggle

        spine = self._make_spine([
            ("Bash", "cmd1", "error"),
            ("Bash", "cmd2", "error"),
            ("Bash", "cmd3", "error"),
            ("Read", "file", "ok"),
        ])
        # 3/4 = 75% error rate → should score high
        assert score_struggle(spine) >= 0.3

    def test_retries_score_high(self):
        from genesis.learning.procedural.struggle_detector import score_struggle

        spine = self._make_spine([
            ("Bash", "scp -r dir/ dest/", "ok"),
            ("Bash", "scp -r dir/* dest/", "ok"),
            ("Bash", "scp dir/ dest/", "ok"),
            ("Bash", "rsync dir/ dest/", "ok"),
        ])
        # 3 retries of same tool with different args
        assert score_struggle(spine) >= 0.2

    def test_empty_spine_scores_zero(self):
        from genesis.learning.procedural.struggle_detector import score_struggle

        assert score_struggle([]) == 0.0

    def test_few_tool_calls_scores_zero(self):
        from genesis.learning.procedural.struggle_detector import score_struggle

        spine = self._make_spine([("Bash", "ls", "ok")])
        assert score_struggle(spine) == 0.0


class TestFormatSpineForJudge:
    """Tests for Judge-ready spine formatting."""

    def test_formats_tool_entry(self):
        from genesis.learning.procedural.struggle_detector import format_spine_for_judge

        spine = [{
            "turn": 1, "type": "tool", "tool": "Bash",
            "args_summary": '{"command": "ls"}', "outcome": "ok", "error_text": "",
        }]
        text = format_spine_for_judge(spine)
        assert "[T=1] TOOL: Bash" in text
        assert "-> OK" in text

    def test_formats_error_entry(self):
        from genesis.learning.procedural.struggle_detector import format_spine_for_judge

        spine = [{
            "turn": 2, "type": "tool", "tool": "Read",
            "args_summary": "/missing.py", "outcome": "error",
            "error_text": "File not found",
        }]
        text = format_spine_for_judge(spine)
        assert "-> ERR: File not found" in text

    def test_formats_user_entry(self):
        from genesis.learning.procedural.struggle_detector import format_spine_for_judge

        spine = [{
            "turn": 3, "type": "user", "tool": None,
            "args_summary": "try again", "outcome": "ok", "error_text": "",
        }]
        text = format_spine_for_judge(spine)
        assert '[T=3] USER: "try again"' in text


# ── Judge response parsing ──────────────────────────────────────────────────


class TestJudgeResponseParsing:
    """Tests for Judge LLM response parsing."""

    def test_parses_valid_json_in_backticks(self):
        from genesis.learning.procedural.judge import _parse_judge_response

        response = '''```json
{
  "worth_storing": true,
  "reason": "valid procedure",
  "task_type": "ha-addon-deploy",
  "principle": "Always uninstall HA addon before rebuild",
  "steps": ["1. Uninstall", "2. Rebuild"],
  "tools_used": ["Bash"],
  "context_tags": ["ha", "docker"]
}
```'''
        data = _parse_judge_response(response)
        assert data is not None
        assert data["task_type"] == "ha-addon-deploy"

    def test_rejects_worth_storing_false(self):
        from genesis.learning.procedural.judge import _parse_judge_response

        response = '```json\n{"worth_storing": false, "reason": "too specific"}\n```'
        assert _parse_judge_response(response) is None

    def test_rejects_missing_required_fields(self):
        from genesis.learning.procedural.judge import _parse_judge_response

        response = '```json\n{"worth_storing": true, "task_type": "test"}\n```'
        assert _parse_judge_response(response) is None  # missing principle, steps

    def test_rejects_malformed_json(self):
        from genesis.learning.procedural.judge import _parse_judge_response

        assert _parse_judge_response("not json at all") is None

    def test_parses_raw_json_without_backticks(self):
        from genesis.learning.procedural.judge import _parse_judge_response

        response = '{"worth_storing": true, "task_type": "test", "principle": "do X", "steps": ["1"]}'
        data = _parse_judge_response(response)
        assert data is not None


# ── Schema: scenario threading ──────────────────────────────────────────────


class TestScenarioField:
    """Tests for scenario field in Extraction dataclass."""

    def test_extraction_has_scenario_field(self):
        ext = Extraction(
            content="test",
            extraction_type="procedure_candidate",
            confidence=0.9,
            scenario="when deploying to HA",
        )
        assert ext.scenario == "when deploying to HA"

    def test_scenario_defaults_to_none(self):
        ext = Extraction(
            content="test",
            extraction_type="entity",
            confidence=0.9,
        )
        assert ext.scenario is None

    def test_parse_captures_scenario(self):
        from genesis.memory.extraction import parse_extraction_response_full

        response = '''```json
{
  "extractions": [{
    "content": "Always uninstall HA addon before rebuild",
    "type": "procedure_candidate",
    "confidence": 0.85,
    "entities": ["HA Supervisor"],
    "scenario": "deploying updated code to HA addon",
    "relationships": []
  }],
  "session_keywords": ["ha"],
  "session_topic": "HA deployment"
}
```'''
        result = parse_extraction_response_full(response)
        assert len(result.extractions) == 1
        ext = result.extractions[0]
        assert ext.extraction_type == "procedure_candidate"
        assert ext.scenario == "deploying updated code to HA addon"

    def test_parse_procedure_candidate_type_not_defaulted(self):
        """Verify procedure_candidate is in the valid type list."""
        from genesis.memory.extraction import parse_extraction_response_full

        response = '''```json
{
  "extractions": [{
    "content": "test content for procedure",
    "type": "procedure_candidate",
    "confidence": 0.8,
    "entities": [],
    "relationships": []
  }],
  "session_keywords": [],
  "session_topic": ""
}
```'''
        result = parse_extraction_response_full(response)
        assert result.extractions[0].extraction_type == "procedure_candidate"
