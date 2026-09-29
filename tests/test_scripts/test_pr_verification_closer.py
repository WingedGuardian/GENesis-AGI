"""The validator's write path — ``scripts/pr_verification.py`` (issue #1718 half B).

The invariants, each one bought by a measured defect rather than imagined:

* ``--repo`` is CHECKED against the open set. Unchecked, an earlier draft closed a
  DIFFERENT repository's obligation with this PR's evidence at exit 0, permanently,
  leaving the real row open so nothing surfaced the mistake.
* The evidence document names the PR it verifies and the commit it checked, and a
  mismatch against ``--pr`` is refused. A closed row cannot be amended, so a
  mispasted document would be permanent and undetectable.
* A closing verdict is refused when any claim FAILED, and ``pass-mechanical`` is
  refused when any claim is ``NOT_VERIFIABLE_HERE`` — a row stamped a clean pass
  over a claim nothing established is a false record.
* A non-closing verdict REQUIRES a note and leaves the row OPEN; a closing verdict
  refuses a note rather than discarding it.
* Every refusal names which state it found, and the exit codes are distinct:
  1 the ledger declined, 2 the request was malformed, 3 refused on policy.

Install-agnostic: synthetic slugs, ``tmp_path`` databases built from the real
migrations, no network, no live services.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest

from tests.conftest import private_module
from tests.test_session_awareness.conftest import build_pr_verifications

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pr_verification.py"
_prv = private_module("pr_verification_under_test", _SCRIPT)

REPO = "owner/repo"
OTHER = "owner/fork"
NOW = "2026-09-20T11:00:00+00:00"


def _doc(**over) -> dict:
    doc = {
        "repo": REPO,
        "pr": 7,
        "merge_commit": "deadbeef1234",
        "deploy": {"method": "content", "detail": "present in the deployed file"},
        "claims": [
            {
                "claim": "the timer fires hourly",
                "verdict": "pass",
                "tier": "MEASURED",
                "measurement": "37/37 runs, median gap 60.0m, no gap >70m",
            }
        ],
        "controls": ["without the override the armed path is taken instead"],
        "scope_limits": [],
        "findings": [],
    }
    doc.update(over)
    return doc


def _doc_for(verdict: str, **over) -> dict:
    """A document whose CLAIMS match *verdict*, which the closer now requires.

    Rules 5 and 6 of :func:`assess` bind a non-closing verdict to the claim outcomes:
    ``fail-intent`` needs a failed claim, ``cannot-verify`` needs one nothing could
    establish. Deriving the document from the verdict keeps each test honest about
    what it exercises, instead of reaching for a passing fixture under a verdict its
    own contents contradict — which is the incoherence those two rules exist to stop.
    """
    if verdict == "fail-intent":
        over.setdefault(
            "claims",
            [
                {
                    "claim": "the timer fires hourly",
                    "verdict": "fail",
                    "tier": "MEASURED",
                    "measurement": "3 of 37 windows had no run at all",
                }
            ],
        )
    elif verdict == "cannot-verify":
        over.setdefault(
            "claims",
            [
                {
                    "claim": "it degrades cleanly where no graph engine is installed",
                    "verdict": "pass",
                    "tier": "NOT_VERIFIABLE_HERE",
                    "measurement": "this install has one; nothing here reaches that path",
                }
            ],
        )
    return _doc(**over)


def _seed(db_path: Path, *, repo: str = REPO, pr: int = 7) -> None:
    async def go() -> None:
        from genesis.db.crud import pr_verifications as crud

        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await build_pr_verifications(conn)
            await conn.commit()
            await crud.open_verification(
                conn, repo=repo, pr_number=pr, pr_title="t", merged_at=NOW, now=NOW
            )

    asyncio.run(go())


def _row(db_path: Path, *, repo: str = REPO, pr: int = 7) -> dict | None:
    async def go() -> dict | None:
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM pr_verifications WHERE repo = ? AND pr_number = ?", (repo, pr)
            )
            got = await cur.fetchone()
            return dict(got) if got else None

    return asyncio.run(go())


def _run(tmp_path: Path, doc: dict, *extra: str, pr: int = 7) -> int:
    ev = tmp_path / "evidence.json"
    ev.write_text(json.dumps(doc))
    return _prv.main(
        [
            "close",
            "--pr",
            str(pr),
            "--evidence-file",
            str(ev),
            "--db-path",
            str(tmp_path / "genesis.db"),
            *extra,
        ]
    )


# ── print-schema ─────────────────────────────────────────────────────────


def test_print_schema_emits_a_document_its_own_validator_shape_accepts(capsys):
    """The template is what an LLM caller will fill in, so its FIELD SET must match
    what the validator requires — otherwise the documented path leads straight to a
    refusal. Values are placeholders, so only the keys are asserted."""
    assert _prv.main(["print-schema"]) == 0
    tpl = json.loads(capsys.readouterr().out)
    assert set(tpl) >= {
        "pr",
        "merge_commit",
        "deploy",
        "claims",
        "controls",
        "scope_limits",
        "findings",
    }
    assert set(tpl["deploy"]) == {"method", "detail"}
    assert set(tpl["claims"][0]) == {"claim", "verdict", "tier", "measurement"}
    assert "repo" in tpl, (
        "the template is what a validator fills in, and repo is now REQUIRED — "
        "a template missing it hands them a document validate_evidence refuses"
    )
    assert set(tpl["findings"][0]) == {"summary", "disposition"}


# ── evidence shape ───────────────────────────────────────────────────────


def test_a_well_formed_document_validates():
    assert _prv.validate_evidence(_doc()) is not None


@pytest.mark.parametrize(
    "mutation, field",
    [
        ({"pr": "7"}, "evidence.pr"),
        ({"pr": True}, "evidence.pr"),
        ({"merge_commit": "  "}, "evidence.merge_commit"),
        ({"deploy": {"method": "vibes", "detail": "x"}}, "deploy.method"),
        ({"deploy": {"method": "content", "detail": " "}}, "deploy.detail"),
        ({"deploy": "nope"}, "evidence.deploy"),
        ({"claims": []}, "claims"),
        ({"claims": "nope"}, "claims"),
        ({"controls": "nope"}, "controls"),
        ({"scope_limits": {}}, "scope_limits"),
        ({"findings": "nope"}, "findings"),
    ],
)
def test_malformed_fields_are_refused_by_name(mutation, field):
    with pytest.raises(_prv.EvidenceError) as exc:
        _prv.validate_evidence(_doc(**mutation))
    assert field in str(exc.value)


def test_evidence_must_be_an_object():
    with pytest.raises(_prv.EvidenceError):
        _prv.validate_evidence(["not", "an", "object"])


@pytest.mark.parametrize("bad", ["measured", "PROBABLY", "", None])
def test_an_unrecognised_tier_is_refused(bad):
    doc = _doc()
    doc["claims"][0]["tier"] = bad
    with pytest.raises(_prv.EvidenceError, match="tier"):
        _prv.validate_evidence(doc)


@pytest.mark.parametrize("bad", ["ok", "PASS", "", None])
def test_an_unrecognised_claim_verdict_is_refused(bad):
    doc = _doc()
    doc["claims"][0]["verdict"] = bad
    with pytest.raises(_prv.EvidenceError, match="verdict"):
        _prv.validate_evidence(doc)


def test_a_non_dict_claim_is_refused_by_name():
    with pytest.raises(_prv.EvidenceError, match=r"claims\[0\]"):
        _prv.validate_evidence(_doc(claims=["just a string"]))


def test_a_non_dict_finding_is_refused_by_name():
    with pytest.raises(_prv.EvidenceError, match=r"findings\[0\]"):
        _prv.validate_evidence(_doc(findings=["just a string"]))


def test_an_undispositioned_finding_is_refused():
    """A finding with no disposition is a drop wearing a record."""
    with pytest.raises(_prv.EvidenceError, match="disposition"):
        _prv.validate_evidence(_doc(findings=[{"summary": "the message clips the target"}]))


def test_all_four_tiers_validate():
    for tier in _prv.TIERS:
        doc = _doc()
        doc["claims"][0]["tier"] = tier
        assert _prv.validate_evidence(doc) is not None


# ── repo resolution (the measured BLOCKER) ───────────────────────────────


def test_an_explicit_repo_not_in_the_open_set_is_refused():
    """THE regression test. An earlier draft returned `given` unchecked, which
    closed a different repository's obligation with this PR's evidence."""
    with pytest.raises(LookupError) as exc:
        _prv.resolve_repo([REPO], 7, OTHER)
    assert OTHER in str(exc.value)
    assert REPO in str(exc.value), "the refusal names the real candidates"


def test_an_explicit_repo_in_the_open_set_is_used():
    assert _prv.resolve_repo([REPO, OTHER], 7, OTHER) == OTHER


def test_an_ambiguous_pr_number_refuses_and_names_both():
    with pytest.raises(LookupError) as exc:
        _prv.resolve_repo([REPO, OTHER], 7, None)
    assert REPO in str(exc.value) and OTHER in str(exc.value) and "--repo" in str(exc.value)


def test_no_open_row_refuses_and_points_at_the_backlog_reader():
    with pytest.raises(LookupError) as exc:
        _prv.resolve_repo([], 7, None)
    assert "no OPEN row for PR #7" in str(exc.value)
    assert "--verification-backlog" in str(exc.value)


def test_a_single_open_repo_needs_no_flag():
    assert _prv.resolve_repo([REPO], 7, None) == REPO


# ── the verdict/note couplings, all before any read ──────────────────────


@pytest.mark.parametrize("verdict", ["fail-intent", "cannot-verify"])
def test_a_non_closing_verdict_without_a_note_is_refused(tmp_path, capsys, verdict):
    rc = _run(tmp_path, _doc(), "--verdict", verdict)
    assert rc == 2
    err = capsys.readouterr().err
    assert "--note" in err
    assert "not-yet-done" in err, "the message must name the abuse it prevents"


@pytest.mark.parametrize("verdict", ["pass-mechanical", "pass-with-measured-gaps"])
def test_a_closing_verdict_refuses_a_note_rather_than_discarding_it(tmp_path, capsys, verdict):
    rc = _run(tmp_path, _doc(), "--verdict", verdict, "--note", "would be dropped")
    assert rc == 2
    assert "silently discarded" in capsys.readouterr().err


def test_a_pr_mismatch_between_flag_and_document_is_refused(tmp_path, capsys):
    rc = _run(tmp_path, _doc(pr=999), "--verdict", "pass-mechanical")
    assert rc == 2
    err = capsys.readouterr().err
    assert "#999" in err and "7" in err


def test_a_failing_claim_refuses_a_closing_verdict(tmp_path, capsys):
    doc = _doc(
        claims=[
            {
                "claim": "the hourly no-op is logged",
                "verdict": "fail",
                "tier": "MEASURED",
                "measurement": "exited 75 and logged nothing on 3 of 3 runs",
            }
        ]
    )
    rc = _run(tmp_path, doc, "--verdict", "pass-mechanical")
    assert rc == 3
    err = capsys.readouterr().err
    assert "REFUSING 'pass-mechanical'" in err, (
        "rule 2 is no longer gated on `closing`, so it names the verdict it refused "
        "rather than the class — a failing claim rules out three of the four"
    )
    assert "fail-intent" in err, "it must name the verdict that IS correct here"


def test_a_failing_claim_is_fine_under_fail_intent(tmp_path):
    """The policy refusal is about DISCHARGING the obligation, not about recording
    a failure — fail-intent is exactly how a failure gets recorded."""
    _seed(tmp_path / "genesis.db")
    doc = _doc(
        claims=[
            {
                "claim": "it works",
                "verdict": "fail",
                "tier": "MEASURED",
                "measurement": "it did not",
            }
        ]
    )
    assert _run(tmp_path, doc, "--verdict", "fail-intent", "--note", "broken at head") == 0
    assert _row(tmp_path / "genesis.db")["status"] == "open"


def test_an_unverifiable_claim_refuses_a_clean_mechanical_pass(tmp_path, capsys):
    doc = _doc()
    doc["claims"][0]["tier"] = "NOT_VERIFIABLE_HERE"
    rc = _run(tmp_path, doc, "--verdict", "pass-mechanical")
    assert rc == 2
    err = capsys.readouterr().err
    assert "NOT_VERIFIABLE_HERE" in err
    assert "pass-with-measured-gaps" in err and "cannot-verify" in err


def test_a_document_that_establishes_NOTHING_cannot_close_a_row(tmp_path, capsys):
    """THE acceptance bar for the floor, and an INVERSION of what this file asserted
    one round ago — the old test pinned this exact document CLOSING the row.

    Every claim NOT_VERIFIABLE_HERE, one named gap, and the row was discharged. It
    satisfied every disqualifying rule that existed because all of them were a
    denylist: no failing claim, and the unverifiable check only ever guarded
    ``pass-mechanical``. So the obligation was marked handled on a document whose own
    contents say nothing was established — the single state this ledger exists to
    make impossible."""
    db = tmp_path / "genesis.db"
    _seed(db)
    doc = _doc(scope_limits=["needs a box with no graph engine"])
    doc["claims"][0]["tier"] = "NOT_VERIFIABLE_HERE"
    assert _run(tmp_path, doc, "--verdict", "pass-with-measured-gaps") == 2
    err = capsys.readouterr().err
    assert "establishing nothing" in err
    assert "cannot-verify" in err, "the refusal must name the verdict that DOES fit"
    assert _row(db)["status"] == "open", "the obligation is not discharged"


def test_measured_gaps_closes_when_something_WAS_established(tmp_path):
    """The other direction, or the floor is just a ban on the verdict. One claim
    measured, one beyond this install's reach, the gap named: that is precisely what
    ``pass-with-measured-gaps`` means and it still closes the row."""
    db = tmp_path / "genesis.db"
    _seed(db)
    doc = _doc(scope_limits=["the no-graph-engine path needs another install"])
    doc["claims"].append(
        {
            "claim": "it degrades cleanly where no graph engine is installed",
            "verdict": "pass",
            "tier": "NOT_VERIFIABLE_HERE",
            "measurement": "this install has one; nothing here reaches that path",
        }
    )
    assert _run(tmp_path, doc, "--verdict", "pass-with-measured-gaps") == 0
    row = _row(db)
    assert row["status"] == "closed"
    assert row["verdict"] == "pass-with-measured-gaps"


@pytest.mark.parametrize("tier", ["INFERRED", "NOT_VERIFIABLE_HERE"])
def test_the_floor_rejects_a_tier_that_establishes_nothing(tmp_path, tier):
    """INFERRED is in the floor's reject set on purpose: an inferred claim never
    enters permanent record in the grammar of a fact, and a discharged obligation is
    permanent record."""
    _seed(tmp_path / "genesis.db")
    doc = _doc(scope_limits=["named"])
    doc["claims"][0]["tier"] = tier
    assert _run(tmp_path, doc, "--verdict", "pass-with-measured-gaps") == 2


@pytest.mark.parametrize("tier", ["MEASURED", "READ"])
def test_the_floor_accepts_READ_as_well_as_MEASURED(tmp_path, tier):
    """READ has to count. A change whose deployment was established by CONTENT — the
    diff is present and correct in the deployed artifact — is verified by reading it,
    and demanding a runtime measurement would leave that whole class no verdict."""
    _seed(tmp_path / "genesis.db")
    doc = _doc()
    doc["claims"][0]["tier"] = tier
    assert _run(tmp_path, doc, "--verdict", "pass-mechanical") == 0


# ── file handling ────────────────────────────────────────────────────────


def test_a_missing_evidence_file_is_refused(tmp_path, capsys):
    rc = _prv.main(
        [
            "close",
            "--pr",
            "7",
            "--verdict",
            "pass-mechanical",
            "--evidence-file",
            str(tmp_path / "absent.json"),
            "--db-path",
            "x",
        ]
    )
    assert rc == 2
    assert "cannot read evidence file" in capsys.readouterr().err


def test_invalid_json_is_refused(tmp_path, capsys):
    ev = tmp_path / "e.json"
    ev.write_text("{not json")
    rc = _prv.main(
        [
            "close",
            "--pr",
            "7",
            "--verdict",
            "pass-mechanical",
            "--evidence-file",
            str(ev),
            "--db-path",
            "x",
        ]
    )
    assert rc == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_non_utf8_evidence_is_refused(tmp_path, capsys):
    ev = tmp_path / "e.json"
    ev.write_bytes(b'{"pr": 7, "x": "\xff\xfe"}')
    rc = _prv.main(
        [
            "close",
            "--pr",
            "7",
            "--verdict",
            "pass-mechanical",
            "--evidence-file",
            str(ev),
            "--db-path",
            "x",
        ]
    )
    assert rc == 2
    assert "not UTF-8" in capsys.readouterr().err


def test_an_oversized_document_is_REFUSED_not_truncated(tmp_path, capsys):
    """A truncated evidence record still looks complete, which is worse than none."""
    doc = _doc(scope_limits=["x" * (_prv.MAX_EVIDENCE_BYTES + 100)])
    rc = _run(tmp_path, doc, "--verdict", "pass-mechanical")
    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED rather than cut" in err


# ── writes against a real migrated database ──────────────────────────────


@pytest.mark.parametrize("verdict", ["pass-mechanical", "pass-with-measured-gaps"])
def test_a_pass_closes_the_row_with_its_verdict_and_evidence(tmp_path, capsys, verdict):
    db = tmp_path / "genesis.db"
    _seed(db)
    # measured-gaps REQUIRES named gaps; mechanical must not need them.
    gaps = ["the no-engine path is unreachable here"] if verdict.endswith("gaps") else []
    assert _run(tmp_path, _doc(scope_limits=gaps), "--verdict", verdict) == 0
    assert "CLOSED" in capsys.readouterr().out
    row = _row(db)
    assert row["status"] == "closed"
    assert row["verdict"] == verdict
    assert row["attempt_count"] == 1
    stored = json.loads(row["evidence"])
    assert stored["merge_commit"] == "deadbeef1234"
    assert row["closed_reason"].startswith(verdict.upper())
    assert "1 MEASURED" in row["closed_reason"]


@pytest.mark.parametrize("verdict", ["fail-intent", "cannot-verify"])
def test_a_non_closing_verdict_leaves_the_row_open_with_its_note(tmp_path, capsys, verdict):
    db = tmp_path / "genesis.db"
    _seed(db)
    rc = _run(tmp_path, _doc_for(verdict), "--verdict", verdict, "--note", "needs another install")
    assert rc == 0
    out = capsys.readouterr().out
    assert "STAYS OPEN" in out
    row = _row(db)
    assert row["status"] == "open", "the obligation is NOT discharged"
    assert row["verdict"] == verdict
    assert row["last_attempt_note"] == "needs another install"
    assert row["attempt_count"] == 1
    assert row["closed_reason"] is None


def test_fail_intent_tells_the_session_to_bring_it_to_the_user(tmp_path, capsys):
    """A standing ruling: a failed verification is a conversation, never a rollback.
    The tool says so at the moment it is recorded, where it will be read."""
    _seed(tmp_path / "genesis.db")
    _run(tmp_path, _doc_for("fail-intent"), "--verdict", "fail-intent", "--note", "broken")
    out = capsys.readouterr().out
    assert "conversation" in out and "rollback" in out


def test_dry_run_writes_nothing_and_renders_the_record(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(), "--verdict", "pass-mechanical", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "CLOSES the row" in out
    assert _row(db)["status"] == "open", "nothing written"


def test_dry_run_says_the_row_stays_open_for_a_non_closing_verdict(tmp_path, capsys):
    _seed(tmp_path / "genesis.db")
    assert (
        _run(
            tmp_path,
            _doc_for("cannot-verify"),
            "--verdict",
            "cannot-verify",
            "--note",
            "n",
            "--dry-run",
        )
        == 0
    )
    assert "STAYS OPEN" in capsys.readouterr().out


def test_an_unknown_pr_is_refused_against_a_real_database(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _seed(db)
    rc = _run(tmp_path, _doc(pr=404), "--verdict", "pass-mechanical", pr=404)
    assert rc == 1
    assert "no OPEN row for PR #404" in capsys.readouterr().err


def test_an_already_closed_row_cannot_be_rewritten(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(), "--verdict", "pass-mechanical") == 0
    first = _row(db)
    rc = _run(
        tmp_path,
        _doc(merge_commit="different", scope_limits=["a gap"]),
        "--verdict",
        "pass-with-measured-gaps",
    )
    assert rc == 1
    assert "no OPEN row" in capsys.readouterr().err
    after = _row(db)
    assert after["verdict"] == first["verdict"]
    assert after["evidence"] == first["evidence"]


def test_an_absent_database_says_so_and_names_the_worktree_trap(tmp_path, capsys):
    """``genesis_db_path()`` is repo-root relative, so running from a linked
    worktree points at a database that does not exist. The message has to name that
    or the diagnosis is not the one anyone would guess."""
    missing = tmp_path / "nope" / "genesis.db"
    ev = tmp_path / "e.json"
    ev.write_text(json.dumps(_doc()))
    rc = _prv.main(
        [
            "close",
            "--pr",
            "7",
            "--verdict",
            "pass-mechanical",
            "--evidence-file",
            str(ev),
            "--db-path",
            str(missing),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no database at" in err and "worktree" in err
    assert not missing.exists(), "a read-write handle must not CREATE it"


def test_the_upgrade_window_is_reported_as_itself(tmp_path, capsys):
    """Table present, verdict columns absent — every existing install passes
    through this between the code deploy and the next migration run."""
    db = tmp_path / "genesis.db"

    async def old_only() -> None:
        from tests.test_session_awareness.conftest import PR_VERIFICATION_MIGRATIONS

        async with aiosqlite.connect(str(db)) as conn:
            await PR_VERIFICATION_MIGRATIONS[0].up(conn)
            await conn.commit()

    asyncio.run(old_only())
    rc = _run(tmp_path, _doc(), "--verdict", "pass-mechanical")
    assert rc == 1
    err = capsys.readouterr().err
    assert "verdict columns are not present yet" in err
    assert "restart" in err, "it must say how to fix it"


def test_a_database_with_no_table_at_all_is_reported_distinctly(tmp_path, capsys):
    db = tmp_path / "genesis.db"

    async def bare() -> None:
        async with aiosqlite.connect(str(db)) as conn:
            await conn.execute("CREATE TABLE unrelated (x INTEGER)")
            await conn.commit()

    asyncio.run(bare())
    rc = _run(tmp_path, _doc(), "--verdict", "pass-mechanical")
    assert rc == 1
    assert "does not exist yet" in capsys.readouterr().err


# ── the survivors a mutation sweep found, and the untested seams ─────────


def test_the_repo_flag_is_bound_END_TO_END_not_just_in_the_helper(tmp_path, capsys):
    """A sweep MEASURED that `resolve_repo(open_repos, pr_number, None)` — ignoring
    `--repo` entirely — passed all 98 tests. The helper had unit coverage; the
    PLUMBING from argv to the helper had none, on the exact seam whose regression
    closed a different repository's obligation."""
    db = tmp_path / "genesis.db"
    _seed(db)  # only REPO holds an open row for PR 7
    # --repo AGREEING with the document, and holding no open row: this is the original
    # BLOCKER's path, and the membership check inside resolve_repo is what refuses it.
    rc = _run(tmp_path, _doc(repo=OTHER), "--verdict", "pass-mechanical", "--repo", OTHER)
    assert rc == 1, "a --repo that holds no open row must be refused"
    err = capsys.readouterr().err
    assert OTHER in err and REPO in err, "the refusal must name the real candidates"
    assert _row(db)["status"] == "open", "the real row must be untouched"


def test_a_repo_flag_that_CONTRADICTS_the_document_is_refused(tmp_path, capsys):
    """The other half of the binding, and the reason the document names its repo at
    all. Before that field existed a document about one repository could be pointed
    at another's open row by a single flag, at exit 0, unamendably. Now the two have
    to agree and neither silently wins."""
    db = tmp_path / "genesis.db"
    _seed(db)
    rc = _run(tmp_path, _doc(), "--verdict", "pass-mechanical", "--repo", OTHER)
    assert rc == 2
    err = capsys.readouterr().err
    assert OTHER in err and REPO in err
    assert "DOCUMENT is the record" in err
    assert _row(db)["status"] == "open"


def test_a_document_naming_the_WRONG_repo_cannot_close_a_row(tmp_path, capsys):
    """THE gap a mutation sweep found, and it is the mis-bind the P1 was about.

    Deleting `or doc["repo"]` from the row resolution left all 150 tests passing.
    Both sibling tests pass --repo explicitly, so assess()'s disagreement rule caught
    them before the resolution seam was reached — and with exactly one repo holding an
    open row, resolve_repo auto-selects it, so the document's repo never had to
    matter. This is the case with NO flag at all: the document names one repository,
    a different one holds the only open row, and the write must not land."""
    db = tmp_path / "genesis.db"
    _seed(db)  # REPO#7 is the only open row
    rc = _run(tmp_path, _doc(repo=OTHER), "--verdict", "pass-mechanical")
    assert rc == 1, "the document's repo must select the row, not the sole candidate"
    err = capsys.readouterr().err
    assert OTHER in err and REPO in err, "name what was asked for and what is open"
    assert _row(db)["status"] == "open", "the wrong row must be untouched"


def test_the_document_alone_selects_the_row_with_no_repo_flag(tmp_path):
    """--repo becomes a disambiguator rather than the binding. The positive direction
    matters: if the document's repo were ignored, every test above would still pass
    while the binding did nothing."""
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(), "--verdict", "pass-mechanical") == 0
    assert _row(db)["status"] == "closed"


def test_the_repo_flag_reaches_the_helper_when_it_IS_valid(tmp_path):
    """The other direction — without this, refusing everything would pass the test
    above and the flag would be inert."""
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(), "--verdict", "pass-mechanical", "--repo", REPO) == 0
    assert _row(db)["status"] == "closed"


def test_a_lost_close_race_is_reported_as_a_race_and_exits_1(tmp_path, capsys, monkeypatch):
    """The row was OPEN at lookup and closed by the time of the UPDATE. A sweep
    MEASURED that turning this branch into `return 0` passed all 56 tests — the
    tool would have reported success having written nothing."""
    db = tmp_path / "genesis.db"
    _seed(db)
    from genesis.db.crud import pr_verifications as crud

    async def lost(*_a, **_k):
        return False

    monkeypatch.setattr(crud, "close_verification", lost)
    rc = _run(tmp_path, _doc(), "--verdict", "pass-mechanical")
    assert rc == 1
    err = capsys.readouterr().err
    assert "another validator" in err
    assert _row(db)["status"] == "open"


def test_a_lost_attempt_race_is_reported_and_exits_1(tmp_path, capsys, monkeypatch):
    """Same seam on the non-closing path — also a sweep survivor."""
    _seed(tmp_path / "genesis.db")
    from genesis.db.crud import pr_verifications as crud

    async def missing(*_a, **_k):
        return "missing"

    monkeypatch.setattr(crud, "record_attempt", missing)
    rc = _run(tmp_path, _doc_for("cannot-verify"), "--verdict", "cannot-verify", "--note", "n")
    assert rc == 1
    assert "missing" in capsys.readouterr().err


def test_the_reason_census_counts_EVERY_claim_not_just_the_first(tmp_path):
    """A sweep MEASURED that computing the census over `claims[:1]` passed all 56
    tests: every fixture had one claim, so a truncating census was indistinguishable
    from a correct one."""
    doc = _doc(
        claims=[
            {"claim": "a", "verdict": "pass", "tier": "MEASURED", "measurement": "m"},
            {"claim": "b", "verdict": "pass", "tier": "MEASURED", "measurement": "m"},
            {"claim": "c", "verdict": "pass", "tier": "INFERRED", "measurement": "m"},
            {"claim": "d", "verdict": "pass", "tier": "READ", "measurement": "m"},
        ]
    )
    reason = _prv.build_reason(verdict="pass-mechanical", doc=doc)
    assert "4 claim(s)" in reason
    assert "2 MEASURED" in reason and "1 INFERRED" in reason and "1 READ" in reason


def test_measured_gaps_requires_its_gaps_to_be_named(tmp_path, capsys):
    """The verdict's whole content is WHICH gaps, and the tool's own refusal message
    routes validators onto this verdict — so without a floor it steers them into an
    unamendable record with the gaps missing."""
    _seed(tmp_path / "genesis.db")
    rc = _run(tmp_path, _doc(scope_limits=[]), "--verdict", "pass-with-measured-gaps")
    assert rc == 2
    assert "scope_limits" in capsys.readouterr().err
    assert _row(tmp_path / "genesis.db")["status"] == "open"


@pytest.mark.parametrize("gaps", [[""], ["   "]])
def test_blank_gap_entries_do_not_satisfy_the_floor(tmp_path, gaps):
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc(scope_limits=gaps), "--verdict", "pass-with-measured-gaps") == 2


# ── the document's own contract (round 1, cause A) ───────────────────────


def test_a_document_that_names_no_repo_is_refused(tmp_path, capsys):
    """The row's key is (repo, pr_number) and the document checked only half of it —
    so a document for one repository could close another's obligation at the same PR
    number. The field was not judged unnecessary; the checks were written one at a
    time instead of derived from the key, so nothing made the missing half visible."""
    _seed(tmp_path / "genesis.db")
    doc = _doc()
    del doc["repo"]
    assert _run(tmp_path, doc, "--verdict", "pass-mechanical") == 2
    assert "evidence.repo" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["controls", "scope_limits"])
@pytest.mark.parametrize("bad", [[{}], [None], [""], ["   "], [42]])
def test_the_prose_lists_reject_anything_that_names_nothing(tmp_path, capsys, field, bad):
    """These two lists are tested by the floors with ``str(x).strip()``, so a nested
    object or a null would satisfy a floor as the truthy text "{}" or "None" — the
    requirement met by something that says nothing."""
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc(**{field: bad}), "--verdict", "pass-mechanical") == 2
    assert f"evidence.{field}[0]" in capsys.readouterr().err


def test_a_clean_mechanical_pass_requires_a_control(tmp_path, capsys):
    """Without a control a verification cannot separate the change working from the
    property already holding. MEASURED in this tool's own pilot round: two of three
    would-be findings were false, and a control is what caught both."""
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc(controls=[]), "--verdict", "pass-mechanical") == 2
    assert "evidence.controls" in capsys.readouterr().err


def test_measured_gaps_still_closes_with_no_control_when_the_gap_is_named(tmp_path):
    """Scoped to pass-mechanical on purpose. "Claim X has no control" is a legitimate
    measured gap, and if the control were demanded here too the honest document would
    have no verdict left — the refusals would route validators to a dead end."""
    db = tmp_path / "genesis.db"
    _seed(db)
    doc = _doc(controls=[], scope_limits=["no control available for the timing claim"])
    assert _run(tmp_path, doc, "--verdict", "pass-with-measured-gaps") == 0
    assert _row(db)["status"] == "closed"


def test_fail_intent_requires_a_claim_that_actually_FAILED(tmp_path, capsys):
    """The row stays open either way, so this is not about a false closure: the row is
    what the next validator reads, and a fail-intent whose every claim passed is a
    headline its own evidence contradicts."""
    _seed(tmp_path / "genesis.db")
    rc = _run(tmp_path, _doc(), "--verdict", "fail-intent", "--note", "n")
    assert rc == 2
    assert "cannot-verify" in capsys.readouterr().err, "name the verdict that fits"


def test_the_cap_is_enforced_on_the_bytes_that_get_STORED(tmp_path, capsys):
    """The read check alone was not the cap. json.dumps(indent=2) re-serialises, so a
    document that fits on disk can cross the cap on the way into the column the cap
    exists to bound — measured here at roughly 2.3x for many short list entries."""
    _seed(tmp_path / "genesis.db")
    doc = _doc(scope_limits=["a"] * 40000)
    raw = json.dumps(doc)
    inflated = json.dumps(doc, indent=2, sort_keys=True)
    assert len(raw) < _prv.MAX_EVIDENCE_BYTES < len(inflated.encode()), (
        "the fixture must sit BETWEEN the two sizes or it tests the old check"
    )
    assert _run(tmp_path, doc, "--verdict", "pass-mechanical") == 2
    assert "serialises to" in capsys.readouterr().err


def test_assess_needs_no_database_at_all():
    """The whole policy surface is a pure function, which is the point of collecting
    it: the rules can be exercised without a ledger, and the writer is left with only
    the writing."""
    assert (
        _prv.assess(_doc(), verdict="pass-mechanical", closing=True, pr=7, repo_override=None)
        is None
    )
    code, message = _prv.assess(
        _doc(), verdict="pass-mechanical", closing=True, pr=8, repo_override=None
    )
    assert code == 2 and "#7" in message


# ── the attempt record's lifecycle (round 1, cause B) ────────────────────


def test_a_failed_verification_KEEPS_the_document_it_assembled(tmp_path):
    """A fail-intent is the highest-value record this tool produces — a merged change
    that does not do what it claimed — and it was the one path that stored only the
    one-line note, discarding every claim, tier, measurement and control behind it."""
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc_for("fail-intent"), "--verdict", "fail-intent", "--note", "n") == 0
    row = _row(db)
    assert row["status"] == "open"
    assert row["evidence"], "the evidence document must survive a non-closing verdict"
    assert "3 of 37 windows" in row["evidence"]


def test_a_later_PASS_clears_the_stale_failure_note(tmp_path):
    """Otherwise the row carries a passing verdict beside a sentence explaining why it
    could not be verified, and a reader has to know which column wins. attempt_count
    is what remembers there were earlier attempts — the part not recoverable from
    anywhere else."""
    db = tmp_path / "genesis.db"
    _seed(db)
    _run(
        tmp_path,
        _doc_for("cannot-verify"),
        "--verdict",
        "cannot-verify",
        "--note",
        "needs an install with no graph engine",
    )
    assert _row(db)["last_attempt_note"] == "needs an install with no graph engine"
    assert _run(tmp_path, _doc(), "--verdict", "pass-mechanical") == 0
    row = _row(db)
    assert row["status"] == "closed"
    assert row["verdict"] == "pass-mechanical"
    assert row["last_attempt_note"] is None, "no sentence contradicting the verdict"
    assert row["attempt_count"] == 2, "the escalation signal survives"


# ── the audit's rule findings (round 1, adversarial pass) ────────────────


def _inferred(**over) -> dict:
    """A document whose only claim is INFERRED — a tier the skill explicitly blesses
    ("an INFERRED claim is one you composed from two measured facts — label it, never
    promote it"). This shape had NO legal verdict before the audit."""
    d = _doc(**over)
    d["claims"][0]["tier"] = "INFERRED"
    return d


def test_an_INFERRED_only_document_HAS_a_legal_verdict(tmp_path):
    """THE BLOCKER. Rule 6 was written as an independent floor rather than the
    complement of rule 8, so every one of the four verdicts exited 2: rule 8 refused
    each closing verdict and pointed at cannot-verify, and rule 6 refused cannot-verify
    and pointed back at a passing one. Measured through the shipped CLI, all four. The
    row was left status=open, verdict=NULL, note=NULL — rendering as NEVER ATTEMPTED —
    and the only escape was relabelling the tier as NOT_VERIFIABLE_HERE, which is the
    tool paying a validator to falsify the one field validate_evidence protects."""
    db = tmp_path / "genesis.db"
    _seed(db)
    rc = _run(tmp_path, _inferred(), "--verdict", "cannot-verify", "--note", "inferred only")
    assert rc == 0, "an INFERRED-only document must have SOME verdict it can be filed under"
    row = _row(db)
    assert row["status"] == "open"
    assert row["verdict"] == "cannot-verify"
    assert row["last_attempt_note"] == "inferred only"


@pytest.mark.parametrize("verdict", ["pass-mechanical", "pass-with-measured-gaps"])
def test_an_INFERRED_only_document_still_cannot_CLOSE_a_row(tmp_path, verdict):
    """The other side of the same boundary — fixing the deadlock must not open the
    floor. INFERRED still establishes nothing for the purpose of discharging."""
    _seed(tmp_path / "genesis.db")
    doc = _inferred(scope_limits=["a named gap"])
    assert _run(tmp_path, doc, "--verdict", verdict) == 2


def test_cannot_verify_is_refused_when_the_document_reached_EVERYTHING(tmp_path, capsys):
    """Rule 6's remaining job, now stated as the complement of the floor: the verdict is
    wrong only when every claim is passing and measured or read, because that is the one
    case where a passing verdict is available."""
    _seed(tmp_path / "genesis.db")
    rc = _run(tmp_path, _doc(), "--verdict", "cannot-verify", "--note", "n")
    assert rc == 2
    err = capsys.readouterr().err
    assert "reached all of them" in err
    assert "pass-mechanical" in err, "name the verdict that fits"


def test_a_MEASURED_FAILURE_cannot_be_parked_as_cannot_verify(tmp_path, capsys):
    """Rule 2 ungated from `closing`. Gated, loosening rule 6 for the BLOCKER above
    would have let a measured failure be filed as cannot-verify — parking the row and
    never printing the standing-ruling escalation, so a PR that demonstrably broke its
    claim would read as one nobody could check."""
    db = tmp_path / "genesis.db"
    _seed(db)
    rc = _run(tmp_path, _doc_for("fail-intent"), "--verdict", "cannot-verify", "--note", "n")
    assert rc == 3, "a failing claim is a policy refusal, not a malformed request"
    err = capsys.readouterr().err
    assert "fail-intent" in err
    assert "not a gap or an unreachable check either" in err
    assert _row(db)["status"] == "open"
    assert _row(db)["verdict"] is None, "nothing was recorded"


def test_fail_intent_is_the_one_verdict_a_failing_claim_DOES_support(tmp_path):
    """The converse, or rule 2 is just a ban. Rules 2 and 5 are exact converses now:
    a failing claim implies fail-intent, and fail-intent implies a failing claim."""
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc_for("fail-intent"), "--verdict", "fail-intent", "--note", "n") == 0
    assert _row(db)["verdict"] == "fail-intent"


def test_the_gaps_refusal_does_not_route_onto_a_verdict_rule_3_refuses(tmp_path, capsys):
    """A refusal is a routing instruction. With an unverifiable claim present,
    'use pass-mechanical if there are none' sends the validator at a verdict the next
    rule refuses — which is how a validator ends up cycling between two messages."""
    _seed(tmp_path / "genesis.db")
    doc = _doc(scope_limits=[])
    doc["claims"][0]["tier"] = "NOT_VERIFIABLE_HERE"
    assert _run(tmp_path, doc, "--verdict", "pass-with-measured-gaps") == 2
    err = capsys.readouterr().err
    assert "evidence.scope_limits" in err
    assert "pass-mechanical" not in err, "do not offer a verdict rule 3 will refuse"


def test_the_gaps_refusal_DOES_offer_pass_mechanical_when_it_would_be_accepted(tmp_path, capsys):
    """The control arm — otherwise the clause could be deleted outright and the test
    above would still pass."""
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc(scope_limits=[]), "--verdict", "pass-with-measured-gaps") == 2
    assert "pass-mechanical" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["owner/repo ", " owner/repo", "owner/repo\n"])
def test_a_whitespace_padded_repo_is_refused_by_NAME(tmp_path, capsys, bad):
    """Compared exactly against the open set, a padded slug fails membership and the
    refusal prints two spellings that look identical on a terminal. Refused here by
    name instead — and not silently trimmed, which would be a coercion inside the one
    check that exists to be exact."""
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc(repo=bad), "--verdict", "pass-mechanical") == 2
    assert "whitespace" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [7, None, ["owner/repo"], {"repo": "x"}])
def test_a_non_string_repo_is_refused(tmp_path, capsys, bad):
    """`pr` was type-checked and `repo` was not, though both are halves of one key."""
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc(repo=bad), "--verdict", "pass-mechanical") == 2
    assert "evidence.repo" in capsys.readouterr().err
