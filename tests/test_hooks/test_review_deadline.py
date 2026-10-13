"""Shared aggregate-deadline arithmetic."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import review_scope  # noqa: E402
import review_state  # noqa: E402
from review_deadline import Deadline, DeadlineExpired, bounded_timeout  # noqa: E402

_COMMIT_SPEC = importlib.util.spec_from_file_location(
    "review_deadline_commit_guard", _SCRIPTS / "review_enforcement_commit.py"
)
_commit_guard = importlib.util.module_from_spec(_COMMIT_SPEC)
assert _COMMIT_SPEC.loader is not None
_COMMIT_SPEC.loader.exec_module(_commit_guard)


def test_absolute_deadline_reuses_one_clock_budget():
    now = [100.0]
    deadline = Deadline.after(5.0, monotonic=lambda: now[0])
    assert deadline.timeout(8.0) == 5.0
    now[0] = 104.0
    assert deadline.remaining() == 1.0
    assert deadline.timeout(8.0) == 1.0


def test_expired_deadline_refuses_to_start_another_probe():
    with pytest.raises(DeadlineExpired):
        bounded_timeout(10.0, 8.0, monotonic=lambda: 11.0)


def test_live_deadline_never_grants_time_beyond_what_remains():
    assert bounded_timeout(10.5, 8.0, monotonic=lambda: 10.0) == 0.5


def test_child_deadline_preserves_post_lookup_headroom():
    now = [100.0]
    outer = Deadline.after(9.5, monotonic=lambda: now[0])
    now[0] += 3.0
    lookup = outer.capped_after(7.5, reserve=2.0)
    assert lookup.expires_at == 107.5
    assert outer.expires_at == 109.5


def test_expired_deadline_starts_no_transitive_local_probe(monkeypatch, tmp_path):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("subprocess must not start after deadline")

    monkeypatch.setattr(review_state.subprocess, "run", forbidden)
    monkeypatch.setattr(review_scope.subprocess, "run", forbidden)
    monkeypatch.setattr(_commit_guard.subprocess, "run", forbidden)
    expired = 0.0

    with pytest.raises(RuntimeError, match="aggregate review-gate deadline expired"):
        review_state.get_current_diff_hash(cwd=str(tmp_path), deadline=expired)
    assert review_scope._git(["status"], str(tmp_path), expired) is None
    with pytest.raises(DeadlineExpired):
        _commit_guard._staged_files(str(tmp_path), deadline=expired)
    assert calls == []


@pytest.mark.parametrize(
    "probe",
    [
        lambda path, deadline: review_state._worktree_root(path, deadline=deadline),
        lambda path, deadline: review_state.get_current_diff_hash(path, deadline=deadline),
        lambda path, deadline: review_state._staged_content_hash(path, deadline=deadline),
        lambda path, deadline: review_state.get_current_branch(path, deadline=deadline),
        lambda path, deadline: review_scope._git(
            ["status"], path, deadline, strict_deadline=True
        ),
        lambda path, deadline: _commit_guard._staged_files(path, deadline=deadline),
        lambda path, deadline: _commit_guard._worktree_root(path, deadline=deadline),
    ],
)
def test_in_flight_timeout_propagates_for_deadline_bound_local_probe(
    monkeypatch, tmp_path, probe
):
    def times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", times_out)
    with pytest.raises(DeadlineExpired, match="expired during subprocess"):
        probe(str(tmp_path), time.monotonic() + 10.0)


def test_in_flight_timeout_preserves_legacy_no_deadline_fallbacks(monkeypatch, tmp_path):
    def times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", times_out)
    path = str(tmp_path)
    assert review_state._worktree_root(path) == path
    assert review_state.get_current_diff_hash(path) == "unknown"
    assert review_state._staged_content_hash(path) == "unknown"
    assert review_state.get_current_branch(path) == "unknown"
    assert review_scope._git(["status"], path) is None
    assert _commit_guard._staged_files(path) is None
    assert _commit_guard._worktree_root(path) == os.path.realpath(path)


def test_network_minimum_is_a_separate_caller_policy():
    deadline = Deadline(10.5, monotonic=lambda: 10.0)
    assert deadline.exhausted(minimum_useful=0.75)
    assert not deadline.exhausted(minimum_useful=0.25)
