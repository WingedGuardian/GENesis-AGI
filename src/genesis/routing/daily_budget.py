"""Per-provider daily budget ledger — deselect a free provider whose
provider-side daily cap is spent, instead of 429-ing into paid fallback.

Free tiers cap usage per DAY, in the provider's OWN unit — Groq caps
TOKENS/day, Gemini caps REQUESTS/day — while Genesis modeled only per-minute
RPM. A provider that burns its daily budget early then fails every call for
the rest of the day, invisibly, while paid fallbacks absorb the load. This
ledger counts what the router observes and lets the chain walk skip a
provider whose configured ``rpd_limit`` / ``tpd_limit`` is spent. Exhaustion
is DESELECTION, never a circuit-breaker trip: budget is not a health signal
(the same doctrine that keeps 429s out of ``record_failure``).

Semantics and invariants — read before changing:

- **Lower bound, undercount-biased.** Counters are Genesis-router-observed
  usage: direct ``LiteLLMDelegate`` callers (eval runner, experimentation
  standalone router, evo) bypass the router and are not counted, and retries
  inside one provider visit count once. Every counting rule errs toward
  UNDERcount deliberately: undercounting means we still call and the
  provider's own 429s backstop us; OVERcounting deselects a servable
  provider with no correcting signal until the next UTC day (a deselected
  provider produces zero evidence). Hence requests count on success and on
  failures that returned a real non-429 status; 429s, timeouts and
  connection errors are NOT counted (whether a provider debits a rejected
  429 against daily quota is unproven, and the cost of guessing wrong is
  asymmetric). Tokens count on success only.
- **UTC-day accounting, and it is sound only for a ROLLING window.**
  Count-since-UTC-midnight is a subset of any rolling-24h window, so against
  a rolling limit this under-deselects, which is the safe direction.

  A FIXED reset at any hour other than 00:00 UTC is NOT safe in either
  direction, and an earlier version of this note claimed at-or-after was
  fine. It is not. Take a provider resetting at midnight Pacific: requests
  made between 00:00 UTC and that reset land in the ledger's NEW day while
  the provider still counts them against its OLD window, and they remain
  after the provider resets — so the ledger OVER-deselects a provider whose
  allowance is available. (CodeRabbit P2, PR #1624, verified.)

  LATENT, not live — WITH ONE LEG UNVERIFIED, and the shape of the argument
  is why that is easy to miss. Reaching this needs a provider with BOTH a
  configured daily limit AND a non-UTC reset. `groq-free` is the only
  provider carrying limits today (enumerated: every `rpd_limit`/`tpd_limit`
  in `config/model_routing.yaml` is in its block), and Gemini — the known
  Pacific-reset case, `docs/reference/models.md` — deliberately carries none.

  But that eliminates the candidate whose boundary is KNOWN and says nothing
  about the one that is live. Groq's own reset window is recorded NOWHERE in
  this repo: `docs/reference/models.md` gives its limits and not its
  boundary, and a search across `docs/`, `config/` and `src/` for groq near
  reset/midnight/boundary returns zero. So "latent" rests on an unmeasured
  assumption about the only provider it actually depends on, not on an
  enumeration. Closing it is cheap — observe when groq's TPD counter resets
  against UTC midnight — and worth doing before the next limit is added.

  The real fix models the provider's own reset boundary, including DST, from
  authoritative metadata rather than applying one calendar to everyone; the
  trigger is the first provider given a limit whose window is not UTC.
- **Limits live in config, not here.** ``exhausted()`` / ``record()`` take
  the live ``ProviderConfig``, so a dashboard config reload takes effect on
  the next check with zero ledger code. A provider with neither limit set is
  never tracked and never touches the state file — a fresh install with no
  limits configured never creates it.
- **Single-writer persistence** (mirrors the circuit-breaker WS-3c rule):
  only the genesis-server process writes the state file; MCP children
  construct with ``persist=False`` — they load the server's counters once,
  so they too skip exhausted providers, but their own usage goes uncounted
  (undercount side, backstopped).
- **The budget is per API key/account, not per provider entry.** Two
  provider entries sharing one account would split one real budget across
  two counters and neither would trip (undercount side). Keep one entry per
  account for daily-limited providers — the same consolidation reasoning as
  the groq-free alias history in ``config/model_routing.yaml``.
- Corrupt or missing state → zero counters (fail-open toward calling: this
  is cost optimization, and the 429s remain the backstop; a corrupt file
  must not silence a provider for a day).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.env import daily_budget_disabled
from genesis.util.atomic import atomic_write_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from genesis.routing.types import CallResult, ProviderConfig

logger = logging.getLogger(__name__)

#: Statuses that must NOT spend the ledger: the vendor either refused before
#: serving (429) or was never known to have served at all (408, which
#: `litellm_delegate` returns for the local deadline as well as a provider
#: timeout). Both would otherwise let a failing network exhaust a free tier.
_NOT_USAGE_STATUSES = frozenset({408, 429})

#: Resolved at CALL time, never frozen at import. `genesis_home()` is this
#: repo's one answer to "where does runtime state live", and it honours
#: GENESIS_HOME — which exists precisely so two installs can share a Unix
#: account. A module-level `Path.home() / ".genesis"` ignores it, so both
#: installs would read and overwrite ONE ledger: each would see the other's
#: requests against its own limits and deselect providers it had barely used
#: (Codex P2, PR #1624). Import-time is also too early to be correct — a test
#: conftest or a CLI that sets GENESIS_HOME after import gets the stale path.
_STATE_FILENAME = "routing_budget_state.json"


def _state_file() -> Path:
    from genesis.env import genesis_home

    return genesis_home() / _STATE_FILENAME


def _sanitized_counters(entry: dict) -> dict | None:
    """Trustworthy COUNTS from a persisted row, or None if it is not a row.

    `isinstance(x, int)` alone is not that test, in two ways that both let a
    damaged row defeat the budget it exists to enforce. A NEGATIVE count
    offsets later increments, so `"requests": -1000` lets the provider run a
    thousand calls past its configured limit before `exhausted()` turns true —
    the one outcome this ledger exists to prevent (CodeRabbit Major, PR #1624).
    And `isinstance(True, int)` is True, so a JSON boolean is accepted and then
    behaves as 1, the same trap `_parse_daily_limit` guards on the config side.

    Sanitised PER FIELD, and that is the load-bearing part. Rejecting the whole
    row on one damaged counter discards the SIBLING's valid spend and hands the
    provider a fresh allowance on a day it already spent — the identical
    over-spend, in the other unit. MEASURED on the first version of this fix:
    a row carrying `requests: 999, tokens: -1` loaded as 999 requests before
    the change and as ZERO after it, so tightening the validator re-created the
    defect it was closing. Keep what is trustworthy, zero only what is not.
    """
    if not isinstance(entry.get("day"), str):
        return None
    out = {"day": entry["day"], "requests": 0, "tokens": 0}
    for key in ("requests", "tokens"):
        value = entry.get(key)
        if not isinstance(value, bool) and isinstance(value, int) and value >= 0:
            out[key] = value
        else:
            logger.warning(
                "Daily budget counter %r is not a count (%r) — that unit starts "
                "from zero; the sibling unit is kept",
                key,
                value,
            )
    return out


class DailyBudgetLedger:
    """UTC-day request/token counters per provider, persisted as one small
    JSON file.

    Thread contract: WRITES happen only on the server's asyncio loop
    (``record``, and ``exhausted``/``record`` from the router's chain walk,
    with no await between a counter's read and write). The dashboard route
    reads from a Flask worker thread via ``status``, which is deliberately
    NON-MUTATING (``_peek``) — a torn cross-thread read's worst case is a
    stale or zero view, which is the undercount side. ``_save`` runs an
    fsync'd atomic write on the loop thread once per counted call; bounded
    by free-tier volume (~10^3/day) and dwarfed by LLM latency — debouncing
    would only move losses to the undercount side, so it is not worth the
    machinery yet."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        persist: bool = True,
    ) -> None:
        self._path = state_path or _state_file()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._persist = persist
        # name -> {"day": "YYYY-MM-DD", "requests": int, "tokens": int}
        self._counters: dict[str, dict] = {}
        self._load()

    # ── public API ──────────────────────────────────────────────────────

    def exhausted(self, cfg: ProviderConfig) -> bool:
        """True when this provider's configured daily budget is spent.

        False for providers with no limits, and always False under the
        GENESIS_DAILY_BUDGET_DISABLED kill switch.
        """
        if daily_budget_disabled() or not _limited(cfg):
            return False
        entry = self._peek(cfg.name)
        if cfg.rpd_limit is not None and entry["requests"] >= cfg.rpd_limit:
            return True
        return cfg.tpd_limit is not None and entry["tokens"] >= cfg.tpd_limit

    def record(self, cfg: ProviderConfig, result: CallResult) -> bool:
        """Record one provider visit's outcome against the daily counters.

        Returns True exactly when this record crossed the provider from
        not-exhausted to exhausted — the emit-once seam for the router's
        ``provider.budget_exhausted`` event. (No separate latch is needed:
        an exhausted provider is skipped by the chain walk, so it cannot
        record again until the day rolls over — including across restarts,
        where reloaded already-exhausted counters keep it skipped.)

        Known blind spot, disclosed: a BORN-exhausted state — an operator
        lowering a limit below counters already recorded today — produces no
        crossing and therefore no event; the dashboard ``daily_budget`` map
        is the visibility for that case. (Zero/negative limits, the other
        born-exhausted route, are rejected at config parse time.)
        """
        if daily_budget_disabled() or not _limited(cfg):
            return False
        # Undercount-biased counting rule — see module docstring.
        #
        # 429 was the only exclusion, and 408 belongs beside it for the same
        # reason: `litellm_delegate` returns 408 for BOTH its timeout paths —
        # including the LOCAL deadline, where Genesis gave up and the provider
        # may never have been asked at all. Counting those spends a vendor's
        # daily allowance on requests it did not serve, so a flaky network
        # deselects an otherwise healthy free provider for the rest of the UTC
        # day — the opposite of what a budget ledger is for, and undetectable
        # because the provider simply stops being chosen (Codex P2, PR #1624).
        #
        # Stated as a SET of not-usage statuses rather than `!= 429`, because
        # the next status that means "the vendor never served this" will be
        # added here rather than discovered the same way.
        # `reached_provider` is checked FIRST because a status code cannot
        # answer this one. The delegate synthesizes 500 for any exception
        # carrying no HTTP status — DNS, socket, TLS — so a transport failure
        # is indistinguishable from a server error by status alone, and
        # counting it spends the vendor's allowance on a request it never
        # saw. Repeated connection failures would otherwise deselect a
        # perfectly usable provider for the rest of the day, which is the
        # OVERCOUNT direction this module's own invariant forbids.
        count_request = result.success or (
            result.reached_provider
            and result.status_code is not None
            and result.status_code not in _NOT_USAGE_STATUSES
        )
        tokens = (result.input_tokens + result.output_tokens) if result.success else 0
        if not count_request and tokens == 0:
            return False
        was_exhausted = self.exhausted(cfg)
        entry = self._entry(cfg.name)
        if count_request:
            entry["requests"] += 1
        if tokens:
            entry["tokens"] += tokens
        self._save()
        return not was_exhausted and self.exhausted(cfg)

    def status(self, cfg: ProviderConfig) -> dict | None:
        """Live counters + limits for one provider, or None if untracked.

        The unit of each pair is named explicitly — requests and tokens are
        never comparable and never converted.
        """
        if not _limited(cfg):
            return None
        entry = self._peek(cfg.name)
        return {
            "requests_used": entry["requests"],
            "rpd_limit": cfg.rpd_limit,
            "tokens_used": entry["tokens"],
            "tpd_limit": cfg.tpd_limit,
            "exhausted": self.exhausted(cfg),
        }

    # ── internals ───────────────────────────────────────────────────────

    def _today(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def _peek(self, name: str) -> dict:
        """Rolled-over VIEW of a counter row without writing any state —
        safe for cross-thread readers and for pure checks. A row from
        another day reads as zeros; only ``record`` (via ``_entry``)
        actually rolls the stored counters over."""
        entry = self._counters.get(name)
        if entry is None or entry.get("day") != self._today():
            return {"day": self._today(), "requests": 0, "tokens": 0}
        return entry

    def _entry(self, name: str) -> dict:
        """Counter row for ``name``, rolled over lazily on UTC-day change."""
        entry = self._counters.get(name)
        today = self._today()
        if entry is None or entry.get("day") != today:
            entry = {"day": today, "requests": 0, "tokens": 0}
            self._counters[name] = entry
        return entry

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            providers = raw.get("providers")
            if not isinstance(providers, dict):
                raise ValueError("bad shape")
            for name, entry in providers.items():
                row = _sanitized_counters(entry) if isinstance(entry, dict) else None
                if row is not None:
                    self._counters[name] = row
                else:
                    # Not a counter row at all (no `day`). Nothing here is
                    # salvageable, so the provider starts from zero — the
                    # fail-open direction this class already takes for an
                    # unreadable file. A row that IS a counter row but has one
                    # damaged field is repaired field-wise above rather than
                    # dropped, so its good counter survives.
                    logger.warning(
                        "Daily budget row for %r is not a counter row (%r) — "
                        "starting it from zero",
                        name,
                        entry,
                    )
        except FileNotFoundError:
            return
        except Exception:
            # Corrupt state fails open (zero counters) — never silences a
            # provider for a day on a bad file.
            logger.warning(
                "Daily budget state at %s unreadable — starting from zero",
                self._path, exc_info=True,
            )
            self._counters = {}

    def _save(self) -> None:
        if not self._persist:
            return
        today = self._today()
        current = {
            name: entry
            for name, entry in self._counters.items()
            if entry.get("day") == today
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self._path,
                json.dumps({"version": 1, "providers": current}, indent=2),
            )
        except Exception:
            # A failed save loses at most a day of counts — undercount side.
            logger.warning(
                "Daily budget state save to %s failed", self._path, exc_info=True,
            )


def _limited(cfg: ProviderConfig) -> bool:
    return cfg.rpd_limit is not None or cfg.tpd_limit is not None
