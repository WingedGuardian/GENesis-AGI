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
    """Clear the module singletons so each test captures from scratch.

    Also clears GENESIS_SLOT so the Part B persistence branch is a no-op by
    default — tests that exercise persistence opt in explicitly and redirect the
    store dir, so no test ever writes to the real ~/.genesis/mcp-spawn/.
    """
    monkeypatch.setattr(si, "_spawn_commit", None)
    monkeypatch.setattr(si, "_spawn_at", None)
    monkeypatch.delenv("GENESIS_SLOT", raising=False)


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


# ── Part B: cross-process persistence (slot-keyed spawn-commit file) ─────────


def test_persists_spawn_commit_when_slotted(tmp_path, monkeypatch):
    _reset(monkeypatch)
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(si, "_run_git", _fake_git(0, sha + "\n"))
    monkeypatch.setenv("GENESIS_SLOT", "4")
    from genesis.observability import mcp_spawn_store as store

    monkeypatch.setattr(store, "_SPAWN_DIR", tmp_path / "mcp-spawn")
    monkeypatch.setattr(store, "session_pid", lambda: 45285)
    si.capture_spawn_identity()
    assert si.spawn_commit() == sha
    ident = store.read_spawn_identity("4", 45285)  # dashboard can read it
    assert ident is not None
    assert ident[0] == sha and ident[1] == si.spawn_at()


def test_persistence_failure_does_not_break_capture(tmp_path, monkeypatch):
    # Isolation: a persistence blow-up must NEVER perturb the in-memory identity
    # the Part A guard depends on.
    _reset(monkeypatch)
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(si, "_run_git", _fake_git(0, sha + "\n"))
    monkeypatch.setenv("GENESIS_SLOT", "4")
    from genesis.observability import mcp_spawn_store as store

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "persist_spawn_commit", _boom)
    si.capture_spawn_identity()  # must not raise
    assert si.spawn_commit() == sha


def test_no_persist_when_unslotted(tmp_path, monkeypatch):
    _reset(monkeypatch)  # clears GENESIS_SLOT
    monkeypatch.setattr(si, "_run_git", _fake_git(0, "abc1234\n"))
    from genesis.observability import mcp_spawn_store as store

    monkeypatch.setattr(store, "_SPAWN_DIR", tmp_path / "mcp-spawn")
    si.capture_spawn_identity()
    assert not (tmp_path / "mcp-spawn").exists()


def test_shared_capture_uses_actual_source_root_without_slot_persistence(tmp_path, monkeypatch):
    _reset(monkeypatch)
    from genesis.observability import mcp_spawn_store as store

    source = tmp_path / "source-checkout"
    seen, persisted = [], []
    monkeypatch.setenv("GENESIS_SLOT", "4")

    def unexpected():
        raise AssertionError("Explicit source root must not read the default checkout")

    def git(root, *args, timeout):
        seen.append(root)
        return 0, "a" * 40, ""

    monkeypatch.setattr(si, "repo_root", unexpected)
    monkeypatch.setattr(si, "_run_git", git)
    monkeypatch.setattr(store, "persist_spawn_commit", lambda *args: persisted.append(args))
    si.capture_spawn_identity(source_root=source, persist_session=False)
    assert seen == [source] and persisted == []
    assert si.spawn_commit() == "a" * 40
    assert datetime.fromisoformat(si.spawn_at()).utcoffset() is not None


def test_shared_capture_cannot_be_rebound_by_later_calls(tmp_path, monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(si, "_run_git", _fake_git(0, "a" * 40))
    si.capture_spawn_identity(source_root=tmp_path, persist_session=False)
    first = si.spawn_commit(), si.spawn_at()
    monkeypatch.setattr(si, "_run_git", _fake_git(0, "b" * 40))
    si.capture_spawn_identity(source_root=tmp_path / "other", persist_session=False)
    assert (si.spawn_commit(), si.spawn_at()) == first
