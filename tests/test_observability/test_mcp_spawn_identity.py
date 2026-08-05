"""Tests for eager MCP spawn-identity capture.

The guard's whole safety story rests on this: if git-capture silently fails,
spawn_commit stays None and the guard fails OPEN (never blocks). These pin the
happy path, that fail-open contract, and idempotency (so a re-entrant bootstrap
can't overwrite the true earliest start time).
"""

from __future__ import annotations

from datetime import datetime

import genesis.observability.mcp_spawn_identity as si


def _reset(monkeypatch):
    """Clear the module singletons so each test captures from scratch."""
    monkeypatch.setattr(si, "_spawn_commit", None)
    monkeypatch.setattr(si, "_spawn_at", None)


def _fake_git(rc, out):
    # Matches _run_git signature: (repo, *args, timeout) -> (rc, stdout, stderr).
    return lambda repo, *args, timeout: (rc, out, "")


def test_happy_path_records_full_sha_and_time(monkeypatch):
    _reset(monkeypatch)
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(si, "_run_git", _fake_git(0, sha + "\n"))
    si.capture_spawn_identity()
    assert si.spawn_commit() == sha
    assert datetime.fromisoformat(si.spawn_at())  # valid ISO-8601


def test_git_failure_leaves_commit_none_but_records_time(monkeypatch):
    # Fail-open contract: git unreadable → commit None (guard fails open), but
    # spawn_at IS recorded (set before the git call).
    _reset(monkeypatch)
    monkeypatch.setattr(si, "_run_git", _fake_git(-1, ""))
    si.capture_spawn_identity()
    assert si.spawn_commit() is None
    assert si.spawn_at() is not None


def test_rc_zero_but_empty_output_is_none(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(si, "_run_git", _fake_git(0, "   \n"))
    si.capture_spawn_identity()
    assert si.spawn_commit() is None


def test_idempotent_second_call_is_noop(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(si, "_run_git", _fake_git(0, "first000\n"))
    si.capture_spawn_identity()
    first_commit, first_at = si.spawn_commit(), si.spawn_at()
    # Second capture with DIFFERENT git output must NOT overwrite the earliest.
    monkeypatch.setattr(si, "_run_git", _fake_git(0, "second11\n"))
    si.capture_spawn_identity()
    assert si.spawn_commit() == first_commit == "first000"
    assert si.spawn_at() == first_at
