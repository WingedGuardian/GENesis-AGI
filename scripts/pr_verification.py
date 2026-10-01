#!/usr/bin/env python3
"""The validator session's write path for ``pr_verifications`` (issue #1718 half B).

WHY THIS EXISTS. The repo-pulse worker opens one row per merged PR and auto-closes
a documentation-only diff by a deterministic path rule; everything else stays OPEN
until a validator records what it concluded. The READER shipped with the table
(``repo_pulse_worker.py --verification-backlog``); the WRITER did not. MEASURED
2026-09-25 on a live install: nothing outside tests had ever called
``close_verification``, so 217 obligations were open and ``evidence`` was NULL on
all 258 rows. A validator could validate and could not record.

USAGE

    python3 scripts/pr_verification.py print-schema
    python3 scripts/pr_verification.py close --pr 2273 \\
        --evidence-file ~/.genesis/output/prv-2273.json
    python3 scripts/pr_verification.py close --pr 1573 \\
        --note "needs an install with no graph engine" \\
        --evidence-file ~/.genesis/output/prv-1573.json

THE VERDICT IS DERIVED FROM THE DOCUMENT, not chosen. The four verdicts are the
owner's standing ruling (2026-09-26); which one a document supports is decided by
``genesis.session_awareness.pr_evidence.derive``, a decision tree that gives every
valid document exactly one:

  deploy.established is false           -> cannot-verify          LEAVES IT OPEN
  a claim failed at MEASURED or READ    -> fail-intent            LEAVES IT OPEN
  a claim failed at INFERRED            -> cannot-verify          LEAVES IT OPEN
  nothing passing at MEASURED or READ   -> cannot-verify          LEAVES IT OPEN
  no gaps                               -> pass-mechanical        CLOSES the row
  otherwise                             -> pass-with-measured-gaps CLOSES the row

Deployment comes first: ``deploy.established`` is required, and false derives
cannot-verify (row OPEN, "DEPLOYMENT NOT ESTABLISHED" named) whatever the claims say.

Gaps are the declared ``scope_limits`` plus the ones the document implies: no
negative control, any NOT_VERIFIABLE_HERE claim, any INFERRED claim, and a named
SUSPECTED FAILURE for each claim that failed only at INFERRED.

EACH CLAIM is ``pass``, ``fail`` or ``unverified``. ``unverified`` goes with tier
NOT_VERIFIABLE_HERE and only with it; the model refuses either without the other,
so a claim nobody could check is never recorded as a pass or as a failure. A
failure is gated by tier like a pass: only one at MEASURED or READ is fail-intent,
the outcome that goes to the user.

WHY DERIVED. An earlier version took ``--verdict`` from the caller and policed it
with eight refusal rules. MEASURED across 176 enumerated documents: 88.6% had
exactly one legal verdict, so asking only created a chance to be refused, and 2.8%
had NO legal verdict — the rules contradicted each other, twice, one fix apart.
A tree cannot contradict itself.

What the caller still decides:

  --park     Only on a document that derives pass-with-measured-gaps: records
             cannot-verify instead, because something was established but nothing
             MATERIAL was. A failure can never be parked — it goes to the USER as a
             conversation, never an automatic rollback, and the defect gets filed.
  --note     Required whenever the row stays open (fail-intent, cannot-verify): it
             is the whole value of an open row, the thing that stops the next
             validator re-deriving why. Refused when the row closes.
  --verdict  OPTIONAL. An expectation checked for equality with the outcome; a
             mismatch refuses with "document derives X". Worth passing: it catches a
             document that does not say what you think it says.

"I DID NOT GET TO IT" IS NOT ``cannot-verify``. The test is whether you could have
done it with the access and the time you had. If yes it is not-yet-done: leave the
row alone. Keep cannot-verify RARE, or the ledger fills with parked rows and the
backlog stops meaning anything.

WHERE THE DATABASE IS, and the trap that costs a confusing run: ``genesis_db_path()``
resolves RELATIVE TO THE REPO ROOT, so running this from a linked worktree points
it at that worktree's ``data/genesis.db``, which does not exist — the tool then
correctly reports "no database" while the real ledger sits in the main checkout.
Run it from the main checkout, or pass ``--db-path``.

THE ROW KEY. The document names its row — ``repo`` and ``pr`` — and ``--pr`` must
match it. That second copy is deliberate: a closed row cannot be amended through
this tool, so a typo INSIDE the document (``"pr": 2257`` in a document about #2273)
would otherwise close the wrong row permanently. Two independent sources is the
point. There is no ``--repo``: the document names the repository, and the row must
be OPEN for exactly that ``(repo, pr)``.

DEPLOYMENT IS NOT ESTABLISHED BY ANCESTRY, which is why ``deploy.method`` exists at
all. MEASURED on PR #2257: ``gh pr view --json mergeCommit`` named a commit
reachable only from that PR's own base branch, because it was a STACKED PR whose
base was a feature branch; the change reached the default branch inside the squash
of its parent, under a different PR number. ``git merge-base --is-ancestor`` therefore
returns a false "not deployed" while the code is live and behaving correctly. Record
``content`` (the change is present in the deployed artifact) or ``behaviour`` (the
live thing was observed doing it); ``ancestry`` is a fast positive path and never a
negative verdict.

EXIT CODES. 0 wrote (or dry-ran); 1 the ledger declined (no such open row, a race,
an absent or quarantined database); 2 the request was malformed (bad evidence, a
missing or discarded note, a PR mismatch, a --verdict/--park the document does not
support); 3 refused on policy (a failing claim under any other --verdict, or --park
on a failure). Deliberately meaningful, which is why this is a separate script
rather than a subcommand of ``repo_pulse_worker.py``: that module's documented
contract is that its exit code is always 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import UTC
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from genesis.session_awareness.pr_evidence import (  # noqa: E402
    MAX_EVIDENCE_BYTES,
    NOT_DEPLOYED,
    NOTE_PREFIXED_GAPS,
    SUSPECTED_FAILURE,
    TIERS,
    Decision,
    EvidenceDocument,
    EvidenceError,
    Refusal,
    decide,
    parse_evidence,
    reason_for,
)

__all__ = [
    "MAX_EVIDENCE_BYTES",
    "SCHEMA_TEMPLATE",
    "TIERS",
    "EvidenceError",
    "build_reason",
    "main",
    "resolve_repo",
    "validate_evidence",
]


def validate_evidence(doc: object) -> EvidenceDocument:
    """Return the validated document, or raise :class:`EvidenceError` naming each field.

    A thin name over :func:`parse_evidence` so the shape rule has ONE home — the
    strict model in ``genesis.session_awareness.pr_evidence``, which a second writer
    can import where it could never import this script.
    """
    return parse_evidence(doc)


def build_reason(doc: EvidenceDocument, decision: Decision) -> str:
    """The generated ``closed_reason``: verdict, tier census, and the gaps or failures."""
    return reason_for(doc, decision)


def resolve_repo(open_repos: list[str], pr_number: int, repo: str) -> str:
    """Return *repo* if ``(repo, pr_number)`` is an OPEN obligation, else raise LookupError.

    The document names the repository, so nothing is chosen here — only CHECKED.
    Before the document named it, a ``--repo`` flag did, unchecked, and it closed a
    DIFFERENT repository's obligation with this PR's evidence at exit 0, permanently
    (MEASURED end-to-end on an earlier draft). "Nothing open for this PR" and "that
    repo is not among the open ones" are different facts, so they read differently.
    """
    if not open_repos:
        raise LookupError(
            f"no OPEN row for PR #{pr_number} — it may be discharged already, or "
            f"never opened. Run `python3 scripts/repo_pulse_worker.py "
            f"--verification-backlog` to see the open set."
        )
    if repo not in open_repos:
        raise LookupError(
            f"no OPEN row for {repo}#{pr_number} (the repository the evidence document "
            f"names). Open rows for PR #{pr_number}: {', '.join(open_repos)}. If the "
            f"document names the wrong repository, correct the document."
        )
    return repo


SCHEMA_TEMPLATE = {
    "repo": "<owner/name — with 'pr' this is the ledger row's key>",
    "pr": 1234,
    "merge_commit": "<sha of the artifact you actually verified>",
    "deploy": {
        "method": "behaviour | content | ancestry",
        "established": True,
        "detail": "HOW deployment was established — ancestry is never a negative verdict",
    },
    "claims": [
        {
            "claim": "what the PR said it would do",
            "verdict": "pass | fail | unverified (unverified iff tier NOT_VERIFIABLE_HERE)",
            "tier": "MEASURED | INFERRED | READ | NOT_VERIFIABLE_HERE",
            "measurement": "the number with its denominator, or the artifact + location",
        }
    ],
    "controls": [
        "the arm that had to come out the other way, and did — without one the "
        "document derives pass-with-measured-gaps, never pass-mechanical"
    ],
    "scope_limits": ["what this install could not reach, named"],
    "findings": [{"summary": "what you found", "disposition": "issue #N | note | none"}],
}


async def _write(
    *,
    db_path: str,
    doc: EvidenceDocument,
    decision: Decision,
    note: str | None,
    payload: str,
    dry_run: bool,
) -> int:
    import aiosqlite

    from genesis.db.crud import pr_verifications as crud
    from genesis.db.integrity import DatabaseIntegrityError

    resolved = Path(db_path)
    if not resolved.exists():
        print(
            f"pr_verification: no database at {resolved}\n"
            f"  If you are running from a linked worktree this is expected — "
            f"genesis_db_path() is repo-root relative. Pass --db-path, or run from "
            f"the main checkout.",
            file=sys.stderr,
        )
        return 1

    # The admission seam every real connection path in this repo uses, NOT the
    # advisory predicate: admission.py says that one is for callers where
    # continuing would not open the database, and this one opens it read-write to
    # mutate a permanent ledger. It raises in TWO places — synchronously at
    # construction and again inside the post-open re-check — so the try must cover
    # the context manager, not just the call.
    from genesis.db.connection import connect_aiosqlite_rw

    pr_number = doc.pr
    # Set the moment a writer COMMITS. Anything that fails after that (the success
    # print, the connection's close) must not be reported as "nothing recorded" —
    # a retry would then meet a closed row and a message that contradicts it.
    committed: str | None = None
    try:
        async with connect_aiosqlite_rw(resolved, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row

            if not await crud.tables_available(db):
                print(
                    "pr_verification: pr_verifications does not exist yet — this "
                    "database predates the obligation ledger.",
                    file=sys.stderr,
                )
                return 1
            if not await crud._verdict_columns_available(db):
                print(
                    "pr_verification: the verdict columns are not present yet. This "
                    "database has the table but not the migration — restart "
                    "genesis-server (or run the migration runner) and retry.",
                    file=sys.stderr,
                )
                return 1

            open_repos = await crud.open_repos_for_pr(db, pr_number=pr_number)
            try:
                target = resolve_repo(open_repos, pr_number, doc.repo)
            except LookupError as exc:
                print(f"pr_verification: {exc}", file=sys.stderr)
                return 1

            verdict = decision.verdict
            reason = build_reason(doc, decision)
            # An open row's note is the validator's own words, with two kinds of
            # generated prefix. The gaps in NOTE_PREFIXED_GAPS (deployment not
            # established, a failure measured there, a suspected failure) go AHEAD of
            # it: the backlog shows one line, and a reason that lived only in the
            # stored document would read there exactly like an ordinary precondition.
            # A long prefix can push the validator's words past the backlog's clip;
            # the clip marker names the read that shows them whole. And a park, which
            # is a judgment the note must own up to.
            attempt_note = None
            if not decision.closing:
                attempt_note = str(note).strip()
                visible = [g for g in decision.derivation.gaps if g.startswith(NOTE_PREFIXED_GAPS)]
                if visible:
                    attempt_note = "; ".join(visible) + " — " + attempt_note
                if decision.parked:
                    attempt_note = "PARKED (nothing material established) — " + attempt_note

            if dry_run:
                print("pr_verification: DRY RUN — nothing written.")
                print(f"  target   : {target}#{pr_number}")
                print(
                    f"  verdict  : {verdict}  "
                    f"({'CLOSES the row' if decision.closing else 'row STAYS OPEN'})"
                )
                if decision.closing:
                    print(f"  reason   : {reason}")
                else:
                    print(f"  note     : {attempt_note}")
                print(f"  evidence : {len(payload.encode('utf-8'))} bytes")
                return 0

            from datetime import datetime

            now = datetime.now(UTC).isoformat()

            if decision.closing:
                changed = await crud.close_verification(
                    db,
                    repo=target,
                    pr_number=pr_number,
                    verdict=verdict,
                    reason=reason,
                    evidence=payload,
                    now=now,
                )
                if not changed:
                    print(
                        f"pr_verification: {target}#{pr_number} was OPEN at lookup and "
                        f"is not now — another validator discharged it in between. "
                        f"Nothing was written; re-read the row before deciding whether "
                        f"your evidence adds anything.",
                        file=sys.stderr,
                    )
                    return 1
                committed = f"CLOSED {target}#{pr_number} as {verdict}"
                print(f"pr_verification: CLOSED {target}#{pr_number} — {reason} at {now}")
                return 0

            outcome = await crud.record_attempt(
                db,
                repo=target,
                pr_number=pr_number,
                verdict=verdict,
                note=attempt_note,
                now=now,
                evidence=payload,
            )
            if outcome != "recorded":
                print(
                    f"pr_verification: {target}#{pr_number} — {outcome}. The row was "
                    f"OPEN at lookup and is not now, so nothing was written.",
                    file=sys.stderr,
                )
                return 1
            committed = f"{verdict} recorded on {target}#{pr_number}, row still OPEN"
            print(
                f"pr_verification: {target}#{pr_number} STAYS OPEN — {verdict} recorded "
                f"at {now}. The obligation is not discharged."
            )
            if verdict == "fail-intent":
                print(
                    "  This is a FAILED verification: bring it to the user as a "
                    "conversation (never an automatic rollback) and file the defect."
                )
                return 0
            gaps = decision.derivation.gaps
            for g in gaps:
                if g.startswith(NOTE_PREFIXED_GAPS):
                    print(f"  {g}")
            if any(g.startswith(NOT_DEPLOYED) for g in gaps):
                print(
                    "  Deployment was not established, so nothing measured here is a "
                    "finding about the PR. Deploy the merge and re-run."
                )
            elif any(g.startswith(SUSPECTED_FAILURE) for g in gaps):
                print(
                    "  A failure was SUSPECTED but not established. Measure it if you "
                    "can; until then it is a named gap on an open row, not a finding."
                )
            return 0
    except DatabaseIntegrityError as exc:
        print(f"pr_verification: refusing to write — {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        if committed:
            # The write landed; only reporting it failed. Saying otherwise sends the
            # validator to retry against a row that has already changed.
            print(
                f"pr_verification: the write COMMITTED ({committed}), but finishing "
                f"the run failed — {type(exc).__name__}: {exc}. Do not re-run; read "
                f"the row with `repo_pulse_worker.py --verification-log --pr "
                f"{pr_number}`.",
                file=sys.stderr,
            )
            return 0
        # A lock held past busy_timeout, a malformed file, a full disk: the ledger
        # declined, which is exit 1 by this tool's contract — not a traceback. Each
        # writer commits in one statement, and none had committed, so nothing was
        # written.
        print(
            f"pr_verification: the ledger declined the write — {type(exc).__name__}: "
            f"{exc}. Nothing was recorded.",
            file=sys.stderr,
        )
        return 1


class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # json.loads keeps the LAST of two equal keys without a word, so a document with
    # "verdict" twice records whichever the author wrote second. Refuse instead.
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(
                f"the evidence document repeats the key {key!r} in one object; JSON "
                f"would keep only the last one silently, so it is refused"
            )
        out[key] = value
    return out


def _refuse(message: str, code: int) -> int:
    print(f"pr_verification: {message}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    from genesis.db.crud import pr_verifications as crud

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "print-schema", help="print a fill-in evidence template and exit (no database access)"
    )

    close = sub.add_parser("close", help="record a validation against an OPEN obligation")
    close.add_argument(
        "--pr",
        type=int,
        required=True,
        help="PR number — must equal the document's own 'pr' (a deliberate second copy "
        "that catches a typo inside the document)",
    )
    close.add_argument(
        "--evidence-file",
        required=True,
        help="path to the evidence JSON document (run `print-schema` for a template). "
        "Write it OUTSIDE the repo tree, e.g. under ~/.genesis/output/",
    )
    close.add_argument(
        "--verdict",
        choices=crud.VERDICTS,
        default=None,
        help="OPTIONAL expectation: refused unless the document derives this verdict",
    )
    close.add_argument(
        "--park",
        action="store_true",
        help="record cannot-verify for a document that derives pass-with-measured-gaps, "
        "because nothing MATERIAL was established (needs --note; never on a failure)",
    )
    close.add_argument(
        "--note",
        default=None,
        help="required whenever the row stays open: why the verification could not be "
        "completed, or what failed",
    )
    close.add_argument(
        "--dry-run", action="store_true", help="show what would be written, write nothing"
    )
    close.add_argument("--db-path", default=None, help="override the database path")

    args = parser.parse_args(argv)

    if args.command == "print-schema":
        print(json.dumps(SCHEMA_TEMPLATE, indent=2))
        return 0

    path = Path(args.evidence_file).expanduser()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _refuse(f"cannot read evidence file {path}: it does not exist", 2)
    except OSError as exc:
        return _refuse(f"cannot read evidence file {path}: {exc}", 2)
    if len(raw) > MAX_EVIDENCE_BYTES:
        return _refuse(
            f"evidence file is {len(raw)} bytes, over the {MAX_EVIDENCE_BYTES}-byte "
            f"cap — REFUSED rather than cut, because a truncated evidence record still "
            f"looks complete.",
            2,
        )
    try:
        doc = validate_evidence(json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates))
    except UnicodeDecodeError as exc:
        return _refuse(f"evidence file is not UTF-8: {exc}", 2)
    except json.JSONDecodeError as exc:
        return _refuse(f"evidence file is not valid JSON: {exc}", 2)
    except _DuplicateKey as exc:
        return _refuse(str(exc), 2)
    except EvidenceError as exc:
        return _refuse(f"the evidence document is malformed:\n{exc}", 2)

    if doc.pr != args.pr:
        return _refuse(
            f"the evidence document says it verifies PR #{doc.pr} but --pr is "
            f"{args.pr}. A closed row cannot be amended, so this mismatch is refused "
            f"rather than resolved in your favour — correct whichever one is wrong.",
            2,
        )

    result = decide(doc, park=args.park, asserted=args.verdict, note=args.note)
    if isinstance(result, Refusal):
        return _refuse(result.message, result.code)

    # The cap is enforced on the bytes that get STORED, not the bytes that were read:
    # the canonical re-serialisation can be larger than the file, and checking only
    # the file would let an over-cap record into the column the cap exists to bound.
    # The CRUD enforces the same cap again, because it owns the column.
    # ensure_ascii=False: the escaped form spends up to 12 bytes on one non-ASCII
    # character, which would refuse an honest document for its alphabet.
    payload = json.dumps(doc.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
    size = len(payload.encode("utf-8"))
    if size > MAX_EVIDENCE_BYTES:
        return _refuse(
            f"the evidence document serialises to {size} bytes, over the "
            f"{MAX_EVIDENCE_BYTES}-byte cap. REFUSED rather than cut, because a "
            f"truncated evidence record still looks complete.",
            2,
        )

    from genesis.env import genesis_db_path

    return asyncio.run(
        _write(
            db_path=args.db_path or str(genesis_db_path()),
            doc=doc,
            decision=result,
            note=args.note,
            payload=payload,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
