"""Model roster — first-class model-diversification policy layer.

Maps roster names (e.g. "glm-5.2") to the CCInvocation overrides that point a
Claude Code subprocess at a non-Anthropic provider's native Anthropic-compatible
endpoint. This is the POLICY layer: call sites (ConversationLoop,
DirectSessionRunner) resolve the active model here and apply the overrides to a
CCInvocation before handing it to the (dumb) CCInvoker. The invoker never selects.

Config: ``config/cc_roster.yaml`` (+ ``cc_roster.local.yaml`` overlay), the same
file backing the ``cc_roster`` settings domain. Auth tokens are resolved from the
process environment by the name in ``auth_env`` — never stored in config.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from genesis._config_overlay import merge_local_overlay

logger = logging.getLogger(__name__)

# roster.py lives at src/genesis/cc/roster.py → parents[3] is the repo root.
_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_ROSTER_FILE = "cc_roster.yaml"

#: The native-subscription default. Never carries routing overrides.
CLAUDE = "claude"


class RosterError(RuntimeError):
    """Roster resolution failure (unknown model, misconfig, or missing auth)."""


@dataclass(frozen=True)
class RosterEntry:
    name: str
    native_subscription: bool = False
    anthropic_base_url: str | None = None
    auth_env: str | None = None
    model_id: str | None = None
    failover_order: int = 0
    validated: str | None = None


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except Exception:
        logger.warning("Failed to read roster config %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        if data is not None:
            logger.warning("Roster config %s is not a mapping — ignoring", path)
        return {}
    return data


def load_roster(config_dir: Path | None = None) -> dict:
    """Return the merged roster config (base file + ``.local`` overlay).

    The overlay is resolved user-dir-first (``~/.genesis/config/``, where the
    ``cc_roster`` settings writer lands) via the shared ``merge_local_overlay``
    helper, so the settings domain actually controls the active model
    (cfg-001: loaders and writers must agree on overlay location).
    """
    cfg_dir = config_dir or _CONFIG_DIR
    base_path = cfg_dir / _ROSTER_FILE
    return merge_local_overlay(_load_yaml(base_path), base_path)


def _entry_from(name: str, raw: dict) -> RosterEntry:
    return RosterEntry(
        name=name,
        native_subscription=bool(raw.get("native_subscription", False)),
        anthropic_base_url=raw.get("anthropic_base_url"),
        auth_env=raw.get("auth_env"),
        model_id=raw.get("model_id"),
        failover_order=int(raw.get("failover_order", 0)),
        validated=str(raw["validated"]) if raw.get("validated") is not None else None,
    )


def active_model(roster: dict | None = None) -> str:
    """The configured active model name (``cc_roster.default``)."""
    r = roster if roster is not None else load_roster()
    return str(r.get("default", CLAUDE))


def resolve(name: str, roster: dict | None = None) -> RosterEntry | None:
    """Return the :class:`RosterEntry` for ``name``, or None if unknown."""
    r = roster if roster is not None else load_roster()
    raw = (r.get("models") or {}).get(name)
    return _entry_from(name, raw) if raw is not None else None


def _is_native(entry: RosterEntry) -> bool:
    return entry.native_subscription or entry.name == CLAUDE


def overrides_for(name: str, roster: dict | None = None) -> dict:
    """CCInvocation override kwargs for ``name``.

    Empty dict for Claude / native-subscription entries (no routing). Raises
    :class:`RosterError` if a non-native entry is misconfigured or its auth token
    is absent from the environment — callers convert this to a user-facing
    failure rather than silently running on the wrong model.
    """
    entry = resolve(name, roster)
    if entry is None:
        raise RosterError(f"unknown roster model {name!r}")
    if _is_native(entry):
        if entry.anthropic_base_url or entry.model_id or entry.auth_env:
            logger.warning(
                "roster model %r is native (subscription) but also defines "
                "routing fields — they are ignored; drop native_subscription "
                "to route it",
                name,
            )
        return {}
    if not (entry.anthropic_base_url and entry.model_id and entry.auth_env):
        raise RosterError(
            f"roster model {name!r} is missing base_url/model_id/auth_env"
        )
    token = os.environ.get(entry.auth_env)
    if not token:
        raise RosterError(
            f"roster model {name!r} auth env {entry.auth_env} is not set"
        )
    return {
        "anthropic_base_url": entry.anthropic_base_url,
        "anthropic_auth_token": token,
        "model_id_override": entry.model_id,
    }


def failover_chain(active: str, roster: dict | None = None) -> list[str]:
    """Ordered peer names to try when ``active`` is rate-limited/exhausted.

    Excludes ``active``; ordered by ``failover_order`` ascending. Only includes
    members that are actually usable — native (Claude) always, others only when
    their ``auth_env`` token is present (skips unconfigured providers so we never
    fail over to a model we can't authenticate).
    """
    r = roster if roster is not None else load_roster()
    peers: list[tuple[int, str]] = []
    for name, raw in (r.get("models") or {}).items():
        if name == active:
            continue
        entry = _entry_from(name, raw)
        if _is_native(entry) or (entry.auth_env and os.environ.get(entry.auth_env)):
            peers.append((entry.failover_order, name))
    peers.sort()
    return [name for _, name in peers]
