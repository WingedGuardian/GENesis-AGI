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

import pytest


@pytest.fixture(autouse=True)
def _hermetic_pr_files(monkeypatch):
    """Hermetic default for the hook-surface override gate's changed-files read:
    without this, force-path tests hit a LIVE `gh api pulls/N/files` call —
    green locally (gh authenticated; PR "1" of the cwd repo answers) and red in
    CI (call fails -> fail-closed block). Tests override per-case."""
    monkeypatch.setenv(
        "_TEST_GH_PR_FILES", '{"filename": "src/benign.py", "previous_filename": null}'
    )

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("git_push_guard", _HOOKS_DIR / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
STALE = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


@pytest.fixture(autouse=True)
def _hermetic_codex_comments(monkeypatch):
    """Default: NO Codex issue-comments, so the clean-comment freshness fallback in
    ``_check_codex_reviewed_head`` is network-free unless a test opts in. Tests override
    with their own ``monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", …)`` (later wins).
    (The required-CI-workflow seam pin these green rollup fixtures rely on is the
    shared autouse fixture in tests/test_hooks/conftest.py.)"""
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")


def _reviews_jsonl(*commit_ids, login="chatgpt-codex-connector[bot]"):
    """One JSON object per line, matching gh api .../reviews --jq '{login, commit_id}'."""
    return "\n".join(json.dumps({"login": login, "commit_id": cid}) for cid in commit_ids)


def _scheduled_marker(head=HEAD, *, login="owner", author_association="OWNER"):
    """_TEST_GH_SCHEDULED_COMMENTS shape — one OWNER-authored row carrying a marker for
    EVERY required kind (code-review + leaks) naming ``head``, so the scheduled gate is
    satisfied and these freshness/binding cases exercise their own target
    (author_association=OWNER satisfies the trust check regardless of derived repo owner)."""
    markers = "\n".join(
        f"<!-- genesis-scheduled-review: head={head} kind={k} -->" for k in ("code-review", "leaks")
    )
    body = "Scheduled review complete.\n" + markers
    return json.dumps({"login": login, "author_association": author_association, "body": body})


def _clean_comment_jsonl(
    short_sha, *, login="chatgpt-codex-connector[bot]", user_type="Bot", flavour="Swish!"
):
    """One Codex CLEAN issue-comment, matching the real body shape (variable flavour
    sentence + a backtick-wrapped abbreviated ``Reviewed commit`` sha)."""
    body = (
        f"Codex Review: Didn't find any major issues. {flavour}\n\n"
        f"**Reviewed commit:** `{short_sha}`\n\n<details>info</details>"
    )
    return json.dumps({"login": login, "type": user_type, "body": body})


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
        # Forced (# stale-review-override) on a NON-hook PR skips verification → no
        # head to bind → the match-head requirement disengages too. (The benign
        # non-hook default from _hermetic_pr_files makes the hook-surface evidence
        # gate a no-op, so the force path returns allowed + unbound.)
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))
        block, _, head = _mod._check_codex_reviewed_head("1", force=True)
        assert block is False and head is None

    def test_force_hook_surface_fresh_review_still_demands_evidence(self, monkeypatch, tmp_path):
        # SECURITY LOCK — Codex #9 dispositioned FALSE-POSITIVE (2026-08-26). A fresh
        # at-head Codex review must NOT skip the hook-surface evidence gate: the same
        # # stale-review-override ALSO waives _check_base_is_default, and the evidence
        # identity binds the BASE tip — which a head-only review provably cannot vouch
        # for (a hook PR retargeted to a non-default base). So a hook-surface forced
        # merge WITH a current review but NO recorded evidence still BLOCKS. Regression
        # guard: a reverted "skip evidence when fresh" attempt opened a base-binding
        # bypass; on that buggy code this test would have returned block=False.
        monkeypatch.setenv("GENESIS_OVERRIDE_REVIEW_EVIDENCE_DIR", str(tmp_path))  # no evidence file
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES",
            '{"filename": "scripts/hooks/git_push_guard.py", "previous_filename": null}',
        )
        monkeypatch.setenv("_TEST_GH_BASE_OID", STALE)  # hermetic base tip (no network)
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))  # FRESH review at head
        block, _, head = _mod._check_codex_reviewed_head("1", force=True)
        assert block is True and head is None


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


def _reviews_jsonl_state(*entries, login="chatgpt-codex-connector[bot]"):
    """One JSON object per line with an explicit state: entries are (commit_id, state)."""
    return "\n".join(
        json.dumps({"login": login, "commit_id": cid, "state": state}) for cid, state in entries
    )


def _compare_json(status="ahead", files=None):
    return json.dumps({"status": status, "files": files or []})


def _code_file(path="src/module_a.py", additions=80, deletions=0, has_patch=True, **kw):
    f = {
        "filename": path,
        "additions": additions,
        "deletions": deletions,
        "status": kw.get("status", "modified"),
        "has_patch": has_patch,
    }
    if "previous_filename" in kw:
        f["previous_filename"] = kw["previous_filename"]
    return f


class TestDismissedAndMalformedReviews:
    """DISMISSED must not vouch for a commit; malformed NDJSON must not crash."""

    def test_dismissed_latest_falls_back_to_earlier(self, monkeypatch):
        # Latest Codex review at HEAD is DISMISSED → the earlier (stale) one governs.
        monkeypatch.setenv(
            "_TEST_GH_CODEX_REVIEWS",
            _reviews_jsonl_state((STALE, "COMMENTED"), (HEAD, "DISMISSED")),
        )
        assert _mod._latest_codex_reviewed_sha("5") == STALE

    def test_all_dismissed_is_absent(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl_state((HEAD, "DISMISSED")))
        assert _mod._latest_codex_reviewed_sha("5") is None

    def test_missing_state_counts_as_active(self, monkeypatch):
        # Backward-compat: pre-existing seam entries carry no state.
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))
        assert _mod._latest_codex_reviewed_sha("5") == HEAD

    def test_non_dict_json_line_skipped(self, monkeypatch):
        # A valid-JSON non-object line (`null`) must be SKIPPED, not raise
        # AttributeError out of the "None on any parse error" contract.
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "null\n" + _reviews_jsonl(HEAD))
        assert _mod._latest_codex_reviewed_sha("5") == HEAD


class TestPostReviewDeltaClassify:
    """_classify_post_review_delta over the _TEST_GH_COMPARE seam."""

    def _lvl(self, monkeypatch, payload):
        monkeypatch.setenv("_TEST_GH_COMPARE", payload)
        return _mod._classify_post_review_delta(STALE, HEAD, None)

    def test_identical_is_inline(self, monkeypatch):
        assert self._lvl(monkeypatch, _compare_json("identical")) == "inline"

    def test_behind_fails_closed(self, monkeypatch):
        # Codex P1 #1373: `behind` (head is an ANCESTOR of the reviewed commit) does
        # NOT make head a content-subset — if the reviewed commit deleted code and the
        # PR reset to its parent, head carries code Codex never approved. Must NOT be
        # inline; it falls to the not-ahead/diverged branch → None → block.
        assert self._lvl(monkeypatch, _compare_json("behind")) is None
        assert self._lvl(monkeypatch, _compare_json("behind", [_code_file()])) is None

    def test_ahead_substantial(self, monkeypatch):
        assert self._lvl(monkeypatch, _compare_json("ahead", [_code_file()])) == "substantial"

    def test_ahead_trivial_is_inline(self, monkeypatch):
        assert self._lvl(monkeypatch, _compare_json("ahead", [_code_file(additions=3)])) == "inline"

    def test_malformed_filename_fails_closed_not_trivial(self, monkeypatch):
        # Codex P2 (round 5): a compare record with a missing/null/empty `filename`
        # must FAIL CLOSED (unclassifiable → None → block), never be silently skipped
        # so an empty delta reads as review-trivial. Pre-fix, classify_compare_
        # substantiality([{filename: None}]) returns "inline" (verified) → a hook-surface
        # delta could merge on a stale review. Fail closed instead.
        assert self._lvl(monkeypatch, _compare_json("ahead", [{"filename": None}])) is None
        assert self._lvl(monkeypatch, _compare_json("ahead", [{"filename": ""}])) is None
        assert self._lvl(monkeypatch, _compare_json("ahead", [{}])) is None  # key absent

    def test_malformed_previous_filename_fails_closed(self, monkeypatch):
        # A present-but-non-string `previous_filename` is also fail-closed (a rename
        # record we cannot fully read must not be assumed trivial).
        payload = _compare_json("ahead", [{"filename": "src/a.py", "previous_filename": 123}])
        assert self._lvl(monkeypatch, payload) is None

    def test_malformed_record_never_reads_as_inline_even_with_a_hook_file(self, monkeypatch):
        # The exact fail-open Codex named: a malformed record alongside real files must
        # never let the delta classify "inline". Fails closed (None) on the bad record
        # before triviality is granted.
        payload = _compare_json(
            "ahead", [{"filename": None}, {"filename": "scripts/hooks/git_push_guard.py"}]
        )
        assert self._lvl(monkeypatch, payload) != "inline"

    def test_diverged_substantial_still_classifies(self, monkeypatch):
        # A rebase/force-push rewrite must NOT be assumed trivial.
        assert self._lvl(monkeypatch, _compare_json("diverged", [_code_file()])) == "substantial"

    def test_docs_only_delta_is_inline(self, monkeypatch):
        docs = [_code_file(path="README.md", additions=200)]
        assert self._lvl(monkeypatch, _compare_json("ahead", docs)) == "inline"

    def test_truncated_compare_is_substantial(self, monkeypatch):
        files = [_code_file(path=f"docs/f{i}.md", additions=1) for i in range(300)]
        assert self._lvl(monkeypatch, _compare_json("ahead", files)) == "substantial"

    def test_unknown_status_fails_closed(self, monkeypatch):
        # Codex P2 #1373: a MISSING/unknown status (`{status:null,files:[]}` from a
        # truncated compare) must fail CLOSED (None → caller blocks), NOT fall
        # through to file classification where empty files would read "inline" and
        # let a STALE review bind the merge on an unverified delta.
        import json as _json

        assert self._lvl(monkeypatch, _json.dumps({"status": None, "files": []})) is None
        assert self._lvl(monkeypatch, _json.dumps({"files": []})) is None  # status absent
        assert self._lvl(monkeypatch, _json.dumps({"status": "weird", "files": []})) is None

    def test_non_dict_payload_unclassifiable(self, monkeypatch):
        assert self._lvl(monkeypatch, "null") is None

    def test_files_not_list_unclassifiable(self, monkeypatch):
        assert self._lvl(monkeypatch, json.dumps({"status": "ahead", "files": "oops"})) is None


class TestSmartDeltaFreshness:
    """Stale review + delta classification inside _check_codex_reviewed_head."""

    def _setup_stale(self, monkeypatch, compare_payload):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(STALE))
        monkeypatch.setenv("_TEST_GH_COMPARE", compare_payload)

    def test_stale_trivial_delta_allows_and_binds_head(self, monkeypatch):
        self._setup_stale(monkeypatch, _compare_json("ahead", [_code_file(additions=3)]))
        block, msg, verified = _mod._check_codex_reviewed_head("5")
        assert block is False
        # TOCTOU: the triviality claim is about exactly this head — merge must bind to it.
        assert verified == HEAD

    def test_stale_substantial_delta_blocks(self, monkeypatch):
        self._setup_stale(monkeypatch, _compare_json("ahead", [_code_file()]))
        block, msg, verified = _mod._check_codex_reviewed_head("5")
        assert block is True and verified is None
        assert "SUBSTANTIAL" in msg
        assert "stale-review-override" in msg

    def test_stale_unclassifiable_delta_blocks(self, monkeypatch):
        # Triviality is an exception granted only on positive evidence — an
        # unclassifiable compare (fail direction of a fail-CLOSED gate) blocks.
        self._setup_stale(monkeypatch, "null")
        block, msg, _ = _mod._check_codex_reviewed_head("5")
        assert block is True
        assert "could not be classified" in msg

    def test_absent_review_still_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        monkeypatch.setenv("_TEST_GH_COMPARE", _compare_json("identical"))
        block, msg, _ = _mod._check_codex_reviewed_head("5")
        assert block is True  # absent is not narrowed by smart-delta
        assert "stale-review-override" in msg


class TestMainLevelIntegration:
    """main()-level wiring: TOCTOU binding enforcement + sigil decoupling.

    These run the REAL _check_codex_reviewed_head / _check_base_is_default via
    the _TEST_GH_* seams (only _check_mergeable and the two advisory scanners
    are stubbed), so the `if verified_head:` binding block and the sigil routing
    in main() are actually exercised — previously zero tests reached them
    (architect SHOULD-FIX on #1366: delete the binding block and nothing went red).
    """

    def _drive(self, monkeypatch, command, *, reviews=None, compare=None):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv(
            "_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD) if reviews is None else reviews
        )
        if compare is not None:
            monkeypatch.setenv("_TEST_GH_COMPARE", compare)
        monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
        monkeypatch.setenv("_TEST_GH_DEFAULT_BRANCH", "main")
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([{"name": "t", "workflowName": "CI", "conclusion": "SUCCESS"}]),
        )
        # A valid OWNER scheduled-review marker AT head, so the scheduled-review merge
        # gate is satisfied and these cases keep exercising the freshness/binding wiring
        # they target (not the new gate).
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _scheduled_marker(HEAD))
        monkeypatch.setattr(_mod, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        monkeypatch.setattr(
            _mod, "_check_pr_review_findings", lambda n, force=False, repo=None: (False, "")
        )
        monkeypatch.setattr(
            _mod, "_check_inline_review_findings", lambda n, force=False, repo=None: (False, "")
        )
        monkeypatch.setattr(
            _mod,
            "read_payload",
            lambda: {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
        )
        return _mod.main()

    # ── TOCTOU binding (fresh review at HEAD → binding REQUIRED) ──
    def test_unbound_merge_blocks(self, monkeypatch, capsys):
        rc = self._drive(monkeypatch, "gh pr merge 5 --squash --admin")
        assert rc == 2
        assert "must be bound" in capsys.readouterr().err

    def test_bound_merge_passes(self, monkeypatch):
        rc = self._drive(monkeypatch, f"gh pr merge 5 --squash --admin --match-head-commit {HEAD}")
        assert rc == 0

    def test_shadow_flag_blocks(self, monkeypatch, capsys):
        rc = self._drive(
            monkeypatch,
            f"gh pr merge 5 --squash --admin --body x --match-head-commit {HEAD}",
        )
        assert rc == 2
        assert "shadow" in capsys.readouterr().err.lower()

    def test_mismatched_binding_blocks(self, monkeypatch, capsys):
        rc = self._drive(monkeypatch, f"gh pr merge 5 --squash --admin --match-head-commit {STALE}")
        assert rc == 2
        assert "does not" in capsys.readouterr().err

    # ── Sigil decoupling (stale review + substantial delta) ──
    def _stale_substantial(self):
        return dict(
            reviews=_reviews_jsonl(STALE),
            compare=_compare_json("ahead", [_code_file()]),
        )

    def test_review_override_does_not_waive_freshness(self, monkeypatch, capsys):
        rc = self._drive(
            monkeypatch,
            "gh pr merge 5 --squash --admin  # review-override",
            **self._stale_substantial(),
        )
        assert rc == 2  # the P1-findings sigil must NOT waive the freshness gate
        assert "STALE" in capsys.readouterr().err

    def test_stale_review_override_waives_freshness(self, monkeypatch):
        rc = self._drive(
            monkeypatch,
            "gh pr merge 5 --squash --admin  # stale-review-override",
            **self._stale_substantial(),
        )
        assert rc == 0  # waived; verified_head None → binding not required

    def test_no_sigil_stale_substantial_blocks(self, monkeypatch):
        rc = self._drive(monkeypatch, "gh pr merge 5 --squash --admin", **self._stale_substantial())
        assert rc == 2

    # ── Smart-delta through main(): stale + trivial → allowed but still bound ──
    def test_stale_trivial_requires_binding(self, monkeypatch, capsys):
        rc = self._drive(
            monkeypatch,
            "gh pr merge 5 --squash --admin",
            reviews=_reviews_jsonl(STALE),
            compare=_compare_json("ahead", [_code_file(additions=3)]),
        )
        assert rc == 2  # allowed by smart-delta, but the merge must bind to HEAD
        assert "must be bound" in capsys.readouterr().err

    def test_stale_trivial_bound_passes(self, monkeypatch):
        rc = self._drive(
            monkeypatch,
            f"gh pr merge 5 --squash --admin --match-head-commit {HEAD}",
            reviews=_reviews_jsonl(STALE),
            compare=_compare_json("ahead", [_code_file(additions=3)]),
        )
        assert rc == 0


class TestStaleSigilDoesNotWaiveScanners:
    """The reverse decoupling direction: # stale-review-override must NOT feed the
    finding scanners' force — a stale-sigil merge with an unresolved P1 still blocks.
    (Architect F5 on this patch: the forward direction alone left a regression that
    routes stale_override into the scanners undetected.)"""

    def test_stale_sigil_p1_findings_still_block(self, monkeypatch, capsys):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(STALE))
        monkeypatch.setenv("_TEST_GH_COMPARE", _compare_json("ahead", [_code_file()]))
        monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
        monkeypatch.setenv("_TEST_GH_DEFAULT_BRANCH", "main")
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([{"name": "t", "workflowName": "CI", "conclusion": "SUCCESS"}]),
        )
        # Scheduled review satisfied at head → the stale-override merge reaches the
        # finding scanner this test is about (not the new scheduled gate).
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _scheduled_marker(HEAD))
        monkeypatch.setattr(_mod, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        # Scanner stub blocks UNLESS its own force (i.e. # review-override) is set.
        monkeypatch.setattr(
            _mod,
            "_check_pr_review_findings",
            lambda n, force=False, repo=None: (not force, "" if force else "P1 unresolved"),
        )
        monkeypatch.setattr(
            _mod, "_check_inline_review_findings", lambda n, force=False, repo=None: (False, "")
        )
        monkeypatch.setattr(
            _mod,
            "read_payload",
            lambda: {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {
                    "command": "gh pr merge 5 --squash --admin  # stale-review-override"
                },
            },
        )
        rc = _mod.main()
        assert rc == 2  # freshness waived, but the P1 findings gate still blocks
        assert "review-body gate did not pass" in capsys.readouterr().err


class TestCheckPrRepoArg:
    """--check-pr accepts every normal gh repo spelling (Codex P2 on #1366)."""

    def test_separate_long(self):
        assert _mod._parse_check_pr_repo(["--repo", "octo/voice"]) == "octo/voice"

    def test_separate_short(self):
        assert _mod._parse_check_pr_repo(["-R", "octo/voice"]) == "octo/voice"

    def test_equals_long(self):
        assert _mod._parse_check_pr_repo(["--repo=octo/voice"]) == "octo/voice"

    def test_equals_short(self):
        assert _mod._parse_check_pr_repo(["-R=octo/voice"]) == "octo/voice"

    def test_absent(self):
        assert _mod._parse_check_pr_repo([]) is None

    def test_glued_short(self):
        # pflag shorthand `-Rowner/repo` — enforcement accepts it; the report must too
        # (architect F3: unrecognized → silently checked the CWD repo).
        assert _mod._parse_check_pr_repo(["-Rocto/voice"]) == "octo/voice"

    def test_explicit_empty_value_rejected(self):
        # Codex P2 #1373: an EXPLICITLY empty repo value must be distinguishable from
        # "no option" (None → cwd repo) so the caller can reject it rather than
        # silently report the WRONG same-numbered PR.
        for argv in (["--repo"], ["--repo="], ["-R"], ["-R="]):
            assert _mod._parse_check_pr_repo(argv) is _mod._CHECK_PR_REPO_EMPTY, argv

    def test_no_option_is_none(self):
        assert _mod._parse_check_pr_repo(["--squash", "--admin"]) is None


class TestCheckPrReportFreshnessLabel:
    """Codex P2 #1373: the canonical report must not print 'ok (current)' when the
    review is STALE-but-trivial — the structured stdout would mislead consumers."""

    def _run_report(self, monkeypatch, capsys, reviews, compare):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", reviews)
        monkeypatch.setenv("_TEST_GH_COMPARE", compare)
        monkeypatch.setattr(_mod, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: ("green", []))
        monkeypatch.setattr(_mod, "_check_base_is_default", lambda n, repo=None: (False, ""))
        monkeypatch.setattr(
            _mod, "_check_pr_review_findings", lambda n, repo=None, force=False: (False, "")
        )
        monkeypatch.setattr(
            _mod, "_check_inline_review_findings", lambda n, repo=None, force=False: (False, "")
        )
        _mod.check_pr_report("5")
        return capsys.readouterr().out

    def test_stale_trivial_labeled_stale(self, monkeypatch, capsys):
        out = self._run_report(
            monkeypatch,
            capsys,
            reviews=_reviews_jsonl(STALE),
            compare=_compare_json("ahead", [_code_file(additions=3)]),
        )
        assert "codex-at-head" in out
        assert "STALE review" in out
        assert "ok (current)" not in out

    def test_current_labeled_current(self, monkeypatch, capsys):
        out = self._run_report(
            monkeypatch,
            capsys,
            reviews=_reviews_jsonl(HEAD),
            compare=_compare_json("identical"),
        )
        assert "ok (current)" in out

    def test_the_scheduled_row_renders_its_per_cause_tail(self, monkeypatch, capsys):
        """The canonical report printed only line 0 of the scheduled-review message.

        Line 0 is the summary, and its `present: none` clause is the exact string an
        operator was measured acting wrongly on — read as "nothing was posted", waited,
        while the marker sat in the thread. Every per-cause bullet lives on lines 1+, so
        printing line 0 alone made the entire diagnosis invisible on the one surface
        where the mistake is actually made. The sibling `pin-receipts` row already
        rendered its tail; this asserts the scheduled row does too.
        """
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "leaks")
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            json.dumps(
                {
                    "login": "drive-by",
                    "author_association": "CONTRIBUTOR",
                    "body": f"<!-- genesis-scheduled-review: head={HEAD} kind=leaks -->",
                }
            ),
        )
        out = self._run_report(
            monkeypatch,
            capsys,
            reviews=_reviews_jsonl(HEAD),
            compare=_compare_json("identical"),
        )
        assert "scheduled-claude: BLOCK" in out
        assert "drive-by" in out, (
            "the per-cause bullet never reached the canonical report, so the whole "
            f"diagnosis is invisible where the operator reads it:\n{out}"
        )


class TestMergeDeadline:
    """Codex P1 #1373: _gh_timeout bounds the AGGREGATE merge-path gh time under the
    hook's ~60s wall-clock via a shared deadline, so per-call caps can't sum past it."""

    def test_no_deadline_returns_cap(self, monkeypatch):
        monkeypatch.setattr(_mod, "_merge_deadline", None)
        assert _mod._gh_timeout(8) == 8
        assert _mod._gh_timeout(6) == 6

    def test_deadline_clamps_to_remaining(self, monkeypatch):
        import time as _t

        monkeypatch.setattr(_mod, "_merge_deadline", _t.monotonic() + 3)
        # remaining ~3s < cap 8 → clamped toward remaining (not the full cap)
        assert _mod._gh_timeout(8) <= 3.5

    def test_expired_deadline_floors_at_one(self, monkeypatch):
        import time as _t

        monkeypatch.setattr(_mod, "_merge_deadline", _t.monotonic() - 5)  # already past
        assert _mod._gh_timeout(8) == 1.0  # fail FAST, never negative/zero

    def test_budget_under_wallclock(self):
        # The documented worst-case pre-binding aggregate must leave headroom under 60s.
        assert _mod._MERGE_GATE_BUDGET_S <= 50


class TestReportFreshnessUnreadable:
    """Codex P2 #1373: a failed relabel re-read must not read as 'ok (current)'."""

    def test_failed_reread_labeled_unverified(self, monkeypatch, capsys):
        monkeypatch.setattr(_mod, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: ("green", []))
        monkeypatch.setattr(_mod, "_check_base_is_default", lambda n, repo=None: (False, ""))
        monkeypatch.setattr(
            _mod, "_check_pr_review_findings", lambda n, repo=None, force=False: (False, "")
        )
        monkeypatch.setattr(
            _mod, "_check_inline_review_findings", lambda n, repo=None, force=False: (False, "")
        )
        # freshness gate passes (allowed), but the relabel re-reads fail (None)
        monkeypatch.setattr(
            _mod, "_check_codex_reviewed_head", lambda n, repo=None: (False, "", HEAD)
        )
        monkeypatch.setattr(_mod, "_latest_codex_reviewed_sha", lambda n, repo=None: None)
        monkeypatch.setattr(_mod, "_pr_head_sha", lambda n, repo=None: None)
        _mod.check_pr_report("5")
        out = capsys.readouterr().out
        assert "unverified" in out
        assert "ok (current)" not in out


class TestCleanCommentParsing:
    """_latest_codex_clean_comment_sha — parse the real Codex clean-comment shape."""

    @pytest.mark.parametrize(
        "flavour",
        [
            "Swish!",
            "You're on a roll.",
            "Keep them coming!",
            ":rocket:",
            "What shall we delve into next?",
        ],
    )
    def test_parses_all_flavour_variants(self, monkeypatch, flavour):
        # Anchor is the STABLE prefix "Didn't find any major issues", not the flavour.
        monkeypatch.setenv(
            "_TEST_GH_CODEX_COMMENTS", _clean_comment_jsonl("0cd13afeb5", flavour=flavour)
        )
        assert _mod._latest_codex_clean_comment_sha("1") == "0cd13afeb5"

    def test_latest_clean_comment_wins(self, monkeypatch):
        # Comments come oldest-first; the most recent clean comment wins.
        lines = "\n".join([_clean_comment_jsonl("aaaaaaa"), _clean_comment_jsonl("bbbbbbb")])
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", lines)
        assert _mod._latest_codex_clean_comment_sha("1") == "bbbbbbb"

    def test_sha_lowercased(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", _clean_comment_jsonl("0CD13AFEB5"))
        assert _mod._latest_codex_clean_comment_sha("1") == "0cd13afeb5"

    def test_clean_marker_without_sha_is_none(self, monkeypatch):
        body = json.dumps(
            {
                "login": "chatgpt-codex-connector[bot]",
                "type": "Bot",
                "body": "Codex Review: Didn't find any major issues. Swish!",
            }
        )
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", body)
        assert _mod._latest_codex_clean_comment_sha("1") is None  # fail-closed

    def test_sha_without_clean_marker_is_none(self, monkeypatch):
        # A FINDINGS comment carries a Reviewed-commit line but no clean marker.
        body = json.dumps(
            {
                "login": "chatgpt-codex-connector[bot]",
                "type": "Bot",
                "body": "### Codex Review\n[P1] a real bug\n\n**Reviewed commit:** `0cd13afeb5`",
            }
        )
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", body)
        assert _mod._latest_codex_clean_comment_sha("1") is None

    def test_non_codex_author_ignored(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_CODEX_COMMENTS",
            _clean_comment_jsonl("0cd13afeb5", login="attacker", user_type="User"),
        )
        assert _mod._latest_codex_clean_comment_sha("1") is None

    def test_codex_login_but_not_bot_type_ignored(self, monkeypatch):
        # Belt-and-suspenders: correct login string but user.type != Bot → ignored.
        monkeypatch.setenv(
            "_TEST_GH_CODEX_COMMENTS", _clean_comment_jsonl("0cd13afeb5", user_type="User")
        )
        assert _mod._latest_codex_clean_comment_sha("1") is None

    def test_short_sha_below_min_length_rejected(self, monkeypatch):
        # <7 hex is not a usable prefix (the regex floors at 7).
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", _clean_comment_jsonl("0cd13"))
        assert _mod._latest_codex_clean_comment_sha("1") is None


class TestCleanCommentFreshness:
    """_check_codex_reviewed_head — a clean Codex ISSUE-COMMENT at head satisfies
    freshness even when the review-object path can't (absent / stale review)."""

    def test_no_review_object_but_clean_comment_at_head_allows(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")  # no review object → would block
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", _clean_comment_jsonl(HEAD[:10]))
        block, msg, head = _mod._check_codex_reviewed_head("1")
        assert block is False and msg == "" and head == HEAD  # bound for --match-head-commit

    def test_stale_review_object_but_clean_comment_at_head_allows(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(STALE))  # stale → would block
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", _clean_comment_jsonl(HEAD[:10]))
        block, _, head = _mod._check_codex_reviewed_head("1")
        assert block is False and head == HEAD

    def test_clean_comment_for_different_sha_still_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", _clean_comment_jsonl(STALE[:10]))  # not head
        block, msg, _ = _mod._check_codex_reviewed_head("1")
        assert block is True and "no codex review" in msg.lower()

    def test_clean_marker_without_sha_still_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        monkeypatch.setenv(
            "_TEST_GH_CODEX_COMMENTS",
            json.dumps(
                {
                    "login": "chatgpt-codex-connector[bot]",
                    "type": "Bot",
                    "body": "Codex Review: Didn't find any major issues. Swish!",
                }
            ),
        )
        block, _, _ = _mod._check_codex_reviewed_head("1")
        assert block is True  # fail-closed: marker alone never vouches

    def test_findings_comment_with_sha_still_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        monkeypatch.setenv(
            "_TEST_GH_CODEX_COMMENTS",
            json.dumps(
                {
                    "login": "chatgpt-codex-connector[bot]",
                    "type": "Bot",
                    "body": f"### Codex Review\n[P1] bug\n\n**Reviewed commit:** `{HEAD[:10]}`",
                }
            ),
        )
        block, _, _ = _mod._check_codex_reviewed_head("1")
        assert block is True

    def test_spoofed_author_clean_comment_still_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        monkeypatch.setenv(
            "_TEST_GH_CODEX_COMMENTS",
            _clean_comment_jsonl(HEAD[:10], login="attacker", user_type="User"),
        )
        block, _, _ = _mod._check_codex_reviewed_head("1")
        assert block is True

    def test_current_review_object_does_not_need_comment(self, monkeypatch):
        # Fast path: a review object at head allows without any comment.
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD))
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")  # none needed
        block, _, head = _mod._check_codex_reviewed_head("1")
        assert block is False and head == HEAD

    def test_report_labels_clean_comment_at_head(self, monkeypatch, capsys):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", _clean_comment_jsonl(HEAD[:10]))
        # Stub the other gates so the report runs; we only assert the codex-at-head label.
        monkeypatch.setattr(_mod, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: ("green", []))
        monkeypatch.setattr(_mod, "_check_base_is_default", lambda n, repo=None: (False, ""))
        monkeypatch.setattr(
            _mod, "_check_pr_review_findings", lambda n, repo=None, force=False: (False, "")
        )
        monkeypatch.setattr(
            _mod, "_check_inline_review_findings", lambda n, repo=None, force=False: (False, "")
        )
        _mod.check_pr_report("1")
        out = capsys.readouterr().out
        assert "clean comment at head" in out


class TestRequiredScheduledReviewKinds:
    """The config lever: default = both required; leaks is irreducible; local config
    (or the _TEST_ seam) may relax the OPTIONAL kinds to advisory. Fails CLOSED (to the
    full default set) on any unreadable/malformed config."""

    def test_default_is_both_when_unconfigured(self, monkeypatch, tmp_path):
        monkeypatch.delenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))  # no genesis.yaml -> default
        assert _mod._required_scheduled_review_kinds() == ("code-review", "leaks")

    def test_env_seam_relaxes_to_leaks_only(self, monkeypatch):
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "leaks")
        assert _mod._required_scheduled_review_kinds() == ("leaks",)

    def test_leaks_is_irreducible(self, monkeypatch):
        # config omits leaks -> still forced in (secret scanner never removable)
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "code-review")
        assert _mod._required_scheduled_review_kinds() == ("code-review", "leaks")

    def test_empty_config_is_leaks_only(self, monkeypatch):
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "")
        assert _mod._required_scheduled_review_kinds() == ("leaks",)

    def test_kinds_are_lowercased(self, monkeypatch):
        # mis-cased config must normalize to the lowercase marker grammar, else it
        # names a kind no real marker can satisfy and silently wedges the gate.
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "Code-Review,LEAKS")
        assert _mod._required_scheduled_review_kinds() == ("code-review", "leaks")

    def test_env_unknown_kind_fails_closed(self, monkeypatch):
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "bogus")
        assert _mod._required_scheduled_review_kinds() == ("code-review", "leaks")

    @staticmethod
    def _write_cfg(tmp_path, monkeypatch, body):
        monkeypatch.delenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", raising=False)
        cfgdir = tmp_path / ".genesis" / "config"
        cfgdir.mkdir(parents=True)
        (cfgdir / "genesis.yaml").write_text(body)
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_config_wrong_type_element_fails_closed(self, monkeypatch, tmp_path):
        self._write_cfg(tmp_path, monkeypatch, "merge_gate:\n  required_scheduled_reviews: [123]\n")
        assert _mod._required_scheduled_review_kinds() == ("code-review", "leaks")

    def test_config_blank_element_fails_closed(self, monkeypatch, tmp_path):
        self._write_cfg(tmp_path, monkeypatch, "merge_gate:\n  required_scheduled_reviews: [' ']\n")
        assert _mod._required_scheduled_review_kinds() == ("code-review", "leaks")

    def test_config_unknown_kind_fails_closed(self, monkeypatch, tmp_path):
        self._write_cfg(tmp_path, monkeypatch, "merge_gate:\n  required_scheduled_reviews: [foo]\n")
        assert _mod._required_scheduled_review_kinds() == ("code-review", "leaks")

    def test_config_empty_list_is_leaks_only(self, monkeypatch, tmp_path):
        self._write_cfg(tmp_path, monkeypatch, "merge_gate:\n  required_scheduled_reviews: []\n")
        assert _mod._required_scheduled_review_kinds() == ("leaks",)

    def test_duplicate_key_fails_closed(self, monkeypatch, tmp_path):
        # A badly-merged file with two merge_gate blocks whose last says [leaks] must
        # NOT silently drop code-review; duplicate keys fail closed to the default.
        body = (
            "merge_gate:\n  required_scheduled_reviews: [code-review, leaks]\n"
            "merge_gate:\n  required_scheduled_reviews: [leaks]\n"
        )
        self._write_cfg(tmp_path, monkeypatch, body)
        assert _mod._required_scheduled_review_kinds() == ("code-review", "leaks")

    def test_reads_config_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", raising=False)
        cfgdir = tmp_path / ".genesis" / "config"
        cfgdir.mkdir(parents=True)
        (cfgdir / "genesis.yaml").write_text("merge_gate:\n  required_scheduled_reviews: [leaks]\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _mod._required_scheduled_review_kinds() == ("leaks",)

    def test_malformed_config_fails_closed_to_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", raising=False)
        cfgdir = tmp_path / ".genesis" / "config"
        cfgdir.mkdir(parents=True)
        (cfgdir / "genesis.yaml").write_text("merge_gate: [unterminated\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _mod._required_scheduled_review_kinds() == ("code-review", "leaks")


class TestScheduledGateUsesRequiredKinds:
    """The gate consumes _required_scheduled_review_kinds(): a relaxed (advisory)
    code-review no longer blocks, the default still requires it, and leaks blocks even
    when config tries to omit it."""

    @staticmethod
    def _marker(*kinds, head=HEAD):
        body = "scheduled review done.\n" + "\n".join(
            f"<!-- genesis-scheduled-review: head={head} kind={k} -->" for k in kinds
        )
        return json.dumps({"login": "owner", "author_association": "OWNER", "body": body})

    def _setup(self, monkeypatch, present_kinds, required_env):
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", self._marker(*present_kinds))
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", required_env)

    def test_relaxed_leaks_only_passes_without_code_review(self, monkeypatch):
        self._setup(monkeypatch, ["leaks"], "leaks")
        assert (
            _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub") is None
        )

    def test_default_blocks_when_code_review_missing(self, monkeypatch):
        self._setup(monkeypatch, ["leaks"], "code-review,leaks")
        msg = _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")
        assert msg and "code-review" in msg

    def test_leaks_irreducible_blocks_even_if_config_omits_it(self, monkeypatch):
        self._setup(monkeypatch, ["code-review"], "code-review")  # config omits leaks
        msg = _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")
        assert msg and "leaks" in msg


OTHER_HEAD = "1111111111111111111111111111111111111111"


class TestScheduledGateBlockMessageBranchesOnCause:
    """The block message is an INVENTORY: every marker block found is listed under the
    kind it names, with its status, and nothing is subtracted from anything.

    HISTORY. This class was written when the message PARTITIONED the missing kinds by
    cause and let one cause per kind win. Across six review rounds every finding on
    that shape was the same defect: the winning cause hid a fact the operator needed.
    The tests below were rewritten from "the winning branch says X and not Y" to "the
    row for X is present"; the reasoning is kept where a test's meaning changed. The
    one conditional that survives is a count: a kind with no OWNER-authored block at
    any head gets the in-flight note, because that is the only state in which waiting
    can help.

    Original framing, still true of the facts even though nothing "branches" now:

    * markers exist for OTHER heads -> a routine ran, then a push moved the head. Waiting
      is unlikely to help; re-review the current head and post the marker by hand.
    * no marker at ANY head -> nothing has run. On a freshly-opened PR a routine may
      still be in flight, and WAITING IS THE CORRECT ACTION.

    An earlier draft asserted an unconditional "waiting will not clear this". That was
    false in the second case and steered the operator toward the override sigil — i.e.
    toward waiving the IRREDUCIBLE leak gate — in the one situation where patience was
    the right answer.

    Each test asserts what its branch ADVISES *and* that it does not carry the other
    branch's advice, so a reworded regression that flips the guidance fails instead of
    sliding through on surviving substrings.
    """

    @staticmethod
    def _marker_at(head, prose="scheduled review done."):
        body = f"{prose}\n<!-- genesis-scheduled-review: head={head} kind=leaks -->"
        return json.dumps({"login": "owner", "author_association": "OWNER", "body": body})

    def _block_msg(self, monkeypatch, comments):
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", comments)
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "leaks")
        msg = _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")
        assert msg, "a head without the required marker must block"
        return msg

    # -- branch 1: a marker exists, but for an earlier head ---------------------
    def test_stale_marker_lists_the_older_head_as_a_row(self, monkeypatch):
        """Was: asserts the advice "unlikely to clear this / re-run". The inventory
        carries no advice; the fact it replaces it with is the accepted-elsewhere row."""
        msg = self._block_msg(monkeypatch, self._marker_at(OTHER_HEAD))
        assert f"accepted at a DIFFERENT head ({OTHER_HEAD[:12]})" in msg
        assert "none of which counts at the current head" in msg

    def test_stale_marker_does_not_tell_the_operator_to_wait(self, monkeypatch):
        """The failure this branch exists to prevent: advice-to-wait on a pushed head."""
        msg = self._block_msg(monkeypatch, self._marker_at(OTHER_HEAD)).lower()
        assert "may still be in flight" not in msg
        assert "waiting is the right move" not in msg

    def test_stale_marker_names_the_head_that_was_actually_reviewed(self, monkeypatch):
        """Evidence, not assertion — the operator sees which commit DID get reviewed."""
        msg = self._block_msg(monkeypatch, self._marker_at(OTHER_HEAD))
        assert OTHER_HEAD[:12] in msg

    # -- branch 2: nothing has ever posted --------------------------------------
    def test_no_marker_anywhere_says_waiting_may_be_correct(self, monkeypatch):
        msg = self._block_msg(monkeypatch, "").lower()
        assert "may still be in flight" in msg
        assert "waiting is the right move" in msg

    def test_no_marker_anywhere_does_not_claim_waiting_is_futile(self, monkeypatch):
        """Regression guard for the overcorrection this class documents."""
        msg = self._block_msg(monkeypatch, "").lower()
        assert "unlikely to clear this" not in msg
        assert "will not clear" not in msg

    # -- branch 3: a marker IS at this head, but was refused as not-clean -------
    # MEASURED on PR #1521 (2026-08-28): an owner marker at the exact head, body
    # "...not a hard block.", was refused by the HARD\\s+BLOCK pattern (which is
    # negation-blind) with no clean phrase to override — and the gate reported
    # "present: none", identical to nobody having posted anything.
    REFUSED_PROSE = "Reviewed the diff. The config value is not a hard block."

    def test_refused_marker_says_a_marker_is_present_at_this_head(self, monkeypatch):
        msg = self._block_msg(monkeypatch, self._marker_at(HEAD, self.REFUSED_PROSE))
        assert "IS present at THIS head" in msg
        assert "REFUSED" in msg

    def test_refused_marker_states_the_cause_without_the_verdict_incantation(self, monkeypatch):
        """INVERTED from its previous form, which asserted the two verdict strings VERBATIM.

        A gate that prints the exact line that makes it pass is explaining how to get
        past itself: the refused body may carry a REAL finding, and "append this string"
        launders it. The rule that decides "refused" is documented in the dev skill; the
        message states the cause and stops.
        """
        msg = self._block_msg(monkeypatch, self._marker_at(HEAD, self.REFUSED_PROSE))
        assert "no clean-verdict line overrides it" in msg
        assert "VERDICT: PASS" not in msg
        assert "PII/Secrets/Wording: CLEAN" not in msg

    def test_refused_marker_does_not_give_the_other_branches_advice(self, monkeypatch):
        """The failure this branch exists to prevent: being told to wait, or to re-post
        the identical comment, when the body's WORDING is what refused it."""
        msg = self._block_msg(monkeypatch, self._marker_at(HEAD, self.REFUSED_PROSE)).lower()
        assert "may still be in flight" not in msg
        assert "waiting is the right move" not in msg
        assert "different head" not in msg

    def test_a_clean_verdict_line_makes_the_same_marker_pass(self, monkeypatch):
        """The remedy the message prescribes must actually work — end to end."""
        prose = self.REFUSED_PROSE + "\n\nVERDICT: PASS"
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", self._marker_at(HEAD, prose))
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "leaks")
        assert (
            _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub") is None
        )

    def test_a_refused_marker_never_satisfies_the_gate(self, monkeypatch):
        """The diagnostic must not have widened what passes: refused still blocks."""
        msg = self._block_msg(monkeypatch, self._marker_at(HEAD, self.REFUSED_PROSE))
        assert msg, "a refused marker must still BLOCK, not merely warn"

    # -- misroutes: states that fell into the WRONG branch ----------------------
    # Each of these reproduced a real misdiagnosis: the branch logic consulted only part
    # of what had been read, so the operator got another branch's advice. That is the
    # exact defect class this change exists to remove, one hop away from where it started.

    @staticmethod
    def _marker_kind(head, kind, prose="scheduled review done."):
        body = f"{prose}\n<!-- genesis-scheduled-review: head={head} kind={kind} -->"
        return json.dumps({"login": "owner", "author_association": "OWNER", "body": body})

    def test_refused_marker_at_a_stale_head_is_not_reported_as_nothing_ran(self, monkeypatch):
        """A REFUSED marker at an earlier head still proves a routine ran on this PR.

        Counting only ACCEPTED markers when looking for other heads sent this state down
        the "nothing has run — wait for it" path: wrong claim AND wrong advice.
        """
        msg = self._block_msg(monkeypatch, self._marker_at(OTHER_HEAD, self.REFUSED_PROSE))
        assert f"REFUSED at a DIFFERENT head ({OTHER_HEAD[:12]})" in msg
        assert "may still be in flight" not in msg.lower()

    def test_partial_acceptance_does_not_contradict_the_present_line(self, monkeypatch):
        """With >1 required kind, one satisfied kind must not yield 'no marker at ANY head'.

        The header lists `present: code-review` — a blanket "no marker at ANY head" on the
        next line contradicts it. Fires on the SHIPPED DEFAULT required set, not an edge.
        """
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", self._marker_kind(HEAD, "code-review"))
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "code-review,leaks")
        msg = _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")
        assert msg and "present: code-review" in msg
        assert "leaks — no marker block found" in msg, "must be scoped to the MISSING kind"
        assert "code-review —" not in msg, "a satisfied kind gets no row at all"

    def test_refused_marker_for_an_already_satisfied_kind_does_not_hijack_the_message(
        self, monkeypatch
    ):
        """A refused marker whose kind is NOT missing must not drive the message.

        leaks is satisfied at HEAD; a second, refused leaks marker also sits at HEAD;
        code-review is what is actually missing. The message must be about code-review.

        REWRITTEN: the satisfying marker now carries an explicit verdict line. As first
        written it was a PLAIN clean marker beside a refused one at the same head, and
        the test asserted the plain one won -- which is the laundering defect
        `TestARefusalAtHeadIsNotLaunderedByAPlainRepost` closes, in the other order. A
        refusal at a head is cleared by a stated verdict, not by whichever row happens
        to be clean; with the verdict present, leaks is genuinely satisfied and the
        original intent of this test (no hijack by a satisfied kind) is preserved.
        """
        comments = "\n".join(
            [
                self._marker_kind(HEAD, "leaks", "scheduled review done.\nVERDICT: PASS"),
                self._marker_kind(HEAD, "leaks", self.REFUSED_PROSE),
            ]
        )
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", comments)
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "code-review,leaks")
        msg = _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")
        assert msg and "code-review" in msg
        assert "REFUSED" not in msg, "the refused kind is already satisfied — not the cause"
        assert "code-review — no marker block found" in msg

    def test_mixed_causes_report_every_missing_kind_not_just_one(self, monkeypatch):
        """Two missing kinds failing for DIFFERENT reasons must BOTH get guidance.

        The regression this guards: picking a single winning cause for the whole block
        explained one kind and left the other with no guidance at all. Three successive
        review findings on this function were that same shape (a decision made from part
        of what had been read), which is why the causes are now a total PARTITION over
        the missing kinds rather than a precedence chain.
        """
        comments = "\n".join(
            [
                self._marker_kind(HEAD, "code-review", self.REFUSED_PROSE),
                self._marker_kind(OTHER_HEAD, "leaks"),
            ]
        )
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", comments)
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "code-review,leaks")
        msg = _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")
        assert msg
        # code-review: refused AT this head. The row states the cause; no incantation.
        assert "code-review — 1 marker block(s) found" in msg
        assert "IS present at THIS head but was REFUSED" in msg
        # leaks: accepted at an EARLIER head, named.
        assert "leaks — 1 marker block(s) found" in msg
        assert f"accepted at a DIFFERENT head ({OTHER_HEAD[:12]})" in msg
        # Neither kind may be silently dropped: one bullet per MISSING kind.
        assert msg.count("  * ") == 2, "one bullet per missing kind, both present"

    def test_three_way_split_reports_all_three_causes(self, monkeypatch):
        """The full partition: refused here, present elsewhere, and absent — at once."""
        third = "2" * 40
        comments = "\n".join(
            [
                self._marker_kind(HEAD, "code-review", self.REFUSED_PROSE),
                self._marker_kind(third, "leaks"),
            ]
        )
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", comments)
        # A third required kind nothing has ever posted for.
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "code-review,leaks")
        msg = _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")
        assert msg and msg.count("  * ") == 2
        assert "code-review — 1 marker block(s) found" in msg
        assert "IS present at THIS head but was REFUSED" in msg
        assert f"accepted at a DIFFERENT head ({third[:12]})" in msg

    # -- shared: what ALL branches must still carry -----------------------------
    @pytest.mark.parametrize("which", ["stale", "empty", "refused"])
    def test_every_branch_gives_the_full_head_and_marker_grammar(self, monkeypatch, which):
        """Route 1 is unusable without the FULL 40-hex head; the summary line shows 12."""
        comments = {
            "stale": self._marker_at(OTHER_HEAD),
            "empty": "",
            "refused": self._marker_at(HEAD, self.REFUSED_PROSE),
        }[which]
        msg = self._block_msg(monkeypatch, comments)
        assert f"head={HEAD}" in msg, "the full 40-hex head must be quoted verbatim"
        assert "genesis-scheduled-review" in msg
        assert "kind=" in msg
        assert "# scheduled-review-override" in msg
        assert "leaks" in msg


SHORT_HEAD = HEAD[:8]


class TestAnUnusableMarkerIsNotReportedAsAbsent:
    """A marker BLOCK that is present but does not parse must not read as "nobody
    posted anything".

    MEASURED on a live PR, 2026-08-29. A leaks marker was posted whose machine
    field carried an ABBREVIATED head (8 hex, not 40). ``_SCHEDULED_REVIEW_HEAD_RE``
    requires the full 40, so the block matched, the head did not, and the marker was
    dropped before it reached EITHER map. The gate then printed ``present: none`` and
    routed the kind into ABSENT -- whose advice is that a routine "may still be in
    flight, and waiting IS the right move". No amount of waiting could clear it; the
    marker needed re-posting. The advice was not merely unhelpful, it was the exact
    opposite of the remedy.

    The partition this extends already documents THREE causes and calls itself total.
    It is total over markers that PARSE. The parse discards a block before the
    partition can see it, which is the same "decided from part of what had been read"
    shape the function's own comment says three prior findings had -- one level up,
    in the scan rather than in the branching.

    Every test here asserts what its branch advises AND that it does not carry the
    absent-branch advice, matching the convention above: a reworded regression that
    flips the guidance fails instead of sliding through on surviving substrings.
    """

    @staticmethod
    def _row(body, *, login="owner", author_association="OWNER", state=None):
        row = {"login": login, "author_association": author_association, "body": body}
        if state is not None:
            row["state"] = state
        return json.dumps(row)

    def _block_msg(self, monkeypatch, comments):
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", comments)
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "leaks")
        msg = _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")
        assert msg, "a head without an ACCEPTED marker must still block"
        return msg

    def test_an_abbreviated_head_is_reported_rather_than_treated_as_absent(self, monkeypatch):
        """The measured case: the operator did post one, and needs to know.

        Asserts the SUBSTANCE rather than a keyword. The first version matched on the
        word "unusable" and broke when the wording changed, even though the behaviour
        it cared about was intact -- a test that fails on a synonym is measuring the
        sentence, not the property. What has to hold is that the malformed value is
        shown and that the message actively denies the reading it used to give.
        """
        body = f"scan done.\n<!-- genesis-scheduled-review: head={SHORT_HEAD} kind=leaks -->"
        msg = self._block_msg(monkeypatch, self._row(body))
        assert f"'{SHORT_HEAD}'" in msg, "the offending value must be quoted back"
        assert "none of which counts at the current head" in msg, (
            "the report must state that a block was FOUND, which is the reading that "
            "used to be replaced by 'nothing was posted, wait'"
        )
        assert "may still be in flight" not in msg.lower(), (
            "an owner block exists, so the routine has evidently run; waiting cannot help"
        )

    def test_an_abbreviated_head_does_not_advise_waiting(self, monkeypatch):
        """The failure this exists to prevent -- and the one that actually happened."""
        body = f"scan done.\n<!-- genesis-scheduled-review: head={SHORT_HEAD} kind=leaks -->"
        msg = self._block_msg(monkeypatch, self._row(body)).lower()
        assert "may still be in flight" not in msg
        assert "waiting is the right move" not in msg

    def test_the_offending_value_is_quoted_so_the_typo_is_visible(self, monkeypatch):
        """Naming the cause without showing the value leaves the operator guessing
        which of several markers on a long thread is the broken one.

        Asserts the QUOTED form. A bare `SHORT_HEAD in msg` passes on the unfixed
        code, because the abbreviated head is by construction a PREFIX of the full
        head the closing grammar line already prints — so the substring is present
        whether or not this branch exists. Verify-RED caught it: this test went green
        before the fix was written.
        """
        body = f"scan done.\n<!-- genesis-scheduled-review: head={SHORT_HEAD} kind=leaks -->"
        msg = self._block_msg(monkeypatch, self._row(body))
        assert f"'{SHORT_HEAD}'" in msg, (
            "the malformed head must be quoted, so it is distinguishable from the "
            f"full head this message also prints: {msg}"
        )

    def test_a_marker_with_no_kind_is_reported_but_claims_no_kind(self, monkeypatch):
        """Reported, yes. Attributed to a required review, no.

        REWRITTEN, and worth saying why rather than quietly relaxing it. The first
        version asserted this block produced an "unusable" bullet AND suppressed the
        waiting guidance. Both were wrong, and review caught it: a block naming no
        kind cannot be credited to any particular review, so attaching it to `leaks`
        would have told the owner to post a leak marker on the strength of a block
        that never mentioned leaks -- and a hand-written marker satisfies the one
        gate this repository calls irreducible.

        The corrected behaviour reports the block as UNSCOPED and leaves every
        unidentified kind in its own true state. Here nothing else has been posted
        for `leaks`, so its true state is absent, and absent legitimately carries the
        in-flight guidance. The assertion that used to forbid that wording was
        encoding the bug.
        """
        body = f"scan done.\n<!-- genesis-scheduled-review: head={HEAD} -->"
        msg = self._block_msg(monkeypatch, self._row(body)).lower()
        assert "unscoped" in msg, "a block that names no kind must still be reported"
        assert "leaks — a marker block is present" not in msg, (
            "the kindless block was credited to leaks; following that guidance would "
            "satisfy the irreducible gate with no leak review behind it"
        )

    def test_a_marker_from_a_non_owner_is_reported_not_silently_dropped(self, monkeypatch):
        """Untrusted is the CORRECT verdict; silent is not. Someone posted a marker.

        Asserts the AUTHOR is named, not merely that the word "owner" appears: the
        block message's closing grammar line already says "by the repo OWNER" on every
        path, so `"owner" in msg` is true whether or not this branch exists. That is
        the assertion-also-true-on-the-success-path shape, and it was the first draft
        of this very test.

        REWRITTEN, and the reason is recorded rather than the line quietly relaxed.
        This test used to ALSO assert the waiting guidance was suppressed. That is the
        same defect a sibling test on this class had already been rewritten to remove:
        `test_a_marker_with_no_kind_is_reported_but_claims_no_kind` records that a block
        which cannot be credited "leaves every unidentified kind in its own true state",
        and that "absent legitimately carries the in-flight guidance. The assertion that
        used to forbid that wording was encoding the bug." An untrusted block is that
        same shape reached by a different route — it says who posted it, and nothing
        whatever about whether a review has run. On a PUBLIC repository the old
        behaviour meant one comment from any account deleted the guidance a freshly
        opened PR needs, which is the branch that exists to stop an operator reaching
        for an override on the irreducible gate.
        """
        body = f"scan done.\n<!-- genesis-scheduled-review: head={HEAD} kind=leaks -->"
        msg = self._block_msg(
            monkeypatch, self._row(body, login="drive-by", author_association="CONTRIBUTOR")
        )
        assert "drive-by" in msg, "the untrusted author must be named"
        assert "may still be in flight" in msg.lower(), (
            "an untrusted block consumed the kind's true state; nothing trustworthy has "
            f"run for leaks, so the waiting guidance must survive:\n{msg}"
        )

    def test_a_marker_in_a_dismissed_review_is_reported(self, monkeypatch):
        """Corrected twice, and the second correction is the honest one.

        First form: waiting guidance suppressed (the dismissed block "won"). Then
        rewritten to keep the guidance, on the argument that a dismissed review says
        nothing about whether anything ran. That argument was wrong: it is an OWNER-
        authored review at this head, which is direct evidence the owner's routine ran.
        The inventory lists the dismissed block; the in-flight note is keyed on owner
        evidence, so it is correctly absent here and correctly present for a stranger's
        block (test below).
        """
        body = f"scan done.\n<!-- genesis-scheduled-review: head={HEAD} kind=leaks -->"
        msg = self._block_msg(monkeypatch, self._row(body, state="DISMISSED")).lower()
        assert "dismissed" in msg
        assert "may still be in flight" not in msg, (
            "an OWNER's dismissed review is evidence the owner's routine already ran at "
            f"this head; waiting for it cannot help:\n{msg}"
        )

    def test_a_GENUINELY_absent_marker_still_advises_waiting(self, monkeypatch):
        """CONTROL. Without this, an implementation that called EVERYTHING unusable
        would pass every assertion above while destroying the one case where patience
        is the correct answer -- the exact regression the three-way partition was
        built to prevent."""
        msg = self._block_msg(monkeypatch, "").lower()
        assert "may still be in flight" in msg
        assert "waiting is the right move" in msg

    def test_prose_mentioning_the_marker_without_one_is_still_absent(self, monkeypatch):
        """CONTROL. A comment that TALKS about markers is not a marker; only a real
        block counts, or every review conversation would read as an unusable marker."""
        msg = self._block_msg(
            monkeypatch, self._row("I will post the genesis-scheduled-review marker shortly.")
        ).lower()
        assert "may still be in flight" in msg


BLOCKING_PROSE = "[P1] a real finding that has not been resolved"


class TestAnUnusableMarkerRemedyMatchesItsCause:
    """Naming a marker unusable is only half the job; the REMEDY has to match WHY.

    The first version of this feature had one remedy -- "re-post the marker" -- and
    applied it to every cause. That is right for exactly one of them. For the rest it
    tells the owner to hand-write an attestation for a review that never ran, never
    finished, or ran and BLOCKED. Following it would satisfy the gate the repository
    calls irreducible without the scan behind it ever happening. A diagnostic that
    makes the message more informative and the outcome less safe is a bad trade, and
    that is what review found here.

    So the causes split in two:
      GRAMMAR ONLY -- the owner posted it, the body is clean, only the marker text is
        malformed. A trusted clean review really did happen. Re-post it.
      NO TRUSTED CLEAN REVIEW -- posted by someone else, carried by a dismissed
        review, or carrying a blocking finding. Minting a marker here would be a lie.
        Run the review; resolve the finding.
    """

    BLOCKING = BLOCKING_PROSE

    @staticmethod
    def _row(body, *, login="owner", author_association="OWNER", state=None):
        row = {"login": login, "author_association": author_association, "body": body}
        if state is not None:
            row["state"] = state
        return json.dumps(row)

    @staticmethod
    def _marker(head=HEAD, kind="leaks"):
        parts = [f"head={head}"] if head else []
        if kind:
            parts.append(f"kind={kind}")
        return "<!-- genesis-scheduled-review: " + " ".join(parts) + " -->"

    def _msg(self, monkeypatch, comments, required="leaks", repo="acme/pub"):
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", comments)
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", required)
        msg = _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo=repo)
        assert msg, "a head without an ACCEPTED marker must still block"
        return msg

    # ---- P1: a block naming no kind must not claim a required one ----------
    def test_a_kindless_block_does_not_claim_the_irreducible_kind(self, monkeypatch):
        """Otherwise the guidance invites minting a leak attestation that nothing ran."""
        body = "scan done.\n" + self._marker(kind=None)
        msg = self._msg(monkeypatch, self._row(body), required="code-review,leaks")
        assert "unscoped" in msg.lower() or "does not say which" in msg.lower(), msg
        assert "leaks — a marker block IS present" not in msg, (
            "a block naming no kind was attributed to leaks; following that guidance "
            "would satisfy the irreducible gate with no leak review"
        )

    # ---- P1: untrusted causes must not be answered with "re-post it" -------
    def test_a_non_owner_marker_is_not_answered_by_minting_one(self, monkeypatch):
        body = "scan done.\n" + self._marker()
        msg = self._msg(
            monkeypatch, self._row(body, login="drive-by", author_association="CONTRIBUTOR")
        )
        assert "drive-by" in msg, "the untrusted author must be named"
        assert "--emit-marker" not in msg, (
            "the emitter mints an attestation and performs no review; offering it here "
            "turns an untrusted marker into a passing gate"
        )

    def test_a_dismissed_review_is_not_answered_by_minting_one(self, monkeypatch):
        body = "scan done.\n" + self._marker()
        msg = self._msg(monkeypatch, self._row(body, state="DISMISSED"))
        assert "dismissed" in msg.lower()
        assert "--emit-marker" not in msg

    def test_a_blocking_body_with_a_malformed_marker_keeps_its_verdict(self, monkeypatch):
        """The sharpest case: a real finding, plus a typo, must not become a typo."""
        body = f"{self.BLOCKING}\n" + self._marker(head=HEAD[:8])
        msg = self._msg(monkeypatch, self._row(body))
        assert "blocking" in msg.lower(), msg
        assert "--emit-marker" not in msg, (
            "re-posting a clean marker over an unresolved blocking finding makes the "
            "gate pass while the finding stands"
        )

    # ---- THE INVARIANT that makes this whole class impossible --------------
    @pytest.mark.parametrize(
        "label,row_kwargs,body_prefix,marker_kwargs",
        [
            ("grammar only", {}, "scan done, all clean.", {"head": HEAD[:8]}),
            ("non-owner", {"login": "drive-by", "author_association": "CONTRIBUTOR"}, "x.", {}),
            ("dismissed", {"state": "DISMISSED"}, "x.", {}),
            ("blocking body", {}, BLOCKING_PROSE, {"head": HEAD[:8]}),
            ("no kind", {}, "x.", {"kind": None}),
            ("wrong kind", {}, "x.", {"head": HEAD[:8], "kind": "code-review"}),
        ],
    )
    def test_no_cause_is_ever_answered_with_a_way_to_mint_a_marker(
        self, monkeypatch, label, row_kwargs, body_prefix, marker_kwargs
    ):
        """The structural guarantee, replacing a remedy that kept being wrong.

        Six of six blocker-class findings on the first version of this feature were
        in its ADVICE and none in its observations, because the advice had to know
        whether a trusted clean review existed and each branch decided that for
        itself. A gate that explains how to write its own attestation is a gate
        explaining how to get past itself.

        So the message reports and prescribes nothing, and this asserts it for EVERY
        cause rather than for the ones review happened to reach. A future branch that
        reintroduces advice fails here instead of shipping and being found later.
        """
        body = body_prefix + "\n" + self._marker(**marker_kwargs)
        msg = self._msg(monkeypatch, self._row(body, **row_kwargs))
        assert "--emit-marker" not in msg, f"{label}: offered a way to mint a marker"
        assert "re-post" not in msg.lower(), f"{label}: prescribed a remedy"

    def test_the_report_still_names_the_cause(self, monkeypatch):
        """CONTROL. Removing the advice must not remove the diagnosis -- an
        implementation that said nothing at all would satisfy every assertion above
        while restoring the exact silence this change was written to end."""
        body = "scan done, all clean.\n" + self._marker(head=HEAD[:8])
        msg = self._msg(monkeypatch, self._row(body))
        assert f"'{HEAD[:8]}'" in msg, "the malformed value must still be quoted"
        assert "could not be counted" in msg
        assert "none of which counts at the current head" in msg

    def test_a_block_naming_an_unrequired_kind_is_still_reported(self, monkeypatch):
        """A near-miss kind is the case that narrowing to named kinds broke: it
        matched the grammar, so it was not malformed-by-field, but it belonged to no
        required review, so it fell out of every group and the message went back to
        claiming nothing had been posted."""
        body = "scan done.\n" + self._marker(head=HEAD[:8], kind="code-review")
        msg = self._msg(monkeypatch, self._row(body), required="leaks")
        assert "unscoped" in msg.lower(), msg
        assert f"'{HEAD[:8]}'" in msg

    # ---- P2: a pending review is in-flight, not unusable -------------------
    def test_a_pending_review_at_head_is_listed_as_unpublished(self, monkeypatch):
        """REWRITTEN from "keeps the waiting guidance". A draft naming the current
        head used to be dropped so the generic in-flight note would stand in for it;
        the inventory lists it as its own row, which says the same thing precisely:
        unpublished, current head, counts once submitted."""
        body = "scan done.\n" + self._marker()
        msg = self._msg(monkeypatch, self._row(body, state="PENDING"))
        assert "PENDING (unpublished) review naming the current head" in msg, msg

    # ---- a current malformed marker is shown BESIDE stale history ----------
    def test_a_current_unusable_marker_is_shown_beside_stale_history(self, monkeypatch):
        """RENAMED from "...outranks a stale valid one". It never asserted the stale
        row was hidden, only that the current typo was shown -- and under the inventory
        both are shown. The name said precedence; the assertion never did."""
        rows = "\n".join(
            [
                self._row("older run.\n" + self._marker(head=OTHER_HEAD)),
                self._row("current run.\n" + self._marker(head=HEAD[:8])),
            ]
        )
        msg = self._msg(monkeypatch, rows)
        assert f"'{HEAD[:8]}'" in msg, "the malformed CURRENT marker must be shown"
        assert OTHER_HEAD[:12] in msg, "and so must the stale history -- nothing outranks"

    # ---- P2: show the detail that belongs to THIS group -------------------
    def test_the_detail_shown_belongs_to_the_kind_being_explained(self, monkeypatch):
        """Truncating the global list first hid the one value that mattered."""
        noise = [
            self._row(f"run {i}.\n" + self._marker(head=HEAD[:8], kind="code-review"))
            for i in range(4)
        ]
        rows = "\n".join([*noise, self._row("leaks run.\n" + self._marker(head=HEAD[:6]))])
        msg = self._msg(monkeypatch, rows, required="leaks")
        assert f"'{HEAD[:6]}'" in msg, (
            "the leaks bullet showed four unrelated code-review reasons and hid its own"
        )


class TestEveryMarkerFormIsAccountedFor:
    """The remaining shape of this feature's defects: a marker form the
    classification does not cover falls back to "nothing was posted".

    That fallback is the original bug. Each case below is a form that reached it
    by a different route -- one parsed too well to be called malformed, one was
    dropped for its review state, one was filtered against the wrong set. They are
    fixed together because they are one class; fixing the reported subset would
    leave the unreported one for the next round.
    """

    @staticmethod
    def _row(body, **kw):
        row = {"login": "owner", "author_association": "OWNER", "body": body}
        row.update(kw)
        return json.dumps(row)

    @staticmethod
    def _marker(head=HEAD, kind="leaks"):
        parts = ([f"head={head}"] if head else []) + ([f"kind={kind}"] if kind else [])
        return "<!-- genesis-scheduled-review: " + " ".join(parts) + " -->"

    def _msg(self, monkeypatch, comments, required="leaks"):
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", comments)
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", required)
        return _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")

    def test_a_wellformed_marker_naming_an_unknown_kind_is_visible(self, monkeypatch):
        """Parses perfectly, names a review nothing knows about (a singular typo).

        It went into the accepted map under its own key, matched no required kind,
        and so vanished -- the message said no marker existed at any head while the
        block sat plainly in the thread. Being well-formed was exactly why it hid.
        """
        msg = self._msg(monkeypatch, self._row("scan done.\n" + self._marker(kind="leak")))
        assert msg, "an unknown kind satisfies nothing, so this must still block"
        assert "leak" in msg and "not a known scheduled" in msg, msg

    def test_an_unknown_kind_is_NOT_attributed_to_the_kind_it_resembles(self, monkeypatch):
        """DELIBERATELY not taken from review, and the reason is a prior finding.

        Review asked that a near-miss kind also stop the required review from
        advising that waiting may help. Doing that means deciding the block was
        MEANT for that review -- and an earlier finding on this same PR was that an
        unattributable block must never claim a required kind, precisely because a
        claimed kind steers the reader toward posting a marker for a review nothing
        vouches for. The two cannot both be satisfied without guessing intent.

        Resolved toward the side with the security consequence: report both facts
        and let the reader join them. The message says a block naming an unknown
        review exists, and separately that the required review has no marker. Both
        are true; neither asserts what the author meant.
        """
        msg = self._msg(monkeypatch, self._row("scan done.\n" + self._marker(kind="leak")))
        assert "not a known scheduled" in msg, "the block must be visible"
        assert "leaks — no marker block found" in msg, (
            "attributing the near-miss to the required kind is the guess a prior "
            "finding forbade; the required kind's own state is that none was posted"
        )

    def test_a_duplicate_for_an_ALREADY_SATISFIED_kind_is_not_called_unscoped(
        self, monkeypatch
    ):
        """The bullet contradicted the summary line two rows above it."""
        rows = "\n".join(
            [
                self._row("clean run.\n" + self._marker(kind="leaks")),
                self._row("dup.\n" + self._marker(head=HEAD[:8], kind="leaks")),
            ]
        )
        msg = self._msg(monkeypatch, rows, required="code-review,leaks")
        assert msg, "code-review is still missing, so this blocks"
        assert "present: leaks" in msg, "leaks IS satisfied and the summary says so"
        assert "unscoped" not in msg.lower(), (
            "a duplicate for a SATISFIED required kind was reported as naming no "
            f"required kind, contradicting the summary line above it:\n{msg}"
        )

    def test_a_pending_review_with_a_STALE_marker_does_not_advise_waiting(
        self, monkeypatch
    ):
        """Submitting a draft cannot help when its marker names an older commit."""
        msg = self._msg(
            monkeypatch,
            self._row("draft.\n" + self._marker(head=OTHER_HEAD), state="PENDING"),
        )
        assert msg
        assert "may still be in flight" not in msg.lower(), (
            "waiting was advised for a draft that is stale whenever it is submitted"
        )

    def test_a_pending_review_at_the_CURRENT_head_is_listed_as_unpublished(self, monkeypatch):
        """Was the CONTROL asserting the generic in-flight note. REWRITTEN: this was
        the last silent drop in the scan -- a current-head draft was `continue`d so the
        generic note would cover it, which contradicts an inventory that promises to
        list every block. The row is the precise form of "in flight": it names the
        draft, the head, and that submission is what makes it count. The stale-draft
        test above still discriminates: its row says submission would NOT help."""
        msg = self._msg(
            monkeypatch, self._row("draft.\n" + self._marker(head=HEAD), state="PENDING")
        )
        assert msg
        assert "PENDING (unpublished) review naming the current head" in msg, msg
        assert "once submitted it is read like any other block" in msg, msg
        assert "may still be in flight" not in msg.lower(), (
            "the generic note beside the specific row would say the same thing twice"
        )

    def test_a_block_with_BOTH_fields_malformed_reports_both(self, monkeypatch):
        """`head=abc kind=leaks/failed`: the old if/else reported the short sha only and
        hid the suffix saying the run FAILED -- one repair cycle per hidden field."""
        msg = self._msg(
            monkeypatch,
            self._row("run failed.\n<!-- genesis-scheduled-review: head=abc kind=leaks/failed -->"),
        )
        assert msg
        assert "'abc'" in msg and "'leaks/failed'" in msg, msg
        assert "did NOT complete cleanly" in msg, msg


class TestTheReportReadsEveryFieldPermissively:
    """One class behind several defects: the GATE's strict grammar was reused to answer
    a REPORTING question, so a REFUSED value became indistinguishable from an ABSENT
    one and the message asserted things that were false.

    This change had already solved that once -- on the head axis, with a permissive
    second read used only for the message. It did not generalise, and every remaining
    field reproduced the defect on its own axis:

      * head  -- had the permissive read, but still called a wrong-CASE 40-hex head
                 "not a full 40-hex commit sha", sending the operator to recount 40
                 characters and find nothing wrong
      * kind  -- had no permissive read, so `kind=leaks/failed` (a form the strict
                 grammar's own comment anticipates) was reported as carrying no kind
                 field at all, and the kind then fell to "waiting IS the right move"
                 for a routine that had run AND FAILED
      * trust -- the fixable/not-fixable flag was computed at every call site and then
                 discarded, so an UNTRUSTED block could suppress trusted evidence

    Each test asserts the honest statement AND denies the false one, so a regression
    that drops the permissive read fails instead of sliding through on a substring.
    """

    @staticmethod
    def _row(body, *, login="owner", author_association="OWNER", **kw):
        row = {"login": login, "author_association": author_association, "body": body}
        row.update(kw)
        return json.dumps(row)

    @staticmethod
    def _marker(head=HEAD, kind="leaks"):
        parts = ([f"head={head}"] if head else []) + ([f"kind={kind}"] if kind else [])
        return "<!-- genesis-scheduled-review: " + " ".join(parts) + " -->"

    def _msg(self, monkeypatch, comments, required="leaks"):
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", comments)
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", required)
        return _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")

    def test_a_status_suffixed_kind_is_not_reported_as_a_missing_field(self, monkeypatch):
        """`kind=leaks/failed` is refused by the gate -- correctly -- but it IS there.

        The strict expression's own comment names this exact form as the thing the
        whitespace terminator exists to reject, so it is an anticipated shape rather
        than a hypothetical. Reporting it as "carries no kind= field" is simply false.
        """
        msg = self._msg(monkeypatch, self._row("run failed.\n" + self._marker(kind="leaks/failed")))
        assert msg, "a failed run satisfies nothing, so this must still block"
        assert "'leaks/failed'" in msg, f"the value the operator wrote must be quoted:\n{msg}"
        assert "carries no kind= field" not in msg, (
            f"the block DOES carry a kind= field; the gate merely refused it:\n{msg}"
        )

    def test_a_status_suffixed_value_says_what_the_suffix_MEANS(self, monkeypatch):
        """Naming only the grammar problem is an instruction to strip the suffix.

        A reader who follows "its kind is not a bare review name" produces a
        well-formed marker attesting to a run that reported its own FAILURE, on the
        gate this repository calls irreducible. The message must therefore say what
        the suffix means, not merely that the grammar refused it.

        Both fields reach this branch by different routes and both are asserted here:
        fixing only the one a review happened to name is how this class recurs.
        """
        for value, mk in (
            ("leaks/failed", lambda: self._marker(kind="leaks/failed")),
            (f"{HEAD}/failed", lambda: self._marker(head=f"{HEAD}/failed")),
        ):
            msg = self._msg(monkeypatch, self._row("run failed.\n" + mk()))
            assert msg, value
            assert f"'{value}'" in msg, f"{value}: the operator's own text must be quoted:\n{msg}"
            assert "did NOT complete cleanly" in msg, (
                f"{value}: the message named the grammar problem only, which reads as "
                f"'strip the suffix' and yields a marker vouching for a failed run:\n{msg}"
            )

    def test_a_refused_value_is_credited_to_no_review(self, monkeypatch):
        """CONTROL for the reverted attribution, and it is the load-bearing one.

        An earlier revision read `leaks/failed` as naming `leaks`, on the reasoning
        that it says so unambiguously. That let a FAILED run occupy the kind and, via
        the state rule, suppress the bullet reporting a real review on an earlier
        commit — so a failed run outranked history that an untrusted stranger's marker
        was not allowed to outrank. A value the strict grammar refuses now names
        nothing, and `leaks` keeps its own true state.

        Asserted for a value whose stem IS a real kind (`leaks/failed`), because that
        is the one an attributing implementation would credit; a near-miss stem would
        pass on both implementations and prove nothing.
        """
        msg = self._msg(monkeypatch, self._row("run failed.\n" + self._marker(kind="leaks/failed")))
        assert msg
        assert "'leaks/failed'" in msg, f"the block must still be REPORTED:\n{msg}"
        assert "may still be in flight" in msg.lower(), (
            "a refused value was credited to the review it names; leaks has no usable "
            f"marker and must keep its own state:\n{msg}"
        )

    def test_an_uppercase_head_is_reported_as_case_not_as_length(self, monkeypatch):
        """40 uppercase hex IS full length. Telling that operator it is not sends them
        to count characters, find 40, and learn nothing about the real cause."""
        msg = self._msg(monkeypatch, self._row("scan done.\n" + self._marker(head=HEAD.upper())))
        assert msg
        assert "lowercase" in msg, f"the real cause is case, and must be named:\n{msg}"
        assert "is not a full 40-hex commit sha" not in msg, (
            f"the head IS full length; only its case is wrong:\n{msg}"
        )

    def test_an_untrusted_block_does_not_erase_the_older_head_evidence(self, monkeypatch):
        """On a PUBLIC repo any account can post a marker naming the current head.

        Doing so used to delete the "a routine ran on an older commit" bullet -- the
        one fact telling the operator a review had ever happened. The verdict never
        moved (an untrusted marker satisfies nothing); the report an operator acts on
        did, and it lost the actionable half.
        """
        rows = "\n".join(
            [
                self._row("older run.\n" + self._marker(head=OTHER_HEAD)),
                self._row(
                    "scan done.\n" + self._marker(head=HEAD),
                    login="drive-by",
                    author_association="CONTRIBUTOR",
                ),
            ]
        )
        msg = self._msg(monkeypatch, rows)
        assert msg
        assert "drive-by" in msg, f"the untrusted block must still be reported:\n{msg}"
        assert OTHER_HEAD[:12] in msg, (
            f"an untrusted commenter erased the trusted older-head evidence:\n{msg}"
        )

    def test_a_dismissed_review_does_not_erase_the_older_head_evidence(self, monkeypatch):
        """Same mechanism, reached by review STATE rather than by authorship -- which
        is why it is fixed on the flag both share rather than at either site."""
        rows = "\n".join(
            [
                self._row("older run.\n" + self._marker(head=OTHER_HEAD)),
                self._row("scan done.\n" + self._marker(head=HEAD), state="DISMISSED"),
            ]
        )
        msg = self._msg(monkeypatch, rows)
        assert msg
        assert "dismissed" in msg.lower(), f"the dismissed block must be reported:\n{msg}"
        assert OTHER_HEAD[:12] in msg, (
            f"a dismissed review erased the trusted older-head evidence:\n{msg}"
        )

    def test_an_untrusted_block_does_not_erase_the_ABSENT_guidance(self, monkeypatch):
        """The same rule on its OTHER consuming branch, and the reason it is one rule.

        Gating only the history branch left this one open: on a freshly-opened PR where
        the routine genuinely has not run, a single comment from any account deleted
        "nothing has run yet, waiting is right" — the branch whose whole purpose is to
        stop an operator reaching for an override on the irreducible gate. On a public
        repository the trigger is one comment from anybody.
        """
        msg = self._msg(
            monkeypatch,
            self._row(
                self._marker(head=HEAD), login="drive-by", author_association="CONTRIBUTOR"
            ),
        )
        assert msg
        assert "drive-by" in msg, f"the untrusted block must still be reported:\n{msg}"
        assert "may still be in flight" in msg.lower(), (
            "an untrusted comment consumed the kind's true state and deleted the "
            f"waiting guidance a fresh PR needs:\n{msg}"
        )

    def test_every_block_for_a_kind_is_listed_and_nothing_outranks_anything(self, monkeypatch):
        """The inventory invariant, on the scenario that ended the previous design.

        A REFUSED marker at an older head — its body carries a real [P1] — and the
        owner's truncated marker at the current head. Under the partition, the typo
        "won" and the [P1] vanished; the message then told the operator their sha was
        short, and the layout handed them the full current head to fix it with. Doing
        so mints a clean attestation at head while a leak finding stands unresolved on
        the older commit. Both rows must be present; the count must say two.
        """
        rows = "\n".join(
            [
                self._row("[P1] a real leak finding\n" + self._marker(head=OTHER_HEAD)),
                self._row("scan done.\n" + self._marker(head=HEAD[:8])),
            ]
        )
        msg = self._msg(monkeypatch, rows)
        assert msg
        assert "leaks — 2 marker block(s) found" in msg, msg
        assert f"REFUSED at a DIFFERENT head ({OTHER_HEAD[:12]})" in msg, msg
        assert f"'{HEAD[:8]}'" in msg, msg

    def test_an_empty_field_value_is_not_reported_as_a_missing_field(self, monkeypatch):
        """`head=` with no value is the single most likely producer fault (an
        uninterpolated variable). It was reported as "carries no head= field", which
        is the REFUSED-vs-ABSENT confusion this file forbids, at the empty-value cell."""
        for block, needle in (
            ("<!-- genesis-scheduled-review: head= kind=leaks -->", "head= field is present but EMPTY"),
            (f"<!-- genesis-scheduled-review: head={HEAD} kind= -->", "kind= field is present but EMPTY"),
        ):
            msg = self._msg(monkeypatch, self._row("scan.\n" + block))
            assert msg, block
            assert needle in msg, f"{block}\n{msg}"
            assert "carries no" not in msg, f"the field IS present:\n{msg}"
class TestNamingProblemsKeepTheBodyVerdict:
    @staticmethod
    def _row(body):
        return json.dumps({"login": "owner", "author_association": "OWNER", "body": body})

    def _msg(self, monkeypatch, body):
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", self._row(body))
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "leaks")
        return _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub") or ""

    def test_an_unknown_kind_with_a_blocking_body_says_so(self, monkeypatch):
        """Reporting only the typo invites a corrected marker over an unresolved finding
        -- the malformed-field branch already keeps the verdict; this branch did not."""
        msg = self._msg(monkeypatch, f"[P1] a finding\n<!-- genesis-scheduled-review: head={HEAD} kind=leak -->")
        assert "not a known scheduled" in msg, msg
        assert "carrying a blocking finding" in msg, msg

    def test_an_uppercase_kind_is_diagnosed_as_case(self, monkeypatch):
        msg = self._msg(monkeypatch, f"scan.\n<!-- genesis-scheduled-review: head={HEAD} kind=LEAKS -->")
        assert "'LEAKS'" in msg and "not lowercase" in msg, msg
        assert "not a bare review name" not in msg, msg
