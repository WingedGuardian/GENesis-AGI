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
  This is the EXACT condition the live adapter's own start-gate enforces — it runs the
  SAME side-effect-free loader (``channels.bridge_config.build_bridge_config`` over the
  raw ``secrets.env`` text, NOT the floor's dotenv view), so the two cannot drift.
  (``channels.bridge._load_bridge_config`` itself can't be reused directly — it drags
  the whole ``GenesisRuntime`` onto the ``setup-status`` hot path and ``sys.exit``\\s on
  a missing file — so both it and readiness call the extracted stdlib-only loader.) A
  parity test still pins the two equal across quoted / commented / malformed shapes.
* **T3 Autonomous** — the ego/awareness loop is enabled (``ego.enabled``) AND
  bootstrap is complete (the ``~/.genesis/setup-complete`` marker, surfaced as
  ``onboarded``). Both are required because ``EgoCadenceManager._should_run()`` rejects
  every cycle when the marker is absent (``ego/cadence.py``), so an enabled-but-not-yet-
  onboarded ego cannot actually act. The ego loop is the honest signal for "acts on its
  own"; the autonomy subsystem itself has **no on/off switch** (its manager/dispatcher
  are always initialised and gated per-action by the mandatory approval gate), so
  "autonomy" is not a tier gate.

Tiers are cumulative: a tier is reached only when its own gate AND all lower gates
are met, so de-configuring a lower capability visibly drops the level (matching the
floor's live-recompute philosophy). Everything else that makes an install *good*
rather than *minimal* — web-search availability, autonomy posture, surplus, voice,
the dashboard password — is enrichment surfaced by the panel, never a tier gate.

Like the floor, readiness is **presence-based and pure over persisted state plus two
injected bools** (``ego_enabled`` from the ego config and ``onboarded`` from the marker,
both resolved by the route so this module needs no runtime/ego import) — safe on the
``setup-status`` hot path.

**Presence-based limitation (deliberate):** readiness answers "is this *configured* to
work", not "is it running right now", exactly as the floor reports "LLM key present"
even if that provider is momentarily down. It therefore does NOT reflect *runtime*
disables that leave persisted config intact — e.g. launching with
``python -m genesis serve --no-telegram`` skips the Telegram adapter
(``hosting/standalone.py``) while the credentials remain, so T2 still reports Connected.
Capturing that would require live-runtime coupling on the hot path (and would never end
— any number of runtime conditions can suppress a configured capability); it is out of
scope for this signal by design.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from genesis.channels.bridge_config import build_bridge_config, parse_secrets_env_text
from genesis.env import secrets_path
from genesis.onboarding.floor import FloorStatus, compute_floor, read_persisted_secrets

_TIER_NAMES = {0: "Bootstrapped", 1: "Functional", 2: "Connected", 3: "Autonomous"}


def _telegram_reach_configured(secrets_text: str) -> bool:
    """Whether Genesis can PROACTIVELY reach the owner via Telegram.

    Delegates to the **same** side-effect-free loader the live adapter start-gate uses
    (``channels.bridge_config``): True iff ``build_bridge_config`` returns a config for
    the raw ``secrets.env`` text — i.e. the adapter would actually load (non-empty,
    non-placeholder token AND ≥1 valid numeric ``TELEGRAM_ALLOWED_USERS`` id AND every
    other parsed setting well-formed). Because it runs the loader's OWN code rather than
    a copy, the T2 signal cannot drift from what Telegram will actually do.

    A value the loader chokes on (a non-numeric ``DAY_BOUNDARY_HOUR``; a
    ``TELEGRAM_ALLOWED_USERS`` entry that ``str.isdigit()`` accepts but ``int()`` rejects,
    e.g. ``'²'``) makes ``build_bridge_config`` RAISE → the adapter would stay stopped →
    not reachable, so this catches it and returns False. Pure — no file IO, no runtime
    import, never raises. ``log`` is omitted so the loader is silent on the hot path.
    """
    try:
        return build_bridge_config(parse_secrets_env_text(secrets_text)) is not None
    except (ValueError, TypeError):
        return False


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
    except (OSError, ValueError):
        # OSError: missing / permission / disk. ValueError covers UnicodeDecodeError
        # (its subclass) — a non-UTF-8 secrets.env must degrade to "not configured",
        # NOT 500 the setup-status route. (``except OSError`` alone let it escape.)
        return ""


@dataclass(frozen=True)
class ReadinessStatus:
    """The readiness gates and the cumulative tier they imply."""

    floor: FloorStatus
    telegram_configured: bool  # T2 — proactive Telegram reach
    ego_enabled: bool  # T3 — ego/awareness loop enabled
    onboarded: bool  # T3 — ~/.genesis/setup-complete marker (the ego gate needs it)

    @property
    def tier(self) -> int:
        """0..3, cumulative — the highest tier whose gate AND all lower gates hold."""
        if not self.floor.floor_met:
            return 0
        if not self.telegram_configured:
            return 1
        # T3 (Autonomous) requires BOTH the ego loop enabled AND bootstrap complete:
        # EgoCadenceManager._should_run() rejects every cycle when the
        # ~/.genesis/setup-complete marker is absent (ego/cadence.py), so an enabled
        # ego that is not onboarded still cannot act — reporting Autonomous there
        # would contradict `onboarded: false`.
        if not (self.ego_enabled and self.onboarded):
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
    onboarded: bool,
    secrets_text: str | None = None,
) -> ReadinessStatus:
    """Compute the cumulative readiness tier.

    ``secrets`` (dotenv-parsed) drives the floor legs; ``secrets_text`` (raw
    ``secrets.env`` text) drives the T2 Telegram gate via the adapter's manual parse.
    Both default to a live read. ``ego_enabled`` (ego config) and ``onboarded`` (the
    ``~/.genesis/setup-complete`` marker) are **injected** — both gate T3, since the
    ego cadence requires the marker AND ``ego.enabled`` to run — so this module needs
    no runtime/ego import on the hot path (the route resolves both and passes them in).
    Never raises (delegates to never-raise reads).
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
        onboarded=bool(onboarded),
    )
