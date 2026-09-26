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
        --verdict pass-mechanical --evidence-file ~/.genesis/output/prv-2273.json
    python3 scripts/pr_verification.py close --pr 1573 \\
        --verdict cannot-verify --note "needs an install with no graph engine" \\
        --evidence-file ~/.genesis/output/prv-1573.json

THE FOUR VERDICTS (owner standing ruling, 2026-09-26), and which write each takes:

  pass-mechanical          CLOSES the row. The claim holds, measured.
  pass-with-measured-gaps  CLOSES the row, with the gaps named in scope_limits.
  fail-intent              LEAVES IT OPEN. The merged change does not do what it
                           claimed. Bring it to the USER as a conversation — never
                           an automatic rollback — and file the defect.
  cannot-verify            LEAVES IT OPEN. The attempt could not reach a verdict
                           here; --note names the precondition a later validator
                           needs.

A non-closing verdict is not a failure of this tool; it is the tool working. The
obligation survives because the work is not done, and the note is what stops the
next validator re-deriving why.

"I DID NOT GET TO IT" IS NOT ``cannot-verify``. The test is whether you could have
done it with the access and the time you had. If yes it is not-yet-done: leave the
row alone. Keep cannot-verify RARE, or the ledger fills with parked rows and the
backlog stops meaning anything.

WHERE THE DATABASE IS, and the trap that costs a confusing run: ``genesis_db_path()``
resolves RELATIVE TO THE REPO ROOT, so running this from a linked worktree points
it at that worktree's ``data/genesis.db``, which does not exist — the tool then
correctly reports "no database" while the real ledger sits in the main checkout.
Run it from the main checkout, or pass ``--db-path``. MEASURED while building this;
the failure is silent in the sense that the message is true and the diagnosis is
not the one you would guess.

EVIDENCE. ``print-schema`` emits a fill-in template; the shape is validated before
anything is read from the database. Two fields are required for a reason worth
stating: a closed row cannot be amended through this tool, so a mispasted
document — right shape, wrong PR — would be permanent and undetectable. ``pr``
must match ``--pr``, and ``merge_commit`` names which artifact was checked, because
``deploy.method`` alone cannot answer that.

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
missing note, a PR mismatch); 3 refused on policy (a failing claim in the document).
Deliberately meaningful, which is why this is a separate script rather than a
subcommand of ``repo_pulse_worker.py``: that module's documented contract is that
its exit code is always 0. Issue filed to revisit the split.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

#: Evidence tiers, in the vocabulary CLAUDE.md's evidence rule already uses, so a
#: validator is not asked to learn a second one.
TIERS = ("MEASURED", "INFERRED", "READ", "NOT_VERIFIABLE_HERE")

#: How deployment was established. ``ancestry`` is listed last and is the weakest.
DEPLOY_METHODS = ("behaviour", "content", "ancestry")

#: Per-CLAIM verdicts — whether that one claim held. Distinct from the four
#: SESSION verdicts, which are the judgement ACROSS claims.
CLAIM_VERDICTS = ("pass", "fail")

#: Serialised-evidence ceiling. The column is untyped TEXT and SQLite enforces no
#: length, so an unbounded document is a decision rather than a budget. This is a
#: stated one: 256 KiB is four times GitHub's PR-body cap, which is the largest
#: single artifact a validator would quote. An over-cap document is REFUSED rather
#: than cut, because a truncated evidence record still looks complete.
MAX_EVIDENCE_BYTES = 256 * 1024


class EvidenceError(ValueError):
    """The evidence document does not satisfy the shape. The message names the field."""


def validate_evidence(doc: object) -> dict:
    """Return *doc* unchanged if it is a well-formed evidence document.

    Raises :class:`EvidenceError` naming the offending field otherwise. Shape
    errors raise rather than coerce: a validator that mistyped a tier has a bug,
    and quietly accepting it would put an unlabelled claim into permanent record,
    which is the one thing the tier field exists to prevent.
    """
    if not isinstance(doc, dict):
        raise EvidenceError(f"evidence must be a JSON object, got {type(doc).__name__}")

    pr = doc.get("pr")
    if not isinstance(pr, int) or isinstance(pr, bool):
        raise EvidenceError(
            "evidence.pr must be the integer PR number this document verifies — a "
            "closed row cannot be amended, so a mispasted document would be permanent"
        )
    if not str(doc.get("merge_commit") or "").strip():
        raise EvidenceError(
            "evidence.merge_commit must name the commit that was verified — 'which "
            "artifact did you check' is the question deploy.method cannot answer"
        )

    deploy = doc.get("deploy")
    if not isinstance(deploy, dict):
        raise EvidenceError("evidence.deploy must be an object with 'method' and 'detail'")
    if deploy.get("method") not in DEPLOY_METHODS:
        raise EvidenceError(
            f"evidence.deploy.method must be one of {list(DEPLOY_METHODS)}, "
            f"got {deploy.get('method')!r}"
        )
    if not str(deploy.get("detail") or "").strip():
        raise EvidenceError(
            "evidence.deploy.detail must say HOW deployment was established — the "
            "method name alone is not evidence"
        )

    claims = doc.get("claims")
    if not isinstance(claims, list) or not claims:
        raise EvidenceError("evidence.claims must be a non-empty list")
    for i, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise EvidenceError(
                f"evidence.claims[{i}] must be an object, got {type(claim).__name__}"
            )
        for field in ("claim", "measurement"):
            if not str(claim.get(field) or "").strip():
                raise EvidenceError(f"evidence.claims[{i}].{field} must be non-empty")
        if claim.get("verdict") not in CLAIM_VERDICTS:
            raise EvidenceError(
                f"evidence.claims[{i}].verdict must be one of {list(CLAIM_VERDICTS)}, "
                f"got {claim.get('verdict')!r}"
            )
        if claim.get("tier") not in TIERS:
            raise EvidenceError(
                f"evidence.claims[{i}].tier must be one of {list(TIERS)}, got {claim.get('tier')!r}"
            )

    for field in ("controls", "scope_limits"):
        if not isinstance(doc.get(field, []), list):
            raise EvidenceError(f"evidence.{field} must be a list when present")

    findings = doc.get("findings", [])
    if not isinstance(findings, list):
        raise EvidenceError("evidence.findings must be a list when present")
    for i, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise EvidenceError(
                f"evidence.findings[{i}] must be an object, got {type(finding).__name__}"
            )
        for field in ("summary", "disposition"):
            if not str(finding.get(field) or "").strip():
                raise EvidenceError(
                    f"evidence.findings[{i}].{field} must be non-empty — an "
                    f"undispositioned finding is a drop wearing a record"
                )
    return doc


def failed_claims(doc: dict) -> list[str]:
    """Claim texts whose verdict is ``fail``.

    Non-empty refuses a CLOSING verdict whatever the headline says: a document can
    carry a failing claim under a passing verdict, and the row must not be
    discharged on it. The right verdict in that case is ``fail-intent``.
    """
    return [
        str(c.get("claim"))
        for c in doc.get("claims", [])
        if isinstance(c, dict) and c.get("verdict") == "fail"
    ]


def unverifiable_claims(doc: dict) -> list[str]:
    """Claim texts at tier ``NOT_VERIFIABLE_HERE``.

    Non-empty refuses ``pass-mechanical``: a row stamped a clean mechanical pass
    while carrying a claim nothing established is a false record, and the whole
    reason the tier exists is to make that visible.
    """
    return [
        str(c.get("claim"))
        for c in doc.get("claims", [])
        if isinstance(c, dict) and c.get("tier") == "NOT_VERIFIABLE_HERE"
    ]


def build_reason(*, verdict: str, doc: dict) -> str:
    """The ``closed_reason`` for a CLOSING verdict: the verdict plus a tier census.

    Generated here rather than typed by the caller so the record cannot drift into
    however each validator happens to phrase it.
    """
    tiers = [str(c.get("tier")) for c in doc.get("claims", []) if isinstance(c, dict)]
    census = ", ".join(f"{tiers.count(t)} {t}" for t in TIERS if t in tiers)
    return f"{verdict.upper()} — {len(tiers)} claim(s): {census}"


def resolve_repo(open_repos: list[str], pr_number: int, given: str | None) -> str:
    """The repo whose OPEN row this write targets.

    ``--repo`` is CHECKED against the open set rather than trusted. Unchecked it
    closed a DIFFERENT repository's obligation with this PR's evidence, at exit 0,
    permanently — MEASURED end-to-end on an earlier draft, with the real row left
    open so nothing surfaced the mistake. ``(repo, pr_number)`` is the row identity
    precisely because two repos can each hold a PR #12, and the flag exists only as
    the escape hatch for the ambiguity refusal below, which made it the one path
    with no check.
    """
    if given:
        if given not in open_repos:
            raise LookupError(
                f"no OPEN row for {given}#{pr_number}. Open rows for PR #{pr_number}: "
                f"{', '.join(open_repos) if open_repos else '(none)'}. Run "
                f"`python3 scripts/repo_pulse_worker.py --verification-backlog`."
            )
        return given
    if not open_repos:
        raise LookupError(
            f"no OPEN row for PR #{pr_number} — it may be discharged already, or "
            f"never opened. Run `python3 scripts/repo_pulse_worker.py "
            f"--verification-backlog` to see the open set."
        )
    if len(open_repos) > 1:
        raise LookupError(
            f"PR #{pr_number} is open for several repos ({', '.join(open_repos)}) — "
            f"pass --repo to say which."
        )
    return open_repos[0]


SCHEMA_TEMPLATE = {
    "pr": 1234,
    "merge_commit": "<sha of the artifact you actually verified>",
    "deploy": {
        "method": "behaviour | content | ancestry",
        "detail": "HOW deployment was established — ancestry is never a negative verdict",
    },
    "claims": [
        {
            "claim": "what the PR said it would do",
            "verdict": "pass | fail",
            "tier": "MEASURED | INFERRED | READ | NOT_VERIFIABLE_HERE",
            "measurement": "the number with its denominator, or the artifact + location",
        }
    ],
    "controls": [
        "the arm that had to come out the OTHER way, and did — without one a "
        "verification is a story"
    ],
    "scope_limits": ["what this install could not reach, named"],
    "findings": [{"summary": "what you found", "disposition": "issue #N | note | none"}],
}


async def _write(
    *,
    db_path: str,
    repo: str | None,
    pr_number: int,
    verdict: str,
    note: str | None,
    doc: dict,
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
                target = resolve_repo(open_repos, pr_number, repo)
            except LookupError as exc:
                print(f"pr_verification: {exc}", file=sys.stderr)
                return 1

            closing = verdict in crud.PASS_VERDICTS
            payload = json.dumps(doc, indent=2, sort_keys=True)
            reason = build_reason(verdict=verdict, doc=doc) if closing else None

            if dry_run:
                print("pr_verification: DRY RUN — nothing written.")
                print(f"  target   : {target}#{pr_number}")
                print(
                    f"  verdict  : {verdict}  ({'CLOSES the row' if closing else 'row STAYS OPEN'})"
                )
                if closing:
                    print(f"  reason   : {reason}")
                else:
                    print(f"  note     : {note}")
                print(f"  evidence : {len(payload)} bytes")
                return 0

            from datetime import datetime

            now = datetime.now(UTC).isoformat()

            if closing:
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
                print(f"pr_verification: CLOSED {target}#{pr_number} — {verdict} at {now}")
                return 0

            outcome = await crud.record_attempt(
                db,
                repo=target,
                pr_number=pr_number,
                verdict=verdict,
                note=str(note),
                now=now,
            )
            if outcome != "recorded":
                print(
                    f"pr_verification: {target}#{pr_number} — {outcome}. The row was "
                    f"OPEN at lookup and is not now, so nothing was written.",
                    file=sys.stderr,
                )
                return 1
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
    except DatabaseIntegrityError as exc:
        print(f"pr_verification: refusing to write — {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    from genesis.db.crud import pr_verifications as crud

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "print-schema", help="print a fill-in evidence template and exit (no database access)"
    )

    close = sub.add_parser("close", help="record a validation against an OPEN obligation")
    close.add_argument("--pr", type=int, required=True, help="PR number")
    close.add_argument(
        "--verdict",
        choices=crud.VERDICTS,
        required=True,
        help="the session verdict; the two pass-* verdicts close the row, the other "
        "two record an attempt and leave it open",
    )
    close.add_argument(
        "--note",
        default=None,
        help="required for fail-intent and cannot-verify: why the verification could "
        "not be completed, or what failed",
    )
    close.add_argument(
        "--repo",
        default=None,
        help="OWNER/REPO — needed only when the same PR number is open for several "
        "repos; it is checked against the open set, never trusted",
    )
    close.add_argument(
        "--evidence-file",
        required=True,
        help="path to the evidence JSON document (run `print-schema` for a template). "
        "Write it OUTSIDE the repo tree, e.g. under ~/.genesis/output/",
    )
    close.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the target and render the record without writing anything",
    )
    close.add_argument("--db-path", default=None, help="genesis.db path")

    args = parser.parse_args(argv)

    if args.command == "print-schema":
        print(json.dumps(SCHEMA_TEMPLATE, indent=2))
        return 0

    closing = args.verdict in crud.PASS_VERDICTS

    if not closing and not str(args.note or "").strip():
        print(
            f"pr_verification: --verdict {args.verdict} requires --note. The row stays "
            f"OPEN, and the note is the whole value of that: it is what stops the next "
            f"validator re-deriving why this could not be finished.\n"
            f"  And if you COULD have finished it with the access and time you had, "
            f"this is not cannot-verify — it is not-yet-done. Leave the row alone.",
            file=sys.stderr,
        )
        return 2
    if closing and str(args.note or "").strip():
        print(
            f"pr_verification: --note is only recorded for a non-closing verdict, and "
            f"would be silently discarded under {args.verdict}. A closing record's prose "
            f"belongs in the evidence document (scope_limits / findings).",
            file=sys.stderr,
        )
        return 2

    path = Path(args.evidence_file).expanduser()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(f"pr_verification: cannot read evidence file: {exc}", file=sys.stderr)
        return 2
    if len(raw) > MAX_EVIDENCE_BYTES:
        print(
            f"pr_verification: evidence document is {len(raw)} bytes, over the "
            f"{MAX_EVIDENCE_BYTES}-byte cap — REFUSED rather than cut, because a "
            f"truncated evidence record still looks complete. Quote less, or link out.",
            file=sys.stderr,
        )
        return 2
    try:
        doc = validate_evidence(json.loads(raw.decode("utf-8")))
    except UnicodeDecodeError as exc:
        print(f"pr_verification: evidence file is not UTF-8: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"pr_verification: evidence file is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except EvidenceError as exc:
        print(f"pr_verification: {exc}", file=sys.stderr)
        return 2

    if doc["pr"] != args.pr:
        print(
            f"pr_verification: the evidence document says it verifies PR #{doc['pr']} "
            f"but --pr is {args.pr}. A closed row cannot be amended, so this mismatch "
            f"is refused rather than resolved in your favour.",
            file=sys.stderr,
        )
        return 2

    failures = failed_claims(doc)
    if failures and closing:
        print(
            f"pr_verification: REFUSING a closing verdict — {len(failures)} claim(s) "
            f"have verdict 'fail':",
            file=sys.stderr,
        )
        for claim in failures:
            print(f"  FAILED: {claim}", file=sys.stderr)
        print(
            "A failing claim does not discharge the obligation. The verdict for this "
            "document is 'fail-intent': the row stays open, you bring it to the user "
            "as a conversation, and the defect gets filed.",
            file=sys.stderr,
        )
        return 3

    # 'pass-with-measured-gaps' means the gaps are MEASURED AND NAMED. Without a
    # floor the verdict degrades to "pass, and I gestured at some gaps" — and the
    # refusal below actively ROUTES validators onto this verdict, so the tool would
    # be steering them into an unamendable record with the gaps missing.
    if args.verdict == "pass-with-measured-gaps" and not [
        g for g in doc.get("scope_limits", []) if str(g).strip()
    ]:
        print(
            "pr_verification: 'pass-with-measured-gaps' requires at least one entry "
            "in evidence.scope_limits — the verdict's whole content is WHICH gaps, "
            "and a closed row cannot be amended to add them later. Name them, or use "
            "'pass-mechanical' if there are none.",
            file=sys.stderr,
        )
        return 2

    unverifiable = unverifiable_claims(doc)
    if unverifiable and args.verdict == "pass-mechanical":
        print(
            f"pr_verification: {len(unverifiable)} claim(s) are tier "
            f"NOT_VERIFIABLE_HERE, so this is not a clean mechanical pass. Use "
            f"'pass-with-measured-gaps' if the rest holds and these are the named "
            f"gaps, or 'cannot-verify' if nothing material was established:",
            file=sys.stderr,
        )
        for claim in unverifiable:
            print(f"  NOT VERIFIABLE HERE: {claim}", file=sys.stderr)
        return 2

    from genesis.env import genesis_db_path

    return asyncio.run(
        _write(
            db_path=args.db_path or str(genesis_db_path()),
            repo=args.repo,
            pr_number=args.pr,
            verdict=args.verdict,
            note=args.note,
            doc=doc,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
