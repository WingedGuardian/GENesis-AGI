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

    def test_behind_is_inline(self, monkeypatch):
        assert self._lvl(monkeypatch, _compare_json("behind")) == "inline"

    def test_ahead_substantial(self, monkeypatch):
        assert self._lvl(monkeypatch, _compare_json("ahead", [_code_file()])) == "substantial"

    def test_ahead_trivial_is_inline(self, monkeypatch):
        assert self._lvl(monkeypatch, _compare_json("ahead", [_code_file(additions=3)])) == "inline"

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
            "_TEST_GH_CI_ROLLUP", json.dumps([{"name": "t", "conclusion": "SUCCESS"}])
        )
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
            "_TEST_GH_CI_ROLLUP", json.dumps([{"name": "t", "conclusion": "SUCCESS"}])
        )
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
        assert "unresolved review findings" in capsys.readouterr().err


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
            _mod, "_check_pr_review_findings", lambda n, repo=None, strict=False: (False, "")
        )
        monkeypatch.setattr(
            _mod, "_check_inline_review_findings", lambda n, repo=None, strict=False: (False, "")
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
