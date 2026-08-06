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
  This is the EXACT condition the live adapter's own start-gate enforces
  (``channels.bridge._load_bridge_config``) — and it is computed the same way that
  gate computes it: the **raw** ``secrets.env`` text parsed with the adapter's own
  manual parser (see ``_parse_secrets_env_text``), NOT the floor's dotenv view.
  (Importing the bridge to reuse the gate directly would drag the whole
  ``GenesisRuntime`` onto the ``setup-status`` hot path, and ``_load_bridge_config``
  additionally ``sys.exit``\\s on a missing file — neither is acceptable here.) The
  two parsers are pinned equal by a parity test across quoted / commented /
  interpolated shapes.
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

Like the floor, readiness is **presence-based and pure over persisted state plus one
injected bool** (``ego_enabled``, resolved by the route from the ego config so this
module needs no runtime/ego import) — safe on the ``setup-status`` hot path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from genesis.env import secrets_path
from genesis.onboarding.floor import FloorStatus, compute_floor, read_persisted_secrets

# The token sentinel the live adapter treats as "unset"
# (``channels.bridge._load_bridge_config``).
_TELEGRAM_TOKEN_PLACEHOLDER = "PLACEHOLDER"  # noqa: S105 - sentinel, not a credential

_TIER_NAMES = {0: "Bootstrapped", 1: "Functional", 2: "Connected", 3: "Autonomous"}


def _parse_secrets_env_text(text: str) -> dict[str, str]:
    """Parse ``secrets.env`` TEXT with the SAME manual semantics the live Telegram
    adapter uses (``channels.bridge._load_bridge_config``): line-based,
    ``key.strip() = value.strip().strip('"')``, ``#``-prefixed and blank lines
    skipped.

    Deliberately NOT dotenv: dotenv additionally strips *single* quotes, strips
    *inline* ``# comments``, and interpolates ``${VAR}`` — so a hand-edited
    ``TELEGRAM_ALLOWED_USERS='12345'`` would parse to a valid id under dotenv but is
    rejected by the adapter's manual parser. The adapter's parse is the ground truth
    for whether Telegram can actually start, so the T2 signal mirrors it exactly
    (pinned by ``tests/test_onboarding/test_readiness.py::
    test_telegram_reach_parity_with_bridge``).
    """
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip().strip('"')
    return parsed


def _telegram_reach_configured(secrets_text: str) -> bool:
    """Whether Genesis can PROACTIVELY reach the owner via Telegram.

    Decided from the RAW ``secrets.env`` text exactly as the live adapter start-gate
    decides it: a non-empty, non-placeholder ``TELEGRAM_BOT_TOKEN`` AND at least one
    valid numeric id in ``TELEGRAM_ALLOWED_USERS`` (its first entry seeds the default
    DM recipient, so without it there is no one to message unprompted). Pure — no
    file IO, no runtime import.
    """
    secrets = _parse_secrets_env_text(secrets_text)
    token = secrets.get("TELEGRAM_BOT_TOKEN", "")
    if not token or token == _TELEGRAM_TOKEN_PLACEHOLDER:
        return False
    allowed_raw = secrets.get("TELEGRAM_ALLOWED_USERS", "")
    return any(uid.strip().isdigit() for uid in allowed_raw.split(","))


def _read_secrets_env_text() -> str:
    """Raw ``secrets.env`` text for the T2 manual parse; never raises.

    A missing file (fresh install) or an unreadable one yields ``""`` → "telegram not
    configured", never an exception onto the ``setup-status`` route. (Unlike the
    adapter's ``_load_bridge_config``, which ``sys.exit``\\s on a missing file — wrong
    behaviour for a dashboard hot path.)
    """
    try:
        path = secrets_path()
        return path.read_text() if path.is_file() else ""
    except OSError:
        return ""


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
    secrets: Mapping[str, str] | None = None,
    *,
    ego_enabled: bool,
    secrets_text: str | None = None,
) -> ReadinessStatus:
    """Compute the cumulative readiness tier.

    ``secrets`` (dotenv-parsed) drives the floor legs; ``secrets_text`` (raw
    ``secrets.env`` text) drives the T2 Telegram gate via the adapter's manual parse.
    Both default to a live read. ``ego_enabled`` is **injected** (not read here) so
    this module needs no runtime/ego import on the hot path — the route resolves it
    from the ego config and passes it in. Never raises (delegates to never-raise
    reads).
    """
    if secrets is None:
        secrets = read_persisted_secrets()
    if secrets_text is None:
        secrets_text = _read_secrets_env_text()
    floor = compute_floor(secrets=secrets)
    return ReadinessStatus(
        floor=floor,
        telegram_configured=_telegram_reach_configured(secrets_text),
        ego_enabled=bool(ego_enabled),
    )
