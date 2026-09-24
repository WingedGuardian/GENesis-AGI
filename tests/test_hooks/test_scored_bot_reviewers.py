"""Devin's inline findings are SCORED, and a Devin/CodeRabbit review at head can
stand in for Codex behind an owner ask.

Owner rulings, 2026-09-24: Devin findings score on equal footing with Codex,
severity-mapped (a severe bug or critical security finding is always-fix; a
non-severe bug or security warning weighs like a Codex P2; an informational
analysis is surfaced, not scored). CodeRabbit scoring is UNCHANGED — Minor stays
surface-only. A finding clears by a maintainer reply, or by the SAME bot that
raised it replying `✅ **Resolved**` in its thread — never by another reviewer,
never by free prose. With the owner asked and approving per PR, a Devin or
CodeRabbit review at the exact head satisfies the Codex freshness check.

Why the comment shape below is trusted: MEASURED 2026-09-24 over every Devin
comment on the 33 open non-draft PRs — 166 comments, every one opening with
`<!-- devin-review-comment {json} -->` and then one of five markers, and the
marker meanings READ from Devin's own documentation (red = severe bug / critical
security, orange = non-severe bug / security warning, gray = informational).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py"

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
STALE = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
DEVIN = "devin-ai-integration[bot]"
CODERABBIT = "coderabbitai[bot]"


@pytest.fixture(autouse=True)
def _hermetic_pr_files(monkeypatch):
    """In-diff set pinned to src/benign.py (a STANDARD-lane path: threshold 2.0)."""
    monkeypatch.setenv(
        "_TEST_GH_PR_FILES", '{"filename": "src/benign.py", "previous_filename": null}'
    )


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _devin_body(
    marker: str,
    title: str,
    *,
    fid: str = "BUG_pr-review-job-aaa_0001",
    kind: str = "bug",
    path: str = "src/benign.py",
    tail: str = "",
) -> str:
    """The live shape, verbatim structure: metadata comment, blank line, marker + bold title."""
    meta = {
        "id": fid,
        "file_path": path,
        "start_line": 10,
        "end_line": 12,
        "side": "RIGHT",
        "based_on_repo_rules": False,
        "kind": kind,
    }
    return (
        f"<!-- devin-review-comment {json.dumps(meta)} -->\n\n"
        f"{marker} **{title}**\n\nWhy it matters, in prose.{tail}\n"
    )


def _c(cid, body, *, login=DEVIN, utype="Bot", path="src/benign.py", reply_to=None, assoc="NONE"):
    d = {
        "id": cid,
        "reply_to": reply_to,
        "login": login,
        "type": utype,
        "assoc": assoc,
        "body": body,
    }
    if path is not None:
        d["path"] = path
    return d


def _mock(guard, comments):
    return patch.object(
        guard.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="\n".join(json.dumps(c) for c in comments), stderr=""
        ),
    )


def _scan(guard, comments, **kw):
    with _mock(guard, comments):
        return guard._check_inline_review_findings("100", **kw)


# ── severity mapping ────────────────────────────────────────────────


class TestDevinSeverity:
    def test_severe_bug_is_always_fix(self, guard):
        """RED before: every Devin comment fell to `unmatched_bot`, surfaced, never scored."""
        block, msg = _scan(
            guard, [_c(1, _devin_body("🔴", "Wrapped shell removals remain invisible"))]
        )
        assert block, "a Devin severe bug must hit the always-fix floor in every lane"
        assert "always-fix floor" in msg
        assert "Wrapped shell removals remain invisible" in msg

    def test_critical_security_is_always_fix(self, guard):
        block, msg = _scan(
            guard,
            [_c(1, _devin_body("🟥", "Token written to a world-readable file", kind="security"))],
        )
        assert block and "always-fix floor" in msg

    def test_one_non_severe_bug_scores_but_does_not_block_a_standard_change(self, guard, capsys):
        block, msg = _scan(guard, [_c(1, _devin_body("🟡", "Argument text becomes a launcher"))])
        assert not block, msg
        err = capsys.readouterr().err
        # It must be SCORED (the score NOTE names it), not merely listed as unrecognised.
        assert "review score 0.5" in err, err
        assert "unrecognised" not in err

    def test_four_non_severe_bugs_block_a_standard_change(self, guard):
        comments = [
            _c(i, _devin_body("🟡", f"finding {i}", fid=f"BUG_pr-review-job-aaa_000{i}"))
            for i in range(1, 5)
        ]
        block, msg = _scan(guard, comments)
        assert block, "4 x 0.5 = 2.0 must reach the STANDARD threshold"
        assert "review score 2.0" in msg

    def test_three_non_severe_bugs_do_not_block_a_standard_change(self, guard):
        comments = [
            _c(i, _devin_body("🟡", f"finding {i}", fid=f"BUG_pr-review-job-aaa_000{i}"))
            for i in range(1, 4)
        ]
        block, msg = _scan(guard, comments)
        assert not block, msg

    def test_security_warning_weighs_like_a_non_severe_bug(self, guard, capsys):
        """Owner 2026-09-24: 🟨 security-warning scores 0.5, NOT the floor."""
        block, msg = _scan(guard, [_c(1, _devin_body("🟨", "Potential weakness", kind="security"))])
        assert not block, msg
        assert "review score 0.5" in capsys.readouterr().err

    def test_informational_analysis_is_surfaced_not_scored(self, guard, capsys):
        block, msg = _scan(
            guard, [_c(1, _devin_body("🔍", "How the lease is renewed", kind="analysis"))]
        )
        assert not block, msg
        err = capsys.readouterr().err
        assert "How the lease is renewed" in err
        # The score NOTE prints only when the score is non-zero ("review score 0.5
        # is under…"); the analysis NOTE itself says "NOT counted toward the review
        # score", so match the scored form, not the phrase.
        assert "review score 0" not in err and "review score 1" not in err

    def test_an_unknown_marker_is_surfaced_as_drift_never_scored(self, guard, capsys):
        block, msg = _scan(guard, [_c(1, _devin_body("🟣", "Some new category"))])
        assert not block, msg
        err = capsys.readouterr().err
        assert "Some new category" in err
        assert "format" in err.lower()

    def test_malformed_metadata_is_surfaced_never_scored(self, guard, capsys):
        body = "<!-- devin-review-comment {not json} -->\n\n🔴 **Broken metadata**\n"
        block, msg = _scan(guard, [_c(1, body)])
        assert not block, msg
        assert "Broken metadata" in capsys.readouterr().err

    def test_a_marker_missing_its_metadata_tag_is_not_scored(self, guard, capsys):
        """No leading tag = not the recognised format, even from Devin's login."""
        block, msg = _scan(guard, [_c(1, "🔴 **No tag at all**\n\nprose")])
        assert not block, msg
        assert "No tag at all" in capsys.readouterr().err


# ── authority: who can make a finding score ────────────────────────


class TestDevinAuthority:
    def test_a_human_posting_devins_exact_body_does_not_score(self, guard, capsys):
        body = _devin_body("🔴", "Forged finding")
        block, msg = _scan(guard, [_c(1, body, login="someone", utype="User")])
        assert not block, f"a non-Bot account made a finding score: {msg}"

    def test_devins_login_without_bot_type_is_not_believed(self, guard):
        """Both conditions are required: the login AND GitHub's own Bot type.

        GitHub reserves the ``[bot]`` suffix, so this record should never occur —
        which is exactly why the check exists: a malformed or spoofed record that
        claims the login without the type must not be parsed for severity.
        """
        body = _devin_body("🔴", "Login without type")
        block, msg = _scan(guard, [_c(1, body, utype="User")])
        assert not block, f"a non-Bot record with Devin's login scored: {msg}"

    def test_another_bot_posting_devins_tag_is_not_scored_as_devin(self, guard, capsys):
        body = _devin_body("🔴", "Tag from another bot")
        block, msg = _scan(guard, [_c(1, body, login="some-other-bot[bot]")])
        assert not block, msg
        assert "unrecognised" in capsys.readouterr().err

    def test_a_devin_body_quoting_a_p1_badge_scores_once_as_devin(self, guard):
        """GLM SF3: the Devin branch must be exclusive and run BEFORE the badge match."""
        body = _devin_body("🟡", "Quotes guard output", tail="\n\n`![P1 Badge](x)` was printed")
        block, msg = _scan(guard, [_c(1, body)])
        assert not block, f"a 0.5 Devin finding was double-counted as a P1: {msg}"


# ── dedup and clearing ─────────────────────────────────────────────


class TestDevinDedupAndClearing:
    def test_duplicate_posts_of_one_finding_count_once(self, guard):
        body = _devin_body("🔴", "Posted twice", fid="BUG_pr-review-job-aaa_0001")
        block, msg = _scan(guard, [_c(1, body), _c(2, body)])
        assert block
        assert "1 unresolved" in msg or "+ 1 " in msg or "1 Devin" in msg, msg
        assert msg.count("Posted twice") == 1, msg

    def test_a_maintainer_reply_clears_the_finding(self, guard):
        comments = [
            _c(1, _devin_body("🔴", "Answered by a maintainer")),
            _c(
                9,
                "Verified and fixed at abc1234.",
                login="owner",
                utype="User",
                reply_to=1,
                assoc="OWNER",
            ),
        ]
        block, msg = _scan(guard, comments)
        assert not block, msg

    def test_a_maintainer_reply_to_one_copy_clears_the_duplicate_too(self, guard):
        body = _devin_body("🔴", "Reply landed on copy two")
        comments = [
            _c(1, body),
            _c(2, body),
            _c(9, "Fixed.", login="owner", utype="User", reply_to=2, assoc="OWNER"),
        ]
        block, msg = _scan(guard, comments)
        assert not block, f"answering one copy left its twin blocking: {msg}"

    def test_devins_own_resolved_reply_clears_its_finding(self, guard):
        """Owner 2026-09-24: a reviewer may withdraw its own finding."""
        comments = [
            _c(1, _devin_body("🔴", "Self-withdrawn")),
            _c(9, "✅ **Resolved**: The path now checks the lock first.", reply_to=1),
        ]
        block, msg = _scan(guard, comments)
        assert not block, msg

    def test_devins_resolved_reply_on_a_duplicate_clears_the_group(self, guard):
        body = _devin_body("🔴", "Resolved on the other copy")
        comments = [
            _c(1, body),
            _c(2, body),
            _c(9, "✅ **Resolved**: fixed.", reply_to=2),
        ]
        block, msg = _scan(guard, comments)
        assert not block, msg

    def test_another_bots_resolved_reply_does_not_clear(self, guard):
        comments = [
            _c(1, _devin_body("🔴", "Not yours to clear")),
            _c(9, "✅ **Resolved**: looks fixed.", login=CODERABBIT, reply_to=1),
        ]
        block, msg = _scan(guard, comments)
        assert block, "a DIFFERENT bot cleared Devin's finding"

    def test_devins_prose_reply_does_not_clear(self, guard):
        """MEASURED: Devin also posts builder-style prose ('Fixed in …'). That is a claim, not a withdrawal."""
        comments = [
            _c(1, _devin_body("🔴", "Prose is not a verdict")),
            _c(9, "Fixed in https://github.com/example/example/pull/1.", reply_to=1),
        ]
        block, msg = _scan(guard, comments)
        assert block, "free prose from the bot cleared its finding"

    def test_a_human_non_maintainer_reply_does_not_clear(self, guard):
        comments = [
            _c(1, _devin_body("🔴", "Drive-by reply")),
            _c(9, "✅ **Resolved**", login="stranger", utype="User", reply_to=1, assoc="NONE"),
        ]
        block, msg = _scan(guard, comments)
        assert block

    def test_resolved_marker_must_lead_the_reply(self, guard):
        comments = [
            _c(1, _devin_body("🔴", "Marker buried")),
            _c(9, "Not quite. ✅ **Resolved** would need the lock check.", reply_to=1),
        ]
        block, msg = _scan(guard, comments)
        assert block


# ── scope exclusions and waivers stay uniform ──────────────────────


class TestDevinScopeAndWaivers:
    def test_off_diff_devin_finding_is_surfaced_not_scored(self, guard, capsys, offdiff_lock):
        offdiff_lock.expected()
        block, msg = _scan(
            guard,
            [_c(1, _devin_body("🔴", "On base content", path="src/other.py"), path="src/other.py")],
        )
        assert not block, msg
        err = capsys.readouterr().err
        assert "[off-diff Devin" in err and "src/other.py" in err

    def test_doc_path_devin_finding_is_surfaced_not_scored(self, guard, capsys, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", '{"filename": "docs/guide.md", "previous_filename": null}'
        )
        block, msg = _scan(
            guard,
            [_c(1, _devin_body("🔴", "Prose nit", path="docs/guide.md"), path="docs/guide.md")],
        )
        assert not block, msg
        assert "Prose nit" in capsys.readouterr().err

    def test_review_override_waives_devin_findings_like_every_other(self, guard):
        block, _ = _scan(guard, [_c(1, _devin_body("🔴", "Waived"))], force=True)
        assert not block

    def test_coderabbit_minor_still_scores_zero(self, guard):
        """Owner 2026-09-24: CodeRabbit Minor stays surface-only — a regression lock."""
        minor = "_🎯 Functional Correctness_ | _🟡 Minor_ | _⚡ Quick win_\n\n**A nit {n}.**\n"
        comments = [_c(i, minor.format(n=i), login=CODERABBIT) for i in range(1, 8)]
        block, msg = _scan(guard, comments)
        assert not block, f"CodeRabbit Minor started scoring: {msg}"


# ── the report's uncounted accounting ──────────────────────────────


class TestUncountedAccounting:
    def test_devin_no_longer_counts_as_an_unrecognised_reviewer(self, guard):
        out: list = []
        _scan(
            guard, [_c(1, _devin_body("🔍", "An explanation", kind="analysis"))], uncounted_out=out
        )
        assert out, "uncounted_out was never written"
        assert out[0]["approx"]["unrecognised_reviewer"] == 0
        # An informational note asserts no defect, so it is NOT an uncounted finding.
        assert sum(out[0]["exact"].values()) == 0, out[0]["exact"]

    def test_drift_is_reported_even_when_a_maintainer_answered_it(self, guard, capsys):
        """Security review L1: the drift note is about the parser, not the finding."""
        body = "<!-- devin-review-comment {not json} -->\n\n🔴 **Garbled but answered**\n"
        comments = [
            _c(1, body),
            _c(9, "Seen.", login="owner", utype="User", reply_to=1, assoc="OWNER"),
        ]
        block, _msg = _scan(guard, comments)
        assert not block
        assert "Garbled but answered" in capsys.readouterr().err

    def test_a_devin_format_drift_is_approximate(self, guard):
        out: list = []
        _scan(guard, [_c(1, _devin_body("🟣", "Drifted"))], uncounted_out=out)
        assert out[0]["approx"]["unrecognised_format"] == 1


# ── duplicated posts: classify the strongest copy ──────────────────


class TestDuplicateCopiesUseTheStrongest:
    """Architect SF2: copies share an id but may differ; first-copy-wins hid a red one."""

    def test_an_informational_first_copy_does_not_hide_a_severe_second(self, guard):
        fid = "BUG_pr-review-job-aaa_0009"
        comments = [
            _c(1, _devin_body("🔍", "Same id, gray first", fid=fid, kind="analysis")),
            _c(2, _devin_body("🔴", "Same id, red second", fid=fid)),
        ]
        block, msg = _scan(guard, comments)
        assert block, "a gray first copy hid the severe copy"
        assert "Same id, red second" in msg

    def test_an_off_diff_first_copy_does_not_hide_an_in_diff_second(self, guard):
        fid = "BUG_pr-review-job-aaa_0010"
        comments = [
            _c(
                1,
                _devin_body("🔴", "Anchored off-diff", fid=fid, path="src/other.py"),
                path="src/other.py",
            ),
            _c(2, _devin_body("🔴", "Anchored in-diff", fid=fid)),
        ]
        block, _msg = _scan(guard, comments)
        assert block, "an off-diff first copy hid the in-diff copy"


# ── the Codex stand-in: review at head by Devin or CodeRabbit ──────


def _reviews(*rows):
    return "\n".join(json.dumps(r) for r in rows)


class TestSubstituteAtHead:
    @pytest.fixture(autouse=True)
    def _seams(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")

    def _check(self, guard, monkeypatch, reviews, *, substitute=True, force=False):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", reviews)
        out: list = []
        block, msg, verified = guard._check_codex_reviewed_head(
            "1", force=force, substitute=substitute, substitute_out=out
        )
        return block, msg, verified, out

    def test_devin_review_at_head_substitutes(self, guard, monkeypatch):
        block, msg, verified, out = self._check(
            guard, monkeypatch, _reviews({"login": DEVIN, "commit_id": HEAD, "state": "COMMENTED"})
        )
        assert not block, msg
        assert verified == HEAD, "the merge must stay bound to the head the substitute reviewed"
        assert out and out[0]["reviewers"] == [DEVIN]

    def test_coderabbit_review_at_head_substitutes(self, guard, monkeypatch):
        block, _msg, verified, out = self._check(
            guard, monkeypatch, _reviews({"login": CODERABBIT, "commit_id": HEAD})
        )
        assert not block and verified == HEAD and out[0]["reviewers"] == [CODERABBIT]

    def test_changes_requested_at_head_still_substitutes(self, guard, monkeypatch):
        """Content is the findings gates' job; freshness asks only that a review exists at head."""
        block, *_ = self._check(
            guard,
            monkeypatch,
            _reviews({"login": DEVIN, "commit_id": HEAD, "state": "CHANGES_REQUESTED"}),
        )
        assert not block

    def test_a_substitute_at_a_stale_head_does_not_count(self, guard, monkeypatch):
        block, msg, verified, out = self._check(
            guard, monkeypatch, _reviews({"login": DEVIN, "commit_id": STALE})
        )
        assert block and verified is None and not out

    def test_a_dismissed_substitute_review_does_not_count(self, guard, monkeypatch):
        block, *_ = self._check(
            guard, monkeypatch, _reviews({"login": DEVIN, "commit_id": HEAD, "state": "DISMISSED"})
        )
        assert block

    def test_a_body_less_reply_wrapper_does_not_count(self, guard, monkeypatch):
        """Architect BLOCKER: an empty-body record at head is the wrapper GitHub makes
        when the bot replies in a thread — not a review of the head. MEASURED on
        live PRs: such wrappers held only 'agreed, I withdraw this finding' replies."""
        block, msg, verified, out = self._check(
            guard,
            monkeypatch,
            _reviews(
                {"login": CODERABBIT, "commit_id": HEAD, "state": "COMMENTED", "has_body": False}
            ),
        )
        assert block and verified is None and not out, "a reply wrapper stood in for Codex"

    def test_a_bodied_review_beside_a_wrapper_still_counts(self, guard, monkeypatch):
        block, _msg, verified, out = self._check(
            guard,
            monkeypatch,
            _reviews(
                {"login": DEVIN, "commit_id": HEAD, "has_body": False},
                {"login": DEVIN, "commit_id": HEAD, "has_body": True},
            ),
        )
        assert not block and verified == HEAD and out[0]["reviewers"] == [DEVIN]

    def test_a_pending_substitute_review_does_not_count(self, guard, monkeypatch):
        block, *_ = self._check(
            guard, monkeypatch, _reviews({"login": DEVIN, "commit_id": HEAD, "state": "PENDING"})
        )
        assert block

    def test_an_unlisted_reviewer_at_head_does_not_count(self, guard, monkeypatch):
        block, *_ = self._check(
            guard, monkeypatch, _reviews({"login": "some-other-bot[bot]", "commit_id": HEAD})
        )
        assert block

    def test_without_the_sigil_nothing_substitutes(self, guard, monkeypatch):
        block, _msg, _v, out = self._check(
            guard,
            monkeypatch,
            _reviews({"login": DEVIN, "commit_id": HEAD}),
            substitute=False,
        )
        assert block and not out

    def test_a_current_codex_review_needs_no_substitute(self, guard, monkeypatch):
        block, _msg, verified, out = self._check(
            guard,
            monkeypatch,
            _reviews(
                {"login": "chatgpt-codex-connector[bot]", "commit_id": HEAD},
                {"login": DEVIN, "commit_id": HEAD},
            ),
        )
        assert not block and verified == HEAD
        assert not out, "a substitute was recorded although Codex covered the head"

    def test_the_block_message_names_who_reviewed_which_head(self, guard, monkeypatch):
        block, msg, *_ = self._check(
            guard, monkeypatch, _reviews({"login": DEVIN, "commit_id": STALE})
        )
        assert block
        assert "substitute" in msg.lower()
        assert STALE[:12] in msg

    def test_codex_review_reader_ignores_substitute_logins(self, guard, monkeypatch):
        """The escalation counter and the Codex freshness identity must not change."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews({"login": DEVIN, "commit_id": HEAD}))
        assert guard._latest_codex_reviewed_sha("1") is None
