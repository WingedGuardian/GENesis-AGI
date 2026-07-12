"""Service orchestration: merge semantics, short-circuit, flock, stage containment.

The refresh runs REAL collectors (read-only against the live system) with all
persistence paths redirected to tmp — the highest-priority coverage gap named
in the PR #1019 structural review.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.infra_profile import service, store
from genesis.infra_profile.types import STATUS_ERROR, STATUS_OK, SectionResult

# ── _merge_section (pure) ─────────────────────────────────────────────────

NOW = "2026-07-12T12:00:00+00:00"


def test_merge_ok_new_hash_stamps_changed_at():
    result = SectionResult(name="cpu", facts={"model": "x"})
    merged = service._merge_section(result, previous=None, now=NOW)
    assert merged["status"] == STATUS_OK
    assert merged["hash"]
    assert merged["facts_changed_at"] == NOW


def test_merge_ok_same_facts_keeps_changed_at():
    result = SectionResult(name="cpu", facts={"model": "x"})
    first = service._merge_section(result, previous=None, now="2026-07-01T00:00:00+00:00")
    second = service._merge_section(result, previous=first, now=NOW)
    assert second["hash"] == first["hash"]
    assert second["facts_changed_at"] == "2026-07-01T00:00:00+00:00"  # unchanged


def test_merge_failed_keeps_prior_facts_and_hash():
    prior = service._merge_section(
        SectionResult(name="cpu", facts={"model": "x"}),
        previous=None,
        now=NOW,
    )
    failed = SectionResult.failed("cpu", "boom")
    merged = service._merge_section(failed, previous=prior, now=NOW)
    assert merged["status"] == STATUS_ERROR
    assert merged["error"] == "boom"
    assert merged["facts"] == {"model": "x"}  # no phantom drift
    assert merged["hash"] == prior["hash"]


def test_merge_failed_without_prior_is_empty_error():
    merged = service._merge_section(SectionResult.failed("cpu", "boom"), previous=None, now=NOW)
    assert merged["status"] == STATUS_ERROR
    assert merged["facts"] == {}
    assert merged["hash"] is None


# ── _recently_refreshed ───────────────────────────────────────────────────


def test_recently_refreshed_fresh_true():
    profile = {"collected_at": datetime.now(UTC).isoformat()}
    assert service._recently_refreshed(profile) is True


def test_recently_refreshed_old_false():
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert service._recently_refreshed({"collected_at": old}) is False


def test_recently_refreshed_missing_or_garbage_false():
    assert service._recently_refreshed({}) is False
    assert service._recently_refreshed({"collected_at": "not-a-date"}) is False


# ── cross-process flock ───────────────────────────────────────────────────


def test_flock_loser_skips(tmp_path, monkeypatch):
    import genesis.infra_profile.paths as paths_mod

    monkeypatch.setattr(paths_mod, "PROFILE_DIR", tmp_path)
    with service._cross_process_lock() as first:
        assert first is True
        # flock is per-open-file-description — a second handle can't take it
        with service._cross_process_lock() as second:
            assert second is False
    # released → acquirable again
    with service._cross_process_lock() as again:
        assert again is True


# ── full refresh (real read-only collectors, tmp persistence) ─────────────


@pytest.fixture
def redirected_paths(tmp_path, monkeypatch):
    import genesis.infra_profile.claude_md as claude_md_mod
    import genesis.infra_profile.paths as paths_mod

    monkeypatch.setattr(paths_mod, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(store, "PROFILE_PATH", tmp_path / "profile.json")
    monkeypatch.setattr(store, "ANNOTATIONS_PATH", tmp_path / "annotations.json")
    monkeypatch.setattr(service, "DOC_PATH", tmp_path / "INFRASTRUCTURE.md")
    monkeypatch.setattr(service, "SHARED_DOC_PATH", tmp_path / "shared" / "INFRASTRUCTURE.md")
    # keep the real user CLAUDE.md out of test blast radius
    monkeypatch.setattr(claude_md_mod, "CLAUDE_MD_PATH", tmp_path / "CLAUDE.md")
    return tmp_path


async def test_refresh_end_to_end_facts_only(redirected_paths):
    profile = await service.refresh("test", force=True)

    sections = profile.get("sections", {})
    assert sections, "no sections collected"
    # container plane collected; host plane unavailable without a guardian
    assert profile["planes"]["container"]["available"] is True
    assert profile["planes"]["host"]["available"] is False
    for name in ("host_system", "host_storage_pool", "host_virt"):
        assert sections[name]["status"] == "unavailable"

    # persistence + render happened (shared mirror dir auto-created)
    assert (redirected_paths / "profile.json").exists()
    assert (redirected_paths / "INFRASTRUCTURE.md").exists()
    assert (redirected_paths / "shared" / "INFRASTRUCTURE.md").exists()
    # no router → annotations empty but file handling didn't blow up
    reloaded = store.load_profile(redirected_paths / "profile.json")
    assert reloaded["collected_at"] == profile["collected_at"]


async def test_refresh_short_circuits_within_window(redirected_paths):
    first = await service.refresh("test", force=True)
    second = await service.refresh("test")  # not forced — within 5-min window
    assert second["collected_at"] == first["collected_at"]


async def test_refresh_survives_render_stage_failure(redirected_paths, monkeypatch):
    """Stage containment: a render crash must not lose the persisted facts."""
    monkeypatch.setattr(
        service,
        "render_document",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render boom")),
    )
    profile = await service.refresh("test", force=True)
    assert profile.get("sections")
    assert (redirected_paths / "profile.json").exists()
    assert not (redirected_paths / "INFRASTRUCTURE.md").exists()
