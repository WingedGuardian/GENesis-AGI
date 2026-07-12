"""Renderer: unavailable wording, stale markers, error retention, digest + headline."""

from __future__ import annotations

from genesis.infra_profile.render import (
    UNAVAILABLE_TEXT,
    headline_facts,
    render_document,
    sentinel_digest,
)


def _profile():
    return {
        "collected_at": "2026-07-11T00:00:00+00:00",
        "planes": {
            "container": {"available": True},
            "host": {"available": False, "reason": "no guardian configured"},
        },
        "sections": {
            "memory": {
                "plane": "container",
                "status": "ok",
                "hash": "h1",
                "facts": {"cgroup_memory_max": 17179869184},
                "metrics": {"mem_available": 1},
            },
            "host_system": {
                "plane": "host",
                "status": "unavailable",
                "error": "no guardian configured",
                "hash": None,
                "facts": {},
                "metrics": {},
            },
            "storage": {
                "plane": "container",
                "status": "error",
                "error": "boom",
                "hash": "h2",
                "facts": {"mounts": [{"mountpoint": "/"}]},
                "metrics": {"root": {"free_bytes": 5 * 1024**3, "pct_used": 41.0}},
            },
        },
    }


def _annotations(storage_source="h2"):
    return {
        "sections": {
            "storage": {"annotation": "- thin pool gotcha", "source_hash": storage_source},
        },
    }


def test_unavailable_section_wording():
    doc = render_document(_profile(), {})
    assert UNAVAILABLE_TEXT in doc
    assert "no guardian configured" in doc


def test_error_section_keeps_prior_facts_with_notice():
    doc = render_document(_profile(), {})
    assert "last collection FAILED" in doc
    assert "mountpoint" in doc  # prior facts still rendered


def test_stale_annotation_flagged():
    doc = render_document(_profile(), _annotations(storage_source="OLD"))
    assert "STALE" in doc
    assert "- thin pool gotcha" in doc  # old note kept, never dropped


def test_fresh_annotation_not_flagged():
    doc = render_document(_profile(), _annotations(storage_source="h2"))
    assert "STALE" not in doc


def test_headline_facts():
    headline = headline_facts(_profile())
    assert headline["memory_limit"] == "16.0 GiB"
    assert headline["host_plane"] == UNAVAILABLE_TEXT
    assert "free" in headline["root_disk"]


def test_sentinel_digest_includes_annotations_and_stale_marker():
    digest = sentinel_digest(_profile(), _annotations(storage_source="OLD"))
    assert "memory_limit: 16.0 GiB" in digest
    assert "- thin pool gotcha" in digest
    assert "STALE" in digest


def test_sentinel_digest_empty_profile_is_empty():
    assert sentinel_digest({}, {}) == ""
