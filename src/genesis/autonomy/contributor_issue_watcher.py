"""Contributor Work-Log resolution watcher — drains held issue posts.

Mirrors the WS-8 email gate watcher (:mod:`genesis.autonomy.email_gate_watcher`).
A periodic drain (``CronTrigger`` */5min, ``max_instances=1`` — no in-drain
races) resolves each ``pending_issue_posts`` ``held`` row against its linked
approval, honoring the lever of the row's LANE (read live each tick):

- **approved + ``live``**       → post the issue to GitHub below the gate + mark posted.
- **approved + ``propose_only``**→ shadow-observe once + mark ``dry_run`` (TERMINAL — never
  posted; a later flip to ``live`` does NOT retro-post it, by design).
- **approved + ``off``**         → leave held (poster paused).
- **rejected / cancelled**       → mark rejected.
- **expired**                    → mark expired (no-decision, not a rejection).
- **orphaned** (approval gone)   → expire, never post.
- **pending**                    → still awaiting the owner; leave held.

Two lanes share this ONE drain (spec: one drain, dispatching on ``source``):

* the Contributor Work-Log (``source`` ``follow_up`` / ``codebase``), governed by
  ``contributor_worklog`` — unchanged;
* WORK-BOARD PROMOTIONS (``source='board'``, written by
  :mod:`genesis.board.promotion`), governed by the ``board`` lever. A board row
  additionally: posts only on a HUMAN-resolved approval (a system or
  self-approval is refused); is deduped by the opaque marker in its body, not
  by title, and adopts an existing marked issue only when the account itself
  authored it; is NOT subject to the contributor daily cap (each one was
  approved individually); re-checks open-question blocks immediately before the
  create; and, once its issue exists, writes the ``board_links`` pointer + a
  ``promotion`` event and puts the issue on the configured project as Proposed
  (only when it has no Status) — both re-tried every tick until recorded.

Across lanes, a hold whose follow-up the OTHER lane already posted is refused at
post time: both tools check each other when proposing, but only this one drain
posts, so only here is the check free of a concurrent proposal.

Public-repo dup-safety (stronger than the email pattern, because a duplicate
public issue is more visible than a duplicate email):

1. ``mark_posted`` runs BEFORE ``mark_consumed`` — a crash after the GitHub post
   can't re-post next cycle, because the row has already left ``held`` (and
   ``list_held`` won't return it).
2. A pre-post open-issue dedup doubles as crash-idempotency: an issue already
   carrying this row's identity is ADOPTED (mark_posted with its number) instead
   of re-created — so a crash in the narrow window between ``gh issue create``
   and ``mark_posted`` self-heals on the next cycle rather than opening a second
   issue. If the dedup lookup fails we do NOT post (can't verify) — the row stays
   held and retries next cycle.

The drain reads ONLY the sanitized ``pending_issue_posts`` row (never re-reads
any source), so the read-private / write-public boundary is enforced at the
table. ``gh`` uses ambient server-side auth (same idiom as
``contribution/pr_opener``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
from datetime import UTC, datetime, timedelta

from genesis.autonomy import shadow_gate
from genesis.autonomy.contributor_worklog_config import (
    effective_mode,
    knob_int,
    load_config,
    normalize_title,
)
from genesis.board import config as board_config
from genesis.db.crud import approval_requests as approval_crud
from genesis.db.crud import pending_issue_posts as pip

logger = logging.getLogger(__name__)

_ISSUE_NUM_RE = re.compile(r"/issues/(\d+)\b")
_MARKER_RE = re.compile(r"<!-- genesis-board:([0-9a-f]{24}) -->")
# GROUNDWORK(autonomous-distribution): the GitHub issue-create egress door. Every
# autonomous external post routes through the shadow-gate (observe) before the gh
# call; the capability cell is observe-only today (enforce stage later).
_GH_TIMEOUT = 60
BOARD_SOURCE = "board"
#: One page of a dedup SEARCH. A full page means "possibly more", so it is
#: treated as unverified, never as a complete answer.
_SEARCH_PAGE = 100


def _board_mode() -> str:
    """The board lane's lever (a module-level seam, patched in tests)."""
    return board_config.effective_mode()


def _run_gh(args: list[str], *, timeout: int = _GH_TIMEOUT) -> tuple[int, str, str]:
    """Run ``gh <args>`` server-side (list-args, no shell, ambient auth). Returns
    ``(returncode, stdout, stderr)``. A timeout/missing-binary maps to a non-zero
    rc with a message on stderr — never raises."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"gh timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", "gh binary not found"


async def _gh(args: list[str]) -> tuple[int, str, str]:
    """``_run_gh`` OFF the event loop. The drain runs as a job on the server's
    loop; a blocking gh call there (0.7-1.3 s MEASURED, up to the 60 s timeout on
    a network stall) would freeze every coroutine, Telegram approval buttons
    included. ``_run_gh`` stays the sync seam tests patch."""
    return await asyncio.to_thread(_run_gh, args)


def _issue_number_from_url(url: str) -> int | None:
    m = _ISSUE_NUM_RE.search(url or "")
    return int(m.group(1)) if m else None


def _normalize_ts(raw: str | None) -> str | None:
    """Normalize a GitHub ``Z``-suffixed timestamp to the same ``+00:00`` isoformat as
    ``datetime.now(UTC).isoformat()``, so ``posted_at`` stays format-uniform for the
    string comparison in ``count_posted_since``. Returns None on a missing/malformed
    value (the caller falls back to ``now`` — fail-safe: counts toward the cap)."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).isoformat()
    except (ValueError, TypeError):
        return None


async def _gh_issue_list(
    repo: str, *, state: str, fields: str, limit: int, search: str | None = None
) -> list[dict] | None:
    """One ``gh issue list`` call; None when it fails or its output is unparseable."""
    args = [
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--json",
        fields,
        "--limit",
        str(limit),
    ]
    if search is not None:
        args += ["--search", search]
    rc, out, err = await _gh(args)
    if rc != 0:
        logger.warning("gh issue list failed for %s rc=%s: %s", repo, rc, err.strip())
        return None
    try:
        issues = json.loads(out or "[]")
    except (ValueError, TypeError):
        logger.warning("gh issue list returned unparseable JSON for %s", repo)
        return None
    return issues if isinstance(issues, list) else None


async def _find_open_issue_by_title(repo: str, title_norm: str) -> tuple[bool, dict | None]:
    """Look for an OPEN issue on *repo* whose normalized title matches. Returns
    ``(ok, issue|None)`` — ``ok=False`` means a lookup itself failed (caller
    must NOT post, since dedup can't be verified).

    TWO reads, because each is blind where the other sees:

    * the 200 most-recently-created open issues — covers the crash-idempotency
      case (an issue created seconds ago), which a search may not have indexed;
    * a title SEARCH across every open issue — covers a pre-existing issue
      outside that window. The window alone was the original design, sized for
      a repo with far fewer than 200 open issues; MEASURED 638 open on the live
      repo (2026-10-02), so a title collision outside it was reachable.

    The search's own matching is fuzzy, so its results are re-checked with the
    exact normalized-title comparison. The title is searched as a QUOTED phrase:
    MEASURED 2026-10-03, unquoted ``<title> in:title`` found 0 of 2 exact-title
    open issues on the live repo (both ``type(scope): ...`` style) where the
    quoted form found 2 of 2 — and quoting also stops title words being read as
    search qualifiers. A search that fills its page is NOT a complete answer, so
    a saturated read counts as unverified (the row waits) rather than as "no
    match" — though an exact hit inside a full page still counts."""
    fields = "number,title,url,createdAt"
    recent = await _gh_issue_list(repo, state="open", fields=fields, limit=200)
    if recent is None:
        return False, None
    for issue in recent:
        if normalize_title(issue.get("title", "")) == title_norm:
            return True, issue
    phrase = title_norm.replace('"', " ").strip()
    searched = await _gh_issue_list(
        repo, state="open", fields=fields, limit=_SEARCH_PAGE, search=f'"{phrase}" in:title'
    )
    if searched is None:
        return False, None
    # An exact hit is conclusive even on a full page; only "no hit" needs the
    # page to be complete before it means absent.
    for issue in searched:
        if normalize_title(issue.get("title", "")) == title_norm:
            return True, issue
    if len(searched) >= _SEARCH_PAGE:
        return False, None
    return True, None


async def _find_issue_by_marker(repo: str, marker_digest: str) -> tuple[bool, dict | None]:
    """Find an issue (ANY state — a closed one still must not be re-created)
    whose body carries this board row's marker. Same two-read shape as the
    title lookup: the recent window catches a just-created issue a search has
    not indexed; the search covers everything older (MEASURED 2026-10-03: once
    indexed, a body search finds the digest inside the HTML-comment marker, in
    unquoted, quoted and prefixed forms alike)."""
    fields = "number,url,createdAt,body,author"
    recent = await _gh_issue_list(repo, state="all", fields=fields, limit=100)
    if recent is None:
        return False, None
    for issue in recent:
        if marker_digest in _MARKER_RE.findall(issue.get("body") or ""):
            return True, issue
    searched = await _gh_issue_list(
        repo, state="all", fields=fields, limit=_SEARCH_PAGE, search=f'"{marker_digest}" in:body'
    )
    if searched is None:
        return False, None
    for issue in searched:  # a hit is conclusive even on a full page
        if marker_digest in _MARKER_RE.findall(issue.get("body") or ""):
            return True, issue
    if len(searched) >= _SEARCH_PAGE:
        return False, None
    return True, None


async def _viewer_login() -> str | None:
    rc, out, _ = await _gh(["api", "user", "-q", ".login"])
    login = out.strip()
    return login if rc == 0 and login else None


async def _create_issue(
    repo: str, title: str, body: str, labels: list[str]
) -> tuple[int | None, str | None, str | None]:
    """Create the issue via ``gh issue create``. Returns ``(number, url, error)``;
    ``error`` non-None means the create failed and nothing was posted."""
    args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        args += ["--label", label]
    rc, out, err = await _gh(args)
    if rc != 0:
        return None, None, (err or out or "unknown gh error").strip()
    url = out.strip().splitlines()[-1] if out.strip() else None
    if not url:
        return None, None, "gh issue create returned no URL"
    return _issue_number_from_url(url), url, None


def _labels_of(row: dict) -> list[str]:
    raw = row.get("labels")
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(x) for x in val] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


async def _link_board(
    rt_db, row: dict, approval: dict | None, *, issue_number: int, adopted: bool, now: str
) -> bool:
    """Write the ``board_links`` pointer + a ``promotion`` event for a posted
    board row. Returns False (retried next tick by the reconcile pass) when the
    board tables are missing or the write fails — never raises into the drain."""
    from genesis.db.crud import board as board_crud

    try:
        if not await board_crud.tables_available(rt_db):
            return False
        kind, _, source_id = (row.get("source_ref") or "").partition(":")
        context = {}
        if approval and approval.get("context"):
            try:
                context = json.loads(approval["context"])
            except (TypeError, ValueError):
                context = {}
        receipt = context.get("scan_receipt") or {"ok": True, "note": "receipt not recorded"}
        # On a connection this drain owns: the board writers refuse the shared
        # one, where another caller's commit or rollback could land mid-write.
        async with board_crud.owned_connection(rt_db) as own:
            link = await board_crud.record_link(
                own,
                source_kind=kind,
                source_id=source_id,
                repo=row["repo"],
                issue_number=issue_number,
                promoted_by=(approval or {}).get("resolved_by") or "unknown",
                scan_receipt=receipt,
                body_sha256=hashlib.sha256(row["body"].encode()).hexdigest(),
                now=now,
                adopted=adopted,
                approval_id=row["request_id"],
            )
            try:
                await board_crud.append_event(
                    own,
                    event="promotion",
                    now=now,
                    repo=row["repo"],
                    issue_number=issue_number,
                    worker="genesis",
                    detail={"link_id": link["id"], "adopted": adopted},
                )
            except Exception:
                # The pointer is written, which is what the re-link pass checks;
                # the event is the audit line, so its loss is logged, not retried.
                logger.error(
                    "promotion event for linked row %s not recorded", row.get("id"), exc_info=True
                )
    except Exception:
        logger.error(
            "board link for posted row %s failed — retried next tick", row.get("id"), exc_info=True
        )
        return False
    # The card is placed by the end-of-tick pass (_place_unplaced_links), which
    # also retries one that failed: one mechanism, not two.
    return True


async def _place_on_board(rt_db, proj, link: dict, now: str) -> bool:
    """Put one linked issue on *proj* (the configured project, already checked
    to have a Status option ``Proposed``) and record its item id.

    Status is written ONLY when the item has none, and only ever as Proposed:
    re-adding an existing item returns it unchanged (MEASURED), so a card the
    owner already moved keeps its column. The audit event is written BEFORE the
    item id, so a failure between the two leaves the link unplaced and the
    retry (which then reads Status as set) records no second event. Any failure
    leaves ``project_item_id`` NULL for the next pass. Returns True once the
    item id is recorded."""
    from genesis.board import projects_v2 as pv
    from genesis.db.crud import board as board_crud

    status = proj.fields[pv.STATUS_FIELD]
    try:
        owner, _, name = link["repo"].partition("/")
        content_id = await pv.issue_node_id(owner, name, int(link["issue_number"]))
        if _board_mode() != "live":
            return False
        item_id = await pv.add_item(proj.id, content_id)
        prior = await pv.item_status(item_id)
        if prior is None:
            await pv.set_single_select(proj.id, item_id, status.id, status.options["Proposed"])
        async with board_crud.owned_connection(rt_db) as own:
            if prior is None:
                await board_crud.append_event(
                    own,
                    event="status_write",
                    now=now,
                    repo=link["repo"],
                    issue_number=int(link["issue_number"]),
                    project_item_id=item_id,
                    worker="genesis",
                    detail={"to": "Proposed", "from": None},
                )
            await board_crud.set_project_item(
                own, link_id=link["id"], project_item_id=item_id, now=now
            )
        return True
    except Exception:
        logger.error(
            "placing board link %s on the project failed — retried next tick",
            link.get("id"),
            exc_info=True,
        )
        return False


async def _board_blocked(rt_db, row: dict) -> bool:
    """True when an unverified open question blocks this board row's source —
    or when that cannot be established (store missing, unparseable source):
    a post that cannot be checked does not go out."""
    from genesis.db.crud import board as board_crud

    kind, _, source_id = (row.get("source_ref") or "").partition(":")
    try:
        if not await board_crud.tables_available(rt_db):
            return True
        return bool(
            await board_crud.blocking_questions(rt_db, target_kind=kind, target_id=source_id)
        )
    except Exception:
        logger.error("block check for board hold %s failed", row.get("id"), exc_info=True)
        return True


async def _other_lane_posted(rt_db, row: dict) -> bool:
    """True when the OTHER lane already posted an issue for the same follow-up.

    Both lanes' tools check each other at propose time, but two concurrent
    proposals can each pass that check. Posting happens only here, in this one
    drain job (max_instances=1, rows in sequence, each ``mark_posted`` committed
    before the next row), so checking at post time is race-free across lanes.

    Keyed on the follow-up id, never on ``source``: a contributor row of ANY
    source may carry one in ``source_ref`` (a ``codebase`` row with
    ``source_follow_up_id``). For a contributor row the board's durable
    ``board_links`` pointer is read too, because posted board rows are pruned
    after 30 days. Raises on a DB error; the caller leaves the row held."""
    from genesis.db.crud import board as board_crud

    if row.get("source") == BOARD_SOURCE:
        kind, _, fid = (row.get("source_ref") or "").partition(":")
        if kind != "follow_up" or not fid:
            return False  # the contributor lane only carries follow-up ids
        cur = await rt_db.execute(
            "SELECT 1 FROM pending_issue_posts WHERE source != ? AND source_ref = ? "
            "AND status = 'posted' AND id != ? LIMIT 1",
            (BOARD_SOURCE, fid, row["id"]),
        )
        return await cur.fetchone() is not None
    fid = row.get("source_ref")
    if not fid:
        return False
    cur = await rt_db.execute(
        "SELECT 1 FROM pending_issue_posts WHERE source = ? AND source_ref = ? "
        "AND status = 'posted' AND id != ? LIMIT 1",
        (BOARD_SOURCE, f"follow_up:{fid}", row["id"]),
    )
    if await cur.fetchone() is not None:
        return True
    if not await board_crud.tables_available(rt_db):
        return False
    return (
        await board_crud.get_link_by_source(rt_db, source_kind="follow_up", source_id=fid)
    ) is not None


async def _reconcile_board_links(rt_db, now: str) -> int:
    """Write the pointer for every POSTED board row that lacks one (a crash
    between ``mark_posted`` and the link write). Returns how many it wrote."""
    from genesis.db.crud import board as board_crud

    try:
        if not await board_crud.tables_available(rt_db):
            return 0
        cur = await rt_db.execute(
            "SELECT p.* FROM pending_issue_posts p WHERE p.source = ? AND p.status = 'posted' "
            "AND p.issue_number IS NOT NULL AND NOT EXISTS (SELECT 1 FROM board_links b "
            "WHERE b.source_kind || ':' || b.source_id = p.source_ref)",
            (BOARD_SOURCE,),
        )
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in await cur.fetchall()]
    except Exception:
        logger.error("board link reconcile query failed", exc_info=True)
        return 0
    written = 0
    for row in rows:
        approval = await approval_crud.get_by_id(rt_db, row["request_id"])
        if await _link_board(
            rt_db,
            row,
            approval,
            issue_number=int(row["issue_number"]),
            adopted=bool(row["adopted"]),
            now=now,
        ):
            written += 1
    await _place_unplaced_links(rt_db, now)
    return written


async def _place_unplaced_links(rt_db, now: str) -> int:
    """Place every linked issue whose card is not recorded yet: links written
    this tick, and any whose placement failed before (the project was
    unreachable, or a crash fell between the issue and its card). The project
    is read once per pass. Returns how many were placed."""
    from genesis.board import projects_v2 as pv

    if _board_mode() != "live":
        return 0
    ref = board_config.project_ref()
    try:
        cur = await rt_db.execute(
            "SELECT id, repo, issue_number FROM board_links WHERE project_item_id IS NULL "
            "ORDER BY created_at"
        )
        links = [{"id": r[0], "repo": r[1], "issue_number": r[2]} for r in await cur.fetchall()]
    except Exception:
        logger.error("unplaced board link query failed", exc_info=True)
        return 0
    if not links:
        return 0
    if ref is None:
        logger.warning("%d board link(s) not placed: no project configured", len(links))
        return 0
    try:
        proj = await pv.get_project(*ref)
    except Exception:
        logger.error("board project read failed — placement retried next tick", exc_info=True)
        return 0
    status = proj.fields.get(pv.STATUS_FIELD)
    if status is None or status.kind != "single_select" or "Proposed" not in status.options:
        logger.error("board project has no Status option 'Proposed' — run board_setup.py")
        return 0
    placed = 0
    for link in links:
        if await _place_on_board(rt_db, proj, link, now):
            placed += 1
    return placed


async def _resolve_approved(
    rt_db, row: dict, mode: str, now: str, *, max_posts_per_day: int, approval: dict | None = None
) -> bool:
    """Handle an approved hold. *mode* is the CURRENT lever of the row's lane
    (``effective_mode`` / ``_board_mode`` at this tick); ``row['mode']`` is the
    lever STAMPED at propose time. Returns True iff the row was resolved
    (posted / dry-run / adopted); returns False (leaves the row held) on a
    transient failure so it retries next cycle.

    Dry-run-terminal invariant: a row proposed under ``propose_only`` is
    dry-run-terminal REGARDLESS of a later flip to live — the STAMPED mode, not
    the current lever, decides. Otherwise a row approved during propose_only and
    still awaiting its drain tick would post the instant the lever flipped to
    live (the surprise-batch-post-on-flip the invariant exists to prevent). A
    ``live``-stamped row posts only while the lever is STILL live; if the owner
    flipped back to propose_only (a deliberate pause), the row is left held and
    resumes on the next live flip.
    """
    repo = row["repo"]
    title = row["title"]
    body = row["body"]
    is_board = row.get("source") == BOARD_SOURCE
    lever = _board_mode if is_board else effective_mode

    row_mode = row.get("mode") or mode  # stamped at propose; fall back for legacy rows

    if row_mode != "live":
        # Dry-run-terminal: shadow-observe the egress ONCE (observe-before-enforce)
        # and mark the hold terminal. NEVER posts; a later flip to live won't
        # retro-post it.
        await shadow_gate.observe_github_issue_create(
            rt_db,
            path="autonomy.contributor_issue_watcher.dry_run",
            verb=row["cell_verb"],
            risk_class=row["cell_risk_class"],
            target=repo,
            content=f"{title}\n\n{body}",
        )
        if await pip.mark_dry_run(rt_db, row["id"], dry_run_at=now):
            await approval_crud.mark_consumed(rt_db, row["request_id"], consumed_at=now)
            logger.info("Issue hold %s dry-run (propose_only) — not posted", row["id"])
            return True
        return False

    # row_mode == "live": post only while the lever is STILL live. A flip back to
    # propose_only pauses posting — leave the row held (retries when live resumes).
    if mode != "live":
        return False

    if is_board:
        # Re-check open-question blocks at POST time: a question raised after the
        # proposal (spec §3.4 "hard at promotion") holds the row until it is
        # resolved, instead of posting work the owner has since questioned.
        # (Checked again immediately before the create, below: the lookups in
        # between await GitHub for up to minutes.)
        if await _board_blocked(rt_db, row):
            logger.info("Board hold %s is blocked by an open question — left held", row["id"])
            return False
        digests = _MARKER_RE.findall(body)
        if len(digests) != 1:
            logger.error(
                "Board hold %s carries %d markers (want 1) — left held", row["id"], len(digests)
            )
            return False
        ok, existing = await _find_issue_by_marker(repo, digests[0])
        if ok and existing is not None:
            # A marked issue exists. Adopt it ONLY if this account authored it
            # (a crash between create and mark_posted); a marker on anyone
            # else's issue is never trusted as ours.
            viewer = await _viewer_login()
            author = (existing.get("author") or {}).get("login")
            if viewer is None or author != viewer:
                logger.error(
                    "Board hold %s: marked issue #%s is not authored by this account — left held",
                    row["id"],
                    existing.get("number"),
                )
                return False
    else:
        ok, existing = await _find_open_issue_by_title(repo, normalize_title(title))
    if not ok:
        # Dedup couldn't be verified — do NOT post (would risk a duplicate). Retry.
        return False
    if existing is not None:
        # Already open (a prior cycle posted then crashed before mark_posted, OR a
        # human/other path opened it). Adopt it — idempotent, no second issue.
        num = existing.get("number") or _issue_number_from_url(existing.get("url", ""))
        # Stamp the ADOPTED issue's OWN creation time as posted_at (not ``now``) so an
        # old/human-made issue we merely reconcile does NOT consume the cautious-rollout
        # daily cap — the cap counts issues actually CREATED in the window. A
        # crash-recovery adopt (we created it seconds ago) has a recent createdAt, so it
        # still counts correctly; a missing createdAt falls back to ``now`` (fail-safe:
        # counts, i.e. under-posts).
        # KNOWN INTERACTION (accepted): an old posted_at also makes prune_terminal
        # (COALESCE(posted_at,…)) reap this tracking row earlier than the 30d retention.
        # Bounded/safe — the pre-post open-issue dedup (_find_open_issue_by_title,
        # state=open) still backstops a re-proposal of the same title, so an early-pruned
        # adopt cannot produce a duplicate OPEN issue.
        # Normalize gh's `Z` suffix to the same `+00:00` isoformat as ``now`` so the
        # string `posted_at >= since` comparison in count_posted_since is format-uniform
        # (no Z-vs-+00:00 footgun); a malformed value falls back to ``now`` (fail-safe).
        adopt_ts = _normalize_ts(existing.get("createdAt")) or now
        # Create-vs-adopt provenance for the close-loop. EVERY adopt is
        # non-authoritative (adopted=True); ONLY an issue Genesis CREATES in-band (the
        # create branch below, adopted=0 by default) is an authoritative close-link.
        # Author identity CANNOT distinguish a Genesis crash-recovery creation from a
        # human coincidental-title issue in this single-owner install — both carry the
        # owner's gh account — and a genuine crash-recovery adopt leaves no DB record to
        # check (the crash happened before mark_posted), so there is no sound signal to
        # infer authorship. Trade (fail-safe): a crash-recovered Genesis issue no longer
        # auto-resolves its follow_up (it stays pending, manually resolvable) — never a
        # false completion. Retires the round-3 author-identity proxy (Codex round-4
        # finding 1: author ≠ creation provenance).
        if await pip.mark_posted(
            rt_db,
            row["id"],
            issue_number=num,
            issue_url=existing.get("url"),
            posted_at=adopt_ts,
            adopted=True,
        ):
            await approval_crud.mark_consumed(rt_db, row["request_id"], consumed_at=now)
            logger.info(
                "Issue hold %s adopted existing issue #%s (dedup/idempotency)", row["id"], num
            )
            if is_board and num:
                await _link_board(
                    rt_db, row, approval, issue_number=int(num), adopted=True, now=now
                )
            return True
        return False

    # Cautious-rollout rate cap: bound real CREATEs per rolling 24h. Enforced HERE —
    # AFTER the adopt branch (the ADOPTING row is never gated by the cap, so
    # crash-recovery always reconciles; the real issue it reconciles still counts
    # toward the window) and BEFORE the create. Re-counted per row from the durable
    # ``posted`` rows, so N approved rows
    # in one drain tick are correctly bounded (each ``mark_posted`` commits → the next
    # row's count includes it) and the count survives a mid-window restart. At the cap
    # → leave held, retry next window. ``max_posts_per_day`` is knob_int-coerced ≥ 1 by
    # the caller, so a mistyped/0/negative value can never uncap the poster.
    # A BOARD row is exempt: the cap bounds AUTONOMOUS posting, and every board row
    # was approved individually by a human (refused otherwise, in the drain loop).
    if not is_board:
        since = (datetime.fromisoformat(now) - timedelta(hours=24)).isoformat()
        posted_recent = await pip.count_posted_since(rt_db, since=since)
        if posted_recent >= max_posts_per_day:
            logger.info(
                "Contributor issue %s deferred — daily post cap reached (%d/%d in last 24h)",
                row["id"],
                posted_recent,
                max_posts_per_day,
            )
            return False

    # Kill-switch recheck: re-read the lane's mode LIVE immediately before the
    # external create so a ``mode: off`` / env-kill flipped mid-tick halts THIS post
    # too — not just at the next tick boundary. Makes the STOP effective per-create, so
    # the worst a mid-tick flip can leak is an in-flight create already past this point.
    if lever() != "live":
        logger.info("Issue hold %s deferred — lever no longer live at post time", row["id"])
        return False
    # Last local checks, with no await on GitHub between them and the create.
    try:
        other_posted = await _other_lane_posted(rt_db, row)
    except Exception:
        logger.error("cross-lane check for hold %s failed — left held", row["id"], exc_info=True)
        return False
    if other_posted:
        # The other lane already posted this follow-up: a second public issue
        # for one record is never right, so this hold ends here.
        if await pip.mark_rejected(rt_db, row["id"], rejected_at=now, expired=True):
            logger.error(
                "Issue hold %s refused — the other lane already posted this follow-up", row["id"]
            )
            return True
        return False
    if is_board and await _board_blocked(rt_db, row):
        logger.info("Board hold %s blocked by an open question at post time — held", row["id"])
        return False

    # Observe the egress, THEN post. mark_posted BEFORE mark_consumed so a crash
    # after the post can't re-post (the row leaves 'held').
    await shadow_gate.observe_github_issue_create(
        rt_db,
        path="autonomy.contributor_issue_watcher.post",
        verb=row["cell_verb"],
        risk_class=row["cell_risk_class"],
        target=repo,
        content=f"{title}\n\n{body}",
    )
    number, url, error = await _create_issue(repo, title, body, _labels_of(row))
    if error is not None:
        logger.warning("Issue hold %s post failed — retry next cycle: %s", row["id"], error)
        return False
    if await pip.mark_posted(rt_db, row["id"], issue_number=number, issue_url=url, posted_at=now):
        await approval_crud.mark_consumed(rt_db, row["request_id"], consumed_at=now)
        logger.info("Issue hold %s posted → %s (#%s)", row["id"], url, number)
        if is_board and number:
            await _link_board(
                rt_db, row, approval, issue_number=int(number), adopted=False, now=now
            )
        return True
    return False


async def drain_pending_issue_posts(rt: object) -> int:
    """Resolve all held issue posts (both lanes). Returns the number resolved.

    Each row follows its own lane's lever; when BOTH levers are ``off`` the
    drain short-circuits (every hold left untouched).
    """
    db = getattr(rt, "_db", None)
    if db is None:
        return 0

    mode = effective_mode()
    board_mode = _board_mode()
    if mode == "off" and board_mode == "off":
        return 0

    # Rate-cap VALUE read once per tick (live, no cache); the per-row COUNT that
    # actually enforces it lives in _resolve_approved. knob_int coerces a
    # bad/0/negative value to a safe positive default — the cap can never be uncapped.
    cap = knob_int(load_config(), "max_posts_per_day")

    resolved = 0
    for row in await pip.list_held(db):
        is_board = row.get("source") == BOARD_SOURCE
        lane_mode = board_mode if is_board else mode
        if lane_mode == "off":
            continue  # this lane's poster is paused; the hold waits
        now = datetime.now(UTC).isoformat()
        approval = await approval_crud.get_by_id(db, row["request_id"])

        if approval is None:
            # Orphaned hold — the approval row vanished. Never post.
            if await pip.mark_rejected(db, row["id"], rejected_at=now, expired=True):
                resolved += 1
            logger.warning("Issue hold %s orphaned (approval missing) — expired", row["id"])
            continue

        status = approval.get("status")
        if status == "approved":
            if approval.get("consumed_at") is not None:
                # Approval already consumed but hold still held: a prior cycle
                # posted+consumed but crashed before the terminal mark. Under the
                # dup-safe ordering (mark_posted BEFORE mark_consumed) this is
                # unreachable for a real post, but guard anyway — expire the hold
                # without re-posting.
                if await pip.mark_rejected(db, row["id"], rejected_at=now, expired=True):
                    resolved += 1
                logger.warning(
                    "Issue hold %s approval already consumed — expired without re-post", row["id"]
                )
                continue
            if is_board and approval_crud.classify_resolver(approval.get("resolved_by")) != "human":
                # A board promotion is human-only (spec §3.3). A system / self /
                # unclassifiable resolver is refused outright, fail-closed.
                if await pip.mark_rejected(db, row["id"], rejected_at=now, expired=True):
                    resolved += 1
                logger.error(
                    "Board hold %s approved by a non-human resolver %r — refused, never posted",
                    row["id"],
                    approval.get("resolved_by"),
                )
                continue
            if await _resolve_approved(
                db, row, lane_mode, now, max_posts_per_day=cap, approval=approval
            ):
                resolved += 1
        elif status in ("rejected", "cancelled"):
            if await pip.mark_rejected(db, row["id"], rejected_at=now):
                resolved += 1
        elif status == "expired":
            if await pip.mark_rejected(db, row["id"], rejected_at=now, expired=True):
                resolved += 1
        # status == 'pending' → still awaiting the owner; leave held.

    if board_mode != "off":
        await _reconcile_board_links(db, datetime.now(UTC).isoformat())
    return resolved
