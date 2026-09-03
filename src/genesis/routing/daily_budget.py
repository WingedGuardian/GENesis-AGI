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
- **UTC-day accounting.** Sound while every limited provider's window is
  rolling or resets at-or-after 00:00 UTC: count-since-UTC-midnight is a
  subset of any rolling-24h window, so vs a rolling window this only
  under-deselects. A provider with a fixed reset BEFORE UTC midnight would
  be over-deselected for the gap — none is configured today; retry-hint
  parsing (W3) is the exact fix if one appears.
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

_STATE_FILE = Path.home() / ".genesis" / "routing_budget_state.json"


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
        self._path = state_path or _STATE_FILE
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
        count_request = result.success or (
            result.status_code is not None and result.status_code != 429
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
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("day"), str)
                    and isinstance(entry.get("requests"), int)
                    and isinstance(entry.get("tokens"), int)
                ):
                    self._counters[name] = entry
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
