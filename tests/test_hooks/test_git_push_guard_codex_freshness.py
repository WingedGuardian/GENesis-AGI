"""Tests for the Codex review-freshness merge gate in git_push_guard.

The finding scanners (_check_pr_review_findings / _check_inline_review_findings)
catch UNRESOLVED findings but not a merge whose head was never reviewed, or was
reviewed at an EARLIER commit (Codex reviewed A, code B pushed after). This gate
requires Codex's latest review ``commit_id`` (the full 40-char oid GitHub records
the review against) to EXACTLY equal the PR's current ``headRefOid``.

Network-free via the _TEST_GH_* env-injection seams (mirrors _pr_ci_status'
_TEST_GH_CI_ROLLUP).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("git_push_guard", _HOOKS_DIR / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
STALE = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _reviews_jsonl(*commit_ids, login="chatgpt-codex-connector[bot]"):
    """One JSON object per line, matching gh api .../reviews --jq '{login, commit_id}'."""
    return "\n".join(json.dumps({"login": login, "commit_id": cid}) for cid in commit_ids)


class TestReviewedCommitParsing:
    def test_parses_commit_id(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))
        assert _mod._latest_codex_reviewed_sha("1") == HEAD

    def test_latest_review_wins(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl("a" * 40, "b" * 40))
        assert _mod._latest_codex_reviewed_sha("1") == "b" * 40

    def test_uppercase_normalized(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD.upper()))
        assert _mod._latest_codex_reviewed_sha("1") == HEAD  # lowercased

    def test_none_when_not_codex(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD, login="human"))
        assert _mod._latest_codex_reviewed_sha("1") is None

    def test_none_when_empty(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        assert _mod._latest_codex_reviewed_sha("1") is None


class TestFreshnessGate:
    def test_current_review_passes(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))
        block, msg = _mod._check_codex_reviewed_head("1")
        assert block is False and msg == ""

    def test_no_review_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        block, msg = _mod._check_codex_reviewed_head("1")
        assert block is True and "no codex review" in msg.lower()

    def test_stale_review_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(STALE))
        block, msg = _mod._check_codex_reviewed_head("1")
        assert block is True and "stale" in msg.lower()

    def test_prefix_no_longer_passes(self, monkeypatch):
        # A stale review whose oid merely shares a PREFIX with head must NOT pass
        # (the security fix: exact identity, not startswith).
        near = HEAD[:12] + "0" * 28  # same 12-char prefix, different full oid
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(near))
        block, _ = _mod._check_codex_reviewed_head("1")
        assert block is True

    def test_force_override_bypasses(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")  # would otherwise block
        block, _ = _mod._check_codex_reviewed_head("1", force=True)
        assert block is False

    def test_head_unresolvable_fails_closed(self, monkeypatch):
        # Enforcement gate: an unreadable head is treated as unverifiable → BLOCK
        # (fail-closed, like _check_mergeable's UNKNOWN), not waved through.
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", "")  # empty → None
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))
        block, msg = _mod._check_codex_reviewed_head("1")
        assert block is True and "head" in msg.lower()
