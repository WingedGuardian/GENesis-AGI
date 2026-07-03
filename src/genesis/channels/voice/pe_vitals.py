"""Voice PE hardware vitals — read from Home Assistant for the dashboard Device panel.

The Voice PE (ESP32-S3) exposes device-health sensors — internal temperature,
wifi signal, uptime, reset reason, free heap, loop time, plus a voice-assistant
connected/status pair — via ESPHome, which surface as Home Assistant entities.
This module polls them over HA's REST API on demand (read-only observability;
nothing here acts on the device).

Entities are addressed by an install-specific prefix ``HA_VOICE_PE_PREFIX``
(e.g. ``home_assistant_openai_realtime_voice_<id>_``) so the device id never
lives in committed source — the same invariant as ``HA_MEDIA_PLAYER_ENTITY``.
The poller NEVER raises: not-configured or HA-unreachable returns
``{"reachable": False, "reason": ...}`` so the dashboard degrades gracefully
(mirrors ``observability.ambient_health.evaluate_ambient_health``).
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# HA is on the LAN and a single-entity GET is quick. Keep this below the route's
# ``_async_route`` timeout so httpx raises first and we return a structured body
# instead of the route's 503 gate.
_HTTP_TIMEOUT_S = 7.0

# (entity-suffix, HA domain, output-key, carries-a-unit).
_VITALS: tuple[tuple[str, str, str, bool], ...] = (
    ("internal_temperature", "sensor", "temperature", True),
    ("wifi_signal", "sensor", "wifi_signal", True),
    ("uptime", "sensor", "uptime", True),
    ("reset_reason", "sensor", "reset_reason", False),
    ("heap_free", "sensor", "heap_free", True),
    ("loop_time", "sensor", "loop_time", True),
    ("voice_assistant_connected", "binary_sensor", "connected", False),
    ("voice_assistant_status", "sensor", "status", False),
)


def _ha_config() -> tuple[str, str, str] | None:
    """``(url, token, prefix)`` from the environment, or ``None`` if not fully set."""
    url = os.environ.get("HA_URL", "").rstrip("/")
    token = os.environ.get("HA_LONG_LIVED_TOKEN", "")
    prefix = os.environ.get("HA_VOICE_PE_PREFIX", "")
    if not (url and token and prefix):
        return None
    return url, token, prefix


async def _fetch_state(
    client: httpx.AsyncClient, url: str, headers: dict, entity_id: str,
) -> tuple[str | None, str | None]:
    """GET one entity's state. Returns ``(state, unit)``; ``(None, None)`` when the
    entity is absent (404). Raises ``httpx.*`` on connection/HTTP failure — the
    caller gathers these with ``return_exceptions=True``."""
    resp = await client.get(f"{url}/api/states/{entity_id}", headers=headers)
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()
    data = resp.json()
    unit = (data.get("attributes") or {}).get("unit_of_measurement")
    return data.get("state"), unit


async def fetch_voice_pe_vitals() -> dict:
    """Poll the Voice PE vitals from Home Assistant. Never raises.

    Returns ``{"reachable": True, "temperature": "111.2", "temperature_unit": "°F",
    "wifi_signal": ..., "connected": "off", "status": "Idle", ...}`` on success, or
    ``{"reachable": False, "reason": ...}`` when unconfigured / HA is unreachable /
    no Voice PE entities answer.
    """
    cfg = _ha_config()
    if cfg is None:
        return {
            "reachable": False,
            "reason": "not configured (needs HA_URL, HA_LONG_LIVED_TOKEN, HA_VOICE_PE_PREFIX)",
        }
    url, token, prefix = cfg
    headers = {"Authorization": f"Bearer {token}"}
    entity_ids = [f"{dom}.{prefix}{suf}" for suf, dom, _, _ in _VITALS]

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            results = await asyncio.gather(
                *(_fetch_state(client, url, headers, eid) for eid in entity_ids),
                return_exceptions=True,
            )
    except Exception as exc:  # client setup / unexpected — stay graceful
        # NOTE: no exc_info — an httpx error can carry the Request (Authorization
        # header with the HA token) in its traceback chain; keep the token out of logs.
        logger.warning("Voice PE vitals fetch failed: %s: %s", type(exc).__name__, exc)
        return {"reachable": False, "reason": type(exc).__name__}

    vitals: dict = {"reachable": True}
    ha_responded = False  # did HA reply at all (200 or an HTTP error), vs unreachable?
    any_ok = False        # did any entity yield a usable reading?
    for (_suf, _dom, key, has_unit), res in zip(_VITALS, results, strict=True):
        if isinstance(res, BaseException):
            # An HTTP status error means HA replied (entity-level failure); a
            # transport/timeout/JSON error means we couldn't get a reading for it.
            if isinstance(res, httpx.HTTPStatusError):
                ha_responded = True
            vitals[key] = None
            continue
        ha_responded = True
        state, unit = res
        # HA reports "unavailable"/"unknown" for a known sensor with no current
        # reading — normalize to None so the panel hides the card, not the word.
        if state in ("unavailable", "unknown"):
            state = None
        vitals[key] = state
        if has_unit and unit and state is not None:
            vitals[f"{key}_unit"] = unit
        if state is not None:
            any_ok = True

    if not ha_responded:
        return {"reachable": False, "reason": "could not reach Home Assistant"}
    if not any_ok:
        # HA replied but every Voice PE entity errored / was absent / unavailable.
        return {"reachable": False, "reason": "Home Assistant reachable but no Voice PE entities reported"}
    return vitals
