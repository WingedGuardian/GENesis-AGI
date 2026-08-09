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

:class:`EnrichmentStatus` / :func:`compute_enrichment` carry those non-gating signals
alongside the tier: which premium web-search providers are keyed, whether S2S voice is
deliberately configured, the ego think-cadence, and the shipped autonomy default level.
Like the tier, they are presence-based and pure over persisted state plus two injected
config ints (``ego_cadence_minutes``, ``autonomy_level``), so the module stays free of
runtime/ego/autonomy imports on the ``setup-status`` hot path.

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

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from genesis.channels.bridge_config import build_bridge_config, parse_secrets_env_text
from genesis.env import secrets_path
from genesis.onboarding.floor import (
    UNSET_SENTINELS,
    FloorStatus,
    compute_floor,
    read_persisted_secrets,
)

logger = logging.getLogger(__name__)

_TIER_NAMES = {0: "Bootstrapped", 1: "Functional", 2: "Connected", 3: "Autonomous"}

# Premium (keyed) web-search providers, kept pre-sorted so the enrichment field has a
# stable order without re-sorting per request. Web search is available regardless of
# these — the keyless SearXNG primary is the bootstrap default — so this is pure
# enrichment: "which PAID providers augment the keyless baseline", never a tier gate.
_WEB_SEARCH_PREMIUM_PROVIDERS = ("brave", "exa", "tavily", "tinyfish")

# Voice S2S provider -> the secrets key that enables it. Mirrors
# ``channels.voice.config.s2s_enabled`` BUT requires an explicit provider: that module
# defaults ``VOICE_S2S_PROVIDER`` to "openai", which would count any OpenAI LLM key as
# "voice configured" — the panel wants deliberate opt-in, not an incidental LLM key.
_VOICE_PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "gemini": "GOOGLE_API_KEY"}


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


# ── Enrichment (non-gating capability signals surfaced by the panel) ───────────────


def _as_int(value: object, default: int) -> int:
    """Coerce an injected config value to ``int``, falling back on any bad value.

    Keeps :func:`compute_enrichment` truly never-raising even if the route hands it a
    non-numeric ego cadence / autonomy level. ``int()`` raises across a WIDER set than
    is obvious: ``TypeError`` (``None``/list), ``ValueError`` (non-numeric str), AND
    ``OverflowError`` (``int(float('inf'))`` — a YAML ``.inf`` in a user-overridden ego/
    autonomy config resolves to a float infinity). All three fall back to ``default``.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _web_search_keyed_providers(secrets: Mapping[str, str]) -> tuple[str, ...]:
    """Premium web-search providers whose canonical key is configured, in stable order.

    Enrichment only: web search is available regardless (the keyless SearXNG primary is
    the bootstrap default), so this reports which PAID providers augment it — never a
    floor/tier gate. Each web-search tool provider reads ONLY its canonical
    ``API_KEY_<TYPE>`` env var (``web/search.py`` for Brave; ``runtime/init/providers.py``
    for Tavily/Exa/TinyFish) — NOT routing's 3-pattern resolver — so we check that exact
    key, not ``provider_key_present`` (whose ``<TYPE>_API_KEY`` / ``_API_TOKEN`` aliases
    would advertise a provider that its adapter can't actually load). Pure; never raises.
    """
    return tuple(
        p
        for p in _WEB_SEARCH_PREMIUM_PROVIDERS
        if str(secrets.get(f"API_KEY_{p.upper()}", "")).strip() not in UNSET_SENTINELS
    )


def _voice_configured(secrets: Mapping[str, str]) -> bool:
    """Whether S2S voice is DELIBERATELY configured (explicit provider + its key).

    Requires an explicit ``VOICE_S2S_PROVIDER`` (``openai``/``gemini``) in ``secrets``
    AND that provider's API key present — NOT ``channels.voice.config.s2s_enabled``,
    whose "openai" default would report voice for any box that merely has an
    ``OPENAI_API_KEY``. Pure dict read; never raises.
    """
    provider = str(secrets.get("VOICE_S2S_PROVIDER", "")).strip()
    key_name = _VOICE_PROVIDER_KEYS.get(provider)
    if key_name is None:
        return False
    return str(secrets.get(key_name, "")).strip() not in UNSET_SENTINELS


@dataclass(frozen=True)
class EnrichmentStatus:
    """Non-gating capability signals surfaced by the readiness panel.

    NONE of these affect the tier (see the module docstring) — they describe how far an
    install is *enriched* beyond the minimal functional path. Presence-based and pure
    over persisted state plus two injected config ints, like :class:`ReadinessStatus`.
    """

    web_search_keyed_providers: tuple[str, ...]
    voice_configured: bool
    ego_cadence_minutes: int
    autonomy_level: int

    def as_dict(self) -> dict[str, object]:
        # Distinct key namespace from the tier/floor fields — the route merges all three
        # dicts into one flat payload, so these must not collide with existing keys.
        return {
            "web_search_keyed_providers": list(self.web_search_keyed_providers),
            "voice_configured": self.voice_configured,
            "ego_cadence_minutes": self.ego_cadence_minutes,
            "autonomy_level": self.autonomy_level,
        }


def compute_enrichment(
    secrets: Mapping[str, str] | None = None,
    *,
    ego_cadence_minutes: int,
    autonomy_level: int,
) -> EnrichmentStatus:
    """Package the panel's non-gating enrichment signals.

    The two pure signals (web-search keyed providers, voice) are read from ``secrets``
    (defaulting to a live persisted read); the two config ints — ``ego_cadence_minutes``
    (ego config) and ``autonomy_level`` (``config/autonomy.yaml`` default) — are
    INJECTED by the route, exactly as :func:`compute_readiness` injects ``ego_enabled``,
    so this module needs no ego/autonomy import on the hot path.

    **Never raises — by construction.** The whole body is wrapped so that ANY
    exception (a raising ``read_persisted_secrets``, an unforeseen coercion edge, a
    future signal reader) degrades to an empty enrichment rather than propagating a 500
    into the first-run ``setup-status`` route. Enrichment is non-gating, so an "unknown"
    baseline is the correct fail-safe; the per-value guards (``_as_int``) keep the good
    fields even when one is bad, and this wrap is the backstop for everything else.
    """
    try:
        if secrets is None:
            secrets = read_persisted_secrets()
        return EnrichmentStatus(
            web_search_keyed_providers=_web_search_keyed_providers(secrets),
            voice_configured=_voice_configured(secrets),
            ego_cadence_minutes=_as_int(ego_cadence_minutes, 60),
            autonomy_level=_as_int(autonomy_level, 1),
        )
    except Exception:  # noqa: BLE001 - enrichment must never raise into setup-status
        logger.warning("compute_enrichment failed; returning empty enrichment", exc_info=True)
        return EnrichmentStatus(
            web_search_keyed_providers=(),
            voice_configured=False,
            ego_cadence_minutes=60,
            autonomy_level=1,
        )
