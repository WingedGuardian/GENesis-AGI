"""#4933: a floor finding clears only on a verified fix or the owner's per-finding sigil.

The fix test reads the PR's commits and the cited commit's files through
``_fetch_comments_paged``, which is faked here by endpoint so each cell states exactly
what GitHub returned.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_GUARD = _HOOKS / "git_push_guard.py"

P1 = "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange)</sub></sub> Real defect**"
P2 = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub> Smaller defect**"
MAJOR = "_🩺 Stability & Availability_ | _🟠 Major_ | _⚡ Quick win_\n\n**A major defect.**"
MINOR = "_🩺 Stability & Availability_ | _🟡 Minor_ | _⚡ Quick win_\n\n**A minor defect.**"
PATH = "src/benign.py"
BASE = "1" * 40
FIX = "abcdef1" + "2" * 33
LATER = "abcdef9" + "3" * 33
MERGE = "fedcba1" + "4" * 33
MAIN = "0" * 40  # the base branch: a parent outside the PR
FOUND_AT = "2026-01-02T05:00:00Z"
DEVIN = "devin-ai-integration[bot]"


def _devin(marker: str = "🔴", fid: str = "BUG_pr-review-job-aaa_0001") -> str:
    meta = '{"id": "' + fid + '", "file_path": "' + PATH + '", "severity": "x", "kind": "bug"}'
    return f"<!-- devin-review-comment {meta} -->\n\n{marker} **A Devin finding**\n\nDetail."


def _row(sha: str, parents: list | None = None, *, date: str = "2026-01-02T06:00:00Z") -> dict:
    return {"sha": sha, "parents": [MAIN] if parents is None else parents, "date": date}


COMMITS = [
    _row(BASE, date="2026-01-02T01:00:00Z"),
    _row(FIX, [BASE]),
    _row(MERGE, [FIX, MAIN]),
]


@pytest.fixture(scope="module")
def guard():
    sys.path.insert(0, str(_HOOKS))
    spec = importlib.util.spec_from_file_location("git_push_guard_4933", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setenv("_TEST_DOC_FINDINGS_MODE", "skip")
    monkeypatch.setenv(
        "_TEST_GH_PR_FILES", '{"filename": "src/benign.py", "previous_filename": null}'
    )
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)


def _finding(cid=1, body=P1, *, path=PATH, login="chatgpt-codex-connector[bot]", orig=BASE) -> dict:
    return {
        "id": cid,
        "reply_to": None,
        "login": login,
        "type": "Bot",
        "assoc": "NONE",
        "path": path,
        "body": body,
        "created": FOUND_AT,
        "orig": orig,
    }


def _reply(
    to=1, body=f"Fixed in {FIX[:9]}.", *, cid=90, login="a-maintainer", utype="User", assoc="OWNER"
) -> dict:
    return {"id": cid, "reply_to": to, "login": login, "type": utype, "assoc": assoc, "body": body}


def _scan(guard, monkeypatch, comments, *, commits=None, files=None, **kw):
    """``commits``/``files``: a value, or ``(rows, complete)`` to fake a failed read."""
    commits = COMMITS if commits is None else commits
    files = {FIX: [PATH]} if files is None else files
    reads: list[str] = []

    def fetch(endpoint, _pr, _repo, _jq):
        reads.append(endpoint)
        if endpoint.endswith("/comments"):
            return comments, True
        if endpoint.endswith("/commits"):
            return commits if isinstance(commits, tuple) else (commits, True)
        got = files.get(endpoint.removeprefix("commits/"), [])
        if isinstance(got, tuple):
            return got
        return [{"f": name, "p": None, "s": "modified", "c": 1} for name in got], True

    monkeypatch.setattr(guard, "_fetch_comments_paged", fetch)
    monkeypatch.setattr(guard, "_pr_lane", lambda *_a, **_k: "standard")
    block, msg = guard._check_inline_review_findings("100", **kw)
    return block, msg, reads


class TestFixTest:
    def test_a_later_single_parent_commit_on_the_file_clears(self, guard, monkeypatch):
        block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply()])
        assert not block, msg

    @pytest.mark.parametrize(
        ("reply", "why"),
        [
            ("Deferred to #123.", "no reply has a line starting 'Fixed in <sha>'"),
            ("Not a defect, by design.", "no reply has a line starting 'Fixed in <sha>'"),
            (f"Documented in {FIX[:9]} as a known limit.", "no reply has a line"),
            (f"Verified at {FIX[:9]}, no defect.", "no reply has a line"),
            (f"Partly fixed in {FIX[:9]}; the rest is tracked.", "no reply has a line"),
            (f"Not fixed in {FIX[:9]}.", "no reply has a line"),
            (f"Unfixed in {FIX[:9]}.", "no reply has a line"),
            (f"Fixed in {FIX[:9].upper()}.", "is not a claim"),
            (f"Not yet fixed in {FIX[:9]}.", "no reply has a line"),
            (f"This will be fixed in {FIX[:9]}.", "no reply has a line"),
            (f"Should be fixed in {FIX[:9]}.", "no reply has a line"),
            (f"It isn\u2019t fixed in {FIX[:9]}.", "no reply has a line"),
            (f"> Fixed in {FIX[:9]}\n\nThat claim is wrong.", "no reply has a line"),
            (f"```\nFixed in {FIX[:9]}\n```", "no reply has a line"),
            (f"````\n```\nFixed in {FIX[:9]}\n````", "no reply has a line"),
            (f"```\n``` info\nFixed in {FIX[:9]}\n```", "no reply has a line"),
            (f"<!-- Fixed in {FIX[:9]} --> Deferred.", "no reply has a line"),
            (f"Deferred.\n\n    Fixed in {FIX[:9]}", "no reply has a line"),
            (f"I cannot say it is fixed in {FIX[:9]}.", "no reply has a line"),
            (f"It hasnt been fixed in {FIX[:9]}.", "no reply has a line"),
            (f"Fixed in {FIX[:9]}? No, the race remains.", "is not a claim"),
            (f"Fixed in `{FIX[:9]}`? No, the race remains.", "is not a claim"),
            (f"Fixed in {LATER[:9]}.", "not among the PR's commits"),
            (f"Fixed in {MERGE[:9]}.", "is a merge commit"),
            (f"Fixed in {BASE[:9]}.", "does not come after the head"),
            (f"Confirmed and fixed in `{FIX[:9]}`, with a test.", "no reply has a line"),
            (f"This was not a race. Fixed in {FIX[:9]}.", "no reply has a line"),
            # Round-1 class audit: text that states no claim.
            (f"Reply with `Fixed in {FIX[:9]}` once it lands.", "no reply has a line"),
            (f"`Fixed in {FIX[:9]}`", "no reply has a line"),
            (f"~~Fixed in {FIX[:9]}~~", "no reply has a line"),
            (f"<del>\nFixed in {FIX[:9]}\n</del>", "no reply has a line"),
            (f"<code>Fixed in {FIX[:9]}</code>", "no reply has a line"),
            (f"<details>\n\nFixed in {FIX[:9]}\n\n</details>", "no reply has a line"),
            (f"> The reviewer said\nFixed in {FIX[:9]}", "no reply has a line"),
            (f"- > Fixed in {FIX[:9]}", "no reply has a line"),
            (f"Fixed in {FIX[:9]}, pending CI.", "is not a claim"),
            (f"Fixed in {FIX[:9]} (partially).", "is not a claim"),
            (f"Fixed in {FIX[:9]} \u2014 reverted.", "is not a claim"),
            (f"Fixed in {FIX[:9]} if CI passes.", "is not a claim"),
            (f"Fixed in {FIX[:9]} on #4999.", "is not a claim"),
            (f"Fixed in {FIX[:9]} ?", "is not a claim"),
            (f"Fixed in {FIX[:9]}, or not?", "is not a claim"),
            (f"Fixed in {FIX[:9]}, right?", "is not a claim"),
            (f"Fixed in {FIX[:9]}, except the retry path.", "is not a claim"),
            (f"Fixed in {FIX[:9]}, mostly.", "is not a claim"),
            (f"Fixed in {FIX[:9]}, the race remains.", "is not a claim"),
            (f"Fixed in {FIX[:9]}, with a follow-up for the rest.", "is not a claim"),
            (f"Fixed in {FIX[:9]}, but it isn\u2019t complete.", "is not a claim"),
            (f"The typo is fixed in {FIX[:9]}, the P1 is not.", "no reply has a line"),
            (f"@codex review: confirm whether this was fixed in {FIX[:9]}.", "no reply has a line"),
            (f"This will be\nfixed in {FIX[:9]}.", "is not a claim"),
            (f"[Fixed in {FIX[:9]}](https://example.com)", "no reply has a line"),
        ],
    )
    def test_a_reply_that_is_not_a_fix_blocks(self, guard, monkeypatch, reply, why):
        block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply(body=reply)])
        assert block
        assert "finding 1 (src/benign.py): " in msg and why in msg

    @pytest.mark.parametrize(
        "reply",
        [
            f"Fixed at {FIX}.",
            f"Fixed in `{FIX[:9]}`, with a test.",
            f"fixed by commit {FIX[:9]}",
            f"- Fixed in {FIX[:9]}.",
            f"Fixed in {FIX[:9]}: the guard now returns early when the file is missing.",
            f"Fixed in {FIX[:9]}. It now returns early if the file is missing.",
            f"Thanks!\nFixed in {FIX[:9]}.",
            f"Fixed in {FIX[:9]} \u2014 the check now runs first.",
            f"```\r\nlog\r\n```\r\nFixed in {FIX[:9]}.",
            f'<!-- devin-review-comment {{"role": "builder"}} --> Fixed in {FIX[:9]}.',
            f"This was not a race.\nFixed in {FIX[:9]}.",
            f"> The reviewer's text\n\nFixed in {FIX[:9]}.",
            f"Fixes in {FIX[:9]}",
            f"Thanks, will check the rest\n\nFixed in {FIX[:9]}.",
        ],
    )
    def test_the_fix_claim_forms(self, guard, monkeypatch, reply):
        block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply(body=reply)])
        assert not block, msg

    def test_a_commit_on_another_file_blocks(self, guard, monkeypatch):
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding(), _reply()], files={FIX: ["src/other.py"]}
        )
        assert block and "does not touch src/benign.py" in msg

    def test_a_rename_source_counts_as_the_file(self, guard, monkeypatch):
        rows = [{"f": "src/new.py", "p": PATH, "s": "renamed", "c": 3}]

        def fetch(endpoint, _pr, _repo, _jq):
            if endpoint.endswith("/comments"):
                return [_finding(), _reply()], True
            if endpoint.endswith("/commits"):
                return COMMITS, True
            return rows, True

        monkeypatch.setattr(guard, "_fetch_comments_paged", fetch)
        monkeypatch.setattr(guard, "_pr_lane", lambda *_a, **_k: "standard")
        assert not guard._check_inline_review_findings("100")[0]

    def test_a_pure_rename_is_no_fix(self, guard, monkeypatch):
        """A commit that only moves the file changes none of its lines."""
        rows = [{"f": "src/new.py", "p": PATH, "s": "renamed", "c": 0}]

        def fetch(endpoint, _pr, _repo, _jq):
            if endpoint.endswith("/comments"):
                return [_finding(), _reply()], True
            if endpoint.endswith("/commits"):
                return COMMITS, True
            return rows, True

        monkeypatch.setattr(guard, "_fetch_comments_paged", fetch)
        monkeypatch.setattr(guard, "_pr_lane", lambda *_a, **_k: "standard")
        block, msg = guard._check_inline_review_findings("100")
        assert block and "does not touch src/benign.py" in msg

    def test_a_fix_after_an_earlier_rename_clears(self, guard, monkeypatch):
        """Devin 4675231808: the finding names the old path, and the fix touches only the
        name this PR's rename gave it."""
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", json.dumps({"filename": "src/new.py", "previous_filename": PATH})
        )
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding(), _reply()], files={FIX: ["src/new.py"]}
        )
        assert not block, msg

    def test_a_fix_to_the_old_name_does_not_clear_a_finding_on_the_new_one(
        self, guard, monkeypatch
    ):
        """Only a rename back could make the old name the same file again."""
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", json.dumps({"filename": "src/new.py", "previous_filename": PATH})
        )
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding(path="src/new.py"), _reply()], files={FIX: [PATH]}
        )
        assert block and "does not touch src/new.py" in msg

    def test_a_rename_elsewhere_does_not_widen_the_match(self, guard, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES",
            "\n".join(
                json.dumps(r)
                for r in (
                    {"filename": PATH, "previous_filename": None},
                    {"filename": "src/new.py", "previous_filename": "src/old.py"},
                )
            ),
        )
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding(), _reply()], files={FIX: ["src/new.py"]}
        )
        assert block and "does not touch" in msg

    def test_an_ambiguous_prefix_is_no_evidence(self, guard, monkeypatch):
        twin = "abcdef1" + "9" * 33
        commits = [*COMMITS, _row(twin)]
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding(), _reply(body="Fixed in abcdef1.")], commits=commits
        )
        assert block and "ambiguous" in msg

    def test_ancestry_decides_whatever_the_list_order(self, guard, monkeypatch):
        """GitHub lists a PR's commits by date. A fix listed BEFORE the finding's head
        (an earlier author date) still descends from it, so it is a real fix."""
        block, msg, _ = _scan(
            guard,
            monkeypatch,
            [_finding(), _reply()],
            commits=[_row(FIX, [BASE], date="2026-01-01T00:00:00Z"), _row(BASE)],
        )
        assert not block, msg

    def test_a_later_listed_ancestor_is_no_fix(self, guard, monkeypatch):
        """A commit listed after the finding's head (a later or forged date) that the
        head descends from came before the finding."""
        block, msg, _ = _scan(
            guard,
            monkeypatch,
            [_finding(orig=FIX), _reply(body=f"Fixed in {BASE[:9]}.")],
            commits=[_row(FIX, [BASE]), _row(BASE, date="2026-02-01T00:00:00Z")],
            files={BASE: [PATH]},
        )
        assert block and "does not come after the head" in msg

    def test_a_side_branch_commit_is_no_fix(self, guard, monkeypatch):
        """S was made beside the finding's head and merged in later: it is listed after
        the head and is still not a fix of what that head showed."""
        side = "abcdef5" + "5" * 33
        commits = [
            _row(BASE),
            _row(FIX, [BASE]),
            _row(side, [BASE], date="2026-01-03T00:00:00Z"),
            _row(MERGE, [FIX, side], date="2026-01-04T00:00:00Z"),
        ]
        block, msg, _ = _scan(
            guard,
            monkeypatch,
            [_finding(orig=FIX), _reply(body=f"Fixed in {side[:9]}.")],
            commits=commits,
            files={side: [PATH]},
        )
        assert block and "does not come after the head" in msg

    def test_unreadable_parents_never_clear(self, guard, monkeypatch):
        commits = [_row(BASE), {"sha": FIX, "parents": 1}]
        block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply()], commits=commits)
        assert block and "parents of" in msg

    def test_a_parent_cycle_ends(self, guard, monkeypatch):
        """Real git cannot loop, but the rows are GitHub's data: the walk must end."""
        commits = [_row(BASE, [FIX]), _row(FIX, [BASE])]
        _scan(guard, monkeypatch, [_finding(), _reply()], commits=commits)

    def test_a_row_with_no_changed_line_count_is_no_fix(self, guard, monkeypatch):
        def fetch(endpoint, _pr, _repo, _jq):
            if endpoint.endswith("/comments"):
                return [_finding(), _reply()], True
            if endpoint.endswith("/commits"):
                return COMMITS, True
            return [{"f": PATH, "p": None, "s": "modified", "c": None}], True

        monkeypatch.setattr(guard, "_fetch_comments_paged", fetch)
        monkeypatch.setattr(guard, "_pr_lane", lambda *_a, **_k: "standard")
        block, msg = guard._check_inline_review_findings("100")
        assert block and "does not touch src/benign.py" in msg

    def test_a_commit_is_not_its_own_descendant(self, guard, monkeypatch):
        commits = [_row(BASE), _row(FIX, [FIX])]
        block, msg, _ = _scan(guard, monkeypatch, [_finding(orig=FIX), _reply()], commits=commits)
        assert block and "does not come after the head" in msg

    @pytest.mark.parametrize(
        "row",
        [
            {"f": PATH, "p": None, "s": "modified", "c": 0},
            {"f": "src/copy.py", "p": PATH, "s": "copied", "c": 0},
            {"f": "src/copy.py", "p": PATH, "s": "copied", "c": 4},
        ],
    )
    def test_a_row_that_changes_no_line_of_the_file_is_no_fix(self, guard, monkeypatch, row):
        """A mode change touches no line; a copy leaves its source as it was."""

        def fetch(endpoint, _pr, _repo, _jq):
            if endpoint.endswith("/comments"):
                return [_finding(), _reply()], True
            if endpoint.endswith("/commits"):
                return COMMITS, True
            return [row], True

        monkeypatch.setattr(guard, "_fetch_comments_paged", fetch)
        monkeypatch.setattr(guard, "_pr_lane", lambda *_a, **_k: "standard")
        block, msg = guard._check_inline_review_findings("100")
        assert block and "does not touch src/benign.py" in msg

    @pytest.mark.parametrize(
        "tag", ["s", "pre", "kbd", "samp", "sub", "sup", "strike", "blockquote", "del", "details"]
    )
    def test_text_inside_an_html_aside_claims_nothing(self, guard, monkeypatch, tag):
        for body in (
            f"<{tag}>\nFixed in {FIX[:9]}.\n</{tag}>",
            f'<{tag} class="x">\nFixed in {FIX[:9]}.',  # never closed: the rest is inside
        ):
            block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply(body=body)])
            assert block and "no reply has a line" in msg, body

    def test_a_hedge_far_back_in_the_sentence_counts(self, guard, monkeypatch):
        body = "We will land this after the refactor of the parser and its many callers, " * 2
        body += f"\nfixed in {FIX[:9]}."
        block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply(body=body)])
        assert block

    def test_an_open_tag_with_no_end_stays_linear(self, guard):
        import time

        body = "<s" * 32768
        started = time.monotonic()
        assert guard._fix_claims(body) == []
        assert time.monotonic() - started < 0.5

    def test_a_gone_head_cannot_prove_order(self, guard, monkeypatch):
        """A rebase refreshes committer dates, so no date stands in for the order."""
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding(orig="f" * 40), _reply()], commits=[_row(FIX, [BASE])]
        )
        assert block and "is gone" in msg

    def test_a_finding_with_no_head_cannot_prove_order(self, guard, monkeypatch):
        block, msg, _ = _scan(guard, monkeypatch, [_finding(orig=None), _reply()])
        assert block and "is gone" in msg

    def test_a_pathless_finding_cannot_pass(self, guard, monkeypatch):
        block, msg, _ = _scan(guard, monkeypatch, [_finding(path=None), _reply()])
        assert block and "only the sigil" in msg

    @pytest.mark.parametrize(
        ("kw", "why"),
        [
            ({"commits": (None, False)}, "commit list unreadable"),
            ({"commits": ([_row(FIX, [BASE])], False)}, "commit list unreadable"),
            ({"files": {FIX: (None, False)}}, "unreadable"),
            ({"files": {FIX: ([{"f": "src/other.py", "p": None}], False)}}, "unreadable"),
        ],
    )
    def test_a_failed_read_never_clears(self, guard, monkeypatch, kw, why):
        block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply()], **kw)
        assert block and why in msg

    @pytest.mark.parametrize("head_listed", [True, False])
    def test_a_clipped_commit_list_says_so(self, guard, monkeypatch, head_listed):
        commits = [_row(f"{i:040x}") for i in range(guard._PR_COMMITS_API_CAP)]
        if head_listed:
            commits[0] = _row(BASE)
        block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply()], commits=commits)
        assert block and "clipped" in msg

    def test_the_commit_read_cap_holds_across_findings(self, guard, monkeypatch):
        cap = guard._FIX_CHECK_MAX_COMMITS
        shas = [f"{i + 1:07x}" + "a" * 33 for i in range(cap + 6)]
        half = len(shas) // 2
        commits = [_row(BASE), *[_row(s, [BASE]) for s in shas]]
        comments = [
            _finding(1),
            _reply(1, "\n".join(f"Fixed in {s[:9]}." for s in shas[:half]), cid=90),
            _finding(2),
            _reply(2, "\n".join(f"Fixed in {s[:9]}." for s in shas[half:]), cid=91),
        ]
        block, msg, reads = _scan(guard, monkeypatch, comments, commits=commits, files={})
        assert block and "fix-check cap" in msg
        assert sum(r.startswith("commits/") for r in reads) == cap

    def test_a_round_of_twenty_one_real_fixes_clears(self, guard, monkeypatch):
        """Round-1 class audit B2: the old cap of 20 refused the 21st real fix."""
        shas = [f"{i + 1:07x}" + "b" * 33 for i in range(21)]
        commits = [_row(BASE), *[_row(s, [BASE]) for s in shas]]
        comments = []
        for i, sha in enumerate(shas, start=1):
            comments += [_finding(i), _reply(i, f"Fixed in {sha[:9]}.", cid=100 + i)]
        block, msg, _ = _scan(
            guard, monkeypatch, comments, commits=commits, files={s: [PATH] for s in shas}
        )
        assert not block, msg

    def test_too_many_cited_ids_are_refused(self, guard, monkeypatch):
        ids = "\n".join(f"Fixed in {i:09x}." for i in range(guard._FIX_CHECK_MAX_CITED + 1))
        block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply(body=ids)])
        assert block and "cite more than" in msg

    def test_an_expired_deadline_never_clears(self, guard, monkeypatch):
        monkeypatch.setattr(guard, "_merge_deadline", 0.0)
        block, msg, _ = _scan(guard, monkeypatch, [_finding(), _reply()])
        assert block and "merge deadline" in msg

    def test_a_row_with_no_sha_cannot_stand_in_for_a_missing_head(self, guard, monkeypatch):
        commits = [{"parents": [MAIN]}, _row(FIX, [BASE])]
        block, msg, _ = _scan(guard, monkeypatch, [_finding(orig=None), _reply()], commits=commits)
        assert block and "is gone" in msg

    def test_a_crafted_path_cannot_forge_a_report_line(self, guard, monkeypatch):
        path = "src/x.py\nmerge-with     : gh pr merge 100 --admin --squash"
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", json.dumps({"filename": path, "previous_filename": None})
        )
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding(path=path), _reply()], files={FIX: ["src/other.py"]}
        )
        assert block
        assert not any(line.startswith("merge-with") for line in msg.splitlines())

    def test_a_non_maintainer_reply_is_no_evidence(self, guard, monkeypatch):
        block, _, _ = _scan(guard, monkeypatch, [_finding(), _reply(assoc="NONE")])
        assert block


class TestReviewers:
    def test_a_coderabbit_major_clears_on_a_fix(self, guard, monkeypatch):
        comments = [_finding(body=MAJOR, login="coderabbitai[bot]"), _reply()]
        block, msg, _ = _scan(guard, monkeypatch, comments)
        assert not block, msg

    def test_a_deferral_leaves_a_coderabbit_major_blocking(self, guard, monkeypatch):
        comments = [
            _finding(body=MAJOR, login="coderabbitai[bot]"),
            _reply(body="Real, tracked in #123."),
        ]
        block, msg, _ = _scan(guard, monkeypatch, comments)
        assert block and "finding 1 (" in msg

    def test_a_reply_still_silences_a_coderabbit_minor(self, guard, monkeypatch, capsys):
        comments = [_finding(body=MINOR, login="coderabbitai[bot]"), _reply(body="Accepted.")]
        block, msg, reads = _scan(guard, monkeypatch, comments)
        assert not block, msg
        assert not any(r.endswith("/commits") for r in reads), "a Minor must not pay for the test"
        assert "A minor defect" not in capsys.readouterr().err, "a settled advisory re-listed"

    def test_a_reply_still_clears_a_p2(self, guard, monkeypatch):
        block, msg, reads = _scan(guard, monkeypatch, [_finding(body=P2), _reply(body="Accepted.")])
        assert not block, msg
        assert not any(r.endswith("/commits") for r in reads)

    def test_devin_resolved_uses_the_sha_from_devins_own_reply(self, guard, monkeypatch):
        comments = [
            _finding(body=_devin(), login=DEVIN),
            _reply(
                body=f"Fixed in {FIX[:9]}.", cid=90, login=DEVIN, utype="Bot", assoc="CONTRIBUTOR"
            ),
            _reply(
                body="✅ **Resolved**: done.", cid=91, login=DEVIN, utype="Bot", assoc="CONTRIBUTOR"
            ),
        ]
        block, msg, _ = _scan(guard, monkeypatch, comments)
        assert not block, msg

    def test_a_clear_resting_on_devins_reply_is_listed(self, guard, monkeypatch, capsys):
        """A maintainer reply that clears nothing must not hide that Devin's reply did."""
        comments = [
            _finding(body=_devin(), login=DEVIN),
            _reply(body="Deferred.", cid=89),
            _reply(
                body=f"Fixed in {FIX[:9]}.", cid=90, login=DEVIN, utype="Bot", assoc="CONTRIBUTOR"
            ),
            _reply(
                body="✅ **Resolved**: done.", cid=91, login=DEVIN, utype="Bot", assoc="CONTRIBUTOR"
            ),
        ]
        block, msg, _ = _scan(guard, monkeypatch, comments)
        assert not block, msg
        assert "[Devin self-withdrawn]" in capsys.readouterr().err

    def test_a_maintainer_fix_on_a_devin_finding_is_not_listed(self, guard, monkeypatch, capsys):
        comments = [
            _finding(body=_devin(), login=DEVIN),
            _reply(cid=89),
            _reply(
                body="✅ **Resolved**: done.", cid=91, login=DEVIN, utype="Bot", assoc="CONTRIBUTOR"
            ),
        ]
        block, msg, _ = _scan(guard, monkeypatch, comments)
        assert not block, msg
        assert "[Devin self-withdrawn]" not in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("body", "login"), [(P1, "chatgpt-codex-connector[bot]"), (MAJOR, "coderabbitai[bot]")]
    )
    def test_only_devin_gets_the_self_withdrawal_route(self, guard, monkeypatch, body, login):
        comments = [
            _finding(body=body, login=login),
            _reply(
                body=f"✅ **Resolved**\n\nFixed in {FIX[:9]}.",
                login=login,
                utype="Bot",
                assoc="CONTRIBUTOR",
            ),
        ]
        block, _, _ = _scan(guard, monkeypatch, comments)
        assert block

    def test_a_fix_must_follow_the_head_of_the_copy_on_its_file(self, guard, monkeypatch):
        """Copy 2 was posted after the cited commit existed, on the same file as copy 1."""
        comments = [
            _finding(1, _devin(), login=DEVIN, orig=BASE),
            _finding(2, _devin(), login=DEVIN, orig=FIX),
            _reply(1),
        ]
        block, msg, _ = _scan(guard, monkeypatch, comments)
        assert block and "does not come after the head" in msg

    def test_each_files_reason_is_its_own(self, guard, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES",
            '{"filename": "src/benign.py", "previous_filename": null}\n'
            '{"filename": "src/second.py", "previous_filename": null}',
        )
        comments = [
            _finding(1, _devin(), login=DEVIN),
            _finding(2, _devin(), login=DEVIN, path="src/second.py"),
            _reply(1),
        ]
        block, msg, _ = _scan(guard, monkeypatch, comments)
        assert block and "(src/second.py): cited commit" in msg

    def test_a_fix_on_one_devin_copy_does_not_clear_another_file(self, guard, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES",
            '{"filename": "src/benign.py", "previous_filename": null}\n'
            '{"filename": "src/second.py", "previous_filename": null}',
        )
        comments = [
            _finding(1, _devin(), login=DEVIN),
            _finding(2, _devin(), login=DEVIN, path="src/second.py"),
            _reply(1),
        ]
        block, _, _ = _scan(guard, monkeypatch, comments)
        assert block, "a commit touching only one copy's file cleared both"
        files = {FIX: [PATH, "src/second.py"]}
        block, msg, _ = _scan(guard, monkeypatch, comments, files=files)
        assert not block, msg

    def test_devins_own_sha_without_the_marker_is_no_evidence(self, guard, monkeypatch):
        comments = [
            _finding(body=_devin(), login=DEVIN),
            _reply(body=f"Fixed in {FIX[:9]}.", login=DEVIN, utype="Bot", assoc="CONTRIBUTOR"),
        ]
        block, _, _ = _scan(guard, monkeypatch, comments)
        assert block

    def test_devin_resolved_with_a_bad_sha_blocks(self, guard, monkeypatch):
        comments = [
            _finding(body=_devin(), login=DEVIN),
            _reply(
                body=f"Fixed in {LATER[:9]}.", cid=90, login=DEVIN, utype="Bot", assoc="CONTRIBUTOR"
            ),
            _reply(
                body="✅ **Resolved**: done.", cid=91, login=DEVIN, utype="Bot", assoc="CONTRIBUTOR"
            ),
        ]
        block, _, _ = _scan(guard, monkeypatch, comments)
        assert block

    def test_an_answered_off_diff_floor_finding_stays_silent(self, guard, monkeypatch, capsys):
        comments = [_finding(path="src/elsewhere.py"), _reply(body="Accepted.")]
        block, msg, _ = _scan(guard, monkeypatch, comments)
        assert not block, msg
        assert "src/elsewhere.py" not in capsys.readouterr().err


class TestSigil:
    def test_it_clears_the_named_finding_and_records_it(self, guard, monkeypatch):
        used: set[int] = set()
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding()], floor_accept=frozenset({1}), floor_accept_used=used
        )
        assert not block, msg
        assert used == {1}

    def test_it_clears_only_the_named_finding(self, guard, monkeypatch):
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding(1), _finding(2)], floor_accept=frozenset({1})
        )
        assert block and "finding 2 (" in msg and "finding 1 (" not in msg

    def test_an_id_on_a_fixed_finding_is_not_spent(self, guard, monkeypatch, capsys):
        used: set[int] = set()
        _scan(
            guard,
            monkeypatch,
            [_finding(), _reply()],
            floor_accept=frozenset({1}),
            floor_accept_used=used,
        )
        assert used == set()
        assert "already cleared by a fix" in capsys.readouterr().err

    def test_an_unknown_id_is_a_note_not_a_clearance(self, guard, monkeypatch, capsys):
        block, _, _ = _scan(guard, monkeypatch, [_finding()], floor_accept=frozenset({7}))
        assert block
        assert "names no open floor finding" in capsys.readouterr().err

    def test_a_dispatched_session_cannot_use_it(self, guard, monkeypatch, capsys):
        monkeypatch.setenv("GENESIS_CC_SESSION", "1")
        used: set[int] = set()
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding()], floor_accept=frozenset({1}), floor_accept_used=used
        )
        assert block and "dispatched" in msg
        assert used == set()
        assert "was refused in a dispatched session" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("command", "ids", "bad"),
        [
            ("gh pr merge 1  # floor-accept=12", {12}, False),
            ("gh pr merge 1  # floor-accept=12,34", {12, 34}, False),
            ("gh pr merge 1  # stale-review-override floor-accept=12", {12}, False),
            ("gh pr merge 1  # floor-accept", set(), True),
            ("gh pr merge 1  # floor-accept:", set(), True),
            ("gh pr merge 1  # floor-accept=12,", set(), True),
            ("gh pr merge 1  # floor-accept=12, 34", set(), True),
            ("gh pr merge 1  # floor-accept=abc", set(), True),
            ("gh pr merge 1  # floor-accept=" + "1" * 21, set(), True),
            ("gh pr merge 1  # floor-accept=١٢", set(), True),
            ("gh pr merge 1  # see floor-accept=12", set(), False),
            ("gh pr merge 1 --body '# floor-accept=12'", set(), False),
        ],
    )
    def test_the_sigil_form(self, guard, command, ids, bad):
        assert guard.trailing_sigil_ids(command, "floor-accept") == (frozenset(ids), bad)

    def test_check_pr_reads_the_flag(self, guard):
        assert guard._parse_check_pr_floor_accept(["--floor-accept", "12,34"]) == {12, 34}
        assert guard._parse_check_pr_floor_accept(["--floor-accept=12"]) == {12}
        assert guard._parse_check_pr_floor_accept(["--floor-accept", "x"]) is None
        assert guard._parse_check_pr_floor_accept(["--floor-accept", "1 2"]) is None
        assert guard._parse_check_pr_floor_accept(["--floor-accept", "1" * 21]) is None
        assert guard._parse_check_pr_floor_accept(["--floor-accept=1", "--floor-accept=2"]) is None
        assert guard._parse_check_pr_floor_accept(["--floor-accept"]) is None
        assert guard._parse_check_pr_floor_accept([]) == frozenset()


class TestReviewOverride:
    def test_a_floor_finding_survives_it(self, guard, monkeypatch):
        block, msg, _ = _scan(guard, monkeypatch, [_finding()], force=True)
        assert block and "always-fix floor" in msg

    def test_the_sigil_clears_under_it(self, guard, monkeypatch):
        block, msg, _ = _scan(
            guard, monkeypatch, [_finding()], force=True, floor_accept=frozenset({1})
        )
        assert not block, msg

    def test_it_still_waives_the_p2_score(self, guard, monkeypatch):
        comments = [_finding(i, P2) for i in range(1, 9)]
        assert _scan(guard, monkeypatch, comments)[0], "control: this score must block"
        assert not _scan(guard, monkeypatch, comments, force=True)[0]

    def test_it_still_waives_an_unreadable_scan(self, guard, monkeypatch, capsys):
        monkeypatch.setattr(guard, "_fetch_comments_paged", lambda *_a: (None, False))
        assert guard._check_inline_review_findings("100")[0], "control"
        assert not guard._check_inline_review_findings("100", force=True)[0]
        assert "waived an unreadable inline scan" in capsys.readouterr().err

    def test_it_still_waives_an_incomplete_scan(self, guard, monkeypatch, capsys):
        monkeypatch.setattr(guard, "_fetch_comments_paged", lambda *_a: ([], False))
        assert guard._check_inline_review_findings("100")[0], "control"
        assert not guard._check_inline_review_findings("100", force=True)[0]
        assert "waived an incomplete inline scan" in capsys.readouterr().err

    def test_a_clean_override_says_nothing_about_unread_scans(self, guard, monkeypatch, capsys):
        assert not _scan(guard, monkeypatch, [], force=True)[0]
        assert "waived" not in capsys.readouterr().err
