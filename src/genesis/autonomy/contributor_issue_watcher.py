"""Contributor Work-Log resolution watcher — drains held issue posts.

Mirrors the WS-8 email gate watcher (:mod:`genesis.autonomy.email_gate_watcher`).
A periodic drain (``CronTrigger`` */5min, ``max_instances=1`` — no in-drain
races) resolves each ``pending_issue_posts`` ``held`` row against its linked
approval, honoring the ``contributor_worklog`` mode lever (read live each tick):

- **approved + ``live``**       → post the issue to GitHub below the gate + mark posted.
- **approved + ``propose_only``**→ shadow-observe once + mark ``dry_run`` (TERMINAL — never
  posted; a later flip to ``live`` does NOT retro-post it, by design).
- **approved + ``off``**         → leave held (poster paused).
- **rejected / cancelled**       → mark rejected.
- **expired**                    → mark expired (no-decision, not a rejection).
- **orphaned** (approval gone)   → expire, never post.
- **pending**                    → still awaiting the owner; leave held.

Public-repo dup-safety (stronger than the email pattern, because a duplicate
public issue is more visible than a duplicate email):

1. ``mark_posted`` runs BEFORE ``mark_consumed`` — a crash after the GitHub post
   can't re-post next cycle, because the row has already left ``held`` (and
   ``list_held`` won't return it).
2. A pre-post ``gh issue list`` open-issue dedup doubles as crash-idempotency:
   a normalized title already open on the repo is ADOPTED (mark_posted with its
   number) instead of re-created — so a crash in the narrow window between
   ``gh issue create`` and ``mark_posted`` self-heals on the next cycle rather
   than opening a second issue. If the dedup LIST call fails we do NOT post
   (can't verify) — the row stays held and retries next cycle.

The drain reads ONLY the sanitized ``pending_issue_posts`` row (never re-reads
any source), so the read-private / write-public boundary is enforced at the
table. ``gh`` uses ambient server-side auth (same idiom as
``contribution/pr_opener``).
"""

from __future__ import annotations

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
from genesis.db.crud import approval_requests as approval_crud
from genesis.db.crud import pending_issue_posts as pip

logger = logging.getLogger(__name__)

_ISSUE_NUM_RE = re.compile(r"/issues/(\d+)\b")
# GROUNDWORK(autonomous-distribution): the GitHub issue-create egress door. Every
# autonomous external post routes through the shadow-gate (observe) before the gh
# call; the capability cell is observe-only today (enforce stage later).
_GH_TIMEOUT = 60


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


def _issue_number_from_url(url: str) -> int | None:
    m = _ISSUE_NUM_RE.search(url or "")
    return int(m.group(1)) if m else None


def _find_open_issue_by_title(repo: str, title_norm: str) -> tuple[bool, dict | None]:
    """Look for an OPEN issue on *repo* whose normalized title matches. Returns
    ``(ok, issue|None)`` — ``ok=False`` means the lookup itself failed (caller
    must NOT post, since dedup can't be verified).

    Scope note: ``--limit 200`` covers the 200 most-recently-created open issues
    (gh's default order). This fully covers the crash-idempotency case (a just-
    created issue is necessarily recent). The only gap is a PRE-EXISTING open
    issue (human- or long-ago-opened) with a matching title on a repo with >200
    OPEN issues — well beyond current scale, and the DB-side dedup already stops
    Genesis re-proposing its own. Tracked for a search-based upgrade before the
    repo approaches that ceiling (follow-up)."""
    rc, out, err = _run_gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,url",
            "--limit",
            "200",
        ]
    )
    if rc != 0:
        logger.warning("gh issue list failed for %s rc=%s: %s", repo, rc, err.strip())
        return False, None
    try:
        issues = json.loads(out or "[]")
    except (ValueError, TypeError):
        logger.warning("gh issue list returned unparseable JSON for %s", repo)
        return False, None
    for issue in issues:
        if normalize_title(issue.get("title", "")) == title_norm:
            return True, issue
    return True, None


def _create_issue(
    repo: str, title: str, body: str, labels: list[str]
) -> tuple[int | None, str | None, str | None]:
    """Create the issue via ``gh issue create``. Returns ``(number, url, error)``;
    ``error`` non-None means the create failed and nothing was posted."""
    args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        args += ["--label", label]
    rc, out, err = _run_gh(args)
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


async def _resolve_approved(
    rt_db, row: dict, mode: str, now: str, *, max_posts_per_day: int
) -> bool:
    """Handle an approved hold. *mode* is the CURRENT lever (``effective_mode``
    at this tick); ``row['mode']`` is the lever STAMPED at propose time. Returns
    True iff the row was resolved (posted / dry-run / adopted); returns False
    (leaves the row held) on a transient failure so it retries next cycle.

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
            logger.info("Contributor issue %s dry-run (propose_only) — not posted", row["id"])
            return True
        return False

    # row_mode == "live": post only while the lever is STILL live. A flip back to
    # propose_only pauses posting — leave the row held (retries when live resumes).
    if mode != "live":
        return False

    title_norm = normalize_title(title)
    ok, existing = _find_open_issue_by_title(repo, title_norm)
    if not ok:
        # Dedup couldn't be verified — do NOT post (would risk a duplicate). Retry.
        return False
    if existing is not None:
        # Already open (a prior cycle posted then crashed before mark_posted, OR a
        # human/other path opened it). Adopt it — idempotent, no second issue.
        num = existing.get("number") or _issue_number_from_url(existing.get("url", ""))
        if await pip.mark_posted(
            rt_db, row["id"], issue_number=num, issue_url=existing.get("url"), posted_at=now
        ):
            await approval_crud.mark_consumed(rt_db, row["request_id"], consumed_at=now)
            logger.info(
                "Contributor issue %s adopted existing open issue #%s (dedup/idempotency)",
                row["id"],
                num,
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
    number, url, error = _create_issue(repo, title, body, _labels_of(row))
    if error is not None:
        logger.warning("Contributor issue %s post failed — retry next cycle: %s", row["id"], error)
        return False
    if await pip.mark_posted(rt_db, row["id"], issue_number=number, issue_url=url, posted_at=now):
        await approval_crud.mark_consumed(rt_db, row["request_id"], consumed_at=now)
        logger.info("Contributor issue %s posted → %s (#%s)", row["id"], url, number)
        return True
    return False


async def drain_pending_issue_posts(rt: object) -> int:
    """Resolve all held contributor-issue posts. Returns the number resolved.

    ``off`` mode short-circuits (poster paused — every hold left untouched).
    """
    db = getattr(rt, "_db", None)
    if db is None:
        return 0

    mode = effective_mode()
    if mode == "off":
        return 0

    # Rate-cap VALUE read once per tick (live, no cache); the per-row COUNT that
    # actually enforces it lives in _resolve_approved. knob_int coerces a
    # bad/0/negative value to a safe positive default — the cap can never be uncapped.
    cap = knob_int(load_config(), "max_posts_per_day")

    resolved = 0
    for row in await pip.list_held(db):
        now = datetime.now(UTC).isoformat()
        approval = await approval_crud.get_by_id(db, row["request_id"])

        if approval is None:
            # Orphaned hold — the approval row vanished. Never post.
            if await pip.mark_rejected(db, row["id"], rejected_at=now, expired=True):
                resolved += 1
            logger.warning("Contributor issue %s orphaned (approval missing) — expired", row["id"])
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
                    "Contributor issue %s approval already consumed — expired without re-post",
                    row["id"],
                )
                continue
            if await _resolve_approved(db, row, mode, now, max_posts_per_day=cap):
                resolved += 1
        elif status in ("rejected", "cancelled"):
            if await pip.mark_rejected(db, row["id"], rejected_at=now):
                resolved += 1
        elif status == "expired":
            if await pip.mark_rejected(db, row["id"], rejected_at=now, expired=True):
                resolved += 1
        # status == 'pending' → still awaiting the owner; leave held.

    return resolved
