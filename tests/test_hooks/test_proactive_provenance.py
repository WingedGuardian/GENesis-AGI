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
