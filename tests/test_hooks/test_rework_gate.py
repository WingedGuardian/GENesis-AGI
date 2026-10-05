"""The BLOCKING ``rework`` gate (``_check_rework`` in git_push_guard.py).

Owner decision: a PR identified as a REBUILD of a PR that was sent back for rework
(labelled ``needs-rework`` or ``needs-architecture-session``) cannot merge unless
(1) the builder posted a ``## Rework acknowledgement`` on the old PR before opening
the new one, and (2) the new PR's body carries a complete ``## Rework`` section.
``# rework-override`` passes it and is logged. The gate checks FORM only.

What is tested here, each case its own test:

  * the two rebuild signals (a declaration, containment of a sent-back head), and
    the shapes that are NOT rebuilds (self-exclusion, a never-sent-back target);
  * every requirement's BLOCK path, each named in the detail;
  * the fail-direction table: a declared rebuild with an unreadable read BLOCKS, an
    undeclared PR with an unreadable read is ``could not check`` and never blocks,
    and the shared merge deadline's error is mapped and never propagates;
  * the override sigil on the real merge arm, and that the report row and the merge
    arm agree.

Network-free throughout via the ``_TEST_GH_*`` seams. The report and merge-arm
cases reuse the characterization corpus's harness (one guard module instance).
"""

from __future__ import annotations

import json
import time

import pytest

from tests.test_hooks import test_merge_gate_characterization as ch

# The characterization module's autouse fixtures, re-exported so they apply here:
# merge-deadline reset, the hermetic changed-files read, and the canonical-repo pin.
from tests.test_hooks.test_merge_gate_characterization import (  # noqa: F401
    _hermetic_pr_files,
    _pin_canonical_public_repo,
    _reset_merge_deadline,
)

G = ch._mod
HEAD = ch.HEAD
REPO = ch.REPO

OLD_HEAD = "1" * 40  # head of the sent-back PR #10
OTHER_HEAD = "2" * 40
CREATED = "2026-01-10T00:00:00Z"  # this PR (#20) was opened then
BEFORE = "2026-01-09T00:00:00Z"
AFTER = "2026-01-11T00:00:00Z"

_SECTION = (
    "## Rework\n"
    "Replaces: #10\n"
    "Split: none, one PR carries the whole rebuild\n"
    "Deviations: none\n"
    "Questions answered: the spec's two open questions, answered in the design notes\n"
)
_BODY = "Rebuild of the sent-back change.\n\n" + _SECTION + "\n## Testing\nUnit tests.\n"


def _sent_back(*rows: tuple[int, str, str]) -> str:
    return "\n".join(json.dumps({"number": n, "head": h, "state": s}) for n, h, s in rows)


def _ack(
    created: str = BEFORE,
    *,
    login: str = "maintainer",
    association: str = "OWNER",
    updated: str | None = None,
    body: str = "## Rework acknowledgement\nI read the spec and will rebuild it.",
) -> dict:
    row = {"login": login, "association": association, "body": body, "created": created}
    if updated is not None:
        row["updated"] = updated  # never read: the gate judges by creation time
    return row


@pytest.fixture
def rebuild(monkeypatch):
    """Seed the canonical declared rebuild: #20 replaces sent-back #10, valid ack,
    full section, commits read at HEAD. Each test changes ONE thing."""

    def seed(
        *,
        body: str = _BODY,
        acks=None,
        sent_back: str | None = None,
        timeline: dict | None = None,
        commits: tuple[str, ...] = (HEAD,),
    ):
        monkeypatch.setenv("_TEST_GH_PR_BODY", body)
        monkeypatch.setenv("_TEST_GH_PR_CREATED_AT", CREATED)
        monkeypatch.setenv(
            "_TEST_GH_REWORK_SENT_BACK",
            _sent_back((10, OLD_HEAD, "CLOSED")) if sent_back is None else sent_back,
        )
        monkeypatch.setenv(
            "_TEST_GH_REWORK_ACK", json.dumps({"10": [_ack()] if acks is None else acks})
        )
        monkeypatch.setenv("_TEST_GH_REWORK_TIMELINE", json.dumps(timeline or {}))
        monkeypatch.setenv(
            "_TEST_GH_PR_COMMITS",
            "\n".join(json.dumps({"sha": c, "parents": 1}) for c in commits),
        )

    return seed


def _check(**kw):
    return G._check_rework("20", REPO, **kw)


# ── 1-15: classification and requirements ───────────────────────────


def test_01_not_a_rebuild_is_na(rebuild):
    rebuild(body="An ordinary change.\n", sent_back=_sent_back((10, OLD_HEAD, "OPEN")))
    state, msg = _check()
    assert state == G.REWORK_NA, msg


def test_02_declared_rebuild_with_ack_and_section_is_ok(rebuild):
    rebuild()
    state, msg = _check()
    assert state == G.REWORK_OK, msg
    assert "#10" in msg.splitlines()[0]


def test_03_missing_rework_heading_blocks(rebuild):
    rebuild(body="Replaces #10\nSplit: x\nDeviations: none\nQuestions answered: y\n")
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "no `## Rework` heading" in msg


def test_04_missing_split_line_blocks(rebuild):
    rebuild(body=_BODY.replace("Split: none, one PR carries the whole rebuild\n", ""))
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "no `Split:` line" in msg


def test_05_empty_deviations_line_blocks(rebuild):
    rebuild(body=_BODY.replace("Deviations: none", "Deviations:"))
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "`Deviations:` line is empty" in msg


def test_06_no_ack_comment_blocks(rebuild):
    rebuild(acks=[{"login": "x", "association": "OWNER", "body": "LGTM", "created": BEFORE}])
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "PR #10 has no `## Rework acknowledgement` comment" in msg


def test_07_ack_created_after_this_pr_blocks(rebuild):
    rebuild(acks=[_ack(AFTER)])
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "created after this PR was opened" in msg


def test_08_ack_created_before_and_edited_after_counts_by_creation(rebuild):
    rebuild(acks=[_ack(BEFORE, updated=AFTER)])
    state, msg = _check()
    assert state == G.REWORK_OK, msg


def test_09_ack_from_a_non_maintainer_blocks(rebuild):
    rebuild(acks=[_ack(login="drive-by", association="NONE")])
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "not from a maintainer" in msg


def test_10_ack_from_the_allowlisted_bot_counts(rebuild):
    rebuild(acks=[_ack(login="devin-ai-integration[bot]", association="NONE")])
    state, msg = _check()
    assert state == G.REWORK_OK, msg


_CONTAINED_BODY = _BODY.replace("Replaces: #10", "Replaces: the earlier sent-back attempt")


def test_11_containment_only_with_ack_is_ok(rebuild):
    rebuild(body=_CONTAINED_BODY, commits=(OLD_HEAD, HEAD))
    assert not G._rework_declared_refs(_CONTAINED_BODY, "20", REPO), "fixture declares"
    state, msg = _check(head=HEAD)
    assert state == G.REWORK_OK, msg
    assert "#10" in msg


def test_11b_containment_only_without_ack_blocks(rebuild):
    rebuild(body=_CONTAINED_BODY, commits=(OLD_HEAD, HEAD), acks=[])
    state, msg = _check(head=HEAD)
    assert state == G.REWORK_BLOCK
    assert "PR #10 has no `## Rework acknowledgement`" in msg


def test_12_self_exclusion_a_sent_back_pr_is_not_its_own_rebuild(rebuild, monkeypatch):
    # #20 is itself labelled needs-rework, and its commits contain its own head.
    rebuild(body="Fixes the findings.\n", sent_back=_sent_back((20, HEAD, "OPEN")))
    state, msg = _check(head=HEAD)
    assert state == G.REWORK_NA, msg


def test_13_declared_replacement_of_a_never_sent_back_pr_is_na(rebuild):
    rebuild(
        body="**Replaces #10 and #11.**\n",
        sent_back=_sent_back((30, OTHER_HEAD, "OPEN")),
        timeline={
            "10": [{"event": "labeled", "label": "documentation"}],
            "11": [],
        },
    )
    state, msg = _check(head=HEAD)
    assert state == G.REWORK_NA, msg


def test_14_timeline_only_sent_back_pr_counts_when_declared(rebuild):
    # Label since removed: #10 is not in the current list, but its timeline holds it.
    rebuild(
        sent_back="",
        timeline={"10": [{"event": "labeled", "label": "needs-rework"}]},
    )
    state, msg = _check()
    assert state == G.REWORK_OK, msg
    assert "#10" in msg.splitlines()[0]


def test_14b_timeline_only_sent_back_but_later_merged_is_na(rebuild):
    rebuild(
        sent_back="",
        timeline={
            "10": [
                {"event": "labeled", "label": "needs-rework"},
                {"event": "merged", "label": None},
            ]
        },
    )
    state, msg = _check()
    assert state == G.REWORK_NA, msg


def test_15_url_form_declaration_is_detected(rebuild):
    body = _BODY.replace("Replaces: #10", "Replaces: see below") + (
        "\nSupersedes https://github.com/o/r/pull/10\n"
    )
    assert G._rework_declared_refs(body, "20", None) == {10}
    rebuild(body=body)
    state, msg = G._check_rework("20", None)
    assert state == G.REWORK_OK, msg


def test_15b_url_into_another_repo_is_not_a_declaration(rebuild):
    body = "Supersedes https://github.com/someone/else/pull/10\n"
    assert G._rework_declared_refs(body, "20", "o/r") == set()


@pytest.mark.parametrize(
    "line, refs",
    [
        # The live split-rebuild shape the first regex missed (review finding).
        ("Replaces the configuration concern in #2892; specification #2887.", {2892}),
        ("Replaces PR #10", {10}),
        ("Supersedes `#10`", {10}),
        ("Replaces #10 / #11", {10, 11}),
        ("replaces #10 & #11", {10, 11}),
        ("**Replaces #1930 and #1931.**", {1930, 1931}),
        ("Supersedes #2819 and #2832 after their terminal review", {2819, 2832}),
        # Not declarations: no reference on the verb's line, or the verb inside a word.
        ("This PR replaces all six walks with one scanner", set()),
        ("`supersedes` was passed\n#10 is unrelated", set()),
        ("the supersede_outcome #10", set()),
    ],
)
def test_15c_declaration_shapes(line, refs):
    assert G._rework_declared_refs(line, "20", None) == refs


# ── 16-18: the fail-direction table ──────────────────────────────────


def test_16_unreadable_sent_back_list_without_declaration_is_could_not_check(
    rebuild, monkeypatch, capsys
):
    rebuild(body="An ordinary change.\n", sent_back="__error__")
    state, msg = _check()
    assert state == G.REWORK_UNCHECKED, msg
    # And on the report: the row says so, and the verdict is untouched.
    ch._report_env(monkeypatch, scheduled=ch._scheduled_marker(HEAD))
    monkeypatch.setenv("_TEST_GH_REWORK_SENT_BACK", "__error__")
    rc = G.check_pr_report("20", repo=REPO)
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if ln.startswith("rework"))
    assert "could not check" in row, row
    assert rc == 0, out


def test_17_declared_with_unreadable_ack_comments_blocks(rebuild):
    rebuild(acks="__error__")
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "could not verify" in msg and "PR #10's comments" in msg


def test_17b_declared_with_unreadable_timeline_blocks(rebuild):
    rebuild(sent_back="", timeline={})  # #10 unseeded → its timeline is unreadable
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "could not verify" in msg and "timeline" in msg


def _no_subprocess(monkeypatch):
    calls: list = []

    def refuse(*a, **k):  # noqa: ANN002, ANN003
        calls.append(a)
        raise AssertionError("a gh call started after the deadline passed")

    monkeypatch.setattr(G.subprocess, "run", refuse)
    return calls


@pytest.mark.parametrize(
    "body, expected",
    [(_BODY, "block"), ("An ordinary change.\n", "could-not-check")],
    ids=["declared-blocks", "undeclared-could-not-check"],
)
def test_18_deadline_error_inside_a_read_is_mapped_and_never_propagates(
    rebuild, monkeypatch, body, expected
):
    rebuild(body=body)
    # The sent-back list goes to gh: no seam, and the deadline is already spent,
    # so `_gh_timeout` raises its RuntimeError before any process starts.
    monkeypatch.delenv("_TEST_GH_REWORK_SENT_BACK")
    calls = _no_subprocess(monkeypatch)
    G._merge_deadline = time.monotonic() - 1
    state, msg = _check()  # must not raise
    assert state == expected, msg
    assert "deadline" in msg
    assert calls == []


def test_18b_a_body_read_early_on_the_merge_path_decides_declared_after_the_deadline(
    rebuild, monkeypatch
):
    """Review finding: the rework gate runs LAST in the merge arm, so a fresh body
    read there can fail on a spent deadline and make a DECLARED rebuild look
    undeclared (advisory → merges). The body the earlier gates read while budget
    was left is kept for the invocation, and decides `declared`."""
    rebuild()
    monkeypatch.delenv("_TEST_GH_PR_BODY")
    monkeypatch.delenv("_TEST_GH_REWORK_SENT_BACK")
    G._MERGE_READ_CACHE.clear()
    import subprocess as _sp

    monkeypatch.setattr(
        G.subprocess,
        "run",
        lambda *a, **k: _sp.CompletedProcess(args=[], returncode=0, stdout=_BODY, stderr=""),
    )
    G._merge_deadline = time.monotonic() + 60  # an earlier gate reads with budget left
    assert G._pr_body_text("20", REPO) == _BODY
    calls = _no_subprocess(monkeypatch)
    G._merge_deadline = time.monotonic() - 1  # spent by the time `rework` runs
    try:
        state, msg = _check()
    finally:
        G._MERGE_READ_CACHE.clear()
    assert state == G.REWORK_BLOCK, msg
    assert "deadline" in msg
    assert calls == []


def test_18c_the_read_cache_is_off_on_the_report_path(monkeypatch):
    monkeypatch.delenv("_TEST_GH_PR_BODY")
    G._MERGE_READ_CACHE.clear()
    import subprocess as _sp

    monkeypatch.setattr(
        G.subprocess,
        "run",
        lambda *a, **k: _sp.CompletedProcess(args=[], returncode=0, stdout="b", stderr=""),
    )
    G._merge_deadline = None
    G._pr_body_text("20", REPO)
    assert G._MERGE_READ_CACHE == {}


def test_18d_commit_list_read_at_another_head_blocks_a_declared_rebuild(rebuild):
    rebuild(sent_back=_sent_back((10, OLD_HEAD, "CLOSED"), (30, OTHER_HEAD, "OPEN")))
    state, msg = _check(head="f" * 40)
    assert state == G.REWORK_BLOCK
    assert "verified head" in msg


def test_18e_a_saturated_sent_back_list_is_could_not_check(monkeypatch, rebuild):
    rebuild(body="An ordinary change.\n")
    monkeypatch.delenv("_TEST_GH_REWORK_SENT_BACK")
    import subprocess as _sp

    many = "\n".join(
        json.dumps({"number": n, "head": "a" * 40, "state": "OPEN"}) for n in range(1000, 1500)
    )
    monkeypatch.setattr(
        G.subprocess,
        "run",
        lambda *a, **k: _sp.CompletedProcess(args=[], returncode=0, stdout=many, stderr=""),
    )
    state, msg = _check()
    assert state == G.REWORK_UNCHECKED, msg
    assert "500-row limit" in msg


def test_18f_unreadable_created_at_blocks_a_rebuild(rebuild, monkeypatch):
    rebuild()
    monkeypatch.delenv("_TEST_GH_PR_CREATED_AT")
    monkeypatch.setattr(
        G.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no gh"))
    )
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "creation time could not be read" in msg


def test_18g_a_declared_ref_in_the_merged_set_is_not_sent_back(rebuild):
    rebuild(sent_back=_sent_back((10, OLD_HEAD, "MERGED")))
    state, msg = _check()
    assert state == G.REWORK_NA, msg


def test_18h_crlf_body_parses(rebuild):
    rebuild(body=_BODY.replace("\n", "\r\n"))
    state, msg = _check()
    assert state == G.REWORK_OK, msg


# ── 19-20: the merge arm, the override, and report/merge agreement ───


def _seed_merge(monkeypatch, *, acks):
    monkeypatch.setenv("_TEST_GH_REWORK_SENT_BACK", _sent_back((10, OLD_HEAD, "CLOSED")))
    monkeypatch.setenv("_TEST_GH_REWORK_ACK", json.dumps({"10": acks}))
    monkeypatch.setenv("_TEST_GH_REWORK_TIMELINE", "{}")
    monkeypatch.setenv("_TEST_GH_PR_COMMITS", json.dumps({"sha": HEAD, "parents": 1}))


def test_19_rework_override_passes_the_merge_arm_and_is_logged(monkeypatch, tmp_path, capsys):
    log = tmp_path / "merge_overrides"
    monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_DIR", str(log))
    _seed_merge(monkeypatch, acks=[])  # blocked rebuild: no acknowledgement
    blocked = ch._run(monkeypatch, ch._merge_cmd(pr="20"), pr_body=_BODY, created_at=CREATED)
    assert blocked == 2, capsys.readouterr().err
    rc = ch._run(
        monkeypatch,
        ch._merge_cmd(pr="20", trailer="# rework-override"),
        pr_body=_BODY,
        created_at=CREATED,
    )
    err = capsys.readouterr().err
    assert rc == 0, err
    rows = [
        json.loads(line)
        for f in sorted(log.glob("*.jsonl"))
        for line in f.read_text().splitlines()
        if line.strip()
    ]
    row = next(r for r in rows if r["sigil"] == "rework-override")
    assert row["waived"] == "rework-contract"
    assert row["pr"] == "20"
    assert row["outcome"] == "allowed"


@pytest.mark.parametrize(
    "acks, report_state, merge_rc",
    [([], "BLOCK", 2), ([_ack()], "ok", 0)],
    ids=["blocked-rebuild", "complete-rebuild"],
)
def test_20_report_and_merge_arm_agree(monkeypatch, capsys, acks, report_state, merge_rc):
    _seed_merge(monkeypatch, acks=acks)
    rc = ch._run(monkeypatch, ch._merge_cmd(pr="20"), pr_body=_BODY, created_at=CREATED)
    err = capsys.readouterr().err
    assert rc == merge_rc, err
    if merge_rc == 2:
        assert "rework contract unmet" in err

    # The report, under the same rework seams and every other gate green.
    monkeypatch.setattr(G.subprocess, "run", ch._router())
    ch._report_env(monkeypatch, scheduled=ch._scheduled_marker(HEAD))
    _seed_merge(monkeypatch, acks=acks)
    report_rc = G.check_pr_report("20", repo=REPO)
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if ln.startswith("rework"))
    assert row.startswith("rework         : " + report_state), row
    assert (report_rc != 0) == (merge_rc != 0), out


def test_21_a_value_written_under_its_field_counts():
    """A bullet list under `Deviations:` is a value, not an empty field."""
    section = (
        "## Rework\nReplaces: #10\nSplit: PR 1 of 2\nDeviations:\n"
        "- the slice check moved to enable, because the CLI owns the unit lifecycle\n"
        "Questions answered:\n  - none were delegated\n"
    )
    assert G._rework_section_problems(section) == []


def test_21b_a_field_followed_directly_by_the_next_field_is_still_empty():
    section = "## Rework\nReplaces: #10\nSplit: x\nDeviations:\n\nQuestions answered: y\n"
    assert G._rework_section_problems(section) == ["the `## Rework` section's `Deviations:` line is empty"]


def test_22_a_heading_with_trailing_text_is_the_section():
    section = _SECTION.replace("## Rework\n", "## Rework (replaces #10)\n")
    assert G._rework_section_problems(section) == []


def test_22b_the_acknowledgement_heading_is_not_the_section():
    body = "## Rework acknowledgement\nReplaces: #10\nSplit: x\nDeviations: none\nQuestions answered: y\n"
    assert G._rework_section_problems(body) == ["the PR body has no `## Rework` heading"]
