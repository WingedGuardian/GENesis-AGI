"""NetworkSentinel control surface — live-read levers + probe tuning.

The ONE place the connectivity sentinel and its downstream consumers consult
for policy (repo_pulse_config lineage — same fresh-per-call, degrade-toward-
less-authority discipline). All keys live under the ``network:`` section of
``config/resilience.yaml`` (+ the ``~/.genesis/config/resilience.local.yaml``
overlay), so they register under the existing ``resilience`` settings domain —
no new settings domain.

Levers (re-read live, no boot cache):
- :func:`sentinel_enabled` — env kill switch ``GENESIS_NETWORK_SENTINEL_DISABLED=1``
  (stdlib-cheap hard off) OR ``enabled: false``. When off, the sentinel never
  starts and every consumer degrades to the empty-state (== fresh install).
- :func:`effective_parking_mode` — ``off | shadow | live`` (default **shadow**;
  PR-3 consumes it to gate degraded-mode parking). Invalid → ``shadow`` (observe
  only, less write authority — never silently ``live``).
- :func:`backup_push_retry_mode` — ``off | live`` (default **live**; PR-4
  consumes it). Invalid → ``off`` (less action).

Probe tuning (:func:`structural`) is read once at sentinel construction but
lives here too so an operator can retune anchors/cadences via config.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only.
``genesis.mcp.health.settings`` imports the mode tuples FROM here, never the
reverse (one-way, the immunity rule).
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

PARKING_MODES = ("off", "shadow", "live")
BACKUP_RETRY_MODES = ("off", "live")

_ENV_KILL_SWITCH = "GENESIS_NETWORK_SENTINEL_DISABLED"
_CONFIG_NAME = "resilience.yaml"
_SECTION = "network"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "parking_mode": "shadow",  # PR-3 lever — soak one outage before live
    "backup_push_retry": "live",  # PR-4 lever — immediate push retry on restore
    # Probe anchors — privacy-bland public DNS (Cloudflare + Google), reachable
    # from any install, config-overridable. DNS+TCP anchors exercise name
    # resolution AND connectivity; IP-literal anchors isolate a DNS-only failure
    # (resolution dead, routing alive) from a full outage.
    "dns_tcp_anchors": ["one.one.one.one", "dns.google"],
    "ip_anchors": ["1.1.1.1", "8.8.8.8"],
    "probe_port": 443,
    "probe_timeout_s": 3,
    # Cadence: fast while not-clean (catch recovery quickly), steady when green.
    "fast_cadence_s": 20,
    "steady_cadence_s": 120,
    # Asymmetric hysteresis: fast to declare OFFLINE, slow to clear.
    "offline_all_fail_rounds": 2,  # consecutive all_fail rounds → OFFLINE
    "online_clean_rounds": 3,  # consecutive clean rounds → ONLINE
    "stable_online_s": 300,  # hold ONLINE this long before recovery hook
    "merge_gap_s": 600,  # new OFFLINE within this of last close merges
}

_INT_KNOBS = (
    "probe_port",
    "probe_timeout_s",
    "fast_cadence_s",
    "steady_cadence_s",
    "offline_all_fail_rounds",
    "online_clean_rounds",
    "stable_online_s",
    "merge_gap_s",
)


@dataclass(frozen=True)
class NetworkTuning:
    """Validated, immutable probe/hysteresis tuning for the sentinel."""

    dns_tcp_anchors: tuple[str, ...]
    ip_anchors: tuple[str, ...]
    probe_port: int
    probe_timeout_s: int
    fast_cadence_s: int
    steady_cadence_s: int
    offline_all_fail_rounds: int
    online_clean_rounds: int
    stable_online_s: int
    merge_gap_s: int

    @property
    def staleness_threshold_s(self) -> int:
        """A snapshot older than 3× the steady cadence is stale (consumers
        fail-safe). Derived, not configured, so it always tracks the cadence."""
        return self.steady_cadence_s * 3


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged ``network:`` section fresh — per call, NO cache.

    Deep-merges DEFAULTS ← ``resilience.yaml`` network section ← overlay.
    Missing/corrupt files degrade toward DEFAULTS. A flat schema (no nested
    dicts) so a shallow ``update`` overlay never drops sibling defaults.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    raw: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            raw = loaded
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("resilience config unreadable at %s", base_path)
    try:
        raw = merge_local_overlay(raw, base_path)
    except Exception:
        logger.warning("resilience overlay merge failed", exc_info=True)
    section = raw.get(_SECTION, {})
    if isinstance(section, dict):
        merged.update(section)
    return merged


def sentinel_enabled() -> bool:
    """Whether the sentinel should run. Env kill switch wins over config."""
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return False
    return bool(load_config().get("enabled", True))


def effective_parking_mode() -> str:
    """PR-3 degraded-mode parking lever. Invalid → ``shadow`` (observe only)."""
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("parking_mode")
    if mode is False:  # YAML-1.1 `parking_mode: off` → boolean False; honor it
        return "off"
    if mode not in PARKING_MODES:
        logger.warning("network parking_mode invalid %r — degrading to shadow", mode)
        return "shadow"
    return mode


def backup_push_retry_mode() -> str:
    """PR-4 restore-push-retry lever. Invalid → ``off`` (less action)."""
    cfg = load_config()
    mode = cfg.get("backup_push_retry")
    if mode is False:
        return "off"
    if mode not in BACKUP_RETRY_MODES:
        logger.warning("network backup_push_retry invalid %r — degrading to off", mode)
        return "off"
    return mode


def parking_decision(now: datetime | None = None) -> str:
    """PR-3 consumer gate: should degraded-mode parking apply *right now*?

    The single source of truth for "the network is actionably OFFLINE", shared
    by every consumer (the CC-invoker preflight and the surplus tier filter) so
    they cannot drift. Combines the :func:`effective_parking_mode` lever with the
    live connectivity snapshot (``network_state.json``) and the same freshness
    guard the watchdog/dashboard use.

    Returns one of:
      ``"off"``    — lever off; caller must no-op (behave as pre-sentinel).
      ``"normal"`` — not actionably OFFLINE (snapshot absent, stale, or
                     NORMAL/DEGRADED); caller proceeds exactly as today.
      ``"shadow"`` — fresh OFFLINE + shadow mode; caller logs "would park" and
                     proceeds (observe-only soak).
      ``"park"``   — fresh OFFLINE + live mode; caller applies its parking action.

    Fail-safe: shadow/off are the only non-``normal`` returns unless the snapshot
    is a *fresh* OFFLINE — any missing/garbled/stale signal degrades to
    ``"normal"`` (never invents a park). ``now`` is injectable for tests.
    """
    from genesis.resilience import network_state

    mode = effective_parking_mode()
    if mode == "off":
        return "off"
    snapshot = network_state.read_state()
    if snapshot is None or snapshot.get("state") != "OFFLINE":
        return "normal"
    age = network_state.probe_age_s(snapshot, now or datetime.now(UTC))
    if age is None or age > structural().staleness_threshold_s:
        # Stalled/absent sentinel — don't act on a connectivity signal we can't
        # trust (mirrors the watchdog's fail-toward-safe staleness rule).
        return "normal"
    return "park" if mode == "live" else "shadow"


def _knob_int(cfg: dict[str, Any], key: str) -> int:
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def _str_list(cfg: dict[str, Any], key: str) -> tuple[str, ...]:
    value = cfg.get(key)
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        value = DEFAULTS[key]
    # Empty list disables that anchor class — a plausible operator intent (e.g.
    # DNS-only). The sentinel guards the zero-TOTAL-anchors case in _probe_round,
    # surfacing it as an explicit 'not configured' state rather than a false green.
    return tuple(value)


def structural(cfg: dict[str, Any] | None = None) -> NetworkTuning:
    """Coerce config into validated :class:`NetworkTuning`. Damage → defaults."""
    cfg = cfg if cfg is not None else load_config()
    return NetworkTuning(
        dns_tcp_anchors=_str_list(cfg, "dns_tcp_anchors"),
        ip_anchors=_str_list(cfg, "ip_anchors"),
        probe_port=_knob_int(cfg, "probe_port"),
        probe_timeout_s=_knob_int(cfg, "probe_timeout_s"),
        fast_cadence_s=_knob_int(cfg, "fast_cadence_s"),
        steady_cadence_s=_knob_int(cfg, "steady_cadence_s"),
        offline_all_fail_rounds=_knob_int(cfg, "offline_all_fail_rounds"),
        online_clean_rounds=_knob_int(cfg, "online_clean_rounds"),
        stable_online_s=_knob_int(cfg, "stable_online_s"),
        merge_gap_s=_knob_int(cfg, "merge_gap_s"),
    )
