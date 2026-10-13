"""The validator's write path — ``scripts/pr_verification.py`` (issue #1718 half B).

The invariants, each one bought by a measured defect rather than imagined:

* The verdict is DERIVED from the document (``pr_evidence.derive``); the caller
  cannot assert one the claims do not support. An earlier caller-asserted design
  had documents with NO legal verdict, twice, one fix apart.
* The document names its row — ``repo`` and ``pr`` — and the row must be OPEN for
  exactly that pair. An earlier ``--repo`` flag, unchecked, closed a DIFFERENT
  repository's obligation at exit 0, permanently. ``--pr`` must still equal the
  document's ``pr``: a deliberate second copy that catches a typo inside the
  document, which a closed row could never be amended to undo.
* A non-closing outcome REQUIRES a note and leaves the row OPEN; a closing outcome
  refuses a note rather than discarding it. ``--park`` is the one judgment bit, and a
  failure can never be parked.
* Every refusal names which state it found, and the exit codes are distinct:
  1 the ledger declined, 2 the request was malformed, 3 refused on policy.

The policy itself (totality, the strict model) is tested in
``tests/test_session_awareness/test_pr_evidence.py``; this file tests the CLI's
plumbing of it end to end against a real migrated database.

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
        "deploy": {
            "method": "content",
            "established": True,
            "detail": "present in the deployed file",
        },
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
    """A document whose CLAIMS derive *verdict*.

    The verdict is derived from the claims (``pr_evidence.derive``), so a test that
    wants a non-closing outcome builds the document that produces it: a MEASURED
    failure for ``fail-intent``, a claim nobody could check (``unverified`` at
    NOT_VERIFIABLE_HERE) for ``cannot-verify``. Building from the verdict keeps each
    test honest about what it exercises.
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
                    "verdict": "unverified",
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
    assert set(tpl["deploy"]) == {"method", "established", "detail"}
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
        ({"deploy": {"method": "vibes", "established": True, "detail": "x"}}, "deploy.method"),
        ({"deploy": {"method": "content", "established": True, "detail": " "}}, "deploy.detail"),
        ({"deploy": {"method": "content", "detail": "x"}}, "deploy.established"),
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
        if tier == "NOT_VERIFIABLE_HERE":
            doc["claims"][0]["verdict"] = "unverified"
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


def test_no_open_row_refuses_and_points_at_the_backlog_reader():
    with pytest.raises(LookupError) as exc:
        _prv.resolve_repo([], 7, None)
    assert "no OPEN row for PR #7" in str(exc.value)
    assert "--verification-backlog" in str(exc.value)


# ── the verdict/note couplings, all before any read ──────────────────────


def test_a_pr_mismatch_between_flag_and_document_is_refused(tmp_path, capsys):
    rc = _run(tmp_path, _doc(pr=999), "--verdict", "pass-mechanical")
    assert rc == 2
    err = capsys.readouterr().err
    assert "#999" in err and "7" in err


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
            "verdict": "unverified",
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
    if tier == "NOT_VERIFIABLE_HERE":
        doc["claims"][0]["verdict"] = "unverified"
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
    # The reason that WOULD be written, so a dry run previews the record, not a label.
    assert "reason   : PASS-MECHANICAL — 1 claim(s): 1 MEASURED" in out
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


def test_a_document_naming_the_WRONG_repo_cannot_close_a_row(tmp_path, capsys):
    """THE mis-bind round 1's P1 was about, and a gap a mutation sweep found once.

    With exactly one repository holding an open row for the PR, an earlier resolver
    auto-selected it, so the document's repo never had to matter and ignoring it left
    every test green. The document names one repository, a different one holds the
    only open row, and the write must not land."""
    db = tmp_path / "genesis.db"
    _seed(db)  # REPO#7 is the only open row
    rc = _run(tmp_path, _doc(repo=OTHER), "--verdict", "pass-mechanical")
    assert rc == 1, "the document's repo must select the row, not the sole candidate"
    err = capsys.readouterr().err
    assert OTHER in err and REPO in err, "name what was asked for and what is open"
    assert _row(db)["status"] == "open", "the wrong row must be untouched"


def test_the_document_alone_selects_the_row_with_no_repo_flag(tmp_path):
    """The positive direction of the binding: the document's repo selects its own row.
    Without it, refusing everything would pass the test above."""
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(), "--verdict", "pass-mechanical") == 0
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


def test_fail_intent_is_the_one_verdict_a_failing_claim_DOES_support(tmp_path):
    """The converse, or rule 2 is just a ban. Rules 2 and 5 are exact converses now:
    a failing claim implies fail-intent, and fail-intent implies a failing claim."""
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc_for("fail-intent"), "--verdict", "fail-intent", "--note", "n") == 0
    assert _row(db)["verdict"] == "fail-intent"


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


# ── round 3: the derived-verdict contract, end to end ────────────────────


def _all_nvh(**over) -> dict:
    d = _doc(**over)
    d["claims"][0]["tier"] = "NOT_VERIFIABLE_HERE"
    d["claims"][0]["verdict"] = "unverified"
    return d


def test_the_resolver_only_CHECKS_the_repo_the_document_names():
    """Nothing is chosen any more, so there is nothing to disambiguate: the named repo
    is either an open row or refused, and the two refusals say different things."""
    assert _prv.resolve_repo([REPO, OTHER], 7, REPO) == REPO
    with pytest.raises(LookupError) as exc:
        _prv.resolve_repo([OTHER], 7, REPO)
    assert REPO in str(exc.value) and OTHER in str(exc.value)
    with pytest.raises(LookupError) as exc:
        _prv.resolve_repo([], 7, REPO)
    assert "no OPEN row for PR #7" in str(exc.value)


def test_two_repos_open_at_one_pr_number_the_document_picks_its_own(tmp_path):
    """What --repo used to exist for. The document names the row, so the other
    repository's obligation at the same number is left alone."""
    db = tmp_path / "genesis.db"
    _seed(db)
    _seed(db, repo=OTHER)
    assert _run(tmp_path, _doc()) == 0
    assert _row(db)["status"] == "closed"
    assert _row(db, repo=OTHER)["status"] == "open", "the other repository is untouched"


def test_the_repo_flag_no_longer_exists(tmp_path):
    """Removed, not ignored: a flag accepted and dropped would read as a binding that
    was applied."""
    _seed(tmp_path / "genesis.db")
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, _doc(), "--repo", REPO)
    assert exc.value.code == 2


def test_no_verdict_flag_is_needed_the_document_decides(tmp_path):
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc()) == 0
    row = _row(db)
    assert (row["status"], row["verdict"]) == ("closed", "pass-mechanical")


def test_the_formerly_DEAD_document_now_closes_with_its_gap_named(tmp_path):
    """The round-2 deadlock at the local head: one MEASURED passing claim, no
    controls, no scope limits — refused under all four verdicts. It now derives
    pass-with-measured-gaps, and the missing control is the named gap."""
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc(controls=[], scope_limits=[])) == 0
    row = _row(db)
    assert (row["status"], row["verdict"]) == ("closed", "pass-with-measured-gaps")
    assert "no negative control" in row["closed_reason"]


def test_a_document_that_establishes_NOTHING_cannot_close_a_row(tmp_path, capsys):
    """Round 1's P1, now by construction: every claim NOT_VERIFIABLE_HERE derives
    cannot-verify, so it needs a note and stays open — and asserting a pass is a
    mismatch, not a closure."""
    db = tmp_path / "genesis.db"
    _seed(db)
    doc = _all_nvh(scope_limits=["needs a box with no graph engine"])
    assert _run(tmp_path, doc, "--verdict", "pass-with-measured-gaps") == 2
    assert "derives cannot-verify" in capsys.readouterr().err
    assert _row(db)["status"] == "open"
    assert _run(tmp_path, doc, "--note", "needs a box with no graph engine") == 0
    row = _row(db)
    assert (row["status"], row["verdict"]) == ("open", "cannot-verify")


@pytest.mark.parametrize("verdict", ["fail-intent", "cannot-verify"])
def test_an_open_outcome_without_a_note_is_refused(tmp_path, capsys, verdict):
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc_for(verdict)) == 2
    err = capsys.readouterr().err
    assert "--note" in err
    if verdict == "cannot-verify":
        assert "not-yet-done" in err, "the message must name the abuse it prevents"
    else:
        # A measured failure is never "not-yet-done": telling its holder to leave the
        # row alone would bury the one outcome that must reach the user.
        assert "what failed" in err and "not-yet-done" not in err


@pytest.mark.parametrize("controls", [["a control"], []])
def test_a_closing_outcome_refuses_a_note_rather_than_discarding_it(tmp_path, capsys, controls):
    """Both closing verdicts: with a control the document derives pass-mechanical,
    without one pass-with-measured-gaps."""
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc(controls=controls), "--note", "would be dropped") == 2
    assert "silently discarded" in capsys.readouterr().err


@pytest.mark.parametrize(
    "asserted", ["pass-mechanical", "pass-with-measured-gaps", "cannot-verify"]
)
def test_a_FAILURE_under_any_other_verdict_is_a_policy_refusal(tmp_path, capsys, asserted):
    """Exit 3, whichever way the verdict tries to route around the failure — into a
    closure, or into a quiet parked row that skips the conversation."""
    db = tmp_path / "genesis.db"
    _seed(db)
    rc = _run(tmp_path, _doc_for("fail-intent"), "--verdict", asserted, "--note", "n")
    assert rc == 3
    err = capsys.readouterr().err
    assert "derives fail-intent" in err
    assert "FAILED: the timer fires hourly" in err, "name the claim that failed"
    assert _row(db)["verdict"] is None, "nothing was recorded"


def test_a_failure_can_NEVER_be_parked(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _seed(db)
    assert _run(tmp_path, _doc_for("fail-intent"), "--park", "--note", "n") == 3
    assert "never parked" in capsys.readouterr().err
    assert _row(db)["verdict"] is None


def test_an_unverifiable_claim_is_a_named_GAP_not_a_clean_pass(tmp_path, capsys):
    db = tmp_path / "genesis.db"
    _seed(db)
    doc = _doc()
    doc["claims"].append(
        {
            "claim": "degrades cleanly with no graph engine",
            "verdict": "unverified",
            "tier": "NOT_VERIFIABLE_HERE",
            "measurement": "not reachable here",
        }
    )
    assert _run(tmp_path, doc, "--verdict", "pass-mechanical") == 2
    err = capsys.readouterr().err
    assert "derives pass-with-measured-gaps" in err
    assert "NOT VERIFIABLE HERE: degrades cleanly" in err, "name the gap"
    assert _run(tmp_path, doc) == 0
    assert _row(db)["verdict"] == "pass-with-measured-gaps"


@pytest.mark.parametrize("asserted", ["cannot-verify", "fail-intent", "pass-with-measured-gaps"])
def test_a_fully_established_document_refuses_every_other_verdict(tmp_path, capsys, asserted):
    """The complement of the floor: everything measured, controlled and gap-free is
    pass-mechanical, and no assertion can make it anything else."""
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc(), "--verdict", asserted, "--note", "n") == 2
    assert "derives pass-mechanical" in capsys.readouterr().err


def test_park_records_cannot_verify_and_OWNS_UP_to_it_in_the_note(tmp_path):
    db = tmp_path / "genesis.db"
    _seed(db)
    doc = _doc(controls=[])  # derives pass-with-measured-gaps
    assert _run(tmp_path, doc, "--park", "--note", "the measured claim was trivial") == 0
    row = _row(db)
    assert (row["status"], row["verdict"]) == ("open", "cannot-verify")
    assert row["last_attempt_note"].startswith("PARKED")
    assert "the measured claim was trivial" in row["last_attempt_note"]


def test_park_is_refused_on_a_clean_pass(tmp_path, capsys):
    _seed(tmp_path / "genesis.db")
    assert _run(tmp_path, _doc(), "--park", "--note", "n") == 2
    assert "no gaps to park on" in capsys.readouterr().err


def test_the_cap_is_enforced_on_the_bytes_that_get_STORED(tmp_path, capsys):
    """json.dumps(indent=2) re-serialises, so a document that fits on disk can cross
    the cap on the way into the column the cap exists to bound."""
    _seed(tmp_path / "genesis.db")
    doc = _doc(scope_limits=["a"] * 40000)
    raw = json.dumps(doc)
    inflated = json.dumps(doc, indent=2, sort_keys=True)
    assert len(raw) < _prv.MAX_EVIDENCE_BYTES < len(inflated.encode()), (
        "the fixture must sit BETWEEN the two sizes or it tests the old check"
    )
    assert _run(tmp_path, doc) == 2
    assert "serialises to" in capsys.readouterr().err


def test_the_reason_census_counts_EVERY_claim_and_names_the_gaps(tmp_path):
    """A mutation that censused only the first claim once passed every test."""
    from genesis.session_awareness import pr_evidence as pe

    doc = _doc(controls=[])
    doc["claims"].append({"claim": "b", "verdict": "pass", "tier": "READ", "measurement": "file:1"})
    model = _prv.validate_evidence(doc)
    reason = _prv.build_reason(model, pe.decide(model))
    assert "2 claim(s)" in reason
    assert "1 MEASURED" in reason and "1 READ" in reason
    assert "gaps: no negative control" in reason


# ── round 3 audit: the claim vocabulary and the refusal routing, end to end ──


def test_an_INFERRED_failure_stays_open_as_a_SUSPICION_not_a_finding(tmp_path, capsys):
    """A failure nobody established is not taken to the user as one, and it never
    closes a row: it derives cannot-verify with the suspicion named."""
    db = tmp_path / "genesis.db"
    _seed(db)
    doc = _doc()
    doc["claims"].append(
        {
            "claim": "the retry backs off",
            "verdict": "fail",
            "tier": "INFERRED",
            "measurement": "the log spacing looks flat, not measured",
        }
    )
    assert _run(tmp_path, doc, "--note", "measure the retry spacing") == 0
    row = _row(db)
    assert (row["status"], row["verdict"]) == ("open", "cannot-verify")
    # Named ON THE ROW, ahead of the note — the backlog shows only the note line.
    assert row["last_attempt_note"] == (
        "SUSPECTED FAILURE (INFERRED): the retry backs off — measure the retry spacing"
    )
    out = capsys.readouterr().out
    assert "SUSPECTED FAILURE (INFERRED): the retry backs off" in out
    assert "conversation" not in out


@pytest.mark.parametrize(
    ("verdict", "tier"), [("pass", "NOT_VERIFIABLE_HERE"), ("unverified", "MEASURED")]
)
def test_an_incoherent_claim_is_refused_at_the_CLI(tmp_path, capsys, verdict, tier):
    _seed(tmp_path / "genesis.db")
    doc = _doc()
    doc["claims"][0].update(verdict=verdict, tier=tier)
    assert _run(tmp_path, doc) == 2
    assert "evidence.claims[0]" in capsys.readouterr().err


def test_asserting_cannot_verify_on_a_gaps_document_names_PARK(tmp_path, capsys):
    """Audit round 3: the mismatch said "change the document", when the move the
    validator wanted was --park."""
    _seed(tmp_path / "genesis.db")
    doc = _doc(controls=[])
    assert _run(tmp_path, doc, "--verdict", "cannot-verify", "--note", "n") == 2
    assert "--park" in capsys.readouterr().err
    assert _run(tmp_path, doc, "--verdict", "cannot-verify", "--park", "--note", "n") == 0


def test_a_duplicate_key_is_refused_not_last_one_wins(tmp_path, capsys):
    """json.loads keeps the LAST of two equal keys silently."""
    _seed(tmp_path / "genesis.db")
    path = tmp_path / "dup.json"
    text = json.dumps(_doc())
    path.write_text(text[:-1] + ', "pr": 7}')
    rc = _prv.main(
        [
            "close",
            "--pr",
            "7",
            "--evidence-file",
            str(path),
            "--db-path",
            str(tmp_path / "genesis.db"),
        ]
    )
    assert rc == 2
    assert "repeats the key 'pr'" in capsys.readouterr().err


def test_a_pr_beyond_the_integer_range_is_a_named_refusal(tmp_path, capsys):
    """10**30 reached the driver and raised OverflowError — a traceback."""
    _seed(tmp_path / "genesis.db")
    big = 2**63
    assert _run(tmp_path, _doc(pr=big), pr=big) == 2
    assert "evidence.pr" in capsys.readouterr().err


def test_non_ASCII_evidence_is_stored_as_written_not_escaped(tmp_path):
    """The escaped form spends up to 12 bytes per character against the size cap."""
    db = tmp_path / "genesis.db"
    _seed(db)
    doc = _doc()
    doc["claims"][0]["measurement"] = "latence mesurée: 12 ms — ✓"
    assert _run(tmp_path, doc) == 0
    stored = _row(db)["evidence"]
    assert "mesurée" in stored and "\\u" not in stored


def test_an_UNDEPLOYED_document_stays_open_with_deployment_named_on_the_row(tmp_path, capsys):
    """Codex round 3 P1, end to end: a clean MEASURED pass on a tree that does not
    carry the merge must not close the obligation, and the backlog line must say why."""
    db = tmp_path / "genesis.db"
    _seed(db)
    doc = _doc()
    doc["deploy"] = {
        "method": "content",
        "established": False,
        "detail": "the changed file on this checkout predates the merge",
    }
    assert _run(tmp_path, doc, "--verdict", "pass-mechanical") == 2
    assert _row(db)["status"] == "open"
    assert _run(tmp_path, doc, "--note", "deploy main, then re-run") == 0
    row = _row(db)
    assert (row["status"], row["verdict"]) == ("open", "cannot-verify")
    assert row["last_attempt_note"] == (
        "DEPLOYMENT NOT ESTABLISHED (content): the changed file on this checkout "
        "predates the merge — deploy main, then re-run"
    )
    out = capsys.readouterr().out
    assert "Deployment was not established" in out and "Deploy the merge" in out


def test_a_SQLite_write_failure_is_exit_1_not_a_traceback(tmp_path, capsys, monkeypatch):
    """Codex round 3: a lock past busy_timeout or a malformed file raised straight
    out of asyncio.run() instead of the documented ledger-declined exit code."""
    import sqlite3

    _seed(tmp_path / "genesis.db")
    from genesis.db.crud import pr_verifications as crud

    async def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(crud, "close_verification", locked)
    assert _run(tmp_path, _doc()) == 1
    err = capsys.readouterr().err
    assert "ledger declined the write" in err and "database is locked" in err
    assert _row(tmp_path / "genesis.db")["status"] == "open"


def test_print_schema_template_carries_a_real_established_bool(capsys):
    assert _prv.main(["print-schema"]) == 0
    tpl = json.loads(capsys.readouterr().out)
    assert tpl["deploy"]["established"] is True


def test_a_failure_AFTER_the_commit_reports_committed_not_nothing_recorded(
    tmp_path, capsys, monkeypatch
):
    """Fresh review of round 4: the write-failure catch also covered code that runs
    after the commit, so a failure there said "Nothing was recorded" (exit 1) about a
    row that had closed — and a retry then met a closed row."""
    db = tmp_path / "genesis.db"
    _seed(db)

    import builtins

    real_print = builtins.print

    def failing_print(*a, **k):
        if a and str(a[0]).startswith("pr_verification: CLOSED"):
            raise OSError("broken pipe")
        return real_print(*a, **k)

    monkeypatch.setattr(builtins, "print", failing_print)
    rc = _run(tmp_path, _doc())
    monkeypatch.setattr(builtins, "print", real_print)
    assert rc == 0
    err = capsys.readouterr().err
    assert "the write COMMITTED (CLOSED owner/repo#7 as pass-mechanical)" in err
    assert "Nothing was recorded" not in err
    assert _row(db)["status"] == "closed"
