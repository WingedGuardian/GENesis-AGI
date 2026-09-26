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
    assert "REFUSING a closing verdict" in err
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


def test_an_unverifiable_claim_is_fine_under_measured_gaps(tmp_path):
    _seed(tmp_path / "genesis.db")
    doc = _doc(scope_limits=["needs a box with no graph engine"])
    doc["claims"][0]["tier"] = "NOT_VERIFIABLE_HERE"
    assert _run(tmp_path, doc, "--verdict", "pass-with-measured-gaps") == 0
    assert _row(tmp_path / "genesis.db")["status"] == "closed"


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
    rc = _run(tmp_path, _doc(), "--verdict", verdict, "--note", "needs another install")
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
    _run(tmp_path, _doc(), "--verdict", "fail-intent", "--note", "broken")
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
    assert _run(tmp_path, _doc(), "--verdict", "cannot-verify", "--note", "n", "--dry-run") == 0
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
    rc = _run(tmp_path, _doc(), "--verdict", "pass-mechanical", "--repo", OTHER)
    assert rc == 1, "a --repo that holds no open row must be refused"
    err = capsys.readouterr().err
    assert OTHER in err and REPO in err
    assert _row(db)["status"] == "open", "the real row must be untouched"


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
    rc = _run(tmp_path, _doc(), "--verdict", "cannot-verify", "--note", "n")
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
