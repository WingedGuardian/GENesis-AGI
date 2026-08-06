"""Tiered *readiness* — the capability spectrum ABOVE the functional floor.

The floor (:mod:`genesis.onboarding.floor`) answers a single binary question:
"can this install *think*?" Readiness answers "how far past the floor is it?" — a
cumulative 4-tier model layered directly on the floor's primitives so the two can
never drift:

* **T0 Bootstrapped** — install ran but the floor is not yet met (can't think).
* **T1 Functional** — the floor is met (thinks + remembers).
* **T2 Connected** — Genesis can PROACTIVELY reach the owner outside the dashboard:
  a real ``TELEGRAM_BOT_TOKEN`` AND at least one valid numeric user id in
  ``TELEGRAM_ALLOWED_USERS`` (whose first entry seeds the default DM recipient).
  This is the exact condition the live adapter's own start-gate enforces
  (``channels.bridge._load_bridge_config``) — replicated here as a pure,
  hot-path-safe secrets read (importing the bridge would drag the whole
  ``GenesisRuntime`` onto the ``setup-status`` path) and pinned to the real gate by
  a parity test.
* **T3 Autonomous** — the ego/awareness loop is enabled (``ego.enabled``), the
  user-controlled driver of proactive behaviour. NOTE: the autonomy subsystem has
  **no on/off switch** — its manager/dispatcher are always initialised on a running
  install and gated per-action by the mandatory approval gate — so "autonomy" is not
  a tier gate; the ego loop is the honest signal for "acts on its own".

Tiers are cumulative: a tier is reached only when its own gate AND all lower gates
are met, so de-configuring a lower capability visibly drops the level (matching the
floor's live-recompute philosophy). Everything else that makes an install *good*
rather than *minimal* — web-search availability, autonomy posture, surplus, voice,
the dashboard password — is enrichment surfaced by the panel, never a tier gate.

Like the floor, readiness is **presence-based and pure over persisted secrets plus
one injected bool** (``ego_enabled``, resolved by the route from the ego config so
this module needs no runtime/ego import) — safe on the ``setup-status`` hot path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from genesis.onboarding.floor import FloorStatus, compute_floor, read_persisted_secrets

# The token sentinel the live adapter treats as "unset"
# (``channels.bridge._load_bridge_config``) — kept in exact sync by
# ``tests/test_onboarding/test_readiness.py::test_telegram_reach_parity_with_bridge``.
_TELEGRAM_TOKEN_PLACEHOLDER = "PLACEHOLDER"  # noqa: S105 - sentinel, not a credential

_TIER_NAMES = {0: "Bootstrapped", 1: "Functional", 2: "Connected", 3: "Autonomous"}


def _telegram_reach_configured(secrets: Mapping[str, str]) -> bool:
    """Whether Genesis can PROACTIVELY reach the owner via Telegram.

    Mirrors the live adapter start-gate (``channels.bridge._load_bridge_config``): a
    non-empty, non-placeholder ``TELEGRAM_BOT_TOKEN`` AND at least one valid numeric
    user id in ``TELEGRAM_ALLOWED_USERS`` (its first entry seeds the default DM
    recipient, so without it there is no one to message unprompted). Pure secrets
    read — no runtime import. Pinned to the real gate by a parity test.
    """
    token = str(secrets.get("TELEGRAM_BOT_TOKEN", "")).strip()
    if not token or token == _TELEGRAM_TOKEN_PLACEHOLDER:
        return False
    allowed_raw = str(secrets.get("TELEGRAM_ALLOWED_USERS", ""))
    return any(part.strip().isdigit() for part in allowed_raw.split(","))


@dataclass(frozen=True)
class ReadinessStatus:
    """The readiness gates and the cumulative tier they imply."""

    floor: FloorStatus
    telegram_configured: bool  # T2 — proactive Telegram reach
    ego_enabled: bool  # T3 — ego/awareness loop enabled

    @property
    def tier(self) -> int:
        """0..3, cumulative — the highest tier whose gate AND all lower gates hold."""
        if not self.floor.floor_met:
            return 0
        if not self.telegram_configured:
            return 1
        if not self.ego_enabled:
            return 2
        return 3

    @property
    def tier_name(self) -> str:
        return _TIER_NAMES[self.tier]

    def as_dict(self) -> dict[str, object]:
        # Only the readiness-specific fields — the floor legs (``cc_oauth`` /
        # ``llm_key_present`` / ``embedding_key_present`` / ``floor_met``) are already
        # emitted by the ``setup-status`` route, so re-emitting them here would
        # duplicate keys.
        return {
            "tier": self.tier,
            "tier_name": self.tier_name,
            "telegram_configured": self.telegram_configured,
            "ego_enabled": self.ego_enabled,
        }


def compute_readiness(
    secrets: Mapping[str, str] | None = None, *, ego_enabled: bool
) -> ReadinessStatus:
    """Compute the cumulative readiness tier from persisted secrets + ego state.

    ``ego_enabled`` is **injected** (not read here) so this module stays pure over
    ``secrets`` with no runtime/ego import on the hot path — the route resolves it
    from the ego config and passes it in. ``secrets`` defaults to a live
    ``read_persisted_secrets()`` read. Never raises (delegates to the floor's own
    never-raise reads).
    """
    if secrets is None:
        secrets = read_persisted_secrets()
    floor = compute_floor(secrets=secrets)
    return ReadinessStatus(
        floor=floor,
        telegram_configured=_telegram_reach_configured(secrets),
        ego_enabled=bool(ego_enabled),
    )
