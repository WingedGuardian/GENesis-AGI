"""CareerOutreachMonitor — an ACTUATOR-in-recon that drives a dormant external
career-agent engine to stage first-touch outreach drafts, then nudges the owner.

Unlike its sibling ``AccountActivityMonitor`` (a sense-and-report monitor), this
one *acts on the owner's behalf*: on a daily tick it dispatches to a configured
external "reasoning" module (the install's own career-agent, declared in the
``~/.genesis`` module overlay) to run that engine's first-touch draft-staging flow
for up to ``max_auto_runs_per_tick`` target accounts, then pushes ONE Telegram
nudge listing the newly-staged drafts. It NEVER sends mail — the external engine
stages drafts into the owner's mail Drafts and the owner clicks Send
(draft-and-hold). The autonomous act is bounded to discover+draft (reversible);
the standing ``live`` lever is the owner's authorization for that bounded loop.

Design notes:
- **SIBLING to ``AccountActivityMonitor``** — reuses the surplus-cron + lazy
  ``GenesisRuntime`` resolution + ``observations`` content-hash dedup + Telegram
  ``submit_raw`` push. But it PUSHES and ACTS, so it is doc'd as an actuator, not
  the no-side-effect recon contract.
- **Bridge is lazy-resolved at tick time** — surplus init runs before some deps;
  the module registry + outreach pipeline are resolved via ``GenesisRuntime.
  instance()`` when the first tick fires (a daily cron, hours after boot).
- **``execute_operation`` returns an ERROR DICT — it does not raise.** A
  disabled/unhealthy module, an IPC exception, or a dispatch timeout all come
  back as ``{"error": ...}``. The tick inspects the return and surfaces
  ``errors`` so the runner records a job-health FAILURE (a naive success would
  lie on every failed dispatch).
- **Health gate.** ``check_health_cached()`` (60s TTL) each tick; an unhealthy
  module (remote box down) is a CLEAN skip, not a failure — an expected transient.
- **Pause-aware.** The pause state is re-checked between external actions, so an
  owner ``/pause`` mid-tick halts the remaining (each up-to-timeout-long) dispatches.
- **Absent user-module → clean no-op.** On an install without the career-agent
  overlay module, ``module_registry.get()`` returns ``None`` and the tick returns
  silently (never a job-health failure on a generic install).
- **State: nudge-dedup only.** The external engine's own staged-draft state is the
  source of truth for which accounts have staged drafts; a ``career_outreach_nudged``
  observation (content-hashed per company) records only "the owner has already
  been nudged for this draft", so a re-tick does not re-nudge. Because the nudge
  list is RE-DERIVED from the staged-draft set each tick minus this ledger, a
  non-delivered nudge simply retries next tick.
- **Honors the engine's own accuracy gate.** The auto-run prompt drives the
  engine's COMPLETE first-touch flow INCLUDING its own verification gates (it does
  not bypass them), under a headless contract; a draft the engine's gate refuses
  comes back as ``verify_failed`` — that is the gate WORKING (NOT a job-health
  failure). Turn budget: the flow is agentic and needs > the ipc default of 25
  turns (measured 42 live), so the auto-run passes ``dispatch_max_turns``.
- **Timeout + orphan semantics.** The gated flow measured ~5.5 min live; there is
  NO fixed SSH cap (``ipc.py::_send_cc`` honors ``timeout_s`` with no ceiling), so
  ``dispatch_timeout_s`` (default 900) bounds a hung run. A timeout kill severs the
  SSH connection but the REMOTE ``claude -p`` keeps running (orphaned); this is
  self-healing — the engine's own staged-draft state is the source of truth and the
  next tick re-derives from it (a late-staged draft is nudged next tick, never lost).
- **Modes** (``career_outreach_config``): ``off`` (default — inert) / ``observe``
  (seed the nudged-set from the existing backlog, never dispatch a staging run or
  nudge) / ``live`` (drive staging runs + nudge new drafts).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import observations
from genesis.recon import career_outreach_config as cfg

logger = logging.getLogger(__name__)

_SOURCE = "recon"
_NUDGE_TYPE = "career_outreach_nudged"

# The external reasoning module owns account selection, the per-day cap, drafting,
# staging, AND its own accuracy/verification gate; Genesis only drives the cadence +
# the owner nudge. The auto-run prompt tells the engine to run its COMPLETE flow
# INCLUDING its own verification gates (never bypass them) under a HEADLESS contract
# (no interactive questions are possible over a `claude -p` dispatch), and — when a
# claim can't be verified headless — to reword the UNVERIFIABLE claim rather than
# fabricate a token, loop, or strip substance (which would game the accuracy gate).
# The engine may lead its reply with its own verdict line; `_parse_json` extracts
# the trailing compact JSON regardless. The phrasing is GENERIC (outcomes, not any
# engine's internal schema/vocabulary) so it ships in the public repo and works with
# any gated career-agent; install-specific gate guidance rides the `autorun_note`
# overlay knob, appended to THIS prompt only (never the read prompt).
#
# (Root cause this replaces: the old "reply with ONLY compact JSON, no prose" fought
# the engine's own outreach hooks, which — triggered by "outreach"/"draft" wording —
# inject "run your verify gate + lead with a verdict line" instructions and a
# headless-impossible confirm, derailing the run. Proven live 2026-08-11.)
_AUTORUN_PROMPT = (
    "This is Genesis driving your outreach engine on a timed HEADLESS dispatch — "
    "there is NO human available to answer any interactive question this run. Pick "
    "your SINGLE highest-priority target account that you have NOT yet staged a "
    "first-touch outreach draft for, and run your COMPLETE standard first-touch "
    "outreach flow for it, INCLUDING your own verification/accuracy gates — do not "
    "skip or bypass them. When your verifier flags a claim it cannot confirm "
    "headless (e.g. a quoted or attributed line), reword to remove the UNVERIFIABLE "
    "claim or paraphrase it from a citable source, then re-verify and MOVE ON — do "
    "NOT loop on the same flag, do NOT fabricate any verification token, and do NOT "
    "strip real substance just to pass the gate. On a clean verification pass, stage "
    "the verified first-touch draft into the owner's mail Drafts (NEVER send), then "
    "mark the target as worked in your own tracking. If after honest rewording the "
    "draft still cannot verify (or hard-fails), stage NOTHING for it, mark it "
    "attempted in your tracking, and report verify_failed. Do EXACTLY ONE account. "
    "If your gates require a verdict line, LEAD your reply with it; then your ENTIRE "
    "remaining reply must be ONLY compact JSON, no other prose — exactly one of: "
    '{"company":"<name>","contact":"<email-or-name>","draft_summary":"<one line>"} '
    'if you staged one, {"verify_failed":"<short reason>","company":"<name>"} if you '
    'drafted but could not honestly verify, {"none_left":true} if no eligible target '
    'remains, or {"error":"<short reason>"} if you could not proceed.'
)

_LIST_WORKING_PROMPT = (
    "This is Genesis. READ-ONLY: list the accounts for which you have already "
    "staged a first-touch draft (awaiting the owner's send). Stage nothing, change "
    "nothing. Your ENTIRE reply is the return value: reply with ONLY a compact JSON "
    'array, no prose — [{"company":"<name>","contact":"<email-or-name>"}, ...] '
    "(empty array [] if none)."
)


def _now_z() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nudge_hash(company: str) -> str:
    """Content-hash namespace for the per-company 'owner already nudged' marker."""
    return hashlib.sha256(f"career_nudged:{company.strip().lower()}".encode()).hexdigest()[:32]


def _reply_text(result: object) -> str:
    """The model's final message out of a dispatch return (the bridge payload)."""
    if isinstance(result, dict):
        return str(result.get("text") or result.get("output") or "")
    return str(result or "")


def _parse_json(text: str) -> object | None:
    """Best-effort parse of a dispatch reply that should be compact JSON. Tolerates
    a stray markdown fence or leading prose by falling back to the outermost
    ``{...}`` / ``[...]`` span."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(text[i : j + 1])
            except Exception:
                continue
    return None


def _autorun_prompt(conf: dict) -> str:
    """The auto-run dispatch prompt: the generic gate-honoring base plus the install's
    optional ``autorun_note`` overlay (install-specific gate guidance). The note is
    appended to the AUTO-RUN prompt ONLY — never the read prompt — so install-specific
    outreach/gate vocabulary cannot trip the external engine's own outreach hooks on
    the otherwise-clean staged-draft read path."""
    note = cfg.text_knob(conf, "autorun_note")
    return f"{_AUTORUN_PROMPT}\n\n{note}" if note else _AUTORUN_PROMPT


@dataclass(frozen=True)
class CareerOutreachResult:
    """Summary of one monitor tick."""

    mode: str = "off"
    health_ok: bool = True
    auto_runs: int = 0  # dispatches that staged a fresh draft
    drafts_working: int = 0  # accounts with a staged first-touch draft
    nudged: int = 0  # newly-staged drafts included in a delivered nudge
    seeded: int = 0  # observe: existing staged-draft accounts seeded into the ledger
    verify_failed: int = 0  # engine drafted but its own accuracy gate refused (NOT an error)
    errors: int = 0  # unsuccessful operations (→ job-health FAILURE)
    details: list[str] = field(default_factory=list)


class CareerOutreachMonitor:
    """Drives the external career-agent engine to stage drafts + nudges the owner.
    See the module docstring."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ── lazy runtime deps (module registry + outreach pipeline) ───────────
    def _module_registry(self):
        try:
            from genesis.runtime._core import GenesisRuntime

            rt = GenesisRuntime.instance()
            return getattr(rt, "module_registry", None) or getattr(rt, "_module_registry", None)
        except Exception:
            logger.debug("career outreach: module registry not resolvable", exc_info=True)
            return None

    def _pipeline(self):
        try:
            from genesis.runtime._core import GenesisRuntime

            return GenesisRuntime.instance().outreach_pipeline
        except Exception:
            logger.debug("career outreach: outreach pipeline not resolvable", exc_info=True)
            return None

    def _is_paused(self) -> bool:
        """True if the owner has /paused Genesis. Re-checked BETWEEN external actions
        so a mid-tick pause halts the remaining dispatches (each up to the dispatch
        timeout long) — /pause promises all background activity stops."""
        try:
            from genesis.runtime._core import GenesisRuntime

            return bool(GenesisRuntime.instance().paused)
        except Exception:
            return False

    # ── main tick ─────────────────────────────────────────────────────────
    async def gather(self) -> CareerOutreachResult:
        mode = cfg.effective_mode()
        if mode == "off":
            return CareerOutreachResult(mode="off")

        conf = cfg.load_config()
        reg = self._module_registry()
        if reg is None:
            logger.debug("career outreach: module registry unavailable — skip")
            return CareerOutreachResult(mode=mode)

        reasoning_name = cfg.module_name(conf, "reasoning_module")
        mod = reg.get(reasoning_name) if reasoning_name else None
        if mod is None:
            # Absent/unnamed user-module → clean no-op (a generic install lacks the
            # overlay). NOT a job-health failure — this is expected off-install.
            logger.debug("career outreach: module %r not loaded — no-op", reasoning_name)
            return CareerOutreachResult(mode=mode)

        try:
            healthy = await mod.check_health_cached()
        except Exception:
            # A RAISE (vs a clean False) is unexpected — surface it as an error so a
            # persistently-throwing health check records a job-health FAILURE rather
            # than a silent clean skip.
            logger.warning("career outreach: health check raised — skipping tick", exc_info=True)
            return CareerOutreachResult(
                mode=mode, health_ok=False, errors=1, details=["health check raised"]
            )
        if not healthy:
            logger.info("career outreach: reasoning module unhealthy (remote down?) — clean skip")
            return CareerOutreachResult(mode=mode, health_ok=False)

        timeout = cfg.knob_int(conf, "dispatch_timeout_s")

        if mode == "observe":
            return await self._observe_seed(mod, timeout)
        return await self._live(mod, conf, timeout)

    async def _observe_seed(self, mod, timeout: int) -> CareerOutreachResult:
        """Seed the nudged-ledger from the pre-existing staged-draft backlog so a
        later flip to ``live`` does NOT nudge drafts that were staged before the
        monitor existed. Never dispatches a staging run, never nudges."""
        working, err = await self._list_working(mod, timeout)
        if err:
            return CareerOutreachResult(
                mode="observe", errors=1, details=["observe: list-staged dispatch error"]
            )
        seeded = 0
        for acct in working:
            company = (acct.get("company") or "").strip()
            if not company:
                continue
            wrote = await observations.create(
                self._db,
                id=uuid.uuid4().hex,
                source=_SOURCE,
                type=_NUDGE_TYPE,
                content=f"seed:{company}",
                priority="low",
                created_at=_now_z(),
                content_hash=_nudge_hash(company),
                skip_if_duplicate=True,
            )
            if wrote:
                seeded += 1
        return CareerOutreachResult(
            mode="observe",
            drafts_working=len(working),
            seeded=seeded,
            details=[f"observe: seeded {seeded} existing staged-draft account(s), no nudge"],
        )

    async def _live(self, mod, conf: dict, timeout: int) -> CareerOutreachResult:
        cap = cfg.knob_int(conf, "max_auto_runs_per_tick")
        # The gated first-touch flow is agentic (research → draft → verify → stage)
        # and needs far more than the ipc default of 25 turns (measured 42 live).
        max_turns = cfg.knob_int(conf, "dispatch_max_turns")
        autorun_prompt = _autorun_prompt(conf)
        auto_runs = 0
        verify_failed_n = 0
        errors = 0
        details: list[str] = []
        adapter_error = False
        # Repeat guard: the engine self-selects its target and may lack a durable
        # "attempted" state, so it can re-pick a company we already handled this tick.
        # Re-selecting a seen company → stop (never loop the cap on one account).
        seen_companies: set[str] = set()

        # 1. Drive up to `cap` first-touch draft-staging dispatches — the module
        #    self-selects + self-caps (won't re-pick an already-drafted account).
        for _ in range(cap):
            if self._is_paused():
                details.append("paused mid-tick — stopping auto-run loop")
                break
            result = await mod.execute_operation(
                "dispatch",
                {"prompt": autorun_prompt, "timeout_s": timeout, "max_turns": max_turns},
            )
            # Adapter-level failure (disabled / unhealthy / IPC error / timeout)
            # comes back as an error DICT — it does NOT raise. Surface it so the
            # runner records a job-health FAILURE, and stop the loop.
            if isinstance(result, dict) and result.get("error"):
                errors += 1
                adapter_error = True
                details.append(f"auto-run dispatch error: {str(result.get('error'))[:120]}")
                break
            payload = _parse_json(_reply_text(result))
            if not isinstance(payload, dict):
                errors += 1
                details.append("auto-run: unparseable reply (protocol failure) — stopping")
                break
            if payload.get("none_left"):
                details.append("auto-run: no target accounts left")
                break
            if payload.get("error"):
                # Module-reported failure (e.g. auth/drafting) — COUNT it so a
                # persistent module error surfaces as a job-health failure, not a
                # silent green tick; still stop the loop.
                errors += 1
                details.append(f"auto-run: module error: {str(payload.get('error'))[:120]}")
                break
            company = (payload.get("company") or "").strip()
            if "verify_failed" in payload:
                # The engine drafted but its OWN accuracy gate refused the draft (a
                # claim it could not verify headless). That is the gate WORKING — NOT a
                # job-health failure. Note it and try the next target. Presence-check
                # (not truthiness): a blank reason must NOT fall through to the staged
                # path and miscount a draft that never staged.
                if not company:
                    details.append("auto-run: verify_failed without 'company' — stopping")
                    break
                if company.lower() in seen_companies:
                    details.append(f"auto-run: engine re-selected {company} — stopping")
                    break
                seen_companies.add(company.lower())
                verify_failed_n += 1
                reason = (str(payload.get("verify_failed")).strip() or "unspecified")[:100]
                details.append(f"verify_failed: {company}: {reason}")
                continue
            if not company:
                errors += 1
                details.append("auto-run: reply missing 'company' (protocol failure) — stopping")
                break
            if company.lower() in seen_companies:
                details.append(f"auto-run: engine re-selected {company} — stopping")
                break
            seen_companies.add(company.lower())
            auto_runs += 1
            details.append(f"staged: {company}")

        # 2. Nudge — RE-DERIVE the nudge list from the current staged-draft set minus
        #    the already-nudged ledger (so an undelivered nudge simply retries next
        #    tick). Skip if the box just errored (avoid a second doomed dispatch).
        # GROUNDWORK(career-outreach-discovery): before the auto-run loop, drive any
        # due discovery step here (scoped small, < the 300s cap) to widen the target
        # set with net-new candidate companies; deferred for the MVP, which runs
        # against the engine's existing target backlog.
        nudged = 0
        drafts_working = 0
        if not adapter_error and self._is_paused():
            details.append("paused mid-tick — skipping staged-draft read + nudge")
        elif not adapter_error:
            working, err = await self._list_working(mod, timeout)
            if err:
                errors += 1
                details.append("nudge: list-staged dispatch error")
            else:
                drafts_working = len(working)
                fresh = []
                seen: set[str] = set()
                for acct in working:
                    company = (acct.get("company") or "").strip()
                    if not company or company.lower() in seen:
                        continue
                    seen.add(company.lower())
                    # unresolved_only=True honors the 30d TTL: resolve_expired sweeps the
                    # marker at TTL, re-opening the gate so a still-unsent draft is gently
                    # re-nudged (bounded dedup, matching the shipped 30d comment).
                    if await observations.exists_by_hash(
                        self._db,
                        source=_SOURCE,
                        content_hash=_nudge_hash(company),
                        unresolved_only=True,
                    ):
                        continue
                    fresh.append(
                        {"company": company, "contact": (acct.get("contact") or "").strip()}
                    )
                # N=0 → NO nudge (never a "0 drafts staged" spam message).
                if fresh and self._is_paused():
                    details.append(f"paused — skipping nudge for {len(fresh)} draft(s)")
                elif fresh:
                    from genesis.outreach.types import OutreachStatus

                    status = await self._nudge(fresh)
                    if status == OutreachStatus.DELIVERED:
                        for acct in fresh:
                            await observations.create(
                                self._db,
                                id=uuid.uuid4().hex,
                                source=_SOURCE,
                                type=_NUDGE_TYPE,
                                content=f"nudged:{acct['company']}",
                                priority="low",
                                created_at=_now_z(),
                                content_hash=_nudge_hash(acct["company"]),
                                skip_if_duplicate=True,
                            )
                        nudged = len(fresh)
                        details.append(f"nudged {nudged} new staged draft(s)")
                    elif status == OutreachStatus.REJECTED:
                        # Pipeline dedup — an identical nudge went out recently, so the
                        # owner already has it. NOT a failure; not re-marked (a harmless
                        # re-derive next tick, dampened by the set-hash topic dedup).
                        details.append(f"nudge deduped (already sent) — {len(fresh)} draft(s)")
                    else:
                        # FAILED / None (no pipeline / exception) — a real delivery
                        # failure. COUNT it so a persistent failure surfaces as a
                        # job-health failure; not marked, so it retries. (submit_raw skips
                        # quiet-hours governance, so IGNORED never occurs here — owner
                        # nudges deliver immediately, which is correct: owner-facing
                        # delivery is never gated by contract, and the tick runs at 08:00,
                        # post-quiet.)
                        errors += 1
                        details.append(
                            f"nudge not delivered (status={status}) — "
                            f"{len(fresh)} draft(s) retry next tick"
                        )

        return CareerOutreachResult(
            mode="live",
            auto_runs=auto_runs,
            drafts_working=drafts_working,
            nudged=nudged,
            verify_failed=verify_failed_n,
            errors=errors,
            details=details,
        )

    async def _list_working(self, mod, timeout: int) -> tuple[list[dict], bool]:
        """Ask the reasoning module for the accounts with an already-staged
        first-touch draft (the source of truth for staged drafts). Returns
        ``(accounts, err)`` where ``err`` is True on a dispatch failure OR a
        NON-EMPTY reply that is not a JSON array (a protocol violation the caller
        surfaces as a job-health failure); a genuinely empty reply is ``([], False)``."""
        # GROUNDWORK(career-outreach-http-read): this reads the staged-draft set via an
        # SSH CC dispatch to the reasoning module (~100s, costs a turn). A future HTTP
        # op returning that set would be a cheaper/faster read path — reintroduce a
        # `data_module` config knob pointing at it when it exists.
        result = await mod.execute_operation(
            "dispatch", {"prompt": _LIST_WORKING_PROMPT, "timeout_s": timeout}
        )
        if isinstance(result, dict) and result.get("error"):
            logger.info(
                "career outreach: list-staged dispatch error: %s",
                str(result.get("error"))[:120],
            )
            return [], True
        text = _reply_text(result)
        data = _parse_json(text)
        if isinstance(data, list):
            valid = [d for d in data if isinstance(d, dict) and (d.get("company") or "").strip()]
            if data and not valid:
                # A non-empty list with no schema-valid (company-bearing) entries is a
                # protocol violation — surface it, don't silently under-seed.
                logger.warning(
                    "career outreach: list-staged reply was a list with no company-bearing "
                    "entries: %r",
                    str(data)[:160],
                )
                return [], True
            return valid, False
        # Empty text or a non-array reply is a protocol violation — the module is
        # contracted to return a JSON array ("[]" for none). Surface it as an error
        # (err=True → job-health failure) so it is diagnosable and observe NEVER
        # under-seeds (which would make a later live tick over-nudge the backlog).
        logger.warning(
            "career outreach: list-staged reply was not a JSON array (malformed/empty): %r",
            (text or "")[:160],
        )
        return [], True

    async def _nudge(self, fresh: list[dict]):
        """Push ONE owner-facing Telegram nudge for the freshly-staged drafts.
        Returns the delivery ``OutreachStatus`` (``None`` if there is no pipeline or
        the send raised) so the caller can distinguish DELIVERED (mark), REJECTED
        (pipeline dedup — already sent, not a failure), and FAILED/None (a job-health
        failure). ``submit_raw`` skips quiet-hours governance, so IGNORED never occurs
        — owner-facing delivery is intentionally never gated."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

        pipeline = self._pipeline()
        if pipeline is None:
            logger.warning("career outreach: pipeline unavailable — cannot nudge")
            return None

        n = len(fresh)
        lines = "\n".join(
            f"• {a['company']}" + (f" — {a['contact']}" if a.get("contact") else "")
            for a in fresh[:20]
        )
        more = f"\n(+{n - 20} more)" if n > 20 else ""
        plural = "s" if n != 1 else ""
        text = (
            f"📇 Career outreach: {n} draft{plural} staged in your Drafts — "
            f"review + send:\n{lines}{more}"
        )
        # A stable hash of the fresh company set in the topic — submit_raw dedups on
        # (signal_type, topic, category) for 24h, so date+count alone would collapse
        # two DISTINCT same-size batches on one day into one; the set-hash keeps them
        # distinct while an identical same-day retry still dedups (idempotent resend).
        set_hash = hashlib.sha256(
            ",".join(sorted(a["company"].strip().lower() for a in fresh)).encode()
        ).hexdigest()[:8]
        request = OutreachRequest(
            category=OutreachCategory.NOTIFICATION,
            channel="telegram",
            topic=(
                f"Career outreach: {n} staged draft(s) "
                f"{datetime.now(UTC).strftime('%Y-%m-%d')} {set_hash}"
            ),
            context=text,
            signal_type="career_outreach_nudged",
            salience_score=0.85,
            verbatim=True,
        )
        try:
            result = await pipeline.submit_raw(text, request)
        except Exception:
            logger.error("career outreach: nudge failed", exc_info=True)
            return None
        status = getattr(result, "status", None)
        if status == OutreachStatus.DELIVERED:
            logger.info("career outreach: nudged %d staged draft(s)", n)
        else:
            logger.warning("career outreach: nudge not delivered (status=%s) — will retry", status)
        return status
