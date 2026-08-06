"""Config lever for the reflection depth → model/effort mapping.

Cloned from ``ego.reconcile_config``: fresh-read-per-call, deep-merge over
hardcoded defaults, degrade field-by-field toward the defaults on config damage
so a reflection can never crash on a bad config.

The reflection bridge (``_bridge.py::_model_for_depth`` / ``_effort_for_context``)
and the prompt resolver (``_prompts.py``) both delegate here, so the depth→model
mapping has a single source of truth. Editable from the dashboard via the
``reflection_models`` settings domain.

Effort semantics: Haiku does not use an effort setting — the CC invoker OMITS
``--effort`` for Haiku (``model_supports_effort``). ``light`` therefore has no
``effort`` in the base yaml (the dashboard shows only its model). The internal
``_HARDCODED_DEFAULTS`` still resolves ``light`` to ``low`` so callers always get
a concrete ``EffortLevel`` — the value is a harmless no-op at dispatch for Haiku.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay +
genesis.cc.types + genesis.awareness.types only. ``genesis.mcp.health.settings``
imports the public helpers from here (never the reverse) — mirror reconcile_config.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.awareness.types import Depth
from genesis.cc.types import CCModel, EffortLevel
from genesis.env import repo_root

logger = logging.getLogger(__name__)

_CONFIG_NAME = "reflection_models.yaml"

# Authoritative fallbacks — match the historical hardcoded _DEPTH_MODEL /
# _effort_for_context, with the 2026-07-30 effort bump (deep/strategic → xhigh).
# ``light`` keeps ``effort: low`` here (not in the yaml) so the runtime value is
# concrete even though Haiku ignores it at dispatch.
_HARDCODED_DEFAULTS: dict[str, dict[str, str]] = {
    "light": {"model": "haiku", "effort": "low"},
    "deep": {"model": "sonnet", "effort": "xhigh"},
    "strategic": {"model": "opus", "effort": "xhigh"},
}

# Public: the settings-domain validator imports these to check keys/defaults.
VALID_DEPTH_KEYS: frozenset[str] = frozenset(_HARDCODED_DEFAULTS)


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults ← base yaml ← .local overlay), per depth, so a partial
    override (e.g. only ``deep.effort``) keeps the other fields from defaults.
    Missing or corrupt files degrade toward ``_HARDCODED_DEFAULTS``.
    """
    merged = copy.deepcopy(_HARDCODED_DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("reflection_models base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("reflection_models overlay merge failed", exc_info=True)
    for depth_key, fields in base.items():
        if not isinstance(fields, dict):
            continue
        if depth_key in merged:
            merged[depth_key] = {**merged[depth_key], **fields}
        else:
            merged[depth_key] = dict(fields)
    return merged


def _depth_key(depth: Depth) -> str:
    # Depth values are capitalized ("Light"/"Deep"/"Strategic"); the config uses
    # lowercase keys, so normalize.
    raw = depth.value if isinstance(depth, Depth) else str(depth)
    return raw.lower()


def model_for_depth(depth: Depth) -> CCModel:
    """Model for a reflection depth. Invalid/missing → hardcoded default → SONNET."""
    key = _depth_key(depth)
    raw = load_config().get(key, {}).get("model") or _HARDCODED_DEFAULTS.get(key, {}).get("model")
    try:
        return CCModel(raw) if raw else CCModel.SONNET
    except ValueError:
        logger.warning("reflection_models: invalid model %r for depth %s — using default", raw, key)
        fallback = _HARDCODED_DEFAULTS.get(key, {}).get("model", "sonnet")
        return CCModel(fallback)


def effort_for_depth(depth: Depth) -> EffortLevel | None:
    """Effort for a reflection depth, or ``None`` when none is configured.

    Invalid values degrade to the hardcoded default (or ``None``). Callers that
    need a concrete level should coalesce ``None`` themselves; the invoker omits
    ``--effort`` for effort-less models (Haiku) regardless of this value.
    """
    key = _depth_key(depth)
    raw = load_config().get(key, {}).get("effort")
    if raw is None:
        raw = _HARDCODED_DEFAULTS.get(key, {}).get("effort")
    if raw is None:
        return None
    try:
        return EffortLevel(raw)
    except ValueError:
        logger.warning(
            "reflection_models: invalid effort %r for depth %s — using default", raw, key
        )
        fallback = _HARDCODED_DEFAULTS.get(key, {}).get("effort")
        return EffortLevel(fallback) if fallback else None


def editor_view(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize a served reflection_models config for the dashboard editor.

    The generic settings editor only renders keys present in the served config, so
    an ``effort`` control appears for a depth only when the config carries an
    ``effort`` key. Expose ``effort`` for exactly the depths whose selected model
    supports it:

      * effort-capable model (Sonnet/Opus/Fable) with no ``effort`` key → inject
        the default effort so the control renders. Without this, switching a depth
        off Haiku silently applies the hidden hardcoded value with no way to edit
        it (Codex P2 on #1261).
      * effort-less model (Haiku) → drop any ``effort`` key so no misleading
        control shows; the invoker omits ``--effort`` for Haiku at dispatch.

    Pure view transform over a COPY — never mutates the stored config, so it is
    safe to call on the merged yaml the dashboard route serves.
    """
    from genesis.cc.types import CCModel, model_supports_effort

    view = copy.deepcopy(config)
    for key, fields in view.items():
        if not isinstance(fields, dict):
            continue
        model_raw = fields.get("model")
        try:
            supports = bool(model_raw) and model_supports_effort(CCModel(model_raw))
        except ValueError:
            supports = False
        if supports:
            if not fields.get("effort"):
                fields["effort"] = _HARDCODED_DEFAULTS.get(key, {}).get("effort") or "medium"
        else:
            fields.pop("effort", None)
    return view
