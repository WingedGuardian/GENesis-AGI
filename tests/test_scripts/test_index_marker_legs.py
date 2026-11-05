"""Provider-leg debt and claim ownership are independent and durable."""

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/lib/index_marker.py"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "owned-state"))
    for name in ("_genesis_index_marker_legacy", "_genesis_index_marker_legs"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    module = SimpleNamespace(**runpy.run_path(str(SCRIPT)))
    assert module.database_path().is_relative_to(tmp_path)
    return module


def test_both_is_two_provider_legs_in_one_queue(queue, tmp_path):
    repo = str(tmp_path / "repo with space 雪")
    queue.write_marker(repo, "both", "fast")
    rows = queue.list_markers()
    assert sorted(row["tools"] for row in rows) == ["cbm", "gitnexus"]
    assert {row["hash"] for row in rows} == {queue.marker_hash(repo)}
    assert all(row["attempts"] == 0 and not row["legacy_unqualified"] for row in rows)


def test_one_leg_failure_does_not_charge_or_remove_sibling(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "fast")
    h = queue.marker_hash(repo)
    for debt in (1, 2):
        claimed = queue.claim(h, provider="cbm")
        queue.remember_outcome(claimed["context"], "restore_failure")
        assert queue.apply_remembered_outcome(claimed["context"]) == "pending"
        rows = {row["provider"]: row for row in queue.list_markers()}
        assert rows["cbm"]["attempts"] == debt and rows["gitnexus"]["attempts"] == 0


def test_success_consumes_only_claimed_leg_and_preserves_new_generation(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "fast")
    claimed = queue.claim(queue.marker_hash(repo), provider="gitnexus")
    queue.write_marker(repo, "gitnexus", "full")
    queue.remember_outcome(claimed["context"], "consume")
    assert queue.apply_remembered_outcome(claimed["context"]) == "consumed"
    rows = {row["provider"]: row for row in queue.list_markers()}
    assert set(rows) == {"cbm", "gitnexus"}
    assert rows["gitnexus"]["mode"] == "full" and rows["gitnexus"]["attempts"] == 0
    assert queue.get_inflight(queue.marker_hash(repo)) is None


def test_replayed_failure_is_charged_once_after_inflight_retirement(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    claimed = queue.claim(queue.marker_hash(repo), provider="cbm")
    queue.remember_outcome(claimed["context"], "restore_failure")
    assert queue.apply_remembered_outcome(claimed["context"]) == "pending"
    assert queue.apply_remembered_outcome(claimed["context"]) == "pending"
    assert queue.list_markers()[0]["attempts"] == 1


@pytest.mark.parametrize(
    "field,value",
    [("provider", "gitnexus"), ("execution_seq", True), ("protocol", 1), ("claim_id", "0" * 32)],
)
def test_terminal_context_cannot_target_another_claim(queue, tmp_path, field, value):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "fast")
    claimed = queue.claim(queue.marker_hash(repo), provider="cbm")
    if field == "claim_id":
        nonce = claimed["context"]["claim_id"]
        value = ("1" if nonce[0] == "0" else "0") + nonce[1:]
    foreign = {**claimed["context"], field: value}
    with pytest.raises((ValueError, RuntimeError)):
        queue.remember_outcome(foreign, "consume")
    assert queue.get_inflight(queue.marker_hash(repo))["claim_id"] == claimed["context"]["claim_id"]


def test_gitnexus_cannot_stamp_cbm_full_clock(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "gitnexus", "full")
    h = queue.marker_hash(repo)
    claimed = queue.claim(h, provider="gitnexus")
    with pytest.raises(ValueError):
        queue.remember_outcome(claimed["context"], "consume_full")
    assert queue.get_repo_state(h)["last_full"] is None
    queue.remember_outcome(claimed["context"], "consume")
    queue.apply_remembered_outcome(claimed["context"])
    assert queue.get_repo_state(h)["last_full"] is None


def test_one_repository_has_only_one_inflight_provider(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "fast")
    h = queue.marker_hash(repo)
    first = queue.claim(h, provider="cbm")
    assert queue.claim(h, provider="gitnexus") is None
    assert queue.get_inflight(h)["claim_id"] == first["context"]["claim_id"]


def test_claim_uses_latest_same_leg_mode_without_widening_sibling(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "gitnexus", "fast")
    listed = queue.list_markers()[0]
    queue.write_marker(repo, "gitnexus", "full")
    queue.write_marker(repo, "cbm", "moderate")
    claimed = queue.claim(listed["hash"], provider="gitnexus")
    assert listed["mode"] == "fast" and claimed["mode"] == "full"
    assert claimed["tools"] == claimed["context"]["provider"] == "gitnexus"
    rows = queue.list_markers()
    assert len(rows) == 1 and rows[0]["provider"] == "cbm" and rows[0]["mode"] == "moderate"


def test_repository_identity_collision_rolls_back_complete_enqueue(queue, tmp_path, monkeypatch):
    original = str(tmp_path / "original")
    queue.write_marker(original, "both", "fast")
    before = queue.list_markers()
    h = queue.marker_hash(original)
    monkeypatch.setitem(queue.write_marker.__globals__, "marker_hash", lambda repo: h)
    with pytest.raises(ValueError, match="another canonical repository"):
        queue.write_marker(str(tmp_path / "different"), "both", "full")
    assert queue.list_markers() == before
