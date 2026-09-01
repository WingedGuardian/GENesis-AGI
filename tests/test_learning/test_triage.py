"""Tests for genesis.learning.triage — summarizer, prefilter, classifier."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from genesis.cc.types import CCOutput
from genesis.learning.triage.classifier import TriageClassifier
from genesis.learning.triage.prefilter import should_skip
from genesis.learning.triage.summarizer import build_summary
from genesis.learning.types import InteractionSummary, TriageDepth

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_output(
    text: str = "done",
    input_tokens: int = 100,
    output_tokens: int = 200,
    **kw,
) -> CCOutput:
    defaults = dict(
        session_id="s1",
        text=text,
        model_used="sonnet",
        cost_usd=0.01,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=500,
        exit_code=0,
    )
    defaults.update(kw)
    return CCOutput(**defaults)


def _make_summary(
    token_count: int = 500,
    tool_calls: list[str] | None = None,
    user_text: str = "do something",
    response_text: str = "done",
) -> InteractionSummary:
    return InteractionSummary(
        session_id="s1",
        user_text=user_text,
        response_text=response_text,
        tool_calls=tool_calls or [],
        token_count=token_count,
        channel="terminal",
        timestamp=datetime.now(UTC),
    )


@dataclass
class FakeRoutingResult:
    success: bool = True
    content: str | None = None


class FakeRouter:
    def __init__(self, content: str = "", success: bool = True):
        self._result = FakeRoutingResult(success=success, content=content)
        self.calls: list[tuple] = []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.calls.append((call_site_id, messages, kwargs))
        return self._result


# ── Summarizer ───────────────────────────────────────────────────────────────


class TestBuildSummary:
    def test_basic(self):
        out = _make_output()
        s = build_summary(out, "sess1", "hello", "terminal")
        assert s.session_id == "sess1"
        assert s.channel == "terminal"
        assert s.token_count == 300  # 100 + 200

    def test_user_text_truncation(self):
        long = "x" * 1000
        s = build_summary(_make_output(), "s", long, "terminal")
        assert len(s.user_text) == 500

    def test_ordinary_response_reaches_the_grader_whole(self):
        """An ordinary reply must not be cut.

        The graders are asked to judge a response; if the pipeline hands them a
        fragment that stops mid-word, the honest reading of that fragment is
        that generation was cut off — and that verdict then lands in permanent
        memory as a fact about the model. Sizing is MEASURED, not guessed: over
        30 days of real outbound messages, mean length 1033 and max 6006, so a
        limit of 1000 silently truncated about two in five graded responses.
        """
        body = "y" * 3213  # the length of the reply that first exposed this
        s = build_summary(_make_output(text=body), "s", "hi", "terminal")
        assert s.response_text == body
        assert s.response_text.endswith(body[-40:]), "the ENDING must survive"

    def test_response_truncated_flag_reflects_real_truncation_only(self):
        """`response_truncated` reports what the RUNTIME did, never what this
        summarizer did. CCOutput.bg_truncated is the real signal."""
        s = build_summary(_make_output(text="short"), "s", "hi", "terminal")
        assert s.response_truncated is False
        s2 = build_summary(
            _make_output(text="short", bg_truncated=True), "s", "hi", "terminal"
        )
        assert s2.response_truncated is True

    def test_summarizer_slicing_never_sets_the_truncated_flag(self):
        """A response elided by THIS code is not a truncated response."""
        s = build_summary(_make_output(text="z" * 40000), "s", "hi", "terminal")
        assert s.response_truncated is False

    def test_response_at_exactly_the_valve_is_untouched(self):
        from genesis.learning.triage.summarizer import _MAX_RESPONSE_TEXT

        body = "E" * _MAX_RESPONSE_TEXT
        s = build_summary(_make_output(text=body), "s", "hi", "terminal")
        assert s.response_text == body

    def test_one_char_over_the_valve_reports_a_positive_elided_count(self):
        """Just past the valve is where an over-generous head+tail would render
        a NEGATIVE count — the existing 30k-char test would not notice."""
        import re

        from genesis.learning.triage.summarizer import _MAX_RESPONSE_TEXT

        s = build_summary(
            _make_output(text="E" * (_MAX_RESPONSE_TEXT + 1)), "s", "hi", "terminal"
        )
        m = re.search(r"\[(-?\d+) characters elided", s.response_text)
        assert m, s.response_text[:200]
        assert int(m.group(1)) > 0, f"negative elided count: {m.group(1)}"

    def test_oversized_response_is_elided_with_a_marker_and_keeps_its_tail(self):
        """Past the safety valve, elide the MIDDLE and say so.

        A grader asked "was this cut off?" answers from the ENDING, so the tail
        is the part that must survive; and the marker tells it the gap was
        imposed here rather than by the model.
        """
        head, tail = "H" * 30000, "T" * 30
        s = build_summary(_make_output(text=head + tail), "s", "hi", "terminal")
        assert len(s.response_text) < len(head + tail)
        assert "elided" in s.response_text
        assert s.response_text.endswith(tail), "the ENDING was dropped"
        assert s.response_text.startswith("H")

    def test_tool_detection_tool_colon(self):
        out = _make_output(text="Tool: Read\nTool: Edit\nresult")
        s = build_summary(out, "s", "hi", "terminal")
        assert s.tool_calls == ["Read", "Edit"]

    def test_tool_detection_using_tool(self):
        out = _make_output(text="Using tool: Bash")
        s = build_summary(out, "s", "hi", "terminal")
        assert s.tool_calls == ["Bash"]

    def test_tool_detection_xml_tag(self):
        out = _make_output(text="<tool_call> Grep\nresult")
        s = build_summary(out, "s", "hi", "terminal")
        assert s.tool_calls == ["Grep"]

    def test_tool_dedup(self):
        out = _make_output(text="Tool: Read\nTool: Read\nUsing tool: Read")
        s = build_summary(out, "s", "hi", "terminal")
        assert s.tool_calls == ["Read"]

    def test_no_tools(self):
        out = _make_output(text="just text")
        s = build_summary(out, "s", "hi", "terminal")
        assert s.tool_calls == []

    def test_timestamp_is_utc(self):
        s = build_summary(_make_output(), "s", "hi", "terminal")
        assert s.timestamp.tzinfo is not None


# ── Prefilter ────────────────────────────────────────────────────────────────


class TestPrefilter:
    def test_skip_low_tokens_no_tools(self):
        assert should_skip(_make_summary(token_count=50, tool_calls=[])) is True

    def test_no_skip_high_tokens(self):
        assert should_skip(_make_summary(token_count=200, tool_calls=[])) is False

    def test_no_skip_with_tools(self):
        assert should_skip(_make_summary(token_count=50, tool_calls=["Read"])) is False

    def test_boundary_99(self):
        assert should_skip(_make_summary(token_count=99)) is True

    def test_boundary_100(self):
        assert should_skip(_make_summary(token_count=100)) is False


# ── Classifier ───────────────────────────────────────────────────────────────


class TestClassifier:
    def test_successful_classification(self):
        resp = json.dumps({"depth": 3, "rationale": "multi-step task"})
        router = FakeRouter(content=resp)
        c = TriageClassifier(router, calibration_path=Path("/nonexistent"))
        result = asyncio.run(
            c.classify(_make_summary())
        )
        assert result.depth == TriageDepth.FULL_ANALYSIS
        assert result.rationale == "multi-step task"
        assert result.skipped_by_prefilter is False

    def test_router_failure_returns_fallback(self):
        router = FakeRouter(success=False)
        c = TriageClassifier(router, calibration_path=Path("/nonexistent"))
        result = asyncio.run(
            c.classify(_make_summary())
        )
        assert result.depth == TriageDepth.QUICK_NOTE

    def test_parse_failure_returns_fallback(self):
        router = FakeRouter(content="not json at all")
        c = TriageClassifier(router, calibration_path=Path("/nonexistent"))
        result = asyncio.run(
            c.classify(_make_summary())
        )
        assert result.depth == TriageDepth.QUICK_NOTE

    def test_invalid_depth_returns_fallback(self):
        router = FakeRouter(content='{"depth": 9, "rationale": "bad"}')
        c = TriageClassifier(router, calibration_path=Path("/nonexistent"))
        result = asyncio.run(
            c.classify(_make_summary())
        )
        assert result.depth == TriageDepth.QUICK_NOTE

    def test_json_embedded_in_text(self):
        resp = 'Here is my answer: {"depth": 2, "rationale": "exploration"} done.'
        router = FakeRouter(content=resp)
        c = TriageClassifier(router, calibration_path=Path("/nonexistent"))
        result = asyncio.run(
            c.classify(_make_summary())
        )
        assert result.depth == TriageDepth.WORTH_THINKING

    def test_prompt_includes_summary_fields(self):
        router = FakeRouter(content='{"depth": 0, "rationale": "skip"}')
        c = TriageClassifier(router, calibration_path=Path("/nonexistent"))
        asyncio.run(
            c.classify(_make_summary(user_text="fix the bug"))
        )
        prompt = router.calls[0][1][0]["content"]
        assert "fix the bug" in prompt
        assert router.calls[0][0] == "29_retrospective_triage"

    def test_calibration_loaded(self, tmp_path):
        cal = tmp_path / "CAL.md"
        cal.write_text("example calibration")
        router = FakeRouter(content='{"depth": 1, "rationale": "ok"}')
        c = TriageClassifier(router, calibration_path=cal)
        asyncio.run(c.classify(_make_summary()))
        prompt = router.calls[0][1][0]["content"]
        assert "example calibration" in prompt

    def test_calibration_reload_on_change(self, tmp_path):
        import os
        import time

        cal = tmp_path / "CAL.md"
        cal.write_text("version1")
        router = FakeRouter(content='{"depth": 1, "rationale": "ok"}')
        c = TriageClassifier(router, calibration_path=cal)
        asyncio.run(c.classify(_make_summary()))

        # Force mtime change
        time.sleep(0.05)
        cal.write_text("version2")
        os.utime(cal, (cal.stat().st_mtime + 1, cal.stat().st_mtime + 1))

        asyncio.run(c.classify(_make_summary()))
        prompt = router.calls[1][1][0]["content"]
        assert "version2" in prompt

    def test_batch(self):
        router = FakeRouter(content='{"depth": 1, "rationale": "ok"}')
        c = TriageClassifier(router, calibration_path=Path("/nonexistent"))
        summaries = [_make_summary(), _make_summary(), _make_summary()]
        results = asyncio.run(
            c.classify_batch(summaries)
        )
        assert len(results) == 3
        assert all(r.depth == TriageDepth.QUICK_NOTE for r in results)
        assert len(router.calls) == 3  # sequential, not parallel

    def test_all_depths(self):
        for d in range(5):
            router = FakeRouter(content=json.dumps({"depth": d, "rationale": f"d{d}"}))
            c = TriageClassifier(router, calibration_path=Path("/nonexistent"))
            result = asyncio.run(
                c.classify(_make_summary())
            )
            assert result.depth == TriageDepth(d)


# ── Calibration file ─────────────────────────────────────────────────────────


_CALIBRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "genesis"
    / "identity"
    / "TRIAGE_CALIBRATION.md"
)

_skip_no_calibration = pytest.mark.skipif(
    not _CALIBRATION_PATH.exists(),
    reason="TRIAGE_CALIBRATION.md is runtime-generated and gitignored",
)


class TestCalibrationFile:
    @_skip_no_calibration
    def test_file_exists(self):
        assert _CALIBRATION_PATH.exists(), f"Missing {_CALIBRATION_PATH}"

    @_skip_no_calibration
    def test_has_frontmatter(self):
        text = _CALIBRATION_PATH.read_text()
        assert text.startswith("---")
        assert "version:" in text

    @_skip_no_calibration
    def test_has_examples_and_rules_section(self):
        text = _CALIBRATION_PATH.read_text()
        assert "## Few-Shot Examples" in text
        assert "## Calibration Rules" in text
