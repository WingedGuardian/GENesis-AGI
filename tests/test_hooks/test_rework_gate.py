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
    rebuild(body="Replaces: #10\nSplit: x\nDeviations: none\nQuestions answered: y\n")
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


def test_15_url_form_declaration_is_detected(rebuild, monkeypatch):
    """With no ``repo`` given, a URL counts when it names the repo gh resolves."""
    monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "o/r")
    body = _BODY.replace("Replaces: #10", "Replaces: see below") + (
        "\nSupersedes: https://github.com/o/r/pull/10\n"
    )
    assert G._rework_declared_refs(body, "20", None) == {10}
    rebuild(body=body)
    state, msg = G._check_rework("20", None)
    assert state == G.REWORK_OK, msg


def test_15b_url_into_another_repo_is_not_a_declaration(rebuild):
    body = "Supersedes: https://github.com/someone/else/pull/10\n"
    assert G._rework_declared_refs(body, "20", "o/r") == set()


def test_15b2_url_with_no_resolvable_repo_yields_no_refs(monkeypatch):
    """No ``repo`` and none resolvable from the cwd: a URL could name any repo, so it
    adds no reference. At the gate it is still a declaration that cannot be
    verified, and blocks (test_27)."""
    monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "")
    body = "Supersedes: https://github.com/other/project/pull/10\n"
    assert G._rework_declared_refs(body, "20", None) == set()


def test_15b3_url_into_another_repo_with_derived_repo_is_not_a_declaration(monkeypatch):
    monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "owner/project")
    body = "Supersedes: https://github.com/other/project/pull/10\nReplaces: #11\n"
    assert G._rework_declared_refs(body, "20", None) == {11}


def test_15d_a_long_whitespace_run_after_a_reference_is_linear():
    """Review finding: overlapping whitespace separators backtracked quadratically."""
    body = "Replaces: #10" + " " * 60000 + "x"
    started = time.monotonic()
    assert G._rework_declared_refs(body, "20", "o/r") == {10}
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    "line, refs",
    [
        # Structured fields declare (owner ruling: never prose).
        ("Replaces: #10", {10}),
        ("replaces: #10", {10}),
        ("Supersedes: #10, #11", {10, 11}),
        ("- Replaces: #10 / #11", {10, 11}),
        ("**Replaces:** #1930 and #1931.", {1930, 1931}),
        ("__Supersedes__: `#10`", {10}),
        ("Replaces: https://github.com/o/r/pull/10 and #11", {10, 11}),
        ("Replaces: #2893 in part (absorbs #2819 and #2832)", {2893, 2819, 2832}),
        ("  Replaces: #10", {10}),
        ("## Rework\nReplaces: #10\nSplit: x", {10}),
        # Prose never declares, however it is phrased (review findings).
        ("This PR never supersedes #11", set()),
        ("Replaces #10", set()),
        ("**Replaces #10**, **#11**", set()),
        ("Replaces the configuration concern in #2892", set()),
        ("This replaces the cache. Follow-up to #10", set()),
        ("Not replacing anything. Replaces nothing in #10", set()),
        # A field on another line is not reached from prose; indented code is not a field.
        ("replaces the cache\n#10 is unrelated", set()),
        ("    Replaces: #10", set()),
        ("Replaces: none", set()),
    ],
)
def test_15c_declaration_shapes(line, refs, monkeypatch):
    monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "o/r")
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


def test_21c_another_label_line_does_not_fill_an_empty_field():
    """Review finding: `Testing: pytest` under an empty `Deviations:` filled it."""
    section = (
        "## Rework\nReplaces: #10\nSplit: x\nDeviations:\nTesting: pytest\n"
        "Questions answered: y\n"
    )
    assert G._rework_section_problems(section) == ["the `## Rework` section's `Deviations:` line is empty"]


def test_21c2_the_templates_kept_line_does_not_fill_an_empty_split():
    section = (
        "## Rework\nReplaces: #10\nSplit:\n"
        "Kept / deleted / reshaped as the spec asked: all kept\n"
        "Deviations: none\nQuestions answered: y\n"
    )
    assert G._rework_section_problems(section) == ["the `## Rework` section's `Split:` line is empty"]


def test_21d_an_unindented_plain_value_under_a_field_still_counts():
    section = "## Rework\nReplaces: #10\nSplit: x\nDeviations:\nnone\nQuestions answered: y\n"
    assert G._rework_section_problems(section) == []


def test_21e_the_bot_allowlist_comes_from_the_reviewer_registry(monkeypatch):
    """No login is hard-coded: the registry's devin-marker logins are the allowlist,
    and an unimportable registry allows no bot."""
    assert G._rework_ack_bot_logins() == G.enforced_logins()["devin-marker"]

    def _broken():
        raise RuntimeError("unimportable")

    monkeypatch.setattr(G, "enforced_logins", _broken)
    assert G._rework_ack_bot_logins() == frozenset()


def test_22_a_heading_with_trailing_text_is_the_section():
    section = _SECTION.replace("## Rework\n", "## Rework (replaces #10)\n")
    assert G._rework_section_problems(section) == []


def test_22b_the_acknowledgement_heading_is_not_the_section():
    body = "## Rework acknowledgement\nReplaces: #10\nSplit: x\nDeviations: none\nQuestions answered: y\n"
    assert G._rework_section_problems(body) == ["the PR body has no `## Rework` heading"]


def test_23_a_referenced_issue_is_not_a_sent_back_pr(rebuild):
    """Review finding: the timeline endpoint serves issues too, so a labelled ISSUE
    must not make the PR that names it a rebuild."""
    body = _BODY.replace("Replaces: #10", "Replaces: #30")
    rebuild(body=body, timeline={"30": "__issue__"})
    state, msg = G._check_rework("20", REPO)
    assert state == G.REWORK_NA, msg


def test_24_merged_dominates_a_duplicate_label_row(monkeypatch):
    """Review finding: a PR merged between the two label reads arrives CLOSED then
    MERGED, and must not be treated as sent back."""
    monkeypatch.setenv(
        "_TEST_GH_REWORK_SENT_BACK",
        "\n".join(
            json.dumps(r)
            for r in (
                {"number": 10, "head": OLD_HEAD, "state": "CLOSED"},
                {"number": 10, "head": OLD_HEAD, "state": "MERGED"},
            )
        ),
    )
    sent_back, merged, why = G._rework_sent_back(REPO)
    assert why == ""
    assert 10 not in sent_back
    assert merged == {10}


def test_25_a_timeline_only_rebuild_of_an_open_pr_gets_the_note(rebuild, monkeypatch):
    """Review finding: a PR whose label was removed is found through its timeline,
    and its OPEN state must still produce the close-it note."""
    body = _BODY.replace("Replaces: #10", "Replaces: #31")
    rebuild(
        body=body,
        timeline={"31": {"state": "OPEN", "events": [{"event": "labeled", "label": "needs-rework"}]}},
    )
    monkeypatch.setenv("_TEST_GH_REWORK_ACK", json.dumps({"31": [_ack(created=BEFORE)]}))
    state, msg = G._check_rework("20", REPO)
    assert state == G.REWORK_OK, msg
    assert "PR #31 is still open" in msg


def test_26_a_replaces_value_on_the_line_below_declares(rebuild):
    """Review finding B1: the section check accepts a value written under its
    field, so the declaration reader must read it too."""
    body = (
        "## Rework\n- Replaces:\n  - #10\n- Split: none\n- Deviations: none\n"
        "- Questions answered: all\n"
    )
    assert G._rework_declared_refs(body, "20", REPO) == {10}
    rebuild(body=body, acks=[])
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "PR #10 has no `## Rework acknowledgement`" in msg


def test_27_an_unresolvable_declared_url_blocks(rebuild, monkeypatch):
    """Review finding S1: a declared URL whose repository cannot be resolved makes
    the PR a declared rebuild that cannot be verified, never an undeclared one."""
    monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "")
    rebuild(body=_BODY.replace("Replaces: #10", "Replaces: https://github.com/o/r/pull/10"))
    state, msg = G._check_rework("20", None)
    assert state == G.REWORK_BLOCK
    assert "could not verify" in msg
# ── 28-35: round-1 review findings (rendered text, exact heading, bounds) ──


_FENCED = "Docs example:\n```\nReplaces: #10\n```\n"


def test_28_a_fenced_declaration_is_not_a_declaration(rebuild):
    """Review finding (Codex, Devin, GLM): a quoted template in a fence blocked an
    ordinary PR as a rebuild of the PR it named."""
    assert G._rework_declared_refs(_FENCED, "20", REPO) == set()
    assert G._rework_declared_refs("~~~~\nReplaces: #10\n~~~~\n", "20", REPO) == set()
    rebuild(body=_FENCED)
    state, msg = _check()
    assert state == G.REWORK_NA, msg


def test_28b_an_unclosed_fence_hides_the_rest_of_the_body():
    assert G._rework_declared_refs("```\nReplaces: #10\n", "20", REPO) == set()


def test_28c_a_fenced_section_does_not_satisfy_the_check():
    body = "```\n" + _SECTION + "```\n"
    assert G._rework_section_problems(body) == ["the PR body has no `## Rework` heading"]


def test_29_an_html_comment_neither_declares_nor_fills_a_field():
    """Review finding (Codex): an unfilled template comment counted as a value."""
    assert G._rework_declared_refs("<!-- Replaces: #10 -->\n", "20", REPO) == set()
    section = _SECTION.replace(
        "Split: none, one PR carries the whole rebuild", "Split:\n<!-- one line per PR -->"
    )
    assert G._rework_section_problems(section) == [
        "the `## Rework` section's `Split:` line is empty"
    ]


def test_30_a_level_three_heading_ends_the_section():
    """Review finding (Devin, GLM): a later subsection filled a missing field."""
    kept = [ln for ln in _SECTION.splitlines() if not ln.startswith("Questions answered")]
    body = "\n".join(kept) + "\n### Notes\nQuestions answered: unrelated\n"
    assert G._rework_section_problems(body) == [
        "the `## Rework` section has no `Questions answered:` line"
    ]


def test_31_a_hyphenated_acknowledgement_heading_is_not_the_section():
    body = _SECTION.replace("## Rework\n", "## Rework-acknowledgement\n")
    assert G._rework_section_problems(body) == ["the PR body has no `## Rework` heading"]


@pytest.mark.parametrize(
    "body",
    [
        "## Rework acknowledgement needed\nPlease acknowledge first.",
        "## Rework acknowledgements outstanding",
        "```\n## Rework acknowledgement\n```",
    ],
)
def test_32_only_the_exact_heading_is_an_acknowledgement(rebuild, body):
    """Review finding (Codex): a prefix match accepted an instruction heading."""
    rebuild(acks=[_ack(body=body)])
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "PR #10 has no `## Rework acknowledgement` comment" in msg


def test_32b_the_heading_is_matched_after_a_leading_comment_and_extra_spaces(rebuild):
    rebuild(acks=[_ack(body="<!-- bot -->\n##  Rework   Acknowledgement \nRead it.")])
    state, msg = _check()
    assert state == G.REWORK_OK, msg


def test_33_a_host_qualified_report_repo_still_matches_a_url():
    """Review finding (Codex): `-R github.com/o/r` silently ignored a URL that the
    merge arm, which normalizes, would have counted."""
    body = "Replaces: https://github.com/o/r/pull/7\n"
    assert G._rework_declarations(body, "20", "github.com/o/r") == ({7}, [])
    assert G._rework_declarations(body, "20", "https://github.com/O/R") == ({7}, [])


def test_33b_an_unnormalizable_repo_cannot_check_a_url():
    refs, problems = G._rework_declarations(
        "Replaces: https://github.com/o/r/pull/7\n", "20", "ghe.example/o/r"
    )
    assert refs == set()
    assert problems


def test_34_too_many_declared_prs_block_without_reading_them(rebuild, monkeypatch):
    """Review finding (Codex): each declared PR costs reads on a path with no shared
    deadline, so the count is bounded."""
    n = G._REWORK_MAX_DECLARED + 1
    refs = " ".join(f"#{100 + i}" for i in range(n))
    rebuild(body=_BODY.replace("Replaces: #10", f"Replaces: {refs}"))
    monkeypatch.setattr(G, "_rework_timeline", lambda *a: pytest.fail("read a timeline"))
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert f"declares {n} replaced PRs" in msg


def test_35_the_first_unverifiable_reference_stops_the_reads(rebuild, monkeypatch):
    calls = []

    def timeline(num, repo):
        calls.append(num)
        return None, f"PR #{num} timeline could not be read", ""

    rebuild(body=_BODY.replace("Replaces: #10", "Replaces: #40 #41 #42"))
    monkeypatch.setattr(G, "_rework_timeline", timeline)
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert calls == [40]
# ── 36-40: round-2 review findings ──


@pytest.mark.parametrize(
    "body",
    [
        "Example:\n\n    Replaces: #10\n",
        "\tReplaces: #10\n",
        "## Notes\n    Replaces: #10\n",
    ],
)
def test_36_indented_code_does_not_declare(body):
    """Review finding (Codex r2): an indented code block renders as code, so a
    quoted template there is not a declaration."""
    assert G._rework_declared_refs(body, "20", REPO) == set()


def test_36b_indented_fields_under_the_heading_do_not_fill_the_section():
    body = "## Rework\n" + "".join(f"    {ln}\n" for ln in _SECTION.splitlines()[1:])
    assert len(G._rework_section_problems(body)) == 4


def test_36c_a_value_indented_under_a_list_item_is_still_read():
    body = (
        "## Rework\n- Replaces:\n    - #10\n- Split: none\n- Deviations: none\n"
        "- Questions answered: all\n"
    )
    assert G._rework_declared_refs(body, "20", REPO) == {10}
    assert G._rework_section_problems(body) == []


def test_36d_an_indented_acknowledgement_is_code(rebuild):
    rebuild(acks=[_ack(body="    ## Rework acknowledgement\nRead it.")])
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "PR #10 has no `## Rework acknowledgement` comment" in msg


def test_37_a_containment_rebuild_blocks_when_a_later_read_raises(rebuild, monkeypatch):
    """Review finding (Codex r2, P1): a rebuild found by containment alone declared
    nothing, so a deadline error in the acknowledgement read came back advisory."""
    rebuild(body="An ordinary change.\n", commits=(OLD_HEAD, HEAD))

    def boom(num, repo):
        raise RuntimeError("merge-gate deadline exceeded")

    monkeypatch.setattr(G, "_rework_ack_comments", boom)
    state, msg = _check()
    assert state == G.REWORK_BLOCK, msg
    assert "could not verify" in msg


def test_38_a_host_qualified_repo_reaches_the_api_normalized(rebuild, monkeypatch):
    """Review finding (Codex r2): `-R github.com/o/r` built REST paths such as
    `repos/github.com/o/r/...`."""
    seen = []
    real = G._rework_ack_comments

    def spy(num, repo):
        seen.append(repo)
        return real(num, repo)

    monkeypatch.setattr(G, "_rework_ack_comments", spy)
    rebuild()
    state, msg = G._check_rework("20", "github.com/" + REPO)
    assert state == G.REWORK_OK, msg
    assert seen == [REPO]


def test_39_a_mixed_case_url_still_declares():
    """Review finding (Codex r2): scheme and host are case-insensitive."""
    body = "Replaces: HTTPS://GitHub.com/o/r/pull/10\n"
    assert G._rework_declared_refs(body, "20", "o/r") == {10}
# ── 40-52: the body is read as CommonMark (markdown-it-py), class audit cases ──

from pathlib import Path  # noqa: E402

_TEMPLATE = (
    Path(__file__).resolve().parents[2] / ".github" / "PULL_REQUEST_TEMPLATE.md"
).read_text()


def test_40_the_repo_pr_template_does_not_hide_the_section(rebuild):
    """Class audit (P1): the template's indented HTML comment once swallowed the
    rest of the body, so a real rebuild read as n/a."""
    body = _TEMPLATE + "\n" + _SECTION
    assert G._rework_declared_refs(body, "20", REPO) == {10}
    assert G._rework_section_problems(body) == []
    rebuild(body=body, acks=[])
    state, msg = _check()
    assert state == G.REWORK_BLOCK
    assert "PR #10 has no `## Rework acknowledgement`" in msg


@pytest.mark.parametrize(
    "body",
    [
        "Strips `<!--` markers.\n\nReplaces: #10\n",
        "<!--\n```\n-->\nReplaces: #10\n",
        "```x``` is inline\n\nReplaces: #10\n",
        "1. Replaces: #10\n",
    ],
)
def test_41_rendered_declarations_are_read(body):
    """Class audit: a stray `<!--` in a code span, a fence inside a comment, an
    inline triple-backtick span, and an ordered list item all render the field."""
    assert G._rework_declared_refs(body, "20", REPO) == {10}


@pytest.mark.parametrize(
    "body",
    [
        "> quoted\nReplaces: #10\n",
        "> Replaces: #10\n",
        "- a\n\n\t\t\tReplaces: #10\n",
        "<div>\nReplaces: #10\n</div>\n",
    ],
)
def test_42_quoted_or_code_or_html_text_does_not_declare(body):
    """Class audit: a lazy blockquote continuation, a quote, code nested in a list
    item, and a raw HTML block are not the author's field."""
    assert G._rework_declared_refs(body, "20", REPO) == set()


def test_43_prose_after_the_last_field_is_not_a_value():
    body = _SECTION.replace("Replaces: #10", "Replaces: #30") + "\nThis also touches #10.\n"
    assert G._rework_declared_refs(body, "20", REPO) == {30}


def test_43b_later_prose_does_not_fill_an_empty_last_field():
    body = (
        _SECTION.replace(
            "Questions answered: the spec's two open questions, answered in the design notes",
            "Questions answered:",
        )
        + "\nThanks for reviewing.\n"
    )
    assert G._rework_section_problems(body) == [
        "the `## Rework` section's `Questions answered:` line is empty"
    ]


def test_44_an_unbulleted_url_under_the_field_is_its_value():
    body = _SECTION.replace("Replaces: #10", "Replaces:\nhttps://github.com/o/r/pull/10")
    assert G._rework_declared_refs(body, "20", "o/r") == {10}
    assert G._rework_section_problems(body) == []


def test_44b_a_list_directly_under_a_top_level_field_is_its_value():
    body = _SECTION.replace("Replaces: #10", "Replaces:\n\n- #10\n- #11\n")
    assert G._rework_declared_refs(body, "20", REPO) == {10, 11}


def test_45_setext_headings_are_headings():
    """Class audit: a setext `Rework` heading is the section, and a setext
    subsection ends it."""
    section = _SECTION.replace("## Rework\n", "Rework\n------\n")
    assert G._rework_section_problems(section) == []
    kept = [ln for ln in _SECTION.splitlines() if not ln.startswith("Deviations")]
    body = "\n".join(kept) + "\n\nNotes\n-----\nDeviations: from notes\n"
    assert G._rework_section_problems(body) == ["the `## Rework` section has no `Deviations:` line"]


def test_45b_a_quote_under_a_field_is_not_its_value():
    body = _SECTION.replace("Replaces: #10", "Replaces: #30\n\n> earlier draft said #10\n")
    assert 10 not in G._rework_declared_refs(body, "20", REPO)


@pytest.mark.parametrize(
    "body, is_ack",
    [
        ("## Rework acknowledgement ##\nRead it.", True),
        ("Rework acknowledgement\n----------------------\nRead it.", True),
        ("> ## Rework acknowledgement\nquoting the builder", False),
        ("Rework acknowledgement\n======================\nRead it.", False),
    ],
)
def test_46_acknowledgement_heading_forms(body, is_ack):
    assert G._rework_is_ack(body) is is_ack


@pytest.mark.parametrize(
    "value, refs",
    [
        ("other/repo#10", set()),
        ("https://example.com/doc#10", set()),
        ("see issue#10", set()),
        ("http://github.com/o/r/pull/10", {10}),
        ("#10, #11", {10, 11}),
    ],
)
def test_47_only_standalone_and_this_repo_references_count(value, refs):
    """Class audit: `#N` inside another token named another repository's PR or a
    URL fragment; `http://` URLs were missed."""
    assert G._rework_declared_refs(f"Replaces: {value}\n", "20", "o/r") == refs


@pytest.mark.parametrize("repo", ["o/r", "github.com/o/r", "https://github.com/o/r.git", "o/r.git"])
def test_48_report_repositories_normalize_for_rest_paths(repo):
    assert G._rework_repo(repo) == "o/r"
    assert G._rework_declarations("Replaces: https://github.com/o/r/pull/7\n", "20", repo) == (
        {7},
        [],
    )


def test_49_a_404_is_an_issue_only_when_the_issue_endpoint_says_so(monkeypatch):
    """Class audit: any 404 on the PR endpoint read as "an issue", so a wrong repo
    spelling made a declared reference "not sent back"."""
    import subprocess as sp

    def fake(argv, **kw):
        path = argv[2]
        if "/pulls/" in path:
            return sp.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
        return sp.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")

    monkeypatch.delenv("_TEST_GH_REWORK_TIMELINE", raising=False)
    monkeypatch.setattr(G.subprocess, "run", fake)
    was, why, _state = G._rework_timeline(10, "o/r")
    assert was is None and why

    def fake_issue(argv, **kw):
        if "/pulls/" in argv[2]:
            return sp.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
        return sp.CompletedProcess(argv, 0, "false\n", "")

    monkeypatch.setattr(G.subprocess, "run", fake_issue)
    assert G._rework_timeline(10, "o/r")[0] is False


def test_50_a_lagging_commit_list_still_proves_containment(rebuild):
    """Class audit: a commit list read at another head that already contains a
    sent-back head was discarded as could-not-check."""
    rebuild(body="An ordinary change.\n", commits=(OLD_HEAD, "c" * 40))
    state, msg = _check(head=HEAD)
    assert state == G.REWORK_BLOCK, msg
    assert "#10" in msg


def test_51_no_parser_reads_the_body_as_unreadable(rebuild, monkeypatch):
    """Without markdown-it-py an undeclared PR is could-not-check (advisory), and a
    rebuild found by containment still blocks."""
    monkeypatch.setattr(G, "_rework_markdown", lambda: None)
    rebuild(body=_BODY, commits=(HEAD,))
    state, msg = _check()
    assert state == G.REWORK_UNCHECKED, msg
    assert "markdown-it-py" in msg
    rebuild(body=_BODY, commits=(OLD_HEAD, HEAD))
    state, msg = _check()
    assert state == G.REWORK_BLOCK, msg
