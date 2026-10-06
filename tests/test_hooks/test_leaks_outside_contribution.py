"""An outside contributor's fork PR needs no scheduled leaks review.

WHY. The leaks review stops the OWNER's private data (install addresses, home
paths, personal context) reaching the public repo. An outsider's own text cannot
hold it, so asking for the review on their PR costs a scan and protects nothing
(owner ruling, 2026-10-05). CI's mechanical leak-detector still runs on every PR.

WHAT KEEPS IT NARROW. The exemption holds only while the PR is WHOLLY theirs:
opened by a human non-maintainer, from a fork, at this head, every commit authored
by them (committer them or web-flow), and no edit of ours to the title or body.
The moment one of our sessions touches the PR -- a fix commit (whose identity is
linked to no account), a body or title edit -- the review is required again. And
an exemption never overrules a leaks review that ran and objected.

Every row below fails one condition and must BLOCK; the happy-path row and the
controls prove the passes are earned. Network-free via the _TEST_GH_* seams.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("git_push_guard", _HOOKS_DIR / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
EARLIER = "1111111111111111111111111111111111111111"
WHO = "outsider"


def _facts(**over):
    facts = {
        "headRefOid": HEAD,
        "isCrossRepository": True,
        "authorAssociation": "CONTRIBUTOR",
        "author": {"__typename": "User", "login": WHO},
        "userContentEdits": {"totalCount": 0, "nodes": []},
        "timelineItems": {"filteredCount": 0, "nodes": []},
    }
    facts.update(over)
    return json.dumps(facts)


def _edits(*editors):
    """A userContentEdits connection; each editor is (typename, login), or None."""
    nodes = [
        {"editor": None if e is None else {"__typename": e[0], "login": e[1]}} for e in editors
    ]
    return {"totalCount": len(nodes), "nodes": nodes}


def _commits(*rows, message="fix the thing"):
    rows = rows or ((HEAD, WHO, WHO),)
    return "\n".join(
        json.dumps({"sha": sha, "parents": 1, "author": a, "committer": c, "message": message})
        for sha, a, c in rows
    )


def _rollup(conclusion="SUCCESS", name="leak-detector", head=HEAD):
    return json.dumps(
        {
            "headRefOid": head,
            "statusCheckRollup": [{"name": name, "workflowName": "CI", "conclusion": conclusion}],
        }
    )


def _marker(kind="leaks", head=HEAD, body="scheduled review done. VERDICT: PASS"):
    return json.dumps(
        {
            "login": "owner",
            "author_association": "OWNER",
            "body": f"{body}\n<!-- genesis-scheduled-review: head={head} kind={kind} -->",
        }
    )


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
    monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "leaks")
    monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", "")
    monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
    monkeypatch.setenv("_TEST_GH_OUTSIDE_PR", _facts())
    monkeypatch.setenv("_TEST_GH_PR_COMMITS", _commits())


def _gate(exempt_out=None):
    return _mod._check_scheduled_claude_reviewed_head(
        "7", head_sha=HEAD, repo="acme/pub", exempt_out=exempt_out
    )


class TestExempt:
    def test_wholly_outside_fork_pr_needs_no_leaks_review(self, capsys):
        exempt: list = []
        assert _gate(exempt_out=exempt) is None
        assert exempt == [("leaks", WHO)]
        assert "outside contribution by outsider" in capsys.readouterr().err

    def test_web_flow_committer_is_theirs(self, monkeypatch):
        """GitHub commits the contributor's web edits (and Update branch) as web-flow."""
        monkeypatch.setenv(
            "_TEST_GH_PR_COMMITS", _commits((EARLIER, WHO, "web-flow"), (HEAD, WHO, WHO))
        )
        assert _gate() is None

    def test_logins_compare_case_insensitively(self, monkeypatch):
        """GitHub logins are case-insensitive; the PR and commit spellings may differ."""
        monkeypatch.setenv(
            "_TEST_GH_OUTSIDE_PR", _facts(author={"__typename": "User", "login": "OutSider"})
        )
        monkeypatch.setenv("_TEST_GH_PR_COMMITS", _commits((HEAD, "outsider", "OUTSIDER")))
        assert _gate() is None

    def test_their_own_title_rename_and_body_edit_keep_it(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_OUTSIDE_PR",
            _facts(
                userContentEdits=_edits(("User", WHO)),
                timelineItems={"filteredCount": 1, "nodes": [{"actor": {"login": WHO}}]},
            ),
        )
        assert _gate() is None

    @pytest.mark.parametrize("assoc", ["NONE", "FIRST_TIME_CONTRIBUTOR", "contributor"])
    def test_any_non_maintainer_association_qualifies(self, monkeypatch, assoc):
        monkeypatch.setenv("_TEST_GH_OUTSIDE_PR", _facts(authorAssociation=assoc))
        assert _gate() is None

    def test_review_bot_body_edits_keep_it(self, monkeypatch):
        """Review apps write summaries and badges into the body (MEASURED on an
        outside PR: CodeRabbit and Devin edits beside the author's)."""
        monkeypatch.setenv(
            "_TEST_GH_OUTSIDE_PR",
            _facts(
                userContentEdits=_edits(
                    ("Bot", "coderabbitai"), ("Bot", "devin-ai-integration"), ("User", WHO)
                )
            ),
        )
        assert _gate() is None


class TestNotExempt:
    """Each row breaks exactly one condition; each must still BLOCK on leaks."""

    @pytest.mark.parametrize(
        "over",
        [
            pytest.param({"isCrossRepository": False}, id="same-repo-branch"),
            pytest.param({"authorAssociation": "COLLABORATOR"}, id="collaborator"),
            pytest.param({"authorAssociation": "MEMBER"}, id="member"),
            pytest.param({"authorAssociation": "OWNER"}, id="owner"),
            pytest.param({"authorAssociation": ""}, id="association-blank"),
            pytest.param({"author": {"__typename": "Bot", "login": WHO}}, id="bot-author"),
            pytest.param({"author": None}, id="author-deleted"),
            pytest.param({"headRefOid": EARLIER}, id="head-moved"),
            pytest.param(
                {"userContentEdits": _edits(("User", "maintainer"))}, id="body-edited-by-us"
            ),
            pytest.param(
                {"userContentEdits": _edits(("Bot", "coderabbitai"), ("User", "maintainer"))},
                id="our-edit-hidden-behind-a-later-bot-edit",
            ),
            pytest.param({"userContentEdits": _edits(None)}, id="editor-deleted"),
            pytest.param(
                {"userContentEdits": {"totalCount": 101, "nodes": []}}, id="edits-truncated"
            ),
            pytest.param({"userContentEdits": None}, id="edits-unreadable"),
            pytest.param(
                {
                    "timelineItems": {
                        "filteredCount": 1,
                        "nodes": [{"actor": {"login": "maintainer"}}],
                    }
                },
                id="title-renamed-by-us",
            ),
            pytest.param(
                {"timelineItems": {"filteredCount": 101, "nodes": []}}, id="renames-truncated"
            ),
            pytest.param({"timelineItems": None}, id="renames-unreadable"),
            pytest.param(
                {"userContentEdits": _edits(("Bot", "some-other-app"))}, id="unknown-app-edit"
            ),
            pytest.param(
                {"userContentEdits": _edits(("User", "coderabbitai"))},
                id="user-account-named-like-a-review-app",
            ),
            pytest.param(
                {"timelineItems": {"filteredCount": 1, "nodes": [{"actor": None}]}},
                id="title-renamed-by-deleted-account",
            ),
            pytest.param(
                {
                    "timelineItems": {
                        "filteredCount": 1,
                        "nodes": [{"actor": {"login": "coderabbitai"}}],
                    }
                },
                id="title-renamed-by-a-bot",
            ),
            pytest.param({"author": {"__typename": "Mannequin", "login": WHO}}, id="mannequin"),
        ],
    )
    def test_pr_fact_breaks_the_exemption(self, monkeypatch, over):
        monkeypatch.setenv("_TEST_GH_OUTSIDE_PR", _facts(**over))
        msg = _gate()
        assert msg and "leaks" in msg

    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param(((EARLIER, None, None), (HEAD, WHO, WHO)), id="unlinked-commit-ours"),
            pytest.param(((HEAD, "maintainer", "maintainer"),), id="commit-by-maintainer"),
            pytest.param(((HEAD, WHO, "maintainer"),), id="rebased-by-us"),
            pytest.param(((HEAD, "maintainer", WHO),), id="our-commit-they-applied"),
            pytest.param(((HEAD, None, WHO),), id="unlinked-author-they-applied"),
            pytest.param(((EARLIER, WHO, WHO),), id="head-not-among-commits"),
        ],
    )
    def test_commit_breaks_the_exemption(self, monkeypatch, rows):
        monkeypatch.setenv("_TEST_GH_PR_COMMITS", _commits(*rows))
        msg = _gate()
        assert msg and "leaks" in msg

    @pytest.mark.parametrize(
        "message",
        [
            pytest.param(
                "fix\n\nCo-authored-by: maintainer <m@example.com>", id="co-author-trailer"
            ),
            pytest.param(
                "fix\n\nco-authored-by: maintainer <m@example.com>", id="co-author-lowercase"
            ),
            pytest.param(None, id="message-unread"),
        ],
    )
    def test_commit_message_breaks_the_exemption(self, monkeypatch, message):
        """A committed review suggestion arrives as the contributor's commit with
        web-flow as committer; only its Co-authored-by trailer names our text."""
        monkeypatch.setenv(
            "_TEST_GH_PR_COMMITS", _commits((HEAD, WHO, "web-flow"), message=message)
        )
        assert "leaks" in (_gate() or "")

    @pytest.mark.parametrize(
        "rollup",
        [
            pytest.param(_rollup("FAILURE"), id="leak-detector-failed"),
            pytest.param(_rollup(None), id="leak-detector-running"),
            pytest.param(_rollup(name="lint"), id="leak-detector-absent"),
            pytest.param(_rollup(head=EARLIER), id="rollup-for-another-head"),
            pytest.param("", id="rollup-unreadable"),
        ],
    )
    def test_leak_detector_not_green_breaks_the_exemption(self, monkeypatch, rollup):
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", rollup)
        assert "leaks" in (_gate() or "")

    def test_failed_pr_read_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_OUTSIDE_PR", "__error__")
        assert "leaks" in (_gate() or "")

    def test_failed_commit_read_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_COMMITS", "__error__")
        assert "leaks" in (_gate() or "")

    def test_saturated_commit_read_blocks(self, monkeypatch):
        rows = [(f"{i:040x}", WHO, WHO) for i in range(_mod._PR_COMMITS_CAP - 1)]
        monkeypatch.setenv("_TEST_GH_PR_COMMITS", _commits(*rows, (HEAD, WHO, WHO)))
        assert "leaks" in (_gate() or "")


class TestNeverOverrulesAFinding:
    """A leaks review that RAN and objected is never waved through as 'not required'."""

    def test_refused_marker_at_head_blocks(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS", _marker(body="### ERROR\n[P1] leaked an address")
        )
        assert "leaks" in (_gate() or "")

    def test_refused_marker_at_an_earlier_head_blocks(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _marker(head=EARLIER, body="### ERROR\n[P1] leaked an address"),
        )
        assert "leaks" in (_gate() or "")

    def test_blocking_residue_blocks(self, monkeypatch):
        """A [P1] body under a short sha is uncreditable residue; it still denies."""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _marker(head=HEAD[:12], body="### ERROR\n[P1] leaked an address"),
        )
        assert "leaks" in (_gate() or "")


class TestScope:
    def test_other_configured_kinds_stay_required(self, monkeypatch):
        """Only leaks is exempt: an install's code-review requirement still binds,
        and the inventory says leaks is not the outstanding kind."""
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "code-review,leaks")
        msg = _gate()
        assert msg and "missing at head" in msg
        first = msg.splitlines()[0]
        assert "code-review" in first.split("(required")[0]
        assert "leaks" not in first.split("(required")[0]
        assert "leaks is not required: an outside contribution by outsider" in msg

    def test_pr_with_its_marker_reads_nothing(self, monkeypatch):
        """CONTROL: a present marker passes without consulting the exemption at all."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker())
        monkeypatch.setenv("_TEST_GH_OUTSIDE_PR", "__error__")
        monkeypatch.setenv("_TEST_GH_PR_COMMITS", "__error__")
        exempt: list = []
        assert _gate(exempt_out=exempt) is None
        assert exempt == []

    def test_off_public_repo_is_untouched(self):
        assert (
            _mod._check_scheduled_claude_reviewed_head("7", head_sha=HEAD, repo="acme/other")
            is None
        )


class TestBlockMessageSaysWhy:
    """A fork PR the exemption could not clear says which condition failed, so a
    session can tell a failed read from a real disqualification."""

    def test_disqualified_fork_pr_names_the_reason(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_OUTSIDE_PR", _facts(userContentEdits=_edits(("User", "maintainer")))
        )
        msg = _gate() or ""
        assert "exemption for an outside contribution not applied" in msg
        assert "edited by maintainer" in msg

    def test_unreadable_facts_say_so(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_OUTSIDE_PR", "__error__")
        assert "could not be read" in (_gate() or "")

    def test_maintainer_pr_message_is_unchanged(self, monkeypatch):
        """CONTROL: a same-repo PR's block message gains no exemption line."""
        monkeypatch.setenv("_TEST_GH_OUTSIDE_PR", _facts(isCrossRepository=False))
        msg = _gate() or ""
        assert "leaks" in msg and "exemption" not in msg and "outside contribution" not in msg
