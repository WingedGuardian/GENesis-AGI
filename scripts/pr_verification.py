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
anything is read from the database, and :func:`assess` then decides whether the
document EARNS the verdict asked of it. That split is deliberate: shape is about the
document being well formed, sufficiency is about it establishing something.

Three fields are required for a reason worth stating: a closed row cannot be amended
through this tool, so a mispasted document — right shape, wrong row — would be
permanent and undetectable. ``repo`` and ``pr`` together ARE the row's key, and the
document has to name both halves or it can be pointed at a different repository's
obligation at the same PR number; ``merge_commit`` names which artifact was checked,
because ``deploy.method`` alone cannot answer that. ``--repo`` is only a
disambiguator for when several repositories hold an open row for one PR, and a
``--repo`` that contradicts the document is refused rather than allowed to win.

EVERY RULE A DOCUMENT MUST SATISFY LIVES IN :func:`assess`, as a numbered
enumeration, and that is a direct answer to a review round: seven findings on this
file, six of them the same shape — *nothing checks X about the document*. They were
six rather than one because the rules were distributed across a shape validator, the
CLI and the writer, so the SET was written down nowhere and a missing rule was
invisible instead of conspicuous.

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
    repo = doc.get("repo")
    if isinstance(repo, str) and repo != repo.strip() and repo.strip():
        # Refused rather than stripped, and named explicitly: compared against the open
        # set, a padded slug fails membership and the refusal prints two spellings that
        # look identical on a terminal. Silently trimming would be the other failure —
        # a coercion inside the one check that exists to be exact.
        raise EvidenceError(
            f"evidence.repo has leading or trailing whitespace ({repo!r}). It is half "
            f"of the ledger row's key and is compared exactly, so it is refused rather "
            f"than trimmed — a padded slug looks identical to a correct one"
        )
    if not isinstance(repo, str) or not repo.strip():
        raise EvidenceError(
            "evidence.repo must be the 'owner/name' this document verifies. The row's "
            "key is (repo, pr_number) and the document has to name BOTH halves of it, "
            "or a document written for one repository closes another repository's "
            "obligation at the same PR number — permanently, since a closed row cannot "
            "be amended through this tool"
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
        entries = doc.get(field, [])
        if not isinstance(entries, list):
            raise EvidenceError(f"evidence.{field} must be a list when present")
        # Both lists are read as prose by a human and by the closing-verdict floors
        # below, which test them with str(x).strip(). A nested object or a null would
        # therefore pass the floor as the truthy text "{}" or "None" — the floor would
        # be satisfied by something that names nothing.
        for i, entry in enumerate(entries):
            if not isinstance(entry, str) or not entry.strip():
                raise EvidenceError(
                    f"evidence.{field}[{i}] must be a non-empty string, got "
                    f"{entry!r} — these entries are the record a later reader acts on"
                )

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


def assess(
    doc: dict, *, verdict: str, closing: bool, pr: int, repo_override: str | None
) -> tuple[int, str] | None:
    """Every rule a document must satisfy to support *verdict*, in ONE enumeration.

    Returns ``(exit_code, message)`` on refusal and ``None`` when the document earns
    the verdict. Pure: it touches no database, so the entire policy surface is
    testable without one.

    WHY THIS IS ONE FUNCTION. An external review round produced seven findings on
    this file, six of them the same shape — *"nothing checks X about the document"*.
    They were six rather than one because the document's rules were distributed:
    shape in :func:`validate_evidence`, binding half in the CLI and half in the
    writer, sufficiency as three ad-hoc branches added one incident at a time. Each
    rule therefore had to be remembered independently and the SET was written down
    nowhere, so a missing rule was invisible rather than conspicuous. Here the next
    missing rule is a gap in a numbered list.

    TWO POLARITIES, deliberately. Rules 3-6 are a CEILING — certain contents
    disqualify certain verdicts. Rules 7-8 are a FLOOR — a closing verdict must
    positively establish something. The original code had only the ceiling, and that is
    precisely how it failed: every disqualifying shape anyone had thought of was
    refused, and a document whose claims were EVERY ONE of them NOT_VERIFIABLE_HERE
    satisfied all of them and closed the row having established nothing. A denylist
    can only ever refuse the cases its author imagined.
    """
    # 1 — BINDING. The row's key is (repo, pr_number) and the document names both, so
    # it cannot be mispasted onto a different row. What makes a silent mis-bind
    # permanent is that a closed row cannot be amended through this tool.
    if doc["pr"] != pr:
        return 2, (
            f"the evidence document says it verifies PR #{doc['pr']} but --pr is {pr}. "
            f"A closed row cannot be amended, so this mismatch is refused rather than "
            f"resolved in your favour."
        )
    if repo_override and repo_override != doc["repo"]:
        return 2, (
            f"--repo says {repo_override!r} and the evidence document says "
            f"{doc['repo']!r}. Refused rather than picking one: the DOCUMENT is the "
            f"record, and --repo exists only to disambiguate when several repositories "
            f"hold an open row for this PR. Drop --repo, or correct the document."
        )

    failures = failed_claims(doc)
    unverifiable = unverifiable_claims(doc)

    # 2 — the headline does not overrule the document's own contents. A document can
    # carry a failing claim under a passing verdict.
    #
    # NOT gated on `closing`, and an adversarial audit is why. Gated, a document with a
    # MEASURED failing claim could be filed as 'cannot-verify' — which parks the row
    # and never prints the standing-ruling escalation below, so a measured failure
    # becomes a PR nobody could verify. A failure is a failure whatever the headline
    # says; the only verdict it supports is 'fail-intent'. This makes rules 2 and 5
    # exact converses: a failing claim implies fail-intent, and fail-intent implies a
    # failing claim.
    if failures and verdict != "fail-intent":
        return 3, (
            f"REFUSING '{verdict}' — {len(failures)} claim(s) have verdict 'fail':\n"
            + "".join(f"  FAILED: {c}\n" for c in failures)
            + "A failing claim does not discharge the obligation, and it is not a gap "
            "or an unreachable check either. The verdict for this document is "
            "'fail-intent': the row stays open, you bring it to the user as a "
            "conversation, and the defect gets filed."
        )

    # 3 — 'pass-mechanical' means CLEAN; an unverifiable claim makes it not that.
    if unverifiable and verdict == "pass-mechanical":
        return 2, (
            f"{len(unverifiable)} claim(s) are tier NOT_VERIFIABLE_HERE, so this is "
            f"not a clean mechanical pass. Use 'pass-with-measured-gaps' if the rest "
            f"holds and these are the named gaps, or 'cannot-verify' if nothing "
            f"material was established:\n"
            + "".join(f"  NOT VERIFIABLE HERE: {c}\n" for c in unverifiable).rstrip()
        )

    # 4 — 'pass-with-measured-gaps' means the gaps are measured AND NAMED. Without a
    # floor the verdict degrades to "pass, and I gestured at some gaps" — and rules 3
    # and 6 actively ROUTE validators onto this verdict, so the tool would be steering
    # them into an unamendable record with the gaps missing.
    if verdict == "pass-with-measured-gaps" and not [
        g for g in doc.get("scope_limits", []) if str(g).strip()
    ]:
        return 2, (
            "'pass-with-measured-gaps' requires at least one entry in "
            "evidence.scope_limits — the verdict's whole content is WHICH gaps, and a "
            "closed row cannot be amended to add them later. Name them"
            # Only offer pass-mechanical when rule 3 would actually accept it. With an
            # unverifiable claim present it would not, and a refusal that routes onto a
            # verdict the next rule refuses is how a validator ends up cycling.
            + ("." if unverifiable else ", or use 'pass-mechanical' if there are none.")
        )

    # 5 and 6 — a NON-closing verdict is bound to the document too. The row stays open
    # either way, so this is not about protecting the ledger from a false closure: it
    # is that the row is what the NEXT validator reads, and a 'fail-intent' whose every
    # claim passed, or a 'cannot-verify' where everything was in fact verified, is a
    # headline its own evidence contradicts.
    if verdict == "fail-intent" and not failures:
        return 2, (
            "'fail-intent' requires at least one claim with verdict 'fail' — that "
            "verdict says the merged change does not do what it claimed, and the claim "
            "it failed is the substance of it. If nothing failed but you could not "
            "finish, the verdict is 'cannot-verify'."
        )
    # 6 is the exact COMPLEMENT of rule 8, not an independent floor, and an
    # adversarial audit caught the difference as a BLOCKER. Written as "requires a
    # NOT_VERIFIABLE_HERE claim", it deadlocked a document whose claims were all
    # INFERRED — a tier the skill explicitly blesses: rule 8 refused every closing
    # verdict and pointed at 'cannot-verify', and this rule refused 'cannot-verify' and
    # pointed back at a passing one. All four verdicts exited 2, the row was left
    # looking NEVER ATTEMPTED, and the only way out was to relabel the tier. An
    # enumeration that pays a validator to falsify the one field validate_evidence
    # exists to protect is worse than no enumeration.
    #
    # So the predicate is the one that matches the verdict's meaning: 'cannot-verify'
    # is refused only when the document establishes EVERYTHING, because that is the one
    # case where a passing verdict is available. Anything less than fully established —
    # inferred, unreachable, partial — is exactly what the verdict is for.
    if verdict == "cannot-verify" and all(
        c.get("verdict") == "pass" and c.get("tier") in ("MEASURED", "READ") for c in doc["claims"]
    ):
        return 2, (
            "'cannot-verify' says this install could not reach the answer, but every "
            "claim in this document is passing and at tier MEASURED or READ — it "
            "reached all of them. Use 'pass-mechanical', or "
            "'pass-with-measured-gaps' if there are gaps to name."
        )

    # 7 — a control, for the CLEAN pass only. An arm that had to come out the other
    # way, and did. Without one a verification cannot separate "the change did this"
    # from "this was already true" — which is how two of three findings in this tool's
    # own pilot round turned out to be false. Scoped to pass-mechanical on purpose:
    # "claim X has no control" is a legitimate MEASURED GAP, and
    # pass-with-measured-gaps is the verdict for a document carrying one, so demanding
    # a control there would leave no verdict for the honest case. Rule 4 still forces
    # that gap to be named.
    if verdict == "pass-mechanical" and not [c for c in doc.get("controls", []) if str(c).strip()]:
        return 2, (
            "'pass-mechanical' requires at least one entry in evidence.controls — the "
            "arm that had to come out the other way, and did. Without a control a "
            "clean pass cannot distinguish the change working from the property already "
            "holding. Name one, or use 'pass-with-measured-gaps' with the missing "
            "control named in scope_limits."
        )

    # 8 — THE FLOOR, and the rule whose absence was the defect. At least one claim has
    # to be both passing AND at a tier that establishes things. INFERRED does not
    # count: CLAUDE.md's own rule is that an inferred claim never enters permanent
    # record in the grammar of a fact, and a discharged obligation is permanent record.
    # LAST on purpose — every rule above can name the offending claim or the verdict
    # that fits, and this one can only say that nothing was established, so it runs
    # once the specific diagnoses have had their turn.
    if closing and not [
        c
        for c in doc["claims"]
        if c.get("verdict") == "pass" and c.get("tier") in ("MEASURED", "READ")
    ]:
        return 2, (
            "REFUSING a closing verdict — not one claim is both passing and at tier "
            "MEASURED or READ, so this document would discharge the obligation while "
            "establishing nothing. That is the single state this ledger exists to make "
            "impossible. An INFERRED-only or NOT_VERIFIABLE_HERE-only verification is "
            "'cannot-verify': the row stays open with your note and the next validator "
            "inherits what you found instead of starting over."
        )

    return None


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
    # NOTHING open for this PR is a different fact from "that repo is not among the
    # candidates", and it is checked FIRST because it is the more likely one and its
    # message is the accurate one. The evidence document now always names a repo, so
    # `given` is effectively always set — before that, this branch was reached by
    # falling through, which is how the two facts came to share one ordering.
    if not open_repos:
        raise LookupError(
            f"no OPEN row for PR #{pr_number} — it may be discharged already, or "
            f"never opened. Run `python3 scripts/repo_pulse_worker.py "
            f"--verification-backlog` to see the open set."
        )
    if given:
        if given not in open_repos:
            raise LookupError(
                f"no OPEN row for {given}#{pr_number}. Open rows for PR #{pr_number}: "
                f"{', '.join(open_repos)}. Run "
                f"`python3 scripts/repo_pulse_worker.py --verification-backlog`."
            )
        return given
    if len(open_repos) > 1:
        raise LookupError(
            f"PR #{pr_number} is open for several repos ({', '.join(open_repos)}) — "
            f"pass --repo to say which."
        )
    return open_repos[0]


SCHEMA_TEMPLATE = {
    "repo": "<owner/name — with 'pr' this is the ledger row's key>",
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
                # The document names its repo (validate_evidence requires it), so
                # that is what gets resolved; --repo is only a disambiguator and
                # assess() has already refused the case where the two disagree. The
                # membership check inside resolve_repo is therefore what enforces the
                # binding — no second rule has to remember it.
                target = resolve_repo(open_repos, pr_number, repo or doc["repo"])
            except LookupError as exc:
                print(f"pr_verification: {exc}", file=sys.stderr)
                return 1

            closing = verdict in crud.PASS_VERDICTS
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
                evidence=payload,
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

    refusal = assess(
        doc,
        verdict=args.verdict,
        closing=closing,
        pr=args.pr,
        repo_override=args.repo,
    )
    if refusal is not None:
        code, message = refusal
        print(f"pr_verification: {message}", file=sys.stderr)
        return code

    # The cap is enforced on the bytes that get STORED, not the bytes that were read.
    # json.dumps(indent=2) re-serialises, and pretty-printing a document that fits
    # under the cap on disk can carry it over — so checking only the file would let an
    # over-cap record into the column the cap exists to bound.
    payload = json.dumps(doc, indent=2, sort_keys=True)
    if len(payload.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        print(
            f"pr_verification: the evidence document serialises to "
            f"{len(payload.encode('utf-8'))} bytes, over the {MAX_EVIDENCE_BYTES}-byte "
            f"cap. REFUSED rather than cut, because a truncated evidence record still "
            f"looks complete.",
            file=sys.stderr,
        )
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
            payload=payload,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
