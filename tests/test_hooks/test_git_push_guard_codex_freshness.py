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
        block, msg, _head = _mod._check_codex_reviewed_head("1")
        assert block is False and msg == ""

    def test_no_review_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        block, msg, _head = _mod._check_codex_reviewed_head("1")
        assert block is True and "no codex review" in msg.lower()

    def test_stale_review_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(STALE))
        block, msg, _head = _mod._check_codex_reviewed_head("1")
        assert block is True and "stale" in msg.lower()

    def test_prefix_no_longer_passes(self, monkeypatch):
        # A stale review whose oid merely shares a PREFIX with head must NOT pass
        # (the security fix: exact identity, not startswith).
        near = HEAD[:12] + "0" * 28  # same 12-char prefix, different full oid
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(near))
        block, _, _head = _mod._check_codex_reviewed_head("1")
        assert block is True

    def test_force_override_bypasses(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")  # would otherwise block
        block, _, _head = _mod._check_codex_reviewed_head("1", force=True)
        assert block is False

    def test_head_unresolvable_fails_closed(self, monkeypatch):
        # Enforcement gate: an unreadable head is treated as unverifiable → BLOCK
        # (fail-closed, like _check_mergeable's UNKNOWN), not waved through.
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", "")  # empty → None
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))
        block, msg, _head = _mod._check_codex_reviewed_head("1")
        assert block is True and "head" in msg.lower()

    def test_pass_returns_verified_head(self, monkeypatch):
        # The caller binds the merge to this oid via --match-head-commit (TOCTOU).
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))
        block, _, head = _mod._check_codex_reviewed_head("1")
        assert block is False and head == HEAD

    def test_force_returns_no_verified_head(self, monkeypatch):
        # Forced (# review-override) skips verification → no head to bind →
        # the match-head requirement disengages too.
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))
        block, _, head = _mod._check_codex_reviewed_head("1", force=True)
        assert block is False and head is None


class TestMergeMatchHead:
    def test_separate_value(self):
        argv = ["gh", "pr", "merge", "5", "--squash", "--admin", "--match-head-commit", HEAD]
        assert _mod._merge_match_head(argv) == HEAD

    def test_equals_form(self):
        argv = ["gh", "pr", "merge", "5", f"--match-head-commit={HEAD}"]
        assert _mod._merge_match_head(argv) == HEAD

    def test_absent(self):
        assert _mod._merge_match_head(["gh", "pr", "merge", "5", "--admin"]) is None

    def test_suggested_cmd_preserves_repo(self):
        # Dropping --repo retargets a copied merge to the cwd repo (Codex P1 r3).
        cmd = _mod._suggested_merge_cmd("5", HEAD, "octo/voice")
        assert "--repo octo/voice" in cmd
        assert cmd.endswith(f"--match-head-commit {HEAD}")
        assert "gh pr merge 5 " in cmd

    def test_suggested_cmd_no_repo_when_cwd(self):
        cmd = _mod._suggested_merge_cmd("5", HEAD, None)
        assert "--repo" not in cmd
        assert cmd == f"gh pr merge 5 --squash --admin --match-head-commit {HEAD}"

    def test_shadowed_as_body_value_is_ignored(self):
        # `--body --match-head-commit=<sha>` — gh takes the sha as BODY TEXT (no
        # binding); the scan must NOT read it as an active binding (Codex P1 r3).
        argv = [
            "gh",
            "pr",
            "merge",
            "5",
            "--body",
            f"--match-head-commit={HEAD}",
            "--squash",
            "--admin",
        ]
        assert _mod._merge_match_head(argv) is None

    def test_shadowed_as_short_flag_value_is_ignored(self):
        for vflag in ("-b", "-F", "-t", "-A", "--body-file", "--subject", "--author-email"):
            argv = ["gh", "pr", "merge", "5", vflag, f"--match-head-commit={HEAD}", "--admin"]
            assert _mod._merge_match_head(argv) is None, vflag

    def test_real_binding_after_a_body_value(self):
        # --body consumes 'msg'; the real --match-head-commit still parses.
        argv = ["gh", "pr", "merge", "5", "--body", "msg", "--match-head-commit", HEAD, "--admin"]
        assert _mod._merge_match_head(argv) == HEAD

    def test_double_dash_ends_options(self):
        argv = ["gh", "pr", "merge", "5", "--", f"--match-head-commit={HEAD}"]
        assert _mod._merge_match_head(argv) is None

    def test_glued_body_containing_flag_is_ignored(self):
        argv = ["gh", "pr", "merge", "5", f"--body=--match-head-commit={HEAD}", "--admin"]
        assert _mod._merge_match_head(argv) is None

    def test_short_cluster_swallows_next_token(self):
        # -db = -d(bool) + -b(value); gh's -b swallows the following
        # --match-head-commit as BODY text (merges UNBOUND) — must NOT be read as
        # a binding (audit round 4).
        for cluster in ("-db", "-dt", "-dA", "-sb", "-mb"):
            argv = [
                "gh",
                "pr",
                "merge",
                "5",
                "--repo",
                "o/r",
                cluster,
                f"--match-head-commit={HEAD}",
                "--admin",
            ]
            assert _mod._merge_match_head(argv) is None, cluster

    def test_bool_cluster_then_real_binding(self):
        # -ds = two booleans; the real --match-head-commit still binds.
        argv = ["gh", "pr", "merge", "5", "-ds", "--match-head-commit", HEAD]
        assert _mod._merge_match_head(argv) == HEAD

    def test_glued_short_body_is_ignored(self):
        argv = ["gh", "pr", "merge", "5", f"-b--match-head-commit={HEAD}", "--admin"]
        assert _mod._merge_match_head(argv) is None


class TestMergeShadowFlag:
    def test_detects_content_flags(self):
        for vflag in (
            "--body",
            "-b",
            "--body-file",
            "-F",
            "--subject",
            "-t",
            "--author-email",
            "-A",
        ):
            assert _mod._merge_has_shadow_flag(["gh", "pr", "merge", "5", vflag, "x"]), vflag
        assert _mod._merge_has_shadow_flag(["gh", "pr", "merge", "5", "--body=x"])

    def test_clean_merge_has_no_shadow(self):
        argv = [
            "gh",
            "pr",
            "merge",
            "5",
            "--repo",
            "o/r",
            "--squash",
            "--admin",
            "--match-head-commit",
            HEAD,
        ]
        assert _mod._merge_has_shadow_flag(argv) is False

    def test_detects_shadow_inside_cluster(self):
        for cluster in ("-db", "-dt", "-dA", "-sb", "-b"):
            assert _mod._merge_has_shadow_flag(["gh", "pr", "merge", "5", cluster, "x"]), cluster

    def test_repo_cluster_not_shadow(self):
        # -R / -dR are --repo (allowed); the value-short stops cluster scanning.
        assert _mod._merge_has_shadow_flag(["gh", "pr", "merge", "5", "-dR", "o/r"]) is False
        assert _mod._merge_has_shadow_flag(["gh", "pr", "merge", "5", "-R", "o/r"]) is False


class TestMergeMatchHeadRepeat:
    def test_repeated_flag_last_wins(self):
        # gh pflag semantics: the LAST repeated string-flag value is the one gh
        # enforces — the hook must validate that same value, else
        # `--match-head-commit <verified> --match-head-commit <other>` passes the
        # hook at <verified> while gh merges at <other> (Codex P1, round 2).
        argv = ["gh", "pr", "merge", "5", "--match-head-commit", HEAD, "--match-head-commit", STALE]
        assert _mod._merge_match_head(argv) == STALE
        argv = [
            "gh",
            "pr",
            "merge",
            "5",
            "--match-head-commit",
            STALE,
            f"--match-head-commit={HEAD}",
        ]
        assert _mod._merge_match_head(argv) == HEAD


class TestPrBaseRef:
    """_pr_base_ref reads the PR's baseRefName (seam _TEST_GH_BASE_REF)."""

    def test_reads_seam(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
        assert _mod._pr_base_ref("1") == "main"

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_BASE_REF", "  release-1.2\n")
        assert _mod._pr_base_ref("1") == "release-1.2"

    def test_empty_is_none(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_BASE_REF", "")
        assert _mod._pr_base_ref("1") is None


class TestRepoDefaultBranch:
    """_repo_default_branch reads defaultBranchRef.name (seam _TEST_GH_DEFAULT_BRANCH)."""

    def test_reads_seam(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_DEFAULT_BRANCH", "main")
        assert _mod._repo_default_branch() == "main"

    def test_empty_is_none(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_DEFAULT_BRANCH", "")
        assert _mod._repo_default_branch() is None


class TestCheckBaseIsDefault:
    """Base-retarget guard: block unless base == default; fail-CLOSED on unreadable;
    force waives. Head-pinning can't catch a base change (the head never moves)."""

    def _set(self, monkeypatch, base, default):
        monkeypatch.setenv("_TEST_GH_BASE_REF", base)
        monkeypatch.setenv("_TEST_GH_DEFAULT_BRANCH", default)

    def test_base_equals_default_passes(self, monkeypatch):
        self._set(monkeypatch, "main", "main")
        block, msg = _mod._check_base_is_default("1")
        assert block is False and msg == ""

    def test_base_differs_blocks(self, monkeypatch):
        self._set(monkeypatch, "release-1.2", "main")
        block, msg = _mod._check_base_is_default("1")
        assert block is True and "not the default branch" in msg

    def test_base_unreadable_fails_closed(self, monkeypatch):
        self._set(monkeypatch, "", "main")
        block, msg = _mod._check_base_is_default("1")
        assert block is True and "could not confirm" in msg.lower()

    def test_default_unreadable_fails_closed(self, monkeypatch):
        self._set(monkeypatch, "main", "")
        block, _ = _mod._check_base_is_default("1")
        assert block is True

    def test_force_waives(self, monkeypatch):
        self._set(monkeypatch, "release-1.2", "main")  # would otherwise block
        block, _ = _mod._check_base_is_default("1", force=True)
        assert block is False


class TestDeriveRepoFromCwd:
    """_derive_repo_from_cwd resolves the OWNER/REPO gh targets in a dir
    (seam _TEST_GH_DERIVED_REPO), normalized; None when unresolvable."""

    def test_reads_seam(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "owner/repo")
        assert _mod._derive_repo_from_cwd("/x") == "owner/repo"

    def test_normalizes_url_form(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "github.com/owner/repo")
        assert _mod._derive_repo_from_cwd("/x") == "owner/repo"

    def test_empty_is_none(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "")
        assert _mod._derive_repo_from_cwd("/x") is None

    def test_unnormalizable_is_none(self, monkeypatch):
        # A shell-variable / enterprise-host value can't be gated → None → caller
        # fails closed.
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "$VAR")
        assert _mod._derive_repo_from_cwd("/x") is None
