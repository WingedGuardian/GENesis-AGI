"""CareerOutreachMonitor — an ACTUATOR-in-recon that drives a dormant external
career-agent engine to stage first-touch outreach drafts, then nudges the owner.

Unlike its sibling ``AccountActivityMonitor`` (a sense-and-report monitor), this
one *acts on the owner's behalf*: on a daily tick it dispatches to a configured
external "reasoning" module (the install's own career-agent, declared in the
``~/.genesis`` module overlay) to run that engine's auto-run-to-staged-draft for
up to ``max_auto_runs_per_tick`` page-worthy accounts, then pushes ONE Telegram
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
- **Absent user-module → clean no-op.** On an install without the career-agent
  overlay module, ``module_registry.get()`` returns ``None`` and the tick returns
  silently (never a job-health failure on a generic install).
- **State: nudge-dedup only.** The remote engine's ``radar_status`` is the source
  of truth for which accounts have staged drafts; a ``career_outreach_nudged``
  observation (content-hashed per company) records only "the owner has already
  been nudged for this draft", so a re-tick does not re-nudge. Because the nudge
  list is RE-DERIVED from the working set each tick minus this ledger, a
  non-delivered nudge simply retries next tick.
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
# and staging; Genesis only drives the cadence + the owner nudge. Each dispatch
# instructs the module to do exactly ONE account and reply with ONLY compact JSON
# (its entire reply is the bridge return value — no prose reaches this side).
_AUTORUN_PROMPT = (
    "This is Genesis driving your outreach engine (a timed dispatch under the "
    "bridge's hard cap). Pick your SINGLE top page-worthy account that is NOT yet "
    "worked (radar_status watch/radar, not already 'working'), and run your "
    "auto-run-to-staged-draft for it: eval-lite -> find-contact -> draft -> stage "
    "the first-touch draft into the owner's mail Drafts, then promote it to "
    "radar_status='working'. NEVER send. Do EXACTLY ONE account. Your ENTIRE reply "
    "is the return value: reply with ONLY compact JSON, no prose — "
    '{"company":"<name>","contact":"<email-or-name>","draft_summary":"<one line>"} '
    'if you staged one, or {"none_left":true} if no page-worthy account remains, '
    'or {"error":"<reason>"} if you could not.'
)

_LIST_WORKING_PROMPT = (
    "This is Genesis. READ-ONLY: list the accounts currently at "
    "radar_status='working' (a first-touch draft is already staged in the owner's "
    "mail Drafts, awaiting the owner's send). Stage nothing, change nothing. Your "
    "ENTIRE reply is the return value: reply with ONLY a compact JSON array, no "
    'prose — [{"company":"<name>","contact":"<email-or-name>"}, ...] (empty array '
    "[] if none)."
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


@dataclass(frozen=True)
class CareerOutreachResult:
    """Summary of one monitor tick."""

    mode: str = "off"
    health_ok: bool = True
    auto_runs: int = 0  # dispatches that staged a fresh draft
    drafts_working: int = 0  # accounts seen at radar_status='working'
    nudged: int = 0  # newly-staged drafts included in a delivered nudge
    seeded: int = 0  # observe: existing working accounts seeded into the ledger
    errors: int = 0  # adapter-level dispatch failures (→ job-health FAILURE)
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
        mod = reg.get(reasoning_name)
        if mod is None:
            # Absent user-module → clean no-op (a generic install lacks the
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
        """Seed the nudged-ledger from the pre-existing working backlog so a later
        flip to ``live`` does NOT nudge drafts that were staged before the monitor
        existed. Never dispatches a staging run, never nudges."""
        working, err = await self._list_working(mod, timeout)
        if err:
            return CareerOutreachResult(
                mode="observe", errors=1, details=["observe: list-working dispatch error"]
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
            details=[f"observe: seeded {seeded} existing working account(s), no nudge"],
        )

    async def _live(self, mod, conf: dict, timeout: int) -> CareerOutreachResult:
        cap = cfg.knob_int(conf, "max_auto_runs_per_tick")
        auto_runs = 0
        errors = 0
        details: list[str] = []
        adapter_error = False

        # 1. Drive up to `cap` auto-run-to-staged-draft dispatches — the module
        #    self-selects + self-caps (won't re-pick a 'working' one).
        for _ in range(cap):
            result = await mod.execute_operation(
                "dispatch", {"prompt": _AUTORUN_PROMPT, "timeout_s": timeout}
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
                details.append("auto-run: unparseable reply — stopping")
                break
            if payload.get("none_left"):
                details.append("auto-run: no page-worthy accounts left")
                break
            if payload.get("error"):
                # Module-reported (not adapter) failure for this account — soft
                # stop, not a job-health failure.
                details.append(f"auto-run: module error: {str(payload.get('error'))[:120]}")
                break
            company = (payload.get("company") or "").strip()
            if not company:
                details.append("auto-run: reply missing 'company' — stopping")
                break
            auto_runs += 1
            details.append(f"staged: {company}")

        # 2. Nudge — RE-DERIVE the nudge list from the current working set minus the
        #    already-nudged ledger (so an undelivered nudge simply retries next
        #    tick). Skip if the box just errored (avoid a second doomed dispatch).
        # GROUNDWORK(career-outreach-discovery): before the auto-run loop, drive any
        # due discovery sweep here (scoped small, < the 300s cap) to widen the
        # radar with net-new agentic-AI companies; deferred for the MVP, which
        # runs against the existing radar backlog.
        nudged = 0
        drafts_working = 0
        if not adapter_error:
            working, err = await self._list_working(mod, timeout)
            if err:
                errors += 1
                details.append("nudge: list-working dispatch error")
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
                if fresh:
                    if await self._nudge(fresh):
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
                    else:
                        details.append(
                            f"nudge not delivered — {len(fresh)} draft(s) retry next tick"
                        )

        return CareerOutreachResult(
            mode="live",
            auto_runs=auto_runs,
            drafts_working=drafts_working,
            nudged=nudged,
            errors=errors,
            details=details,
        )

    async def _list_working(self, mod, timeout: int) -> tuple[list[dict], bool]:
        """Ask the reasoning module for the current ``radar_status='working'``
        accounts (the source of truth for staged drafts). Returns ``(accounts,
        err)`` where ``err`` is True on an adapter-level dispatch failure (the
        caller surfaces it). A successful dispatch whose reply is not a JSON array
        yields ``([], False)`` but is logged at WARNING — a persistently-malformed
        reply must be diagnosable, not silently indistinguishable from "none"."""
        # GROUNDWORK(career-outreach-http-read): this reads the working set via an SSH
        # CC dispatch to the reasoning module (~100s, costs a turn). A future Career
        # Agent HTTP op returning the radar working-set would be a cheaper/faster read
        # path — reintroduce a `data_module` config knob pointing at it when it exists.
        result = await mod.execute_operation(
            "dispatch", {"prompt": _LIST_WORKING_PROMPT, "timeout_s": timeout}
        )
        if isinstance(result, dict) and result.get("error"):
            logger.info(
                "career outreach: list-working dispatch error: %s",
                str(result.get("error"))[:120],
            )
            return [], True
        text = _reply_text(result)
        data = _parse_json(text)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)], False
        # Not an adapter failure, but do not swallow a malformed reply silently —
        # it would otherwise look identical to "no working accounts".
        if text.strip():
            logger.warning(
                "career outreach: list-working reply was not a JSON array "
                "(malformed module reply?): %s",
                text.strip()[:160],
            )
        return [], False

    async def _nudge(self, fresh: list[dict]) -> bool:
        """Push ONE owner-facing Telegram nudge for the freshly-staged drafts.
        Returns True ONLY on a confirmed DELIVERED result (submit_raw returns
        FAILED/IGNORED/REJECTED without raising — those must not count)."""
        pipeline = self._pipeline()
        if pipeline is None:
            logger.warning("career outreach: pipeline unavailable — cannot nudge")
            return False
        from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

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
        request = OutreachRequest(
            category=OutreachCategory.NOTIFICATION,
            channel="telegram",
            # Date-stamped so distinct ticks are not deduped into one, while an
            # identical same-day fresh-set (an undelivered retry within the day)
            # dedups rather than double-sends.
            topic=f"Career outreach: {n} staged draft(s) {datetime.now(UTC).strftime('%Y-%m-%d')}",
            context=text,
            signal_type="career_outreach_nudged",
            salience_score=0.85,
            verbatim=True,
        )
        try:
            result = await pipeline.submit_raw(text, request)
        except Exception:
            logger.error("career outreach: nudge failed", exc_info=True)
            return False
        if result is not None and result.status == OutreachStatus.DELIVERED:
            logger.info("career outreach: nudged %d staged draft(s)", n)
            return True
        logger.warning(
            "career outreach: nudge not delivered (status=%s) — will retry",
            getattr(result, "status", None),
        )
        return False
