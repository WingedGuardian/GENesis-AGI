"""Tests for scripts/lib/index_marker.py — the code-intel index-request queue.

The marker store replaces per-commit index spawns: triggers drop a marker, the
idle runner consumes it. Two properties are load-bearing and get dedicated
tests here:

  * HASH PARITY — the marker filename must byte-match the entrypoint's
    single-flight lock hash (``code_intel_index.sh``: canonicalize with
    ``pwd -P`` then ``printf '%s' … | sha1sum | cut -c1-16``). If they diverge, a
    runner marker maps to a different lock than the one the host freeze holds.
  * MOVE-ASIDE consume — a commit that lands DURING an in-flight index must not
    be dropped when the runner consumes the marker on success.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MARKER_PY = _REPO_ROOT / "scripts" / "lib" / "index_marker.py"
_ENTRYPOINT = _REPO_ROOT / "scripts" / "lib" / "code_intel_index.sh"

_spec = importlib.util.spec_from_file_location("index_marker", _MARKER_PY)
im = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(im)


def _with_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))


# ── hash parity (the load-bearing invariant) ──────────────────────────────


def test_marker_hash_matches_entrypoint_lock_hash():
    """python marker_hash == the entrypoint's bash sha1sum lock hash, exactly."""
    for p in ("/home/ubuntu/genesis", "/tmp", str(_REPO_ROOT)):
        canonical = os.path.realpath(p)
        bash_hash = hashlib.sha1(canonical.encode()).hexdigest()[:16]
        assert im.marker_hash(p) == bash_hash


def test_marker_hash_matches_live_bash_sha1sum():
    """Guard against a python/bash sha1 encoding drift by running real sha1sum."""
    canonical = os.path.realpath("/tmp")
    out = subprocess.run(
        f"printf '%s' {canonical} | sha1sum | cut -c1-16",
        shell=True,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert im.marker_hash("/tmp") == out


def test_hash_is_stable_across_equivalent_spellings():
    assert im.marker_hash("/tmp") == im.marker_hash("/tmp/") == im.marker_hash("/tmp/.")


# ── write / coalesce ───────────────────────────────────────────────────────


def test_write_creates_marker(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    p = im.write_marker("/tmp", tools="cbm", mode="fast")
    data = json.loads(Path(p).read_text())
    assert data["repo_path"] == "/tmp"
    assert data["tools"] == "cbm"
    assert data["mode"] == "fast"
    assert data["attempts"] == 0


def test_coalesce_unions_tools_and_keeps_thorough_mode(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    im.write_marker("/tmp", tools="cbm", mode="fast")
    first = json.loads(Path(im.marker_dir() / f"{im.marker_hash('/tmp')}.json").read_text())
    time.sleep(0.01)
    im.write_marker("/tmp", tools="gitnexus", mode="full")
    data = json.loads(Path(im.marker_dir() / f"{im.marker_hash('/tmp')}.json").read_text())
    assert data["tools"] == "both"  # disagreement widens to both
    assert data["mode"] == "full"  # keeps the more thorough mode
    assert data["requested_at"] == first["requested_at"]  # earliest preserved


def test_list_reports_pending_only(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    im.write_marker("/tmp", tools="both", mode="fast")
    listed = im.list_markers()
    assert len(listed) == 1
    assert listed[0]["repo_path"] == "/tmp"
    assert "age_s" in listed[0]


# ── claim → consume / restore (S1 move-aside) ─────────────────────────────


def test_claim_moves_aside(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    im.write_marker("/tmp", tools="both", mode="fast")
    h = im.marker_hash("/tmp")
    data = im.claim(h)
    assert data is not None
    assert not (im.marker_dir() / f"{h}.json").exists()  # canonical gone
    assert (im.marker_dir() / f"{h}.inflight.json").exists()  # moved aside


def test_consume_drops_inflight(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    im.write_marker("/tmp", tools="both", mode="fast")
    h = im.marker_hash("/tmp")
    im.claim(h)
    im.consume(h)
    assert not (im.marker_dir() / f"{h}.inflight.json").exists()
    assert im.list_markers() == []


def test_concurrent_commit_during_index_survives_consume(tmp_path, monkeypatch):
    """S1: a commit that coalesces AFTER claim must survive the consume."""
    _with_home(tmp_path, monkeypatch)
    im.write_marker("/tmp", tools="cbm", mode="fast")
    h = im.marker_hash("/tmp")
    im.claim(h)  # runner claims (move-aside)
    im.write_marker("/tmp", tools="gitnexus", mode="fast")  # commit lands mid-index
    im.consume(h)  # runner finishes the OLD work
    remaining = im.list_markers()
    assert len(remaining) == 1  # the new commit's request lives
    assert remaining[0]["tools"] == "gitnexus"


def test_restore_merges_inflight_back(tmp_path, monkeypatch):
    """rc=75 (frozen) restores the claimed marker, coalescing with any new one."""
    _with_home(tmp_path, monkeypatch)
    im.write_marker("/tmp", tools="cbm", mode="full")
    h = im.marker_hash("/tmp")
    im.claim(h)
    im.write_marker("/tmp", tools="gitnexus", mode="fast")  # concurrent
    state = im.restore(h)
    assert state == "pending"
    data = json.loads((im.marker_dir() / f"{h}.json").read_text())
    assert data["tools"] == "both"  # inflight(cbm) ∪ new(gitnexus)
    assert data["mode"] == "full"  # keeps the thorough one


def test_restore_without_attempts_inc_never_euthanizes(tmp_path, monkeypatch):
    """rc=75/rc=3 restore must not burn the attempts budget."""
    _with_home(tmp_path, monkeypatch)
    im.write_marker("/tmp", tools="both", mode="fast")
    h = im.marker_hash("/tmp")
    for _ in range(10):
        im.claim(h)
        assert im.restore(h) == "pending"  # never "failed"
    assert not (im.marker_dir() / f"{h}.failed.json").exists()


def test_attempts_euthanize_at_max(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    im.write_marker("/tmp", tools="both", mode="fast")
    h = im.marker_hash("/tmp")
    states = []
    for _ in range(im.MAX_ATTEMPTS):
        im.claim(h)
        states.append(im.restore(h, attempts_inc=True))
    assert states[-1] == "failed"
    assert (im.marker_dir() / f"{h}.failed.json").exists()
    assert im.list_markers() == []  # failed marker not re-listed


# ── weekly-full escalation gate ────────────────────────────────────────────


def test_should_escalate_when_no_last_full(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    assert im.should_escalate_full(h) is True


def test_stamp_full_suppresses_escalation(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    im.stamp_full(h)
    assert im.should_escalate_full(h) is False


def test_stale_last_full_re_escalates(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    old = time.time() - (im.FULL_INTERVAL_S + 3600)
    im.last_full_path(h).parent.mkdir(parents=True, exist_ok=True)
    im.last_full_path(h).write_text(f"{old}\n")
    assert im.should_escalate_full(h) is True


def test_full_backoff_suppresses_escalation(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    im.mark_full_backoff(h)  # a full just failed
    assert im.should_escalate_full(h) is False  # fall back to fast for a while


def test_stale_backoff_allows_re_escalation(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    im.full_backoff_path(h).parent.mkdir(parents=True, exist_ok=True)
    im.full_backoff_path(h).write_text(f"{time.time() - (im.FULL_BACKOFF_S + 60)}\n")
    assert im.should_escalate_full(h) is True


def test_stamp_full_clears_backoff(tmp_path, monkeypatch):
    _with_home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    im.mark_full_backoff(h)
    im.stamp_full(h)  # a later full succeeded
    assert not im.full_backoff_path(h).exists()


# ── CLI surface (bash callers) ─────────────────────────────────────────────


def test_cli_hash_matches_module(tmp_path):
    out = subprocess.run(
        ["python3", str(_MARKER_PY), "hash", "--repo", "/tmp"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == im.marker_hash("/tmp")


def test_cli_write_and_list_roundtrip(tmp_path):
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}
    subprocess.run(
        [
            "python3",
            str(_MARKER_PY),
            "write",
            "--repo",
            "/tmp",
            "--tools",
            "both",
            "--mode",
            "fast",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    out = subprocess.run(
        ["python3", str(_MARKER_PY), "list"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "/tmp" in out and "both" in out and "fast" in out
