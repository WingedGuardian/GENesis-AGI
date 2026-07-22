"""Tests for the intelligence intake pipeline — atomization paths.

Coverage added by the 2026-07-03 context-layer audit (PR-1): model output
arrives wrapped in ```json fences that the bare json.loads() could not parse,
WING_AUDIT was missing from the multi-finding set, and an empty findings
envelope fell through to json_single storage — together these severed the
surplus → knowledge pipeline and stored raw fenced envelopes as units.
"""

from __future__ import annotations

import json

from genesis.surplus.intake import (
    MULTI_FINDING_TASK_TYPES,
    AtomicFinding,
    IntakeSource,
    atomize,
    kb_content_for_finding,
    source_for_task_type,
)

_FINDINGS_PAYLOAD = {
    "findings": [
        {"title": "A", "content": "alpha"},
        {"title": "B", "content": "beta", "sources": ["s1"], "relevance": "rel"},
    ]
}
_FINDINGS_JSON = json.dumps(_FINDINGS_PAYLOAD)


class TestFenceUnwrapping:
    def test_fenced_multi_finding_json(self):
        findings, path = atomize(f"```json\n{_FINDINGS_JSON}\n```", "anticipatory_research")
        assert path == "json_findings"
        assert [f.title for f in findings] == ["A", "B"]
        assert findings[1].sources == ["s1"]
        assert findings[1].relevance == "rel"

    def test_fence_tag_case_insensitive(self):
        findings, path = atomize(f"```JSON\n{_FINDINGS_JSON}\n```", "anticipatory_research")
        assert path == "json_findings"
        assert len(findings) == 2

    def test_fenced_single_object(self):
        findings, path = atomize('```json\n{"title": "Solo", "note": "x"}\n```', "code_audit")
        assert path == "json_single"
        assert len(findings) == 1
        assert findings[0].title == "Solo"
        assert "```" not in findings[0].content

    def test_unfenced_json_regression(self):
        findings, path = atomize(_FINDINGS_JSON, "gap_clustering")
        assert path == "json_findings"
        assert len(findings) == 2

    def test_inline_fence_not_unwrapped(self):
        content = f"Preamble text.\n```json\n{_FINDINGS_JSON}\n```\nTrailing."
        findings, path = atomize(content, "anticipatory_research")
        # Not a whole-message fence — must NOT parse as findings JSON.
        assert path == "single_item"
        assert len(findings) == 1

    def test_non_json_fence_untouched(self):
        findings, path = atomize("```python\nprint('hi')\n```", "anticipatory_research")
        assert path == "single_item"
        assert findings[0].content == "```python\nprint('hi')\n```"


class TestEmptyFindingsEnvelope:
    def test_empty_envelope_stores_nothing(self):
        findings, path = atomize('{"findings": []}', "anticipatory_research")
        assert findings == []
        assert path == "empty_findings"

    def test_fenced_empty_envelope_stores_nothing(self):
        findings, path = atomize('```json\n{"findings": []}\n```', "anticipatory_research")
        assert findings == []
        assert path == "empty_findings"

    def test_empty_dict_entry_is_kept(self):
        findings, path = atomize('{"findings": [42, null, {}]}', "anticipatory_research")
        # {} yields a finding with empty title/content — kept (dict shape is
        # usable); scalars are dropped. Only fully-unusable envelopes skip.
        assert path == "json_findings"
        assert len(findings) == 1

    def test_scalar_only_envelope_stores_nothing(self):
        findings, path = atomize('{"findings": [42, null]}', "anticipatory_research")
        assert findings == []
        assert path == "empty_findings"

    def test_non_list_findings_falls_to_json_single(self):
        findings, path = atomize('{"findings": "oops"}', "anticipatory_research")
        assert path == "json_single"
        assert len(findings) == 1


class TestItemRobustness:
    """Model output is untrusted — malformed fields must degrade per-item,
    never abort the whole payload back to a raw single_item unit."""

    def test_null_sources_does_not_degrade_payload(self):
        payload = '{"findings": [{"title": "A", "content": "x", "sources": null}]}'
        findings, path = atomize(payload, "anticipatory_research")
        assert path == "json_findings"
        assert findings[0].sources == []

    def test_string_sources_wrapped_not_char_iterated(self):
        payload = '{"findings": [{"title": "A", "content": "x", "sources": "http://example.test"}]}'
        findings, path = atomize(payload, "anticipatory_research")
        assert path == "json_findings"
        assert findings[0].sources == ["http://example.test"]

    def test_null_relevance_is_empty_string(self):
        payload = '{"findings": [{"title": "A", "content": "x", "relevance": null}]}'
        findings, path = atomize(payload, "anticipatory_research")
        assert path == "json_findings"
        assert findings[0].relevance == ""

    def test_nested_list_flattened_one_level(self):
        payload = '[[{"title": "A", "content": "x"}, {"title": "B", "content": "y"}]]'
        findings, path = atomize(payload, "code_audit")
        assert path == "json_findings"
        assert [f.title for f in findings] == ["A", "B"]


class TestFenceVariants:
    def test_crlf_fence(self):
        content = f"```json\r\n{_FINDINGS_JSON}\r\n```"
        findings, path = atomize(content, "anticipatory_research")
        assert path == "json_findings"
        assert len(findings) == 2

    def test_whitespace_padded_fence(self):
        content = f"  ```json\n{_FINDINGS_JSON}\n```  \n"
        findings, path = atomize(content, "anticipatory_research")
        assert path == "json_findings"

    def test_bare_fence_without_json_tag_not_unwrapped(self):
        # Documented behavior: the unwrap requires the ```json tag; a bare
        # fence stays opaque and falls through to single_item.
        content = f"```\n{_FINDINGS_JSON}\n```"
        findings, path = atomize(content, "anticipatory_research")
        assert path == "single_item"


class TestBareArrayPayload:
    """code_audit-style prompts return a bare top-level array, not an envelope."""

    _AUDIT_JSON = json.dumps(
        [
            {
                "file": "src/x.py",
                "line": 3,
                "severity": "high",
                "description": "desc one",
                "confidence": 0.8,
            },
            {
                "file": "src/y.py",
                "line": None,
                "severity": "low",
                "description": "desc two",
                "confidence": 0.6,
            },
        ]
    )

    def test_fenced_bare_array_atomized(self):
        findings, path = atomize(f"```json\n{self._AUDIT_JSON}\n```", "code_audit")
        assert path == "json_findings"
        assert [f.title for f in findings] == ["src/x.py", "src/y.py"]
        assert "desc one" in findings[0].content
        assert "severity" in findings[0].content

    def test_unfenced_bare_array_atomized(self):
        findings, path = atomize(self._AUDIT_JSON, "code_audit")
        assert path == "json_findings"
        assert len(findings) == 2

    def test_string_items_array(self):
        findings, path = atomize('["insight one", "insight two"]', "brainstorm_self")
        assert path == "json_findings"
        assert [f.title for f in findings] == ["insight one", "insight two"]

    def test_empty_array_stores_nothing(self):
        findings, path = atomize("[]", "code_audit")
        assert findings == []
        assert path == "empty_findings"

    def test_untitled_dict_gets_fallback_title(self):
        findings, path = atomize('[{"description": "d"}]', "code_audit")
        assert path == "json_findings"
        assert findings[0].title == "Code Audit finding 1"


class TestWingAudit:
    def test_wing_audit_in_multi_finding_set(self):
        assert "wing_audit" in MULTI_FINDING_TASK_TYPES

    def test_wing_audit_fenced_findings_atomized(self):
        findings, path = atomize(f"```json\n{_FINDINGS_JSON}\n```", "wing_audit")
        assert path == "json_findings"
        assert len(findings) == 2

    def test_wing_audit_source_mapping(self):
        assert source_for_task_type("wing_audit") is IntakeSource.ANTICIPATORY_RESEARCH


class TestExistingPaths:
    def test_blank_content(self):
        assert atomize("", "anticipatory_research") == ([], "empty")
        assert atomize("   \n", "anticipatory_research") == ([], "empty")

    def test_single_output_type_bypasses_parsing(self):
        content = f"```json\n{_FINDINGS_JSON}\n```"
        findings, path = atomize(content, "memory_audit")
        assert path == "single_item"
        assert findings[0].title == "Memory Audit"

    def test_markdown_split_still_works(self):
        content = "## First\nbody one\n## Second\nbody two"
        findings, path = atomize(content, "anticipatory_research")
        assert path == "markdown_split"
        assert [f.title for f in findings] == ["First", "Second"]


class TestKbContent:
    def test_full_shape(self):
        f = AtomicFinding(title="T", content="C", sources=["s1", "s2"], relevance="R")
        assert kb_content_for_finding(f) == "T\n\nC\n\nSources: s1, s2\n\nRelevance: R"

    def test_minimal_shape(self):
        f = AtomicFinding(title="T", content="C")
        assert kb_content_for_finding(f) == "T\n\nC"


# ── source_pipeline honest labels + json_single prose render ─────────────

from genesis.surplus.intake import (  # noqa: E402
    _pipeline_for_source,
    _render_finding_body,
)


class TestPipelineForSource:
    def test_genesis_authored_is_surplus(self):
        for src in (
            IntakeSource.ANTICIPATORY_RESEARCH,
            IntakeSource.BACKGROUND_TASK,
            IntakeSource.USER_DIRECTED,
            IntakeSource.FOREGROUND_WEB,
        ):
            assert _pipeline_for_source(src) == "surplus"

    def test_crawled_sources_get_distinct_labels(self):
        assert _pipeline_for_source(IntakeSource.MODEL_INTELLIGENCE) == "model_intelligence"
        assert _pipeline_for_source(IntakeSource.FREE_MODEL_INVENTORY) == "model_intelligence"
        assert _pipeline_for_source(IntakeSource.GITHUB_LANDSCAPE) == "github_landscape"
        assert _pipeline_for_source(IntakeSource.WEB_MONITORING) == "web_monitoring"
        assert _pipeline_for_source(IntakeSource.SOURCE_DISCOVERY) == "source_discovery"
        assert _pipeline_for_source(IntakeSource.EMAIL_RECON) == "email_recon"


class TestJsonSingleProseRender:
    def test_bare_object_renders_summary_not_raw_json(self):
        # A MULTI_FINDING task returning a bare object (no findings key) →
        # json_single. The body must be readable prose, never a JSON dump.
        findings, path = atomize(
            '{"title": "Idea", "summary": "a readable summary line"}', "brainstorm_self"
        )
        assert path == "json_single"
        assert findings[0].content == "a readable summary line"
        assert "{" not in findings[0].content

    def test_render_finding_body_falls_back_to_key_values(self):
        body = _render_finding_body({"analysis": "x found", "recommendation": "do y"})
        assert body == "analysis: x found\nrecommendation: do y"

    def test_render_finding_body_skips_meta_keys(self):
        # title/file/sources/relevance are structural, not body.
        assert _render_finding_body({"title": "T", "detail": "the detail"}) == "the detail"
