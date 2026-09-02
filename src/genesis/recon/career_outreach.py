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
- **State: this-tick nudge only.** The owner nudge lists the drafts THIS tick's
  auto-run loop staged (the engine's own ``Staged`` replies are deterministic ground
  truth for what was staged); a ``career_outreach_nudged`` observation (content-hashed
  per company) records "the owner has been nudged for this draft" as belt-and-suspenders
  idempotency. There is NO mailbox census — a bare SSH ``claude -p`` read of the engine's
  staged-draft set is unreliable (it answers from priors, returning a false-empty list;
  proven live 2026-08-11).
- **Honors the engine's own accuracy gate.** The auto-run prompt drives the
  engine's COMPLETE first-touch flow INCLUDING its own verification gates (it does
  not bypass them), under a headless contract; a draft the engine's gate refuses
  comes back as ``verify_failed`` — that is the gate WORKING (NOT a job-health
  failure). Turn budget: the flow is agentic and needs > the ipc default of 25
  turns (measured 42 live), so the auto-run passes ``dispatch_max_turns``.
- **Timeout + orphan semantics.** The gated flow measured ~5.5 min live; the SSH CC
  adapter clamps ``timeout_s`` to a 3600s ceiling and this config caps it at 1800, so
  ``dispatch_timeout_s`` (default 900) bounds a hung run. A timeout kill severs the SSH
  connection but the REMOTE ``claude -p`` keeps running (orphaned). A draft the orphan
  stages AFTER the kill is NOT nudged (its reply never reached us) but is still visible
  in the owner's Drafts — a conscious limitation. The SAME strand happens when a
  mid-tick ``/pause`` skips the nudge for drafts already staged this tick: correct (no
  Telegram send while paused), but that draft is not re-surfaced next tick either.
  Durable cross-tick nudge retry (covering BOTH the orphan and the pause strand) is a
  tracked follow-up.
- **Modes** (``career_outreach_config``): ``off`` (default — inert) / ``observe``
  (a bridge-reachability probe — a minimal dispatch proving the ``claude -p`` hop
  answers; stages nothing, nudges nothing) / ``live`` (drive staging runs + nudge new
  drafts).
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import observations
from genesis.recon import career_outreach_config as cfg
from genesis.security.sanitizer import strip_control_chars

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

# Observe-mode reachability probe: a MINIMAL dispatch that exercises the real
# `claude -p` OAuth hop end-to-end. `check_health_cached()` passes even when the
# remote's model auth is dead (it checks the SSH/module, not the model login), so
# ONLY a real dispatch proves the bridge can actually answer. The reply CONTENT is
# irrelevant — any non-empty answer proves reachability. Deliberately NOT
# injection-shaped: a self-identifying liveness ping that asks for a plain
# acknowledgement, NOT a "reply with this opaque blob, nothing else" demand — the
# latter (verified live 2026-08-21) trips the engine's injection-defense into a
# refusal. Kept free of outreach/draft/stage vocabulary so it never trips the
# engine's outreach hooks either (see the _AUTORUN_PROMPT note above).
_PROBE_PROMPT = (
    "Genesis bridge liveness check — automated, no action required. Reply with a brief "
    "'ack' (any short text) to confirm this dispatch channel is reachable."
)
# A trivial reply returns in ~4s live; these bound a wedged probe without tying up the
# full agentic dispatch_timeout_s/max_turns budget. (A raw subprocess dispatch with no
# other watchdog is the justified exception to the no-speculative-timeout policy.)
_PROBE_TIMEOUT_S = 120
_PROBE_MAX_TURNS = 2


def _now_z() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nudge_hash(company: str) -> str:
    """Content-hash namespace for the per-company 'owner already nudged' marker."""
    return hashlib.sha256(f"career_nudged:{company.strip().lower()}".encode()).hexdigest()[:32]


# Bite-relay: pipeline stages that count as a "bite" — an advance INTO an engaged
# stage. Pre-engagement stages (researching / contacts_mapped / outreach_sent) and the
# terminal `closed` are deliberately excluded (user decision 2026-09-01). These match
# the external engine's own /api/pipeline stage vocabulary (verified against its source).
BITE_STAGES = ("in_conversation", "interviewing", "offer")

# Hard per-tick ceiling on engaged-stage entries the bite-relay will PROCESS (a DB op
# each — a live exists_by_hash read or an observe _record_bite write). Bounds the tick's
# work against a buggy or compromised data_module returning a huge pipeline; independent
# of the per-tick nudge cap (which bounds owner MESSAGES only). Generous: a single
# person's real engaged pipeline is tiny — this only bites on an anomalous response.
_MAX_BITE_CANDIDATES_PER_TICK = 500


def _bite_hash(company_key: object, stage: str) -> str:
    """Content-hash namespace for the per-(company, stage) 'advance already relayed'
    marker. Keyed on the STABLE company id (not the display name, which can change) +
    stage. A stage advance is a POINT EVENT, so this marker permanently dedups it
    (checked with unresolved_only=False)."""
    return hashlib.sha256(
        f"career_bite:{str(company_key).strip().lower()}:{stage}".encode()
    ).hexdigest()[:32]


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
    # Tolerate a verdict line (prose OR JSON), markdown fences, and trailing content
    # around the payload. Scan every candidate JSON-value start, decode a COMPLETE
    # value from each, and return the one whose decode reaches FURTHEST into the text
    # (largest end index). Per the "lead with a verdict line, THEN the compact JSON"
    # contract the payload is the TRAILING value, so the rightmost-reaching complete
    # object is it — ONE rule that handles a leading verdict-is-JSON, a ```-fenced
    # payload, trailing prose, AND nested objects (a nested value always ends before
    # its parent, so a top-level object out-reaches its own children).
    # KNOWN LIMIT: a reply with a SECOND standalone JSON object AFTER the payload
    # would mis-select the trailing one; classify_autorun_reply then fail-closes it as
    # ProtocolError (job-health error, never a false success or broken nudge). Accepted:
    # the auto-run prompt forbids any trailing content after the payload.
    decoder = json.JSONDecoder()
    best: object | None = None
    best_end = -1
    for idx, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            obj, end = decoder.raw_decode(text, idx)
        except (ValueError, RecursionError):
            # ValueError = not valid JSON at idx; RecursionError = pathologically
            # nested reply (absurd but possible) — neither should escape this
            # best-effort parser (a raise would be miscounted as a job-health error).
            continue
        if end > best_end:
            best, best_end = obj, end
    return best


def _autorun_prompt(conf: dict) -> str:
    """The auto-run dispatch prompt: the generic gate-honoring base plus the install's
    optional ``autorun_note`` overlay (install-specific gate guidance). The note is
    appended to the AUTO-RUN prompt ONLY — never the read prompt — so install-specific
    outreach/gate vocabulary cannot trip the external engine's own outreach hooks on
    the otherwise-clean staged-draft read path."""
    note = cfg.text_knob(conf, "autorun_note")
    return f"{_AUTORUN_PROMPT}\n\n{note}" if note else _AUTORUN_PROMPT


# ── auto-run reply: parse-into-a-closed-type (robust by construction) ──────────
# The engine's auto-run reply is untrusted-SHAPED LLM output. Rather than branch on
# raw payload keys in the tick loop (which leaks a new edge case per review), the
# reply is classified ONCE into EXACTLY ONE of these five outcomes; every malformed
# shape (non-dict, unparseable, missing/blank required field, wrong type, or two
# outcome signals at once) collapses to ProtocolError, decided HERE. ``_live`` then
# matches on the outcome TYPE and never inspects a raw payload.


@dataclass(frozen=True)
class NoneLeft:
    """No eligible target account remains — terminal, not an error."""


@dataclass(frozen=True)
class Staged:
    """The engine verified + staged a first-touch draft for ``company``."""

    company: str
    contact: str
    draft_summary: str


@dataclass(frozen=True)
class VerifyFailed:
    """The engine drafted but its OWN accuracy gate refused ``company`` — a soft
    outcome (the gate working), NOT a job-health error."""

    company: str
    reason: str


@dataclass(frozen=True)
class ModuleError:
    """The engine reported a hard failure (auth, drafting, …) — a job-health error."""

    detail: str


@dataclass(frozen=True)
class ProtocolError:
    """The reply did not match exactly one valid outcome shape — a job-health error."""

    detail: str


AutorunOutcome = NoneLeft | Staged | VerifyFailed | ModuleError | ProtocolError

# The mutually-exclusive OUTCOME-signal keys — a well-formed reply carries EXACTLY ONE
# (or none, which then must be a `Staged` shape identified by a company).
_OUTCOME_SIGNAL_KEYS = ("none_left", "error", "verify_failed")


def classify_autorun_reply(reply_text: str) -> AutorunOutcome:
    """Classify a raw auto-run dispatch reply into EXACTLY ONE closed outcome.

    Contract (the shipped ``_AUTORUN_PROMPT``): the engine replies — optionally led by
    its own verdict line — with a single compact JSON object that is exactly one of:
      - ``{"none_left": true}``                              → NoneLeft
      - ``{"company","contact","draft_summary"}``            → Staged   (no signal key)
      - ``{"verify_failed": <str>, "company": <str>}``       → VerifyFailed
      - ``{"error": <non-empty str>}``                       → ModuleError
    Anything else — not a JSON object, unparseable, ≥2 signal keys, a signal key with
    the wrong value/type, or no recognizable outcome — is a ProtocolError. This is the
    ONE place raw reply shape is inspected; it is exhaustively table-tested.
    """
    payload = _parse_json(reply_text)
    if not isinstance(payload, dict):
        return ProtocolError("reply is not a JSON object")

    present = [k for k in _OUTCOME_SIGNAL_KEYS if k in payload]
    if len(present) > 1:
        return ProtocolError(f"contradictory multi-signal reply: {sorted(present)}")

    company = (
        (payload.get("company") or "").strip() if isinstance(payload.get("company"), str) else ""
    )

    # A terminal signal (none_left / error) must NOT also carry a `company` KEY: a
    # company is the Staged outcome, so its presence alongside a terminal signal is a
    # merged (contradictory) reply, not "exactly one outcome". Test KEY PRESENCE (not
    # the normalized value) so a non-string/blank company (`123`, `null`, `"  "`) is
    # also rejected. verify_failed REQUIRES a company, handled separately below.
    if present == ["none_left"]:
        if "company" in payload:
            return ProtocolError("'none_left' outcome must not carry a 'company'")
        if payload.get("none_left") is True:
            return NoneLeft()
        return ProtocolError("'none_left' present but not literal true")

    if present == ["error"]:
        if "company" in payload:
            return ProtocolError("'error' outcome must not carry a 'company'")
        detail = payload.get("error")
        detail = detail.strip() if isinstance(detail, str) else ""
        if not detail:
            return ProtocolError("'error' outcome with empty/non-string detail")
        return ModuleError(detail[:200])

    if present == ["verify_failed"]:
        if not company:
            return ProtocolError("'verify_failed' outcome missing 'company'")
        raw = payload.get("verify_failed")
        # None/blank/whitespace → "unspecified"; any other value renders as-is.
        reason = ("" if raw is None else str(raw).strip()) or "unspecified"
        return VerifyFailed(company, reason[:100])

    # No outcome signal key → the reply must be a Staged shape (company required).
    if not company:
        return ProtocolError(
            "no recognized outcome (missing none_left/error/verify_failed and 'company')"
        )
    contact = payload.get("contact")
    contact = contact.strip() if isinstance(contact, str) else ""
    summary = payload.get("draft_summary")
    summary = summary.strip() if isinstance(summary, str) else ""
    return Staged(company=company, contact=contact, draft_summary=summary)


@dataclass(frozen=True)
class CareerOutreachResult:
    """Summary of one monitor tick (both sub-capabilities merged)."""

    mode: str = "off"  # the auto-run driver's lever
    bite_mode: str = "off"  # the INDEPENDENT bite-relay lever
    health_ok: bool = True
    auto_runs: int = 0  # dispatches that staged a fresh draft
    drafts_working: int = 0  # == auto_runs: drafts staged THIS tick (no backlog census)
    nudged: int = 0  # newly-staged drafts included in a delivered nudge
    verify_failed: int = 0  # engine drafted but its own accuracy gate refused (NOT an error)
    bites: int = 0  # pipeline advances relayed to the owner this tick
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
        """Run the two INDEPENDENT sub-capabilities and merge their results:
        the auto-run driver (SSH ``reasoning_module``, ``mode`` lever) and the
        bite-relay (HTTP ``data_module`` read + owner nudge, ``bite_relay_mode``
        lever). Each is health-gated on its OWN module, so one being off / absent /
        unhealthy never disables the other. The tick is skipped only when BOTH
        levers are off."""
        mode = cfg.effective_mode()
        bite_mode = cfg.effective_bite_relay_mode()
        if mode == "off" and bite_mode == "off":
            return CareerOutreachResult(mode="off", bite_mode="off")

        conf = cfg.load_config()
        reg = self._module_registry()
        if reg is None:
            logger.debug("career outreach: module registry unavailable — skip")
            return CareerOutreachResult(mode=mode, bite_mode=bite_mode)

        auto = await self._run_autorun(mode, conf, reg) if mode != "off" else None
        bite = await self._run_bite_relay(bite_mode, conf, reg) if bite_mode != "off" else None
        return self._merge_results(mode, bite_mode, auto, bite)

    @staticmethod
    def _merge_results(
        mode: str,
        bite_mode: str,
        auto: CareerOutreachResult | None,
        bite: CareerOutreachResult | None,
    ) -> CareerOutreachResult:
        """Combine the two sub-capability results into one tick summary. A branch that
        did not run contributes neutrally (0 counters, health_ok, no errors), so
        job-health reflects whatever DID run."""
        a = auto or CareerOutreachResult(mode=mode)
        b = bite or CareerOutreachResult(bite_mode=bite_mode)
        return CareerOutreachResult(
            mode=mode,
            bite_mode=bite_mode,
            health_ok=a.health_ok and b.health_ok,
            auto_runs=a.auto_runs,
            drafts_working=a.drafts_working,
            nudged=a.nudged,
            verify_failed=a.verify_failed,
            bites=b.bites,
            errors=a.errors + b.errors,
            details=[*a.details, *b.details],
        )

    async def _run_autorun(self, mode: str, conf: dict, reg) -> CareerOutreachResult:
        """The auto-run driver: dispatch the SSH reasoning module to stage drafts +
        nudge (``live``), or probe bridge reachability (``observe``). Health-gated on
        the reasoning module. (Body relocated verbatim from the former ``gather``.)"""
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
            logger.info(
                "career outreach: reasoning module unreachable (health check failed) — recording failure"
            )
            # An unreachable bridge is a job-health FAILURE, not a clean skip: observe's
            # whole purpose is to detect a dead bridge, so a persistent outage must SURFACE.
            # Recording a failure (a) never books a false success that would permanently
            # disqualify the job from the never-succeeded alarm, and (b) keeps last_run
            # fresh + total_failures climbing so the outage stays visible via PR #1428
            # (after the 3-failure floor) and the gap detector — instead of freezing the
            # counters and ageing out of the recency window. Non-noisy: the tick is daily,
            # nothing alarms until 3 consecutive misses, and the alarm is WARNING /
            # dashboard-only (never auto-remediated). Mirrors the health-check-raised path.
            return CareerOutreachResult(
                mode=mode,
                health_ok=False,
                errors=1,
                details=["reasoning module unreachable (health check failed)"],
            )

        timeout = cfg.knob_int(conf, "dispatch_timeout_s")

        if mode == "observe":
            return await self._observe_probe(mod)
        return await self._live(mod, conf, timeout)

    async def _run_bite_relay(self, bite_mode: str, conf: dict, reg) -> CareerOutreachResult:
        """Read the external pipeline and nudge the owner when a company has advanced
        into an engaged stage (``BITE_STAGES``). ``observe`` = read + SEED the dedup
        ledger without nudging; ``live`` = nudge on a NEW advance, then mark it.

        Health-gated on the DATA module (distinct from the auto-run's reasoning
        module). An unhealthy/absent data bridge is a CLEAN SKIP, NOT a job-health
        failure — the external read service is legitimately down when the search is
        dormant, and a daily false alarm on a dormant service is noise, not signal. A
        read the service DID answer with an error (service up, op failed) IS a genuine
        failure and is counted."""
        data_name = cfg.module_name(conf, "data_module")
        mod = reg.get(data_name) if data_name else None
        if mod is None:
            # The lever is ON here (this method runs only when bite_mode != "off"), so an
            # unresolvable data_module is a MISCONFIG worth surfacing — NOT the silent
            # generic-install case (that ships bite_mode=off and never reaches here).
            logger.warning(
                "career outreach bite-relay: enabled (bite_mode=%s) but data_module %r is "
                "unset/unresolvable — no-op; set data_module in the overlay",
                bite_mode,
                data_name,
            )
            return CareerOutreachResult(
                bite_mode=bite_mode,
                details=[f"bite-relay: data_module {data_name!r} unresolvable — no-op"],
            )

        try:
            healthy = await mod.check_health_cached()
        except Exception:
            logger.debug(
                "career outreach bite-relay: data health raised — clean skip", exc_info=True
            )
            return CareerOutreachResult(
                bite_mode=bite_mode, details=["bite-relay: data module health raised — skipped"]
            )
        if not healthy:
            logger.debug("career outreach bite-relay: data module unreachable — clean skip")
            return CareerOutreachResult(
                bite_mode=bite_mode, details=["bite-relay: data module unreachable — skipped"]
            )

        try:
            result = await mod.execute_operation("pipeline")
        except Exception as exc:
            # execute_operation normally returns an error DICT, but a malformed response
            # (e.g. a non-JSON body → resp.json() raising inside HttpIPCAdapter) can RAISE.
            # Guard it so a bite-relay read failure never escapes gather() and discards the
            # independently-computed auto-run result (mirrors the health-check guard above).
            logger.warning(
                "career outreach bite-relay: pipeline read raised — recording failure",
                exc_info=True,
            )
            return CareerOutreachResult(
                bite_mode=bite_mode,
                errors=1,
                details=[f"bite-relay: pipeline read raised: {str(exc)[:120]}"],
            )
        if not isinstance(result, dict) or result.get("error"):
            detail = result.get("error") if isinstance(result, dict) else "non-dict reply"
            return CareerOutreachResult(
                bite_mode=bite_mode,
                errors=1,
                details=[f"bite-relay: pipeline read failed: {str(detail)[:120]}"],
            )
        pipeline_by_stage = result.get("pipeline")
        if not isinstance(pipeline_by_stage, dict):
            # Service up but an unexpected shape (a schema change) — surface a failure
            # rather than silently reading nothing.
            return CareerOutreachResult(
                bite_mode=bite_mode,
                errors=1,
                details=["bite-relay: pipeline reply missing a 'pipeline' dict"],
            )

        from genesis.outreach.types import OutreachStatus

        # Flatten (stage, entry) across the engaged-stage buckets → a single pass, so the
        # per-tick nudge cap and the pause check need only one loop + one break.
        # Distinguish an ABSENT bucket (stage legitimately has no entries → skip) from a
        # PRESENT-but-wrong-type bucket (a partial schema change the top-level dict check
        # can't catch): the latter is surfaced via `malformed` → errors, never silently
        # dropped, so a jerbs schema drift can't masquerade as "no new advances" forever.
        candidates: list[tuple[str, dict]] = []
        malformed = 0
        for stage in BITE_STAGES:
            bucket = pipeline_by_stage.get(stage)
            if bucket is None:
                continue  # stage legitimately absent (JSON null / missing key) — normal
            if isinstance(bucket, list):
                candidates.extend((stage, e) for e in bucket)  # empty [] is fine (no entries)
            else:
                # Present but NOT a list ({}, "", 0, False, a non-empty dict/str, …) = a
                # partial schema change the top-level dict check can't catch → surface it,
                # never silently absorb as "no advances".
                malformed += 1
                logger.warning(
                    "career outreach bite-relay: stage %r bucket is %s, expected list "
                    "(schema drift?) — surfacing as failure",
                    stage,
                    type(bucket).__name__,
                )
        if len(candidates) > _MAX_BITE_CANDIDATES_PER_TICK:
            # Hard scan ceiling — bounds the per-tick DB work (a read/write per candidate)
            # against a buggy/oversized data_module response, independent of the
            # message-only nudge cap. NOTE: this is a WORK-bound, not a fair scheduler —
            # entries beyond the ceiling that PERSIST in the pipeline (e.g. a company that
            # stays in an engaged stage) keep occupying the truncated prefix, so a
            # pathological >ceiling-engaged response could starve the tail indefinitely.
            # Accepted: a real personal pipeline never has hundreds of simultaneously
            # engaged companies, and under the buggy/compromised response this defends
            # against, tail liveness is not a requirement (bounding work is). Applying the
            # ceiling AFTER the seen-filter would reintroduce the unbounded exists_by_hash
            # reads it exists to bound, so the prefix-cap is deliberate.
            logger.warning(
                "career outreach bite-relay: %d engaged-stage entries returned — truncating "
                "scan to %d (buggy/oversized data_module response?)",
                len(candidates),
                _MAX_BITE_CANDIDATES_PER_TICK,
            )
            candidates = candidates[:_MAX_BITE_CANDIDATES_PER_TICK]

        nudge_cap = cfg.knob_int(conf, "max_bite_nudges_per_tick")
        bites = 0
        errors = 0
        attempts = 0  # live nudge attempts this tick (the cap bounds owner messages)
        details: list[str] = []
        for stage, entry in candidates:
            if self._is_paused():
                details.append("paused — halting bite-relay")
                break
            if not isinstance(entry, dict):
                malformed += 1  # present-but-wrong-type entry → surface, never silent-drop
                continue
            company_id = entry.get("id")
            if company_id is None or company_id == "":
                continue  # no stable key → cannot dedup safely; skip (fail-safe)
            # Bound + sanitize the external name. strip_control_chars collapses the
            # line-forging / line-concealing class — C0/C1 controls (incl. \n\r\t), the
            # Unicode line+paragraph separators, and zero-width / bidi overrides — to a
            # single space and trims. company_name is rendered into a parse_mode="HTML"
            # Telegram message where html.escape (in _bite_nudge) neutralizes <>&"' but
            # NOT those characters, so a crafted job posting could otherwise forge extra
            # notification lines or visually reorder/conceal the message. Then [:200] so a
            # crafted long name can't bloat the obs row / nudge. The marker dedups on id
            # (not name), so neither transform affects dedup.
            company_name = (
                strip_control_chars(str(entry.get("name") or "")) or f"company {company_id}"
            )[:200]
            h = _bite_hash(company_id, stage)
            # unresolved_only=False: a stage advance is a POINT EVENT — the marker
            # permanently suppresses a re-nudge (never re-emits on resolution/TTL).
            if await observations.exists_by_hash(
                self._db, source=_SOURCE, content_hash=h, unresolved_only=False
            ):
                continue
            if bite_mode == "observe":
                # SEED the ledger without nudging (no owner message → the cap does not
                # apply), so the first live tick does not fire a backlog for cos already
                # advanced when observe started.
                await self._record_bite(h, company_name, stage)
                continue
            # live: bound the per-tick owner-message burst. Un-nudged advances are NOT
            # marked, so they surface on a later tick (loud-truncation, never dropped).
            if attempts >= nudge_cap:
                details.append(
                    f"bite-relay: per-tick nudge cap ({nudge_cap}) reached — "
                    "further advances deferred to the next tick"
                )
                break
            status = await self._bite_nudge(company_name, stage, h)
            attempts += 1
            if status == OutreachStatus.DELIVERED:
                await self._record_bite(h, company_name, stage)
                bites += 1
                details.append(f"bite: {company_name} → {stage}")
            elif status == OutreachStatus.REJECTED:
                # Pipeline dedup: submit_raw found an identical recent (company, stage)
                # nudge (the topic is unique per (company, stage)), so the owner ALREADY
                # received this exact advance. Treat it as relayed and WRITE THE MARKER —
                # this closes the crash-window where a prior tick delivered the nudge but
                # crashed before _record_bite committed (marker absent): without marking
                # here, the next daily tick (after the pipeline's 24h dedup expires) would
                # re-deliver the same point-event, violating the one-time contract. Not a
                # failure; not counted as a fresh bite (no new delivery this tick).
                await self._record_bite(h, company_name, stage)
                details.append(f"bite nudge deduped (marked relayed) — {company_name} → {stage}")
            else:
                # FAILED / None (no pipeline / exception) — a real delivery failure.
                errors += 1
                details.append(
                    f"bite nudge not delivered (status={status}) — {company_name} → {stage}"
                )
        if malformed:
            # Present-but-wrong-type buckets/entries → job-health failure, so a partial
            # jerbs schema change surfaces instead of silently reading nothing.
            errors += malformed
            details.append(
                f"bite-relay: {malformed} malformed engaged-stage bucket(s)/entry(ies) "
                "(schema drift?) — surfaced as failures"
            )
        if not details:
            details.append(f"bite-relay: {bite_mode} — no new advances")
        return CareerOutreachResult(
            bite_mode=bite_mode, bites=bites, errors=errors, details=details
        )

    async def _record_bite(self, content_hash: str, company_name: str, stage: str) -> None:
        """Write the permanent per-(company, stage) 'advance already relayed' marker."""
        await observations.create(
            self._db,
            id=uuid.uuid4().hex,
            source=_SOURCE,
            type="career_bite",
            content=f"bite:{company_name}:{stage}",
            priority="low",
            created_at=_now_z(),
            content_hash=content_hash,
            skip_if_duplicate=True,
        )

    async def _observe_probe(self, mod) -> CareerOutreachResult:
        """Observe mode: a bridge-REACHABILITY probe, NOT a seeder. A minimal dispatch
        exercises the real ``claude -p`` OAuth hop end-to-end (``check_health_cached``
        passes even when the remote's model auth is dead, so only a real dispatch proves
        the bridge answers). Stages nothing, nudges nothing, seeds nothing. A reachable
        bridge records job-health SUCCESS (which, once the bridge is alive again, clears a
        never-succeeded-job alarm); an adapter error or an empty reply records a FAILURE so
        a dead bridge surfaces on the daily tick instead of failing silently."""
        result = await mod.execute_operation(
            "dispatch",
            {"prompt": _PROBE_PROMPT, "timeout_s": _PROBE_TIMEOUT_S, "max_turns": _PROBE_MAX_TURNS},
        )
        # Reachable = the bridge answered AND did not report its OWN failure, AND the
        # reply is non-empty. Do NOT require an exact token (a live-but-verbose bridge is
        # still reachable). Two failure channels: a top-level `error` (the adapter's
        # transport error, `claude -p` non-zero exit) OR the payload's `is_error` — a
        # dead-model/OAuth run commonly exits 0 with `{"is_error": true, "text": "<auth
        # error>"}` and NO top-level `error` key, so checking only `error` would record
        # that dead bridge as reachable, the exact silent failure this probe exists to
        # catch. (A `parse_fallback` reply is still reachable — the hop answered.)
        if isinstance(result, dict) and (result.get("error") or result.get("is_error")):
            detail = result.get("error") or "claude -p reported is_error"
            return CareerOutreachResult(
                mode="observe",
                errors=1,
                details=[f"observe: bridge probe failed: {str(detail)[:120]}"],
            )
        if not _reply_text(result).strip():
            return CareerOutreachResult(
                mode="observe", errors=1, details=["observe: bridge probe returned empty reply"]
            )
        return CareerOutreachResult(mode="observe", details=["observe: bridge reachable"])

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
        # Repeat guard: the engine self-selects its target and may lack a durable
        # "attempted" state, so it can re-pick a company we already handled this tick.
        # Re-selecting a seen company → stop (never loop the cap on one account).
        seen_companies: set[str] = set()
        # The drafts THIS tick staged — the deterministic nudge source (no census).
        staged_this_tick: list[dict] = []

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
                details.append(f"auto-run dispatch error: {str(result.get('error'))[:120]}")
                break
            # Classify the reply ONCE into a closed outcome; match on TYPE. Every
            # malformed shape is a ProtocolError decided inside classify_autorun_reply,
            # so this loop never inspects a raw payload (robust by construction).
            outcome = classify_autorun_reply(_reply_text(result))
            if isinstance(outcome, NoneLeft):
                details.append("auto-run: no target accounts left")
                break
            if isinstance(outcome, ProtocolError):
                errors += 1
                details.append(f"auto-run: {outcome.detail} (protocol failure) — stopping")
                break
            if isinstance(outcome, ModuleError):
                # Engine-reported hard failure (auth/drafting) — a job-health failure.
                errors += 1
                details.append(f"auto-run: module error: {outcome.detail}")
                break
            if isinstance(outcome, (Staged, VerifyFailed)):
                # Both name a (classifier-validated non-empty) company and share the
                # repeat guard: the self-selecting engine may re-pick a target it already
                # acted on this tick (no durable "attempted" state) — stop rather than
                # loop the cap on one account.
                if outcome.company.lower() in seen_companies:
                    details.append(f"auto-run: engine re-selected {outcome.company} — stopping")
                    break
                seen_companies.add(outcome.company.lower())
                if isinstance(outcome, VerifyFailed):
                    # Engine drafted but its OWN accuracy gate refused this target — the
                    # gate WORKING, NOT a job-health failure. Note it, try the next target.
                    verify_failed_n += 1
                    details.append(f"verify_failed: {outcome.company}: {outcome.reason}")
                    continue
                # Staged: a verified draft was staged for this target. Record it as the
                # deterministic nudge source (the engine's own reply is ground truth for
                # what THIS tick staged — no mailbox census needed).
                auto_runs += 1
                staged_this_tick.append({"company": outcome.company, "contact": outcome.contact})
                details.append(f"staged: {outcome.company}")
                continue
            # Unreachable for the closed AutorunOutcome union — a defensive guard so a
            # future outcome type can never be silently mishandled (fail loud, not green).
            errors += 1
            details.append("auto-run: unhandled outcome type (protocol failure) — stopping")
            break

        # 2. Nudge the owner about the drafts THIS tick staged — from the auto-run's own
        #    `Staged` outcomes (deterministic), NOT a mailbox census. Nudge REGARDLESS of
        #    a mid-loop adapter error: the staged drafts are real and the engine won't
        #    re-stage them, so skipping the nudge would strand them (the error is already
        #    counted above). The ledger dedup is belt-and-suspenders — the engine won't
        #    re-pick an already-staged company (see _AUTORUN_PROMPT), so a company enters
        #    this set at most once; the marker only guards a rare re-stage.
        # GROUNDWORK(career-outreach-http-read): a future RELIABLE HTTP read of the
        #    engine's full staged-draft set could reintroduce a census-based nudge that
        #    ALSO surfaces drafts staged outside this monitor's own auto-runs. A bare SSH
        #    `claude -p` read of that set is NOT reliable — the engine answers it from
        #    priors, returning a false-empty list (proven live 2026-08-11); reintroduce a
        #    `data_module` knob pointing at a structured read op when one exists.
        # GROUNDWORK(career-outreach-discovery): before the auto-run loop, drive any due
        #    discovery step here (scoped small, < the dispatch cap) to widen the target
        #    set with net-new candidate companies; deferred for the MVP, which runs
        #    against the engine's existing target backlog.
        drafts_working = len(staged_this_tick)
        nudged = 0
        if self._is_paused():
            details.append(f"paused — skipping nudge for {len(staged_this_tick)} staged draft(s)")
        elif staged_this_tick:
            fresh = []
            for acct in staged_this_tick:
                company = acct["company"]  # classifier-validated non-empty
                # unresolved_only=True honors the 30d TTL on the marker (bounded dedup).
                if await observations.exists_by_hash(
                    self._db,
                    source=_SOURCE,
                    content_hash=_nudge_hash(company),
                    unresolved_only=True,
                ):
                    continue
                fresh.append(acct)
            # N=0 (all already nudged) → NO nudge (never a "0 drafts staged" spam message).
            if fresh:
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
                    # Pipeline dedup — an identical nudge went out recently, so the owner
                    # already has it. NOT a failure; not re-marked.
                    details.append(f"nudge deduped (already sent) — {len(fresh)} draft(s)")
                else:
                    # FAILED / None (no pipeline / exception) — a real delivery failure.
                    # COUNT it so a persistent failure surfaces as a job-health failure.
                    # (IGNORED/HELD can't reach here: both are EMAIL-channel-only
                    # terminals in _deliver, and this nudge is telegram + verbatim — so
                    # the only reachable statuses are DELIVERED / REJECTED / FAILED / None.)
                    errors += 1
                    details.append(f"nudge not delivered (status={status}) — {len(fresh)} draft(s)")

        return CareerOutreachResult(
            mode="live",
            auto_runs=auto_runs,
            drafts_working=drafts_working,
            nudged=nudged,
            verify_failed=verify_failed_n,
            errors=errors,
            details=details,
        )

    async def _nudge(self, fresh: list[dict]):
        """Push ONE owner-facing Telegram nudge for the freshly-staged drafts.
        Returns the delivery ``OutreachStatus`` (``None`` if there is no pipeline or
        the send raised) so the caller can distinguish DELIVERED (mark), REJECTED
        (pipeline dedup — already sent, not a failure), and FAILED/None (a job-health
        failure). IGNORED/HELD can't occur — both are EMAIL-channel-only terminals in
        the pipeline, and this owner nudge is telegram + verbatim."""
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

    async def _bite_nudge(self, company_name: str, stage: str, dedup_key: str):
        """Push ONE owner Telegram nudge that a company advanced into an engaged stage.
        ``dedup_key`` is the caller's per-(full company_id, stage) marker hash (``_bite_hash``).
        Returns the delivery ``OutreachStatus`` (``None`` if no pipeline / the send
        raised) so the caller marks only on DELIVERED. Plain ``submit_raw`` (mirrors
        ``_nudge``) — NOTE: the ``best_effort`` no-retry flag is not on origin/main yet
        (PR #1585); applying it to the career nudges is a follow-up once that lands."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

        pipeline = self._pipeline()
        if pipeline is None:
            logger.warning("career outreach bite-relay: pipeline unavailable — cannot nudge")
            return None

        # company_name is EXTERNAL pipeline input and the outreach telegram path sends
        # parse_mode="HTML" WITHOUT escaping — HTML-escape it so a crafted name (e.g. from
        # an ingested job posting) can't render as live markup in the owner's chat. (When
        # PR #1585's shared _sanitize_ping_field lands on main, reuse it here — follow-up.)
        safe_name = html.escape(company_name)
        text = f"🎯 Career: {safe_name} advanced to {stage.replace('_', ' ')}"
        request = OutreachRequest(
            category=OutreachCategory.NOTIFICATION,
            channel="telegram",
            # Dedup identity on BOTH of submit_raw's dedup queries is `dedup_key` — the
            # caller's per-(FULL company_id, stage) marker hash (_bite_hash), the SAME key
            # as the permanent 'already relayed' observation. The PRIMARY query keys on
            # (signal_type, topic, category); the SECONDARY on content_hash(context[:200]).
            # Using the full-id hash (NOT a raw str(company_id)[:100] prefix) for both means
            # REJECTED ⟺ this exact (full id, stage) was delivered in the last 24h — so
            # mark-on-REJECTED below can never suppress a DISTINCT advance (two ids sharing
            # a >100-char prefix would collide after truncation; the hash cannot). The
            # delivered message is the `text` arg (submit_raw ignores request.context for
            # delivery — it feeds only the dedup hash), so this sets dedup identity only,
            # not what the owner sees.
            topic=f"Career bite: {dedup_key}",
            context=f"{dedup_key}\n{text}",
            signal_type="career_bite",
            salience_score=0.85,
            verbatim=True,
        )
        try:
            result = await pipeline.submit_raw(text, request)
        except Exception:
            logger.error("career outreach bite-relay: nudge failed", exc_info=True)
            return None
        status = getattr(result, "status", None)
        if status == OutreachStatus.DELIVERED:
            logger.info("career outreach bite-relay: relayed %s → %s", company_name, stage)
        else:
            logger.warning("career outreach bite-relay: nudge not delivered (status=%s)", status)
        return status
