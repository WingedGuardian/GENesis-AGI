"""BuildLane — the autonomous capability-build lane's control service.

Closes the Stage-1 loop that the rest of the V1 groundwork left dark:

    inbox eval `build` verdict
        -> materialize a dispatcher-ready plan (fail-closed)
        -> record a build_candidate + send a one-tap greenlight card
        -> (user taps) -> dispatcher.submit(source='build_lane')
        -> existing executor delivery (scope gate + draft PR, NEVER a merge)
        -> reconcile the candidate's outcome from task state

Nothing builds without a tap; the greenlight card IS the approval gate for
the whole build chain (mirroring the /task-submission = approval-for-steps
precedent). ``dont_build`` / ``needs_discussion`` verdicts record a
calibration row only — never a card, never a queued build.

Dedup is permanent on ``item_key`` (title/URL, excluding next_step): a
capability stays in the notepad after it is built and is re-evaluated on
every rescan, so we skip any item that already has a candidate row. A
genuine title/URL edit changes the key and legitimately re-proposes.

The lane ships behind ``build_lane.enabled`` (default OFF). When disabled,
``handle_eval`` / ``poll_pending`` are no-ops and the runtime never spawns
the poll loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from genesis.db.crud import build_candidates, task_states
from genesis.inbox.recommendation import parse_recommendations
from genesis.inbox.scanner import extract_urls, normalize_url_line

logger = logging.getLogger(__name__)

_PLANS_DIR = Path.home() / ".genesis" / "plans"

# Task phases that mean "delivery has run" — reconcile the candidate outcome.
_TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})


class BuildLane:
    """Consumes capability-build verdicts and drives them to draft PRs."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        dispatcher: Any,
        approval_gate: Any,
        enabled: bool,
    ) -> None:
        self._db = db
        self._dispatcher = dispatcher
        self._gate = approval_gate
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- keys -------------------------------------------------------------

    @staticmethod
    def item_key(title: str) -> str:
        """Stable per-notepad-item key: sha256 of the tracking-normalized
        primary URL, or the lowercased title when there is no URL.

        Deliberately excludes ``next_step`` (which varies run-to-run) so the
        same item maps to the same key across re-evaluations. A title/URL
        edit changes the key — that is the intended re-proposal signal.
        """
        text = (title or "").strip()
        urls = extract_urls(text)
        primary = normalize_url_line(urls[0]) if urls else text.lower()
        return hashlib.sha256(f"inbox_build|{primary}".encode()).hexdigest()

    # -- eval intake ------------------------------------------------------

    async def handle_eval(
        self,
        *,
        evaluation_text: str,
        batch_id: str,
        item: Any,
        response_path: Any = None,
    ) -> int:
        """Process an inbox evaluation for capability-build verdicts.

        Returns the number of NEW candidate rows created (0 when disabled or
        when every verdict item is already tracked). Never raises — the
        caller wires this as a non-fatal hook.
        """
        if not self._enabled:
            return 0

        source_file = getattr(item, "file_path", "") or ""
        eval_path = str(response_path) if response_path else None
        created = 0

        for rec in parse_recommendations(evaluation_text):
            if rec.verdict is None:
                continue
            title = (rec.item_title or "").strip()
            if not title:
                logger.debug("build verdict with empty title — skipping")
                continue
            key = self.item_key(title)

            # Permanent dedup: one candidate per item_key, any decision state.
            if await build_candidates.get_any_by_item_key(self._db, key):
                logger.debug("build item already tracked (%s) — skipping", title)
                continue

            try:
                if rec.verdict == "build":
                    made = await self._handle_build(
                        rec=rec, key=key, title=title,
                        source_file=source_file, batch_id=batch_id,
                        eval_path=eval_path,
                    )
                else:  # dont_build | needs_discussion — calibration row only
                    made = await self._record_calibration(
                        rec=rec, key=key, title=title, verdict=rec.verdict,
                        source_file=source_file, batch_id=batch_id,
                        eval_path=eval_path,
                    )
                created += made
            except aiosqlite.IntegrityError:
                # Race: another writer inserted an open candidate for this
                # key between the dedup check and the insert. Deduped.
                logger.debug("build candidate race on %s — deduped", title)
            except Exception:
                logger.warning(
                    "build_lane: failed to process verdict for %s (non-fatal)",
                    title, exc_info=True,
                )

        return created

    async def _handle_build(
        self, *, rec: Any, key: str, title: str,
        source_file: str, batch_id: str, eval_path: str | None,
    ) -> int:
        """Materialize a plan, send a greenlight card, record the candidate.

        Card-first-then-row: because the greenlight ``extra_context`` is
        reconstructable from row fields, ``ensure_approval`` is idempotent on
        its stable approval_key, so a crash between card and row re-cards to
        the same request on the next eval instead of stranding the item.
        """
        plan_path = self._materialize_plan(key, title, rec.build_spec)
        if plan_path is None:
            # Malformed/empty build_spec — fail closed to needs_discussion.
            logger.info(
                "build_spec unusable for %s — recording as needs_discussion", title,
            )
            return await self._record_calibration(
                rec=rec, key=key, title=title, verdict="needs_discussion",
                source_file=source_file, batch_id=batch_id, eval_path=eval_path,
            )

        extra = self._greenlight_extra(
            title=title, build_spec=rec.build_spec, plan_path=plan_path,
        )
        _status, request_id, _reason = await self._gate.ensure_approval(
            subsystem="build_lane",
            policy_id="build_greenlight",
            action_label=f"Build: {title[:60]} [{key[:8]}]",
            invocation=None,
            action_type="build_greenlight",
            extra_context=extra,
        )

        candidate_id = _new_id()
        await build_candidates.create(
            self._db,
            id=candidate_id,
            item_key=key,
            item_title=title,
            source_file=source_file,
            verdict="build",
            batch_id=batch_id,
            eval_path=eval_path,
            verdict_reason=rec.verdict_reason,
            confidence=rec.confidence,
            build_spec=json.dumps(rec.build_spec),
            plan_path=plan_path,
            approval_request_id=request_id,
        )
        await self._ledger_hook(candidate_id, "build", rec.confidence)
        logger.info(
            "build_lane: greenlight card sent for %s (candidate carded, req=%s)",
            title, request_id,
        )
        return 1

    async def _record_calibration(
        self, *, rec: Any, key: str, title: str, verdict: str,
        source_file: str, batch_id: str, eval_path: str | None,
    ) -> int:
        """Record a report-only calibration row (no card, no plan)."""
        candidate_id = _new_id()
        await build_candidates.create(
            self._db,
            id=candidate_id,
            item_key=key,
            item_title=title,
            source_file=source_file,
            verdict=verdict,
            batch_id=batch_id,
            eval_path=eval_path,
            verdict_reason=rec.verdict_reason,
            confidence=rec.confidence,
            build_spec=json.dumps(rec.build_spec) if rec.build_spec else None,
        )
        await self._ledger_hook(candidate_id, verdict, rec.confidence)
        logger.info("build_lane: recorded %s calibration row for %s", verdict, title)
        return 1

    async def _ledger_hook(
        self, candidate_id: str, verdict: str, confidence_label: str | None,
    ) -> None:
        """WS-2 P1b: user_greenlights prediction for BOTH create sites — the
        carded 'build' path AND the report-only calibration path (a
        dont_build/needs_discussion verdict predicts the complement). Wrapped
        so even an import failure can never break the lane."""
        try:
            from genesis.ledger.writers import on_build_verdict

            await on_build_verdict(
                self._db,
                candidate_id=candidate_id,
                verdict=verdict,
                confidence_label=confidence_label,
            )
        except Exception:  # noqa: BLE001 — ledger is best-effort
            logger.debug("ledger prediction hook failed", exc_info=True)

    # -- plan materialization --------------------------------------------

    def _materialize_plan(
        self, item_key: str, title: str, build_spec: Any,
    ) -> str | None:
        """Render a dispatcher-ready plan, or None if the spec is unusable.

        Fail-closed: a missing spec or ANY empty required list returns None
        so the caller downgrades to needs_discussion. The four exact section
        headers required by ``dispatcher._validate_plan_content`` are emitted
        verbatim as whole lines.
        """
        if not isinstance(build_spec, dict):
            return None
        requirements = _as_list(build_spec.get("requirements"))
        steps = _as_list(build_spec.get("steps"))
        success = _as_list(build_spec.get("success_criteria"))
        risks = _as_list(build_spec.get("risks"))
        if not (requirements and steps and success and risks):
            return None
        intended_paths = _as_list(build_spec.get("intended_paths"))

        # Deterministic filename (no date): permanent item_key dedup means a
        # key is materialized at most once, so the date added no uniqueness —
        # only a crash-retry idempotency gap (the path is folded into the
        # greenlight approval_key). The row's created_at carries the timestamp.
        _PLANS_DIR.mkdir(parents=True, exist_ok=True)
        path = _PLANS_DIR / f"build-{item_key[:8]}.md"

        lines: list[str] = [
            f"# Build: {title}",
            "",
            "> Autonomous capability-build lane. Draft PR only — never a merge.",
            "> Scope-gated to capability trees; out-of-tree writes are blocked.",
            "",
            "## Requirements",
            "",
        ]
        lines += [f"- {r}" for r in requirements]
        lines += ["", "## Steps", ""]
        for i, step in enumerate(steps, 1):
            if isinstance(step, dict):
                stype = str(step.get("type", "code"))
                desc = str(step.get("description", "")).strip() or "(no description)"
            else:
                stype, desc = "code", str(step)
            lines.append(f"{i}. ({stype}) {desc}")
        if intended_paths:
            lines += ["", "Intended paths (scope-gated):"]
            lines += [f"- {p}" for p in intended_paths]
        lines += ["", "## Success Criteria", ""]
        lines += [f"- {c}" for c in success]
        lines += ["", "## Risks and Failure Modes", ""]
        lines += [f"- {r}" for r in risks]
        lines.append("")

        path.write_text("\n".join(lines))
        return str(path)

    @staticmethod
    def _greenlight_extra(
        *, title: str, build_spec: Any, plan_path: str,
    ) -> dict[str, Any]:
        """Card context — deterministic from row-stored fields so the
        approval_key stays stable across a card-then-row crash re-card."""
        spec = build_spec if isinstance(build_spec, dict) else {}
        steps = _as_list(spec.get("steps"))
        paths = _as_list(spec.get("intended_paths"))
        return {
            "title": title,
            "steps_count": len(steps),
            "intended_paths": [str(p) for p in paths],
            "plan_path": plan_path,
        }

    # -- greenlight resolution + outcome reconcile -----------------------

    async def poll_pending(self) -> None:
        """Resolve tapped greenlight cards and reconcile submitted builds.

        Pass 1: open candidates with a greenlight card — approved -> consume
        + submit; rejected -> record + abandon. Pass 2: submitted candidates
        whose task has reached a terminal phase — reconcile the outcome from
        task state (the engine writes task outputs, never build_candidates).
        Never raises.
        """
        if not self._enabled:
            return
        try:
            await self._resolve_cards()
            await self._reconcile_submitted()
        except Exception:
            logger.warning("build_lane poll_pending failed (non-fatal)", exc_info=True)

    async def _resolve_cards(self) -> None:
        for cand in await build_candidates.list_open(self._db):
            request_id = cand.get("approval_request_id")
            if not request_id or cand.get("task_id"):
                # Calibration rows (no card) or already-submitted — skip.
                continue
            req = await self._gate.get_request(request_id)
            if not req:
                continue
            status = str(req.get("status") or "")
            if status == "approved":
                # Atomic single-consume — a lost race means another path
                # already handled this tap; do not double-submit.
                if not await self._gate.mark_consumed(request_id):
                    continue
                await build_candidates.record_user_decision(
                    self._db, cand["id"], user_decision="approved",
                )
                try:
                    task_id = await self._dispatcher.submit(
                        cand["plan_path"],
                        cand["item_title"],
                        source="build_lane",
                    )
                except Exception:
                    logger.error(
                        "build_lane: dispatch failed for candidate %s",
                        cand["id"], exc_info=True,
                    )
                    await build_candidates.update(
                        self._db, cand["id"], outcome="build_failed",
                    )
                    continue
                await build_candidates.update(
                    self._db, cand["id"], task_id=task_id, outcome="submitted",
                )
                logger.info(
                    "build_lane: candidate %s approved -> task %s dispatched",
                    cand["id"], task_id,
                )
            elif status == "rejected":
                await build_candidates.record_user_decision(
                    self._db, cand["id"], user_decision="rejected",
                )
                await build_candidates.update(
                    self._db, cand["id"], outcome="abandoned",
                )
                logger.info("build_lane: candidate %s rejected", cand["id"])

    async def _reconcile_submitted(self) -> None:
        # Query submitted rows directly (indexed) — a recency-bounded scan
        # would drop an in-flight candidate out of the window as unrelated
        # calibration rows pile up, stranding it at 'submitted' forever.
        for cand in await build_candidates.list_by_outcome(self._db, "submitted"):
            if not cand.get("task_id"):
                continue
            task = await task_states.get_by_id(self._db, cand["task_id"])
            if not task:
                continue
            phase = str(task.get("current_phase") or "")
            if phase not in _TERMINAL_PHASES:
                continue  # still running
            outputs = _parse_outputs(task.get("outputs"))
            branch = outputs.get("branch")
            pr_url = outputs.get("pr_url")
            scope_gate = outputs.get("scope_gate")
            if outputs.get("scope_blocked"):
                outcome = "scope_blocked"
            elif pr_url:
                outcome = "pr_opened"
            elif branch:  # pushed but PR-open failed
                outcome = "built"
            else:
                outcome = "build_failed"
            await build_candidates.update(
                self._db, cand["id"],
                outcome=outcome,
                branch=branch,
                pr_url=pr_url,
                scope_gate_result=scope_gate,
            )
            logger.info(
                "build_lane: candidate %s reconciled -> %s", cand["id"], outcome,
            )


def _new_id() -> str:
    return f"bc-{uuid.uuid4().hex[:12]}"


def _as_list(value: Any) -> list:
    """Coerce a build_spec field to a list of non-empty entries."""
    if not value:
        return []
    if isinstance(value, list):
        return [v for v in value if v not in (None, "")]
    return [value]


def _parse_outputs(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
