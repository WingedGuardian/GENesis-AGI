"""``contributor_issue_propose`` MCP tool — the Contributor Work-Log's PROPOSE
door (server-side, trusted boundary).

A LOCAL curator campaign (follow-up-backlog or codebase scan) drafts a public
GitHub issue and calls this tool. Here — NOT in the untrusted curator subprocess
— the draft passes the fail-closed prose sanitizer
(:func:`genesis.contribution.scan_prose`), and only if clean is it parked in
``pending_issue_posts`` (status='held') with a linked ``approval_requests`` row.
The owner reviews the FULL body + approves/rejects per item on the dashboard; a
separate poster drain (``contributor_issue_watcher``) posts it below the gate on
approval (in ``live`` mode) or dry-runs it (in ``propose_only``).

Mirrors the WS-8 email autonomy gate exactly (two-row create, approval FIRST so
a crash never orphans a hold). DB access mirrors ``evo_run`` — the long-lived
health-service connection, so a row written here is visible to the dashboard and
the drain. GitHub open-issue dedup happens at POST time in the drain (this tool
does DB-side dedup only; the curator profile has no ``gh``).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.contributor_worklog_config import (
    CELL_DOMAIN,
    CELL_RISK_CLASS,
    CELL_VERB,
    CONTRIBUTOR_ISSUE_ACTION_TYPE,
    effective_mode,
    knob_int,
    load_config,
)
from genesis.contribution import scan_prose
from genesis.contribution.findings import Severity
from genesis.db.crud import pending_issue_posts as pip
from genesis.env import github_public_repo, github_user
from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


def _normalize_title(title: str) -> str:
    """Case- and whitespace-insensitive title key for dedup."""
    return " ".join(title.lower().split())


def _default_repo() -> str:
    owner = github_user()
    name = github_public_repo()
    return f"{owner}/{name}" if owner else name


async def _impl_contributor_issue_propose(
    db,
    *,
    title: str,
    body: str,
    labels: list[str] | None = None,
    repo: str = "",
    source: str = "codebase",
    source_follow_up_id: str = "",
) -> dict:
    """Testable core (takes an explicit db). See the tool docstring below."""
    mode = effective_mode()
    if mode == "off":
        return {"status": "disabled", "reason": "contributor_worklog mode is off"}

    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        return {"status": "error", "reason": "title and body are both required"}
    if source not in ("follow_up", "codebase"):
        return {"status": "error", "reason": f"source must be follow_up|codebase, got {source!r}"}

    repo = (repo or "").strip() or _default_repo()
    if "/" not in repo:
        return {"status": "error", "reason": f"repo must be owner/name, got {repo!r}"}
    source_ref = (source_follow_up_id or "").strip() or None
    label_list = [str(x).strip() for x in (labels or []) if str(x).strip()]

    # 1) Privacy sanitizer at the TRUSTED server boundary (fail-closed). A
    #    misbehaving curator subprocess cannot bypass this — it runs HERE.
    #    Labels reach the public repo too (drain: `gh issue create --label ...`),
    #    so they are scanned alongside title+body — the sanitizer covers EVERY
    #    curator-supplied string that egresses, not just the body.
    scan_input = f"{title}\n\n{body}"
    if label_list:
        scan_input += "\n\n" + "\n".join(label_list)
    scan = scan_prose(scan_input)
    if not scan.ok:
        reasons = [
            f"{f.kind.value}: {f.message}" for f in scan.findings if f.severity == Severity.BLOCK
        ]
        logger.warning("contributor_issue_propose BLOCKED %d finding(s): %s", len(reasons), reasons)
        return {"status": "blocked", "reasons": reasons, "scanners_run": scan.scanners_run}

    # 2) Backpressure + DB-side dedup (GitHub open-issue dedup is done at post
    #    time in the drain — the curator profile has no gh).
    cfg = load_config()
    active = await pip.list_dedup_active(db, repo)
    held_count = sum(1 for r in active if r["status"] == "held")
    if held_count >= knob_int(cfg, "max_held"):
        return {
            "status": "backpressure",
            "reason": f"{held_count} issue(s) already awaiting review (max_held reached)",
        }
    title_norm = _normalize_title(title)
    for r in active:
        if _normalize_title(r["title"]) == title_norm or (
            source_ref is not None and r["source_ref"] == source_ref
        ):
            return {
                "status": "duplicate",
                "reason": "an active proposal already covers this item",
                "existing_id": r["id"],
            }

    # 3) Two-row create — approval FIRST so a crash never orphans a hold.
    context = json.dumps(
        {
            "kind": CONTRIBUTOR_ISSUE_ACTION_TYPE,
            "repo": repo,
            "labels": label_list,
            "source": source,
            "source_follow_up_id": source_ref,
            "cell": [CELL_DOMAIN, CELL_VERB, CELL_RISK_CLASS],
        }
    )
    approval = ApprovalManager(db=db)
    request_id = await approval.request_approval(
        action_type=CONTRIBUTOR_ISSUE_ACTION_TYPE,
        action_class="irreversible",  # a public-repo post is not cleanly undoable
        description=f"Post GitHub issue to {repo}:\n\n# {title}\n\n{body}",
        context=context,
        timeout_seconds=None,  # wait for the owner; never auto-approve, never auto-drop
    )
    now = datetime.now(UTC).isoformat()
    pending_id = str(uuid.uuid4())
    await pip.create(
        db,
        id=pending_id,
        request_id=request_id,
        repo=repo,
        title=title,
        body=body,
        labels=json.dumps(label_list) if label_list else None,
        source=source,
        source_ref=source_ref,
        cell_domain=CELL_DOMAIN,
        cell_verb=CELL_VERB,
        cell_risk_class=CELL_RISK_CLASS,
        held_at=now,
    )
    logger.info(
        "contributor_issue_propose HELD %s → %s (request=%s, mode=%s, source=%s)",
        pending_id,
        repo,
        request_id,
        mode,
        source,
    )
    return {
        "status": "held",
        "pending_id": pending_id,
        "request_id": request_id,
        "repo": repo,
        "mode": mode,
        "message": (
            "Issue draft held for owner approval on the dashboard. "
            + (
                "It will post on approval (live mode)."
                if mode == "live"
                else "On approval it is dry-run only (propose_only) — flip to live to post."
            )
        ),
    }


@mcp.tool()
async def contributor_issue_propose(
    title: str,
    body: str,
    labels: list[str] | None = None,
    repo: str = "",
    source: str = "codebase",
    source_follow_up_id: str = "",
) -> dict:
    """Propose a public GitHub issue for the Contributor Work-Log — sanitize it
    server-side and, if clean, hold it for owner approval on the dashboard.

    NOTHING is posted by this call. In the default ``propose_only`` mode an
    approved hold is still dry-run (never posted); flip the ``contributor_worklog``
    lever to ``live`` to actually post. Blocked drafts create NO row.

    Args:
        title: issue title (sanitized here; must be public-safe).
        body: issue body / description (sanitized here).
        labels: GitHub label names (e.g. ["good first issue"]). Optional.
        repo: target ``owner/name``. Defaults to this install's public repo.
        source: provenance — "follow_up" (backlog-derived) or "codebase".
        source_follow_up_id: originating follow_up id, for the close-loop link
            (stored on ``source_ref``). Optional.

    Returns a status dict: ``held`` (parked + approval created), ``blocked``
    (sanitizer findings, no row), ``duplicate`` / ``backpressure`` (no row),
    ``disabled`` (mode off), or ``error``.
    """
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = getattr(health_mcp_mod, "_service", None)
    db = getattr(_service, "_db", None) if _service is not None else None
    if db is None:
        return {"status": "error", "reason": "health service DB unavailable"}

    return await _impl_contributor_issue_propose(
        db,
        title=title,
        body=body,
        labels=labels,
        repo=repo,
        source=source,
        source_follow_up_id=source_follow_up_id,
    )
