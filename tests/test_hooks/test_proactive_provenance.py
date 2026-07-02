"""WS-7 / D12: the proactive hook must label KB results as external-world.

A knowledge_base hit injected into the prompt should carry its external source
tier (``KB·<tier>``) so the model never reads ingested content as its own
first-party memory. Episodic results keep the plain ``Memory`` tag.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Hook lives outside the package tree — add scripts/ to import path.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import proactive_memory_hook as hook  # noqa: E402


def test_format_results_labels_kb_with_source_tier(monkeypatch):
    monkeypatch.setattr(hook, "_enrich_with_metadata", lambda results: None)
    out = hook._format_results([
        {
            "memory_id": "kb123456ab", "content": "external API doc",
            "collection": "knowledge_base", "source_pipeline": "recon",
            "_wing": "research", "_created_at": "", "memory_class": "fact",
        },
    ])
    assert "KB·recon" in out
    assert "external API doc" in out


def test_format_results_kb_null_pipeline_safe(monkeypatch):
    monkeypatch.setattr(hook, "_enrich_with_metadata", lambda results: None)
    out = hook._format_results([
        {
            "memory_id": "kb999", "content": "doc", "collection": "knowledge_base",
            "source_pipeline": None, "_wing": "", "_created_at": "",
            "memory_class": "fact",
        },
    ])
    # NULL pipeline still labels as KB (external), with the generic 'ext' tier.
    assert "KB·ext" in out


def test_format_results_episodic_keeps_plain_memory_tag(monkeypatch):
    monkeypatch.setattr(hook, "_enrich_with_metadata", lambda results: None)
    out = hook._format_results([
        {
            "memory_id": "ep123456ab", "content": "my own note",
            "collection": "episodic_memory", "_wing": "memory",
            "_created_at": "", "memory_class": "fact",
        },
    ])
    assert out.startswith("[Memory")
    assert "KB·" not in out


# ── PR2: strip leaked boundary markers; stop blunt-dropping wrapped hits ─────


def test_is_garbage_no_longer_drops_external_content_marker():
    # PR2 removed the blunt drop: a hit carrying a leaked <external-content>
    # marker must SURFACE (stripped+labeled), not be silently discarded.
    assert hook._is_garbage("<external-content source='x'>hello</external-content>") is False


def test_is_garbage_still_filters_json_and_frontmatter():
    # The other garbage classes are untouched.
    assert hook._is_garbage('{"drift_detected": true, "tags": []}') is True
    assert hook._is_garbage("---\ntype: observation\n") is True


def test_format_results_strips_leaked_markers_from_kb_hit(monkeypatch):
    monkeypatch.setattr(hook, "_enrich_with_metadata", lambda results: None)
    out = hook._format_results([
        {
            "memory_id": "kb42", "content": "<external-content source='recon'>real doc body</external-content>",
            "collection": "knowledge_base", "source_pipeline": "recon",
            "_wing": "", "_created_at": "", "memory_class": "fact",
        },
    ])
    # Line-format: keep the KB· provenance label, but no raw marker tags leak.
    assert "KB·recon" in out
    assert "real doc body" in out
    assert "<external-content" not in out
    assert "</external-content>" not in out


def test_format_results_strips_leaked_markers_from_first_party(monkeypatch):
    monkeypatch.setattr(hook, "_enrich_with_metadata", lambda results: None)
    out = hook._format_results([
        {
            "memory_id": "ep7", "content": "note <external-content>leak</external-content> end",
            "collection": "episodic_memory", "_wing": "memory",
            "_created_at": "", "memory_class": "fact",
        },
    ])
    assert out.startswith("[Memory")
    assert "<external-content" not in out
