"""Promotion — a private Genesis record becomes a public issue on the board.

Spec §3.3, owner-approved:

1. propose (``board_promote``, any session may call it);
2. owner approval (the existing approval-gated posting pattern; ONE drain —
   ``autonomy.contributor_issue_watcher`` — dispatching on ``source='board'``);
3. privacy scan (``scan_prose``, fail-closed, here at the trusted boundary);
4. issue created (idempotent: an opaque marker in the body is the dedup key);
5. pointer row written (``board_links``) with its ``promotion`` event, in one
   transaction — by the drain, once the issue exists.

Neither this module nor the drain touches the project: a promoted issue is
created and linked, and the board reconciler (a later change) puts every open
issue on the board.

What can refuse a promotion, and why each is a refusal rather than a warning:

* no resolvable public tracker (``github.user`` / ``github.public_repo``);
* a source that does not resolve to exactly one ledger row / follow-up;
* an active (held/posted) hold for the source, in either lane;
* a privacy-scan finding — reported as line number + scanner ONLY, never the
  matched text, because this answer travels further than the local surfaces;
* any failed :func:`preconditions` check — everything that can CHANGE while a
  hold waits for the owner, so the drain re-runs the same function immediately
  before it creates the issue: the lever, the board store, the tracker's labels,
  the source's state, an UNVERIFIED open question blocking it (spec §3.4 "hard
  at promotion"), an existing pointer. Labels are checked only AFTER the scan
  passes, since the check sends the label names to GitHub.

A board hold is ALWAYS owner-approved: unlike the contributor lane there is no
self-approval posture, and the drain refuses a hold unless
``approval_requests.classify_resolver`` classifies its resolver as "human". That
classification names the channel (a dashboard or Telegram resolution), which
cannot prove a person, so board posts also share the daily posting cap.

The public body never names the private record: the dedup marker is a salted
hash of ``kind:id``, opaque to a reader.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import urllib.parse
import uuid
from dataclasses import dataclass, field
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
#: The marker's exact shape. The drain requires exactly ONE per board body and
#: imports this, so a draft that already carries one is refused at propose.
MARKER_RE = re.compile(r"<!-- genesis-board:([0-9a-f]{24}) -->")
#: Records past these states have nothing left to promote. A tabled follow-up
#: is "never filed as an issue" by the house rule (CLAUDE.md, deferred work).
_TERMINAL_FOLLOW_UP = ("completed", "failed")
_TERMINAL_LEDGER = ("done", "absorbed", "dropped")
_GH_TIMEOUT = 60  # same bound as the drain's gh calls: a hung gh never wedges a session's tool call


def _label_lookup(repo: str, name: str) -> tuple[int, str]:
    """``gh api`` for one label: ``(returncode, stderr)``. The test seam."""
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/labels/{urllib.parse.quote(name, safe='')}", "--silent"],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
        check=False,
    )
    return out.returncode, out.stderr


def _repo_lookup(repo: str) -> tuple[int, str]:
    """``gh api`` for the repo itself: ``(returncode, stderr)``. The test seam."""
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}", "--silent"],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
        check=False,
    )
    return out.returncode, out.stderr


async def _missing_labels(repo: str, labels: list[str]) -> list[str] | None:
    """The labels *repo* does not have, or None when that cannot be established
    (a lookup failed for a reason other than "not found")."""
    missing = []
    for name in labels:
        try:
            rc, err = await asyncio.to_thread(_label_lookup, repo, name)
        except (OSError, subprocess.SubprocessError):
            logger.warning("label lookup for %s failed", repo, exc_info=True)
            return None
        if rc == 0:
            continue
        if "404" in err or "Not Found" in err:
            missing.append(name)
        else:
            logger.warning("label lookup for %s failed rc=%s: %s", repo, rc, err.strip()[:200])
            return None
    if missing:
        # A 404 for the repo itself (missing, or invisible to the token) reads the
        # same as a missing label; the repo answering 200 is what makes "missing
        # label" true rather than a wrong diagnosis.
        try:
            rc, err = await asyncio.to_thread(_repo_lookup, repo)
        except (OSError, subprocess.SubprocessError):
            logger.warning("repo lookup for %s failed", repo, exc_info=True)
            return None
        if rc != 0:
            logger.warning("repo %s not readable rc=%s: %s", repo, rc, err.strip()[:200])
            return None
    return missing


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
    """A session's ``kind:<id or unique prefix>`` -> ``(kind, full_id)``. Only
    the NAME is resolved here; whether that record may still be promoted is a
    :func:`preconditions` check, because it can change while the hold waits."""
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
    # PASSTHROUGH (a full-length id) is not existence-checked by the resolver;
    # preconditions() reads the row.
    return (kind, matches[0]), None


@dataclass(frozen=True)
class Refusal:
    """Why a promotion may not proceed — at propose time, or at post time.

    ``permanent`` is the drain's question: a PERMANENT refusal cannot clear by
    waiting (the record is gone or closed, it already has an issue, the tracker
    lacks a label), so the drain ends the hold and logs why, and the owner can
    re-propose once it is fixed. A TRANSIENT one (an open question, the lever,
    an unreachable GitHub or store) leaves the hold waiting. ``status`` and
    ``extra`` are what :func:`propose` answers with."""

    status: str
    reason: str
    permanent: bool
    extra: dict = field(default_factory=dict)

    def answer(self) -> dict:
        return {"status": self.status, "reason": self.reason, **self.extra}


async def _source_refusal(db, kind: str, source_id: str) -> Refusal | None:
    """The source record still exists and is still open (not closed, not tabled)."""
    cur = await db.execute(
        "SELECT status, NULL FROM session_ledger WHERE id = ?"
        if kind == "ledger"
        else "SELECT status, kind FROM follow_ups WHERE id = ?",
        (source_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return Refusal("error", f"no {kind} with id {source_id!r}", permanent=True)
    status, fu_kind = row[0], row[1]
    if (kind == "ledger" and status in _TERMINAL_LEDGER) or (
        kind == "follow_up" and status in _TERMINAL_FOLLOW_UP
    ):
        what = "ledger row" if kind == "ledger" else "follow-up"
        return Refusal(
            "error", f"{what} is {status}; a closed record has nothing to promote", permanent=True
        )
    if kind == "follow_up" and fu_kind == "tabled":
        return Refusal(
            "error",
            "a tabled follow-up is consciously not pursued and is never filed as an issue",
            permanent=True,
        )
    return None


async def preconditions(
    db, *, source_ref: str, repo: str, labels: list[str], require_live: bool = False
) -> Refusal | None:
    """Every promotion check whose answer can CHANGE while a hold waits for the
    owner. :func:`propose` runs it before holding; the drain runs it once more,
    immediately before ``gh issue create``. One function, so the two can never
    disagree about what makes a promotion valid. Returns None when it may go on.

    Order: GitHub lookups first (labels), local reads last (the records), and
    the mode last of all. The GitHub lookups await the network (one call per
    label plus a repo lookup, each bounded at ``_GH_TIMEOUT``), so everything
    read after them is the freshest state before the caller acts — the lever
    above all, because the drain creates the issue right after this returns: a
    kill switch flipped during the lookups must still stop that post. The
    mode's verdict OUTRANKS every other refusal (a board turned off ends no
    hold, even one a permanent check would end). A failure to READ anything is
    transient: a check that cannot be made never passes, and never ends a hold
    either.

    ``require_live`` is the drain's posture (it posts only under ``live``);
    propose accepts ``propose_only`` too."""
    refusal = await _record_refusal(db, source_ref=source_ref, repo=repo, labels=labels)
    mode = board_config.effective_mode()  # LAST read: see the docstring
    if mode == "off":
        return Refusal("disabled", "board mode is off", permanent=False)
    if require_live and mode != "live":
        return Refusal("error", f"board mode is {mode}, not live", permanent=False)
    return refusal


async def _record_refusal(db, *, source_ref: str, repo: str, labels: list[str]) -> Refusal | None:
    """Every :func:`preconditions` check except the lever: the tracker's labels
    (GitHub), then the board store and the source record (local)."""
    from genesis.db.crud import board as board_crud

    kind, _, source_id = (source_ref or "").partition(":")
    if kind not in SOURCE_KINDS or not source_id:
        return Refusal("error", f"unparseable board source {source_ref!r}", permanent=True)

    if labels:
        missing = await _missing_labels(repo, labels)
        if missing is None:
            return Refusal(
                "error", f"could not verify the labels on {repo}; retry", permanent=False
            )
        if missing:
            return Refusal(
                "error",
                f"{repo} has no label(s) {missing}; create them or drop them",
                permanent=True,
            )

    try:
        if not await board_crud.tables_available(db):
            return Refusal(
                "error", "board tables not migrated yet (restart genesis-server)", permanent=False
            )
        refusal = await _source_refusal(db, kind, source_id)
        if refusal is not None:
            return refusal
        blocking = await board_crud.blocking_questions(db, target_kind=kind, target_id=source_id)
        if blocking:
            return Refusal(
                "refused",
                "an unverified open question blocks this record; resolve it first",
                permanent=False,
                extra={"blocking_question_ids": [q["id"] for q in blocking]},
            )
        existing = await board_crud.get_link_by_source(db, source_kind=kind, source_id=source_id)
    except Exception:
        logger.error("promotion precondition read failed for %s", kind, exc_info=True)
        return Refusal("error", "the board store could not be read; retry", permanent=False)
    if existing is not None:
        return Refusal(
            "duplicate",
            "already promoted",
            permanent=True,
            extra={"issue": f"{existing['repo']}#{existing['issue_number']}"},
        )
    return None


async def log_refusal(db, now: str | None, reason: str, detail: dict) -> None:
    """Best-effort audit row for a refusal. The refusal itself is the answer the
    session needs, so a failed write is logged and never replaces it."""
    from genesis.db.crud import board as board_crud

    try:
        async with board_crud.owned_connection(db) as own:
            await board_crud.append_event(
                own,
                event="promotion_refused",
                now=now or datetime.now(UTC).isoformat(),
                reason=reason,
                detail=detail,
            )
    except Exception:
        logger.error("promotion_refused event not recorded (%s)", reason, exc_info=True)


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
    from genesis.db.crud import pending_issue_posts as pip

    # Read here for the STAMP (and the cheap answer when the board is off);
    # preconditions() re-reads it, and it is read once more before holding.
    mode = board_config.effective_mode()
    if mode == "off":
        return {"status": "disabled", "reason": "board mode is off"}
    tracker = board_config.tracker_repo()
    if tracker is None:
        return {
            "status": "error",
            "reason": "no public tracker configured (github.user / github.public_repo)",
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
    if any(MARKER_RE.search(text) for text in (title, body, *criteria, *label_list)):
        # The drain posts a board body only with exactly one marker (its own);
        # a draft quoting one would be held forever after approval.
        return {
            "status": "error",
            "reason": "the draft contains a board marker (<!-- genesis-board:... -->); remove it",
        }

    resolved, error = await _resolve_source(db, source)
    if error:
        return {"status": "error", "reason": error}
    kind, source_id = resolved
    source_ref = f"{kind}:{source_id}"

    # Either lane's live hold for this record blocks a second public issue: a
    # board hold keys "kind:id"; a contributor-lane hold keys the bare follow-up
    # id in source_ref, whatever its source (a 'codebase' row may carry one).
    cur = await db.execute(
        "SELECT id, status FROM pending_issue_posts WHERE status IN ('held', 'posted') AND "
        "((source = 'board' AND source_ref = ?) OR (source != 'board' AND source_ref = ?))",
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
        await log_refusal(
            db, now, "privacy scan", {"source": source_ref, "findings": refusal["findings"]}
        )
        return refusal

    repo = "/".join(tracker).lower()
    # After the scan: the label check sends the label names to GitHub.
    refusal = await preconditions(db, source_ref=source_ref, repo=repo, labels=label_list)
    if refusal is not None:
        if refusal.status == "refused":
            await log_refusal(
                db,
                now,
                "blocked by open question(s)",
                {"source": source_ref, "questions": refusal.extra["blocking_question_ids"]},
            )
        return refusal.answer()
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
    # The hold is STAMPED with the mode (a propose_only hold stays dry-run even
    # after a flip to live), and the label lookups above can take minutes, so
    # the lever is re-read here rather than trusted from entry.
    if board_config.effective_mode() != mode:
        return {"status": "error", "reason": "the board mode changed while proposing; retry"}
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
