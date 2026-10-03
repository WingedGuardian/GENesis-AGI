"""Promotion — a private Genesis record becomes a public issue on the board.

Spec §3.3, human-only:

1. propose (``board_promote``, any session may call it);
2. owner approval (the existing approval-gated posting pattern; ONE drain —
   ``autonomy.contributor_issue_watcher`` — dispatching on ``source='board'``);
3. privacy scan (``scan_prose``, fail-closed, here at the trusted boundary);
4. issue created (idempotent: an opaque marker in the body is the dedup key);
5. pointer row written (``board_links``) — by the drain, once the issue exists.

The reconciler adds every open repo issue to the project (as Proposed), so this
module never touches the project itself.

What can refuse a promotion, and why each is a refusal rather than a warning:

* board mode ``off`` (the lever);
* a source that does not resolve to exactly one ledger row / follow-up;
* an UNVERIFIED open question blocking that source (spec §3.4 "hard at
  promotion") — the one place a block is enforced;
* an existing pointer, or an active (held/posted) board hold, for the source;
* a privacy-scan finding — reported as line number + scanner ONLY, never the
  matched text, because this answer travels further than the local surfaces.

A board hold is ALWAYS human-approved: unlike the contributor lane there is no
self-approval posture, and the drain refuses a hold whose approval was not
resolved by a human (``approval_requests.classify_resolver``).

The public body never names the private record: the dedup marker is a salted
hash of ``kind:id``, opaque to a reader.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from genesis.board import config as board_config

logger = logging.getLogger(__name__)

BOARD_PROMOTION_ACTION_TYPE = "board_promotion"
SOURCE_KINDS = {"ledger": "session_ledger", "follow_up": "follow_ups"}
MAX_TITLE_CHARS = 256  # GitHub's own issue-title limit
MAX_BODY_CHARS = 20000
MAX_CRITERIA = 20
MAX_CRITERION_CHARS = 500
MAX_LABELS = 10
MAX_LABEL_CHARS = 50  # GitHub's own label-name limit
_MARKER_SALT = "genesis-board-v1"


def source_marker(kind: str, source_id: str) -> str:
    """The idempotency marker for a source — an HTML comment (invisible when
    rendered), keyed by an opaque hash so the private id never appears."""
    digest = hashlib.sha256(f"{_MARKER_SALT}:{kind}:{source_id}".encode()).hexdigest()[:24]
    return f"<!-- genesis-board:{digest} -->"


def render_body(body: str, acceptance_criteria: list[str], marker: str | None) -> str:
    """The fixed issue template (CCPM-style: description, then an
    ``## Acceptance criteria`` checklist), ending with the marker when given."""
    parts = [body.strip()]
    if acceptance_criteria:
        parts.append(
            "## Acceptance criteria\n\n" + "\n".join(f"- [ ] {c}" for c in acceptance_criteria)
        )
    if marker:
        parts.append(marker)
    return "\n\n".join(parts)


async def _resolve_source(db, raw: str) -> tuple[tuple[str, str] | None, str | None]:
    from genesis.db.crud._id_resolve import AMBIGUOUS, NOT_FOUND, resolve_unique_prefix

    kind, sep, value = (raw or "").strip().partition(":")
    if not sep or kind not in SOURCE_KINDS:
        return None, f"source must be 'ledger:<id>' or 'follow_up:<id>'; got {raw!r}"
    table = SOURCE_KINDS[kind]
    matches, outcome = await resolve_unique_prefix(
        db, table=table, id_column="id", raw_id=value, min_len=8
    )
    if outcome in (AMBIGUOUS, NOT_FOUND) or not matches:
        return None, f"{kind} id {value!r} did not resolve to exactly one row ({outcome})"
    full = matches[0]
    # PASSTHROUGH (a full-length id) is not existence-checked by the resolver.
    cur = await db.execute(
        "SELECT 1 FROM session_ledger WHERE id = ?"
        if kind == "ledger"
        else "SELECT 1 FROM follow_ups WHERE id = ?",
        (full,),
    )
    if await cur.fetchone() is None:
        return None, f"no {kind} with id {full!r}"
    return (kind, full), None


def _scan_refusal(scan) -> dict:
    """Line + scanner only — never the matched text or the scanner's message."""
    from genesis.contribution.findings import Severity

    hits = sorted(
        {(f.line, f.scanner) for f in scan.findings if f.severity == Severity.BLOCK},
        key=lambda t: (t[0] or 0, t[1] or ""),
    )
    return {
        "status": "blocked",
        "reason": "privacy scan refused the draft; rewrite the flagged lines",
        "findings": [{"line": line, "scanner": scanner} for line, scanner in hits],
        "scanners_run": scan.scanners_run,
    }


async def propose(
    db,
    *,
    source: str,
    title: str,
    body: str,
    acceptance_criteria: list[str] | None = None,
    labels: list[str] | None = None,
    now: str | None = None,
) -> dict:
    """Validate, scan and hold a promotion for the owner's approval. Writes
    nothing to GitHub. Returns a status dict (``held`` / ``disabled`` /
    ``refused`` / ``blocked`` / ``duplicate`` / ``error``)."""
    from genesis.autonomy.approval import ApprovalManager
    from genesis.autonomy.contributor_worklog_config import CELL_DOMAIN, CELL_RISK_CLASS, CELL_VERB
    from genesis.contribution import scan_prose
    from genesis.db.crud import board as board_crud
    from genesis.db.crud import pending_issue_posts as pip
    from genesis.env import github_public_repo, github_user

    mode = board_config.effective_mode()
    if mode == "off":
        return {"status": "disabled", "reason": "board mode is off"}
    if not await board_crud.tables_available(db):
        return {
            "status": "error",
            "reason": "board tables not migrated yet (restart genesis-server)",
        }

    title = (title or "").strip()
    body = (body or "").strip()
    criteria = [str(c).strip() for c in (acceptance_criteria or []) if str(c).strip()]
    label_list = [str(x).strip() for x in (labels or []) if str(x).strip()]
    if not title or not body:
        return {"status": "error", "reason": "title and body are both required"}
    if len(title) > MAX_TITLE_CHARS or len(body) > MAX_BODY_CHARS:
        return {
            "status": "error",
            "reason": f"title <= {MAX_TITLE_CHARS} and body <= {MAX_BODY_CHARS} chars",
        }
    if len(criteria) > MAX_CRITERIA or any(len(c) > MAX_CRITERION_CHARS for c in criteria):
        return {
            "status": "error",
            "reason": f"<= {MAX_CRITERIA} criteria of <= {MAX_CRITERION_CHARS} chars",
        }
    if len(label_list) > MAX_LABELS or any(len(x) > MAX_LABEL_CHARS for x in label_list):
        return {
            "status": "error",
            "reason": f"<= {MAX_LABELS} labels of <= {MAX_LABEL_CHARS} chars",
        }

    resolved, error = await _resolve_source(db, source)
    if error:
        return {"status": "error", "reason": error}
    kind, source_id = resolved
    source_ref = f"{kind}:{source_id}"

    blocking = await board_crud.blocking_questions(db, target_kind=kind, target_id=source_id)
    if blocking:
        async with board_crud.owned_connection(db) as own:
            await board_crud.append_event(
                own,
                event="promotion_refused",
                now=now or datetime.now(UTC).isoformat(),
                reason="blocked by open question(s)",
                detail={"source": source_ref, "questions": [q["id"] for q in blocking]},
            )
        return {
            "status": "refused",
            "reason": "an unverified open question blocks this record; resolve it first",
            "blocking_question_ids": [q["id"] for q in blocking],
        }

    existing = await board_crud.get_link_by_source(db, source_kind=kind, source_id=source_id)
    if existing is not None:
        return {
            "status": "duplicate",
            "reason": "already promoted",
            "issue": f"{existing['repo']}#{existing['issue_number']}",
        }
    # Either lane's live hold for this record blocks a second public issue: a
    # board hold keys "kind:id"; a contributor-lane hold of a follow-up keys the
    # bare follow-up id (source='follow_up').
    cur = await db.execute(
        "SELECT id, status FROM pending_issue_posts WHERE status IN ('held', 'posted') AND "
        "((source = 'board' AND source_ref = ?) OR (source = 'follow_up' AND source_ref = ?))",
        (source_ref, source_id if kind == "follow_up" else None),
    )
    active = await cur.fetchone()
    if active is not None:
        return {
            "status": "duplicate",
            "reason": f"a promotion for this record is already {active[1]}",
            "existing_id": active[0],
        }

    marker = source_marker(kind, source_id)
    rendered = render_body(body, criteria, marker)
    # Scan everything a SESSION wrote, exactly as it will render — minus the
    # marker. The marker is Genesis's own opaque digest (hex, so detect-secrets
    # reads it as a high-entropy secret, MEASURED), carries no session text,
    # and has a fixed shape the drain checks; scanning it would refuse every
    # promotion while adding no privacy coverage.
    scan_input = "\n\n".join([title, render_body(body, criteria, None), *label_list])
    scan = scan_prose(scan_input)
    if not scan.ok:
        refusal = _scan_refusal(scan)
        async with board_crud.owned_connection(db) as own:
            await board_crud.append_event(
                own,
                event="promotion_refused",
                now=now or datetime.now(UTC).isoformat(),
                reason="privacy scan",
                detail={"source": source_ref, "findings": refusal["findings"]},
            )
        return refusal

    owner, name = github_user(), github_public_repo()
    repo = (f"{owner}/{name}" if owner else name).lower()
    receipt = {
        "ok": True,
        "scanners_run": list(scan.scanners_run),
        "body_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
    }
    context = json.dumps(
        {
            "kind": BOARD_PROMOTION_ACTION_TYPE,
            "repo": repo,
            "source": source_ref,
            "labels": label_list,
            "scan_receipt": receipt,
            "cell": [CELL_DOMAIN, CELL_VERB, CELL_RISK_CLASS],
        }
    )
    approval = ApprovalManager(db=db)
    request_id = await approval.request_approval(
        action_type=BOARD_PROMOTION_ACTION_TYPE,
        action_class="irreversible",  # a public issue is not cleanly undoable
        description=f"Promote a private {kind.replace('_', '-')} to a public issue in {repo}:\n\n# {title}\n\n{rendered}",
        context=context,
        timeout_seconds=None,  # wait for the owner; never auto-approve, never auto-drop
    )
    pending_id = str(uuid.uuid4())
    await pip.create(
        db,
        id=pending_id,
        request_id=request_id,
        repo=repo,
        title=title,
        body=rendered,
        labels=json.dumps(label_list) if label_list else None,
        source="board",
        source_ref=source_ref,
        cell_domain=CELL_DOMAIN,
        cell_verb=CELL_VERB,
        cell_risk_class=CELL_RISK_CLASS,
        held_at=now or datetime.now(UTC).isoformat(),
        mode=mode,  # stamped: a propose_only hold stays dry-run even after a flip to live
    )
    logger.info(
        "board promotion HELD %s (%s) request=%s mode=%s", pending_id, kind, request_id, mode
    )
    return {
        "status": "held",
        "pending_id": pending_id,
        "request_id": request_id,
        "repo": repo,
        "mode": mode,
        "message": "Held for the owner's approval. "
        + (
            "It posts on approval (live)."
            if mode == "live"
            else "propose_only: on approval it is dry-run, never posted."
        ),
    }
