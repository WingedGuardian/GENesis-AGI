#!/usr/bin/env python3
"""Entry point for the detached repo-pulse worker (session-manager PR-4a).

Spawned by the SessionStart hook at startup/resume/compact boundaries:

    python scripts/repo_pulse_worker.py --trigger session_start \
        [--db-path <genesis.db>]

Manual / E2E form (bypasses the 30-minute global debounce):

    python scripts/repo_pulse_worker.py --trigger manual --force \
        [--lookback-days 7]

Exit code is always 0 unless argument parsing fails — outcomes (including
errors) are recorded in repo_pulse_runs and the call-site telemetry row,
because nothing is attached to read a detached process's exit status.
Uncaught early failures land on stderr, which the hook redirects to
~/.genesis/session_awareness/repo_pulse_err.log.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sqlite3
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _print_verification_backlog(db_path: str | None) -> None:
    """The pr_verifications day-one reader: open obligations in BACKLOG order —
    FAILED rows first, then not-yet-done, then never-attempted, then parked ones,
    oldest merge first within each (``list_open``'s ORDER BY: an established failure
    is a finding the user must see, so no cap may hide it; after that a validator
    should reach an untouched row before one a colleague already tried).

    Read-only, no worker run, no debounce — usable while the Wave-3 validator
    session (the eventual consumer) does not exist yet. Prints one line per
    open row plus a status histogram; "0 open" with a nonzero closed count
    means the lane is running and everything recent was docs-exempt or
    verified, while an EMPTY histogram means the table is empty or
    pre-migration — two different states, both printed as what they are.
    """
    import asyncio as _asyncio

    from genesis.db.crud import pr_verifications as verif_crud
    from genesis.env import genesis_db_path

    resolved = db_path or str(genesis_db_path())

    if not Path(resolved).exists():
        # mode=ro below refuses to create the file, which is the point — but a
        # bare failure would read as a defect rather than "not set up yet".
        print(f"pr_verifications: no database at {resolved}")
        return

    # Admission fence (fail-closed): a quarantined
    # database is not opened even read-only — this worker runs automatically
    # at session boundaries, exactly the entry class the fence exists to bind.
    try:
        from genesis.db.admission import database_is_fenced

        fenced = database_is_fenced(resolved)
    except Exception:
        fenced = True
    if fenced:
        print("pr_verifications: database quarantined — skipped")
        return

    async def _read() -> tuple[list[dict], dict]:
        import aiosqlite

        # mode=ro, not a plain path: this command only REPORTS. A read-write
        # handle would create an empty database when pointed at a wrong path and
        # then truthfully report it as empty. `mode=ro` (not `immutable=1`) is
        # the WAL-aware read-only form — `immutable` ignores the -wal file and
        # would miss rows the worker committed moments earlier.
        #
        # The URI is BUILT, never interpolated. `?` and `#` are legal POSIX
        # filename characters with URI meaning, so an f-string let SQLite parse
        # part of a real path as a query or fragment: it opened a different,
        # shorter path — and could swallow `mode=ro` itself, turning the
        # report-only guarantee above into a read-write handle that CREATES
        # that unintended file, while the existence check above had validated
        # the original path and said nothing (Codex P2, PR #1836).
        uri = f"{Path(resolved).absolute().as_uri()}?mode=ro"
        async with aiosqlite.connect(uri, uri=True, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            return await verif_crud.list_open(db), await verif_crud.counts(db)

    # Exit 0 is this module's contract, and a file that is not a database raised
    # straight through it — a traceback and exit 1 (MEASURED; issue #2603). An
    # unreadable database is a state of its own, distinct from "no database" and
    # "no rows", so it is reported as one.
    try:
        rows, histogram = _asyncio.run(_read())
    except (sqlite3.Error, OSError) as exc:
        print(f"pr_verifications: database unreadable at {resolved} — {exc}")
        return
    if not histogram:
        print("pr_verifications: no rows (table empty or pre-migration)")
        return
    for row in rows:
        title = (row.get("pr_title") or "").strip()
        # The row's identity is (repo, pr_number), so printing the number alone
        # is a partial key: two repos — a fork, or a rename — can both hold a
        # PR #12, and the reader could neither tell which row was pending nor
        # supply the `repo` argument `close_verification` requires without
        # going to SQLite (Codex P2, PR #1836).
        print(
            f"OPEN  {row.get('repo') or '<unknown repo>'}#{row['pr_number']}  "
            f"merged {str(row['merged_at'])[:10]}  {title[:80]}"
        )
        # A PARKED row: a validator attempted it and could not discharge it. Shown
        # inline because the alternative is a validator re-deriving a conclusion
        # someone already reached — which is the whole reason the note is stored.
        # `verdict` is only ever set on an OPEN row by a non-closing attempt (a
        # pass closes the row), so its presence IS the parked signal.
        if row.get("verdict"):
            note = (row.get("last_attempt_note") or "").strip()
            tries = row.get("attempt_count") or 0
            print(
                f"      ATTEMPTED {tries}x  {row['verdict']}{'  — ' + _clip(note, 120, row['pr_number']) if note else ''}"
            )
    # `list_open` has its own row cap, so the lines above can be a SUBSET while
    # the histogram below reports the true total — printing both without saying
    # so lets the two numbers disagree in silence, and a reader who counts the
    # lines gets a wrong answer that looks complete. The omission is stated with
    # both numbers, which are known exactly here.
    open_total = histogram.get("open", 0)
    if len(rows) < open_total:
        print(
            f"  <listed the first {len(rows)} of {open_total} open row(s) in backlog "
            f"order — failed first, then not-yet-done, then never-attempted, then parked, "
            f"oldest merge first within each; "
            f"{open_total - len(rows)} not shown — close some, or query "
            f"pr_verifications directly for the full set>"
        )
    print(f"pr_verifications: {open_total} open, {histogram.get('closed', 0)} closed ({resolved})")
    print(
        "  close one: python3 scripts/pr_verification.py close --pr <N> "
        "--evidence-file <doc.json> [--note <why>]  (the verdict is derived from the "
        "document; run `pr_verification.py print-schema` for a template)   "
        "(--verification-log --pr <N> shows what was decided, on any row)"
    )


def _clip(text: str, limit: int, pr_number: object) -> str:
    """Return *text*, or its first *limit* characters with a marker SAYING it was cut.

    A one-line census has a width, so a long note or reason is shortened there — but
    silently shortened text still looks complete, which is how a reader acts on half a
    reason. The marker states how much is missing and names the read that shows it
    whole (a scoped ``--verification-log --pr N`` prints the row unclipped).
    """
    if len(text) <= limit:
        return text
    return (
        f"{text[:limit]}…(+{len(text) - limit} chars — "
        f"--verification-log --pr {pr_number} shows it whole)"
    )


#: Rows one `--verification-log` run will show. Named rather than inline so the
#: truncation disclosure below cannot drift from the value it describes.
_LOG_LIMIT = 500


def _print_verification_log(db_path: str | None, pr_number: int | None) -> None:
    """What a validator DECIDED about an obligation, and on what evidence.

    UNSCOPED it lists discharged rows — the census of what has been closed. SCOPED to a
    PR it shows every row for that PR, OPEN ones included, because an open row carrying
    a recorded attempt is exactly where the evidence document is most worth reading: a
    fail-intent is the highest-value record here and its row never closes. An
    adversarial audit measured that filtering on status made that row unreachable from
    every reader at once, which is this reader's own defect one row-state over.

    The counterpart to the backlog. Without it the evidence column is write-only —
    MEASURED before this shipped: nothing in ``src/`` or ``scripts/`` ever SELECTed
    it, while the daily retention timer deletes closed rows after 180 days
    (``prune_repo_pulse.py --verification-days``, default 180; ``disk_hygiene.sh``
    passes no override). A record nothing can read is not a record.

    Read-only, no worker run, no debounce — same contract and same guards as the
    backlog reader above, including the built-not-interpolated ``mode=ro`` URI.
    """
    import asyncio as _asyncio

    from genesis.db.crud import pr_verifications as verif_crud
    from genesis.env import genesis_db_path

    resolved = db_path or str(genesis_db_path())
    if not Path(resolved).exists():
        print(f"pr_verifications: no database at {resolved}")
        return
    try:
        from genesis.db.admission import database_is_fenced

        fenced = database_is_fenced(resolved)
    except Exception:
        fenced = True
    if fenced:
        print("pr_verifications: database quarantined — skipped")
        return

    async def _read() -> list[dict]:
        import aiosqlite

        uri = f"{Path(resolved).absolute().as_uri()}?mode=ro"
        async with aiosqlite.connect(uri, uri=True, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            # The filter goes to SQL, never to a Python pass over a capped page:
            # paging 500 and filtering after it reported "no closed rows for PR #N"
            # about a row that WAS closed, and blamed an empty table for it.
            #
            # A SCOPED read asks "what is the state of this obligation", which includes
            # an OPEN row carrying a recorded attempt — the state where the evidence
            # document is most worth reading, since a fail-intent is the highest-value
            # record here and its row never closes. Filtering on status made that row
            # unreachable from every reader at once.
            if pr_number is not None:
                return await verif_crud.list_for_pr(db, pr_number=pr_number)
            return await verif_crud.list_closed(db, limit=_LOG_LIMIT, pr_number=None)

    # This module's documented contract is exit 0 unless argument parsing fails, and a
    # corrupt or unreadable file raised straight through it — exit 1 and a traceback
    # (MEASURED on a file that is not a database). The read failing is a STATE worth
    # reporting as itself, distinct from "no database" and "no rows".
    try:
        rows = _asyncio.run(_read())
    except (sqlite3.Error, OSError) as exc:
        print(f"pr_verifications: database unreadable at {resolved} — {exc}")
        return
    if not rows:
        if pr_number is not None:
            print(
                f"pr_verifications: no rows for PR #{pr_number} — the table is empty, "
                f"pre-migration, or no obligation was ever opened for it. This read "
                f"covers OPEN rows too, so a parked attempt would have appeared here"
            )
        else:
            print(
                "pr_verifications: no closed rows — the table is empty, "
                "pre-migration, or nothing has been discharged yet"
            )
        return

    if pr_number is not None:
        # A SCOPED read is someone asking what the record SAYS about one obligation,
        # so it prints the rows themselves: every column, the evidence document parsed
        # back into structure, nothing clipped and nothing relabelled. Two review
        # rounds found defects in the formatted version of this path — a closed-only
        # count printed under a row reading OPEN, silent clips, a cap notice for a
        # query with no cap — and a faithful dump has no formatting to get wrong.
        for row in rows:
            record = dict(row)
            evidence = record.get("evidence")
            if isinstance(evidence, str):
                # A stored string that is not JSON is still the record: keep it as is.
                with contextlib.suppress(ValueError):
                    record["evidence"] = json.loads(evidence)
            print(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True, default=str))
        n_open = sum(1 for r in rows if r.get("status") != "closed")
        print(
            f"pr_verifications: {len(rows)} row(s) for PR #{pr_number} — "
            f"{n_open} open, {len(rows) - n_open} closed ({resolved})"
        )
        return

    # UNSCOPED: the census of discharged obligations, one line per row.
    for row in rows:
        # A NULL verdict is stated, never blanked: it means the row was closed
        # before verdicts existed, or by the deterministic docs-path exemption —
        # not that a validator reached no conclusion.
        verdict = row.get("verdict") or "<no verdict — auto-exempt by path, or pre-verdict>"
        print(
            f"CLOSED  {row.get('repo') or '<unknown repo>'}#{row['pr_number']}  "
            f"{str(row.get('closed_at') or '')[:19]}  {verdict}"
        )
        reason = (row.get("closed_reason") or "").strip()
        if reason:
            print(f"        reason  : {_clip(reason, 160, row['pr_number'])}")
        evidence = row.get("evidence")
        if evidence:
            # A census can carry 500 rows; printing every document would bury it.
            print(
                f"        evidence: {len(evidence)} chars "
                f"— read it with --verification-log --pr {row['pr_number']}"
            )
        else:
            print("        evidence: none recorded")
    # A listing whose length EQUALS its cap is a truncated read, and printing the
    # count alone lets a reader take it for a total — the same omission the backlog
    # reader states with both numbers. Say so rather than letting the two disagree
    # in silence.
    if len(rows) >= _LOG_LIMIT:
        print(
            f"  <listed the {_LOG_LIMIT} most recently closed; this is a CAPPED read, "
            f"not a total — narrow with --pr, or query pr_verifications directly>"
        )
    print(f"pr_verifications: {len(rows)} closed row(s) shown ({resolved})")


#: Rows one merged-PR search window may return before the listing counts as
#: capped. GitHub search stops at 1,000 results, so a window that reaches this
#: is incomplete by construction. MEASURED 2026-10-29 over the last 8 weeks:
#: 7-day windows held 47-114 merged PRs, so the cap is far from binding here.
_NEXT_WINDOW_LIMIT = 1000

#: `gh pr list --json` fields the pick needs: base and merge commit decide
#: deployment, the body carries the `E2E:` plan.
_NEXT_FIELDS = "number,title,body,mergedAt,baseRefName,mergeCommit"

_NEXT_TIER_LABEL = {0: "not-yet-done", 1: "never-attempted", 2: "cannot-verify, re-attempt"}


def _load_sibling(name: str):
    """Load ``scripts/<name>.py`` by path under a private module name.

    ``scripts/`` stays OFF ``sys.path`` (``deploy_candidates.py`` strips it on
    purpose: every entry there is consulted for every import). Registered before
    exec because the helpers' dataclasses resolve their module from
    ``sys.modules``, and popped on failure so a half-initialised entry cannot
    poison a later load, the hygiene ``git_push_guard._load_e2e_declaration``
    keeps. Raises when the module cannot be loaded.
    """
    import importlib.util

    private = f"_repo_pulse_worker_{name}"
    if private in sys.modules:
        return sys.modules[private]
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(private, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[private] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(private, None)
        raise
    return mod


def _print_next_for_validation(
    db_path: str | None,
    *,
    runner=None,
    root: Path | None = None,
    serving=None,
    today=None,
    now: str | None = None,
) -> None:
    """The ONE open obligation a standing validator should work next, or why none.

    Candidates come from ``list_validation_candidates`` (not-yet-done, then
    never-attempted, then a parked row past its re-attempt window; fail-intent
    never). This reader then asks GitHub, in 8-day windows stepping 7 days back
    from today, for each candidate's base branch, merge commit and body, and ranks
    within a tier: deployed with an ``E2E:`` plan, deployed without one, then
    deploy-unproven; newest merge first inside each.

    Deployment: on the default branch, a merge commit that is an ancestor of the
    serving commit (``deploy_code_only.sh status``) is deployed; one that is not,
    or that is absent from the local object store (a merge newer than the last
    fetch), is not yet deployed and is skipped. A STACKED PR (base is another
    branch) has a merge commit that never reached the default branch under its
    own number, so ancestry proves nothing about it either way (``pr_verification
    .py``: "DEPLOYMENT IS NOT ESTABLISHED BY ANCESTRY"). It is kept, ranked last
    and labelled, never dropped.

    Fails CLOSED, printing the cause and picking nothing, when GitHub cannot be
    read, a window is capped or dropped rows, the serving commit or default branch
    is unknown, git cannot answer, or a helper will not load: ranking on a partial
    picture picks the wrong row, and skipping one pass costs nothing.

    A candidate the listing does NOT hold is skipped and counted, not a failure:
    the listing itself was complete (neither capped nor lossy), so the row names a
    PR that is gone or not yet indexed, and either way it cannot be shown deployed.
    Rows spanning more than one repository are refused: the serving commit belongs
    to this checkout's repository only.

    The scan stops early once a window holds a deployed, planned row of the best
    tier among ALL candidates, since no older merge can outrank it. Candidates in
    windows it never reached are counted as such, never as missing from GitHub.

    Read-only; exit 0 always (this module's contract). ``runner``, ``root``,
    ``serving``, ``today`` and ``now`` are test seams.
    """
    import asyncio as _asyncio
    import os
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import pr_verifications as verif_crud
    from genesis.env import genesis_db_path

    resolved = db_path or str(genesis_db_path())
    if not Path(resolved).exists():
        print(f"pr_verifications: no database at {resolved}")
        return
    try:
        from genesis.db.admission import database_is_fenced

        fenced = database_is_fenced(resolved)
    except Exception:
        fenced = True
    if fenced:
        print("pr_verifications: database quarantined — skipped")
        return

    now_s = now or datetime.now(UTC).isoformat()

    async def _read() -> tuple[list[dict] | None, dict]:
        import aiosqlite

        uri = f"{Path(resolved).absolute().as_uri()}?mode=ro"
        async with aiosqlite.connect(uri, uri=True, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            if not await verif_crud.tables_available(db):
                return None, {}
            return (
                await verif_crud.list_validation_candidates(db, now=now_s),
                await verif_crud.counts(db),
            )

    try:
        candidates, histogram = _asyncio.run(_read())
    except (sqlite3.Error, OSError) as exc:
        print(f"pr_verifications: database unreadable at {resolved} — {exc}")
        return
    if candidates is None:
        print("pr_verifications: table absent (pre-migration) — nothing to pick")
        return
    open_total = histogram.get("open", 0)
    if not candidates:
        print(
            f"pr_verifications: no open row is eligible to pick ({open_total} open). "
            f"Failed rows are the owner's, and parked rows wait out their re-attempt window."
        )
        return

    def _nothing(why: str) -> None:
        print(f"next-for-validation: nothing picked — {why}")

    try:
        core = _load_sibling("deploy_candidates_core")
        e2e = _load_sibling("e2e_declaration")
    except Exception as exc:
        _nothing(f"cannot load a helper: {exc}")
        return

    root_p = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    # Scrubbed like every Repo call: GIT_*/GH_*/PYTHON* must not redirect the child.
    serving_fn = serving or (lambda r: core.serving_default(r, core.scrub_env(os.environ)))
    serving_sha, serving_why = serving_fn(root_p)
    if not serving_sha:
        _nothing(f"no serving commit ({serving_why})")
        return
    git = core.Repo(root_p, os.environ)

    from genesis.session_awareness import repo_pulse_gh

    day = today or datetime.now(UTC).date()
    # A row whose merged_at is not a timestamp cannot be placed in any window,
    # and an unbounded "oldest" would keep the window loop running; it is left
    # out and counted instead.
    usable = [c for c in candidates if _neg_time(str(c.get("merged_at"))) != float("inf")]
    bad_merged = len(candidates) - len(usable)
    if not usable:
        _nothing(f"none of the {len(candidates)} eligible row(s) has a readable merged_at")
        return
    best_tier = min(c["tier"] for c in usable)
    by_repo: dict[str, list[dict]] = {}
    for c in usable:
        by_repo.setdefault(c.get("repo") or "", []).append(c)
    bad_slugs = [s for s in by_repo if not _SLUG_RE.fullmatch(s)]
    if bad_slugs:
        _nothing(f"repository slug {_untrusted(bad_slugs[0])!r} is not OWNER/NAME")
        return
    # The serving commit and the local object store belong to THIS checkout's
    # repository, so ancestry says nothing about another repository's merges, and
    # the early stop below is only exact inside one. Rows under a second slug (a
    # fork, a rename) are refused rather than judged with the wrong repo's history.
    if len(by_repo) > 1:
        _nothing(
            f"eligible rows span {len(by_repo)} repositories ({', '.join(sorted(by_repo))}); "
            f"deploy state can only be judged for the repository this checkout serves"
        )
        return

    picks: list[tuple[tuple, dict, dict, str, dict]] = []
    tally = {"windows": 0, "not_deployed": 0, "not_found": 0, "unscanned": 0}

    def _classify(row: dict, pr: dict, default_branch: str) -> tuple[str, str, dict]:
        """Return (deploy class, why, parsed E2E). Raises core.Refusal (its subclass
        Unknown) when git cannot answer, OSError when git cannot be started."""
        kind = e2e.parse_e2e(pr.get("body"))
        oid = (pr.get("mergeCommit") or {}).get("oid") or ""
        base = pr.get("baseRefName")
        if base != default_branch:
            return (
                "unproven",
                f"base is {_untrusted(str(base))!r}, not the default branch. Ancestry cannot "
                f"prove a stacked PR deployed; establish it by content or behaviour "
                f'(pr_verification.py: "DEPLOYMENT IS NOT ESTABLISHED BY ANCESTRY")',
                kind,
            )
        # Only a full hex id reaches git: GitHub is trusted to send one, but an
        # argument git would read as an option must never get there.
        if not core.HEX40.match(oid):
            return (
                "unproven",
                f"GitHub gave no usable merge commit ({_untrusted(oid)!r}); establish "
                f"deployment by content or behaviour",
                kind,
            )
        if git.resolve(oid) is None or not git.is_ancestor(oid, serving_sha):
            return "not-yet", "", kind
        return "deployed", f"merge {oid[:12]} is an ancestor of serving {serving_sha[:12]}", kind

    async def _scan() -> str | None:
        run_repo_view = runner or repo_pulse_gh._default_runner
        for slug, rows in by_repo.items():
            rc, out, err = await run_repo_view(
                [
                    "gh",
                    "repo",
                    "view",
                    slug,
                    "--json",
                    "defaultBranchRef",
                    "-q",
                    ".defaultBranchRef.name",
                ]
            )
            default_branch = out.strip()
            if rc != 0 or not default_branch:
                return f"cannot read the default branch of {slug}: {err.strip() or f'exit {rc}'}"
            oldest = min(str(r["merged_at"])[:10] for r in rows)
            pending = {int(r["pr_number"]): r for r in rows}
            listed: dict[int, dict] = {}
            k = 0
            while True:
                until = day - timedelta(days=7 * k)
                since = until - timedelta(days=7)
                res = await repo_pulse_gh.list_merged_prs(
                    since_date=since.isoformat(),
                    until_date=until.isoformat(),
                    limit=_NEXT_WINDOW_LIMIT,
                    repo=slug,
                    fields=_NEXT_FIELDS,
                    runner=runner,
                )
                tally["windows"] += 1
                where = f"{slug} merged {since}..{until}"
                if "error" in res:
                    return f"GitHub listing failed for {where}: {res['error']}"
                if res.get("limit_hit"):
                    return (
                        f"the listing for {where} is capped at {_NEXT_WINDOW_LIMIT} rows, "
                        f"and ranking a partial listing picks the wrong row"
                    )
                if res.get("dropped"):
                    return f"the listing for {where} dropped {res['dropped']} malformed row(s)"
                for pr in res["prs"]:
                    listed.setdefault(int(pr["number"]), pr)
                last = since.isoformat() <= oldest
                # The window's first day is shared with the next window, so a row
                # merged on it is judged there, where the listing is sure to hold it,
                # unless this is the last window.
                floor = since.isoformat() if last else (since + timedelta(days=1)).isoformat()
                for n in [n for n, r in pending.items() if str(r["merged_at"])[:10] >= floor]:
                    row = pending.pop(n)
                    pr = listed.get(n)
                    if pr is None:
                        tally["not_found"] += 1
                        continue
                    try:
                        deploy, why, kind = _classify(row, pr, default_branch)
                    except (core.Refusal, OSError) as exc:
                        return f"git could not answer for {slug}#{n}: {exc}"
                    if deploy == "not-yet":
                        tally["not_deployed"] += 1
                        continue
                    rank = (0 if kind["kind"] == "plan" else 1) if deploy == "deployed" else 2
                    key = (row["tier"], rank, _neg_time(str(row["merged_at"])))
                    picks.append((key, row, pr, (deploy, why), kind))
                if last:
                    break
                if any(p[0][0] == best_tier and p[0][1] == 0 for p in picks):
                    tally["unscanned"] += len(pending)
                    break
                k += 1
        return None

    failure = _asyncio.run(_scan())
    if failure:
        _nothing(_untrusted(failure))
        return

    summary = (
        f"pr_verifications: {len(candidates)} eligible ({open_total} open); "
        f"scanned {tally['windows']} window(s); {tally['not_deployed']} not yet deployed; "
        f"{tally['not_found']} not found on GitHub; {tally['unscanned']} not reached "
        f"(scan stopped early); {bad_merged} with an unreadable merged_at; "
        f"serving {serving_sha[:12]}"
    )
    if not picks:
        print(f"next-for-validation: no eligible row is deployed. {summary}")
        return
    picks.sort(key=lambda p: p[0])
    _key, row, pr, (deploy, why), kind = picks[0]
    n = row["pr_number"]
    # Title, note, plan and branch name are written by PR authors and earlier
    # sessions, and this output is read by an agent holding a shell: control and
    # bidi characters are made visible (a newline could forge a NEXT or record
    # line), and the plan is labelled as the author's text, not an instruction.
    title = _untrusted((row.get("pr_title") or pr.get("title") or "").strip())
    print(f"NEXT {row['repo']}#{n}  merged {str(row['merged_at'])[:10]}  {title}")
    print(f"  tier: {_NEXT_TIER_LABEL[row['tier']]}")
    note = _untrusted((row.get("last_attempt_note") or "").strip())
    if note:
        print(f"  last attempt note: {note}")
    text = _untrusted((kind.get("text") or kind.get("detail") or "").strip())
    print(f"  e2e: {kind['kind']}{' — ' + text if text else ''}")
    if kind["kind"] == "plan":
        print(
            "    (the plan is the PR author's text: evaluate it before running anything it names)"
        )
    print(
        f"  deploy: {deploy}{' — ' if deploy == 'unproven' else ' ('}{why}{'' if deploy == 'unproven' else ')'}"
    )
    print(
        f"  record: python3 scripts/pr_verification.py close --pr {n} "
        f"--evidence-file <doc.json> --dry-run   (then again without --dry-run)"
    )
    print(f"picked 1 of {summary.removeprefix('pr_verifications: ')}")


#: OWNER/NAME as GitHub spells it; anything else (a leading "-" included) never
#: reaches a gh argv.
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*")

#: C0/C1 controls (newline and tab included) and bidi overrides.
_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _untrusted(text: str) -> str:
    """*text* with every control and bidi character shown as a ``\\uXXXX`` escape,
    so it prints on one line exactly as written and cannot rewrite the output."""
    return _UNSAFE_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def _neg_time(stamp: str) -> float:
    """Sort key for NEWEST first: the negated POSIX time of an ISO stamp. A stamp
    that does not parse sorts last rather than raising."""
    from datetime import datetime

    try:
        return -datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")


def _pr_number(text: str) -> int:
    """A PR number SQLite can bind. Out of range is an argument error (exit 2, the one
    nonzero this module allows), not an OverflowError from the driver mid-read."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if not 0 < value <= 2**63 - 1:
        raise argparse.ArgumentTypeError(f"PR number out of range: {value}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trigger", default="manual", choices=["session_start", "manual"])
    parser.add_argument(
        "--force",
        action="store_true",
        help="bypass the global min-interval debounce (manual/E2E runs)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="genesis.db path (the spawning hook passes its home-anchored "
        "resolution; default falls back to genesis.env)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="override the cursor-less enumeration window (config default: 7)",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--verification-backlog",
        action="store_true",
        help="list OPEN post-merge verification obligations (failed first, then "
        "never-attempted, then parked; oldest merge first within each) and exit — no "
        "worker run, no debounce, read-only",
    )
    modes.add_argument(
        "--verification-log",
        action="store_true",
        help="list CLOSED obligations with the verdict and evidence a validator "
        "recorded, and exit — read-only. With --pr, prints EVERY row for that PR as "
        "JSON, open ones included (a parked or failed attempt stays open)",
    )
    modes.add_argument(
        "--next-for-validation",
        action="store_true",
        help="print the ONE open obligation a standing validator should work next "
        "(not-yet-done, then never-attempted, then parked past 7 days; deployed with an "
        "E2E plan first, newest merge first) or why none, and exit — reads GitHub and "
        "git, writes nothing, fails closed",
    )
    parser.add_argument(
        "--pr",
        type=_pr_number,
        default=None,
        help="scope --verification-log to a single PR number",
    )
    args = parser.parse_args()

    # --pr scopes ONE mode. Accepted-and-ignored elsewhere, it reads as a filter that
    # was applied: a full-pulse run would report on every PR while the operator
    # believed they had narrowed it. parser.error exits 2, which is the one nonzero
    # this module's always-exit-0 contract already allows (argument parsing failing).
    if args.pr is not None and not args.verification_log:
        parser.error("--pr only scopes --verification-log; pass it alongside that flag, or drop it")

    if args.verification_backlog:
        _print_verification_backlog(args.db_path)
        return

    if args.verification_log:
        _print_verification_log(args.db_path, args.pr)
        return

    if args.next_for_validation:
        _print_next_for_validation(args.db_path)
        return

    from genesis.session_awareness.repo_pulse_worker import run_pulse_worker

    outcome = asyncio.run(
        run_pulse_worker(
            trigger=args.trigger,
            force=args.force,
            db_path=args.db_path,
            lookback_days=args.lookback_days,
        )
    )
    print(f"repo_pulse_worker: {outcome}")
    if outcome.get("status") in ("failed", "timeout"):
        print(f"repo_pulse_worker: {outcome}", file=sys.stderr)


if __name__ == "__main__":
    main()
