"""Model roster — first-class model-diversification policy layer.

Maps roster names (e.g. "glm-5.2") to the CCInvocation overrides that point a
Claude Code subprocess at a non-Anthropic provider's native Anthropic-compatible
endpoint. This is the POLICY layer.

SELECTION runs at the CCInvoker chokepoint: ``apply_active`` is called at the top
of ``CCInvoker.run``/``run_streaming`` and routes invocations that opt in via
``roster_eligible``. FAILOVER selection (``failover_chain`` / ``failover_invocations``)
builds peer invocations for the conversation layer's outage retry loop — selection
only; the failover ORCHESTRATION lives at the call site, never in the invoker.

Config: ``config/cc_roster.yaml`` (+ ``cc_roster.local.yaml`` overlay), the same
file backing the ``cc_roster`` settings domain. Auth tokens are resolved from the
process environment by the name in ``auth_env`` — never stored in config.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.cc.types import CCInvocation

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


def apply_active(
    inv: CCInvocation, roster: dict | None = None,
) -> tuple[CCInvocation, str]:
    """Resolve the active roster model and stamp its overrides onto ``inv``.

    This is the SELECTION chokepoint, called at the top of ``CCInvoker.run`` /
    ``run_streaming``. Returns ``(possibly_new_inv, roster_model_name)`` — the
    name is for honest span attribution.

    **Never raises** — it sits on the universal CC path, so any failure degrades
    to native Claude (logged) rather than breaking every invocation. No-ops for:
    non-opted-in invocations (``roster_eligible=False`` — the default), already-
    routed invocations (a resume reconstruction or failover pre-stamped the
    override fields), and bare resumes (never reroute an existing session onto a
    different endpoint — the resume-safety invariant; routed resumes are
    reconstructed by the call site before reaching here).
    """
    try:
        if not inv.roster_eligible:
            return inv, CLAUDE
        # ORDER IS LOAD-BEARING: the override-present check MUST precede the
        # resume check. A reconstructed routed resume (conversation._reconstruct_
        # _resume) arrives with BOTH override fields AND resume_session_id set; it
        # must be respected (not forced native). Do not reorder these two guards.
        if inv.model_id_override or inv.anthropic_base_url:
            return inv, (inv.model_id_override or "routed")
        if inv.resume_session_id is not None:
            return inv, CLAUDE
        active = active_model(roster)
        overrides = overrides_for(active, roster)
        if not overrides:
            return inv, active
        return replace(inv, **overrides), active
    except Exception:
        logger.error(
            "roster apply_active failed — running native Claude", exc_info=True,
        )
        return inv, CLAUDE


def endpoint_payload(name: str, roster: dict | None = None) -> dict | None:
    """Persistable endpoint context for roster model ``name`` (NEVER the token).

    Stored with a routed session so it can be resumed on the SAME endpoint.
    Returns ``None`` for native/Claude or a misconfigured entry (nothing to
    persist → the session resumes native, which is correct).
    """
    entry = resolve(name, roster)
    if entry is None or _is_native(entry):
        return None
    if not (entry.anthropic_base_url and entry.model_id and entry.auth_env):
        return None
    return {
        "base_url": entry.anthropic_base_url,
        "auth_env": entry.auth_env,  # NAME only — token resolved at resume time
        "model_id": entry.model_id,
        "roster_model": name,
    }


def overrides_from_persisted(payload: dict) -> dict:
    """Rebuild CCInvocation override kwargs from a persisted endpoint payload.

    Token is re-read from ``os.environ[auth_env]`` (never stored). Raises
    :class:`RosterError` if the payload is incomplete or the token is now absent
    — callers must surface this rather than silently resume on the wrong model.
    """
    base_url = payload.get("base_url")
    auth_env = payload.get("auth_env")
    model_id = payload.get("model_id")
    if not (base_url and auth_env and model_id):
        raise RosterError(f"incomplete persisted endpoint payload: {payload!r}")
    token = os.environ.get(auth_env)
    if not token:
        raise RosterError(f"persisted endpoint auth env {auth_env} is not set")
    return {
        "anthropic_base_url": base_url,
        "anthropic_auth_token": token,
        "model_id_override": model_id,
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


def failover_invocations(
    active: str, base_inv: CCInvocation, roster: dict | None = None,
) -> list[tuple[str, CCInvocation]]:
    """FRESH peer invocations to try, in order, when ``active`` is unavailable.

    For each usable peer in :func:`failover_chain`, returns ``(peer_name,
    peer_inv)`` where ``peer_inv`` is ``base_inv`` re-pointed at that peer:
    a fresh session (``resume_session_id=None`` — a CC session can't resume across
    providers), with the peer's routing overrides stamped on. The caller owns the
    retry loop (selection-only here; failover ORCHESTRATION must not live in the
    invoker).

    ``roster_eligible`` is set to ``bool(overrides)`` — LOAD-BEARING:
    - A ROUTED peer (overrides present) keeps ``roster_eligible=True`` so the
      chokepoint's override-present guard honors the pre-stamped endpoint AND
      reports the correct model name (``apply_active`` returns CLAUDE for any
      ``roster_eligible=False`` invocation, which would mis-attribute a routed run).
    - A NATIVE peer (empty overrides, e.g. Claude when default=glm) gets
      ``roster_eligible=False`` so the chokepoint does NOT re-select the global
      default and loop straight back to the model that just failed.

    Misconfigured peers (``overrides_for`` raises) are skipped, not fatal.
    """
    r = roster if roster is not None else load_roster()
    out: list[tuple[str, CCInvocation]] = []
    for name in failover_chain(active, r):
        try:
            overrides = overrides_for(name, r)
        except RosterError:
            logger.warning("failover peer %r unusable — skipping", name, exc_info=True)
            continue
        # Always set ALL routing fields so a native peer (empty overrides) CLEARS
        # any routing the base invocation carried (e.g. a routed resume when
        # default=glm failing over to native Claude) instead of leaking it through.
        routing: dict = {
            "anthropic_base_url": None,
            "anthropic_auth_token": None,
            "model_id_override": None,
        }
        routing.update(overrides)
        peer_inv = replace(
            base_inv,
            resume_session_id=None,
            roster_eligible=bool(overrides),
            **routing,
        )
        out.append((name, peer_inv))
    return out
