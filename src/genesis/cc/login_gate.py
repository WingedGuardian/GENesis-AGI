"""Interactive-slot OAuth decision gate — `python -m genesis.cc.login_gate`.

`scripts/cc-slot.sh` runs this on slot CREATE to decide whether the pane should
inject the stored 1-year setup-token (``CLAUDE_CODE_OAUTH_TOKEN``) so an
interactive session survives a dead ``/login`` without a re-login prompt.

Contract: exit 0 = inject, exit 1 = do NOT inject. On exit 0 the gate prints the
human-facing notice (which the pane echoes to stderr) to STDOUT — and NOTHING
sensitive (never the token). Fails CLOSED — any error → exit 1 (the slot keeps
its normal login behavior; a dead login just prompts ``/login`` as today).

This gate is the SINGLE authority for the whole slot-fallback decision, and a
faithful mirror of ``CCInvoker._apply_login_fallback`` (invoker.py). Beyond the
shared ``login_health.fallback_env_if_login_dead`` gate it also enforces:

- **Fail-closed lever**: only ``conditional`` / ``always`` / ``off`` inject as
  documented; any other value (a typo like ``of``) is rejected, not silently
  treated as ``conditional``.
- **Competing-auth exclusion**: never inject when another auth mechanism is in
  play — a peer-route (``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN``), an API
  key (``ANTHROPIC_API_KEY``), an already-present ``CLAUDE_CODE_OAUTH_TOKEN``, or
  a custom ``CLAUDE_CONFIG_DIR``. This preserves the invoker's credential-
  isolation contract (the Anthropic setup-token must never travel toward a
  third-party endpoint), for BOTH ``conditional`` and ``always``.
- **Stale-token exclusion**: a setup-token past its ~1-year life is treated as
  absent (shared ``login_health.fallback_token_is_stale``), for both modes.

Lever ``GENESIS_CC_SLOT_OAUTH``:
- ``conditional`` (default): inject ONLY when the interactive login is
  hard-expired AND a live ``claude auth status`` probe confirms logged-out.
  Preserves Remote Control + claude.ai connectors while the login is alive.
- ``always``: inject whenever a (fresh) setup-token exists, bypassing the login
  gate. Overrides even a live login (loses connectors until the lever is set
  back to ``conditional`` and the slot restarts).
- ``off``: never inject (kill switch / per-slot opt-out).

The 60s probe cache in ``login_health`` is per-process, so it does NOT amortize
across separate slot launches — each launch is a fresh process. That is fine:
the cheap ``credentials.json`` timestamp gate short-circuits before the probe on
a healthy login, so the common case never spawns a subprocess.
"""

from __future__ import annotations

import asyncio
import os
import sys

_VALID_MODES = ("conditional", "always", "off")

# Env markers that mean "another auth mechanism owns this session" — the exact
# set CCInvoker._apply_login_fallback excludes (invoker.py). Full parity: never
# cross the Anthropic setup-token into a peer-routed (ANTHROPIC_BASE_URL/
# AUTH_TOKEN) or alternately-authed (ANTHROPIC_API_KEY) process, never clobber an
# already-present CLAUDE_CODE_OAUTH_TOKEN, and never inject over a custom
# CLAUDE_CONFIG_DIR. The last matters because read_fallback_token() reads ONE
# fixed, global token (~/.genesis/cc_oauth_token.env) — NOT config-dir-scoped —
# so a slot pointed at a different Claude identity via CLAUDE_CONFIG_DIR must not
# be silently re-authed as the primary operator. Normal slots never set it, so
# this only excludes deliberately custom-config-dir slots (same tradeoff the
# invoker accepts).
_COMPETING_AUTH_ENV = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
)

_NOTICE_CONDITIONAL = (
    "Genesis: primary CC login is dead — running on the stored setup-token. "
    "Remote Control and claude.ai connectors are OFF; run /login, then restart "
    "this slot to use them (the setup-token overrides /login in this session). "
    "Local MCP servers still work."
)
_NOTICE_ALWAYS = (
    "Genesis: forced onto the setup-token (GENESIS_CC_SLOT_OAUTH=always) — "
    "Remote Control and claude.ai connectors are OFF; set the lever to "
    "conditional and restart this slot to use /login. Local MCP servers still work."
)


def _mode() -> str:
    return (os.environ.get("GENESIS_CC_SLOT_OAUTH") or "conditional").strip().lower()


def _competing_auth() -> str | None:
    """Name of a competing-auth env var that is set (non-empty), else None."""
    for name in _COMPETING_AUTH_ENV:
        if os.environ.get(name):
            return name
    return None


async def _decide() -> str | None:
    """Return the notice to show if this slot should inject, else None."""
    # Imported lazily so the module stays cheap to import and the gate's only
    # heavy dependency (login_health → asyncio/subprocess) loads on demand.
    from genesis.cc import login_health

    mode = _mode()
    if mode == "off":
        return None
    if mode not in _VALID_MODES:
        # Do NOT echo the raw lever value — CodeQL's clear-text-logging taint
        # rule flags ANY env-var value reaching a log as sensitive (false
        # positive here: the lever is conditional/always/off, never a secret),
        # and the value adds nothing the allowed-set line doesn't.
        print(
            "Genesis: unknown GENESIS_CC_SLOT_OAUTH value — not injecting "
            "(allowed: conditional, always, off).",
            file=sys.stderr,
            flush=True,
        )
        return None

    # Never inject over a competing auth mechanism (credential-isolation parity
    # with the invoker) — applies to BOTH conditional and always.
    competing = _competing_auth()
    if competing:
        print(
            f"Genesis: {competing} is set — leaving auth to it, not injecting the setup-token.",
            file=sys.stderr,
            flush=True,
        )
        return None

    if mode == "always":
        token = login_health.read_fallback_token()
        if token is None:
            print(
                "Genesis: GENESIS_CC_SLOT_OAUTH=always but no setup-token is "
                "stored (~/.genesis/cc_oauth_token.env) — starting on the normal "
                "login. Provision it: `claude setup-token` + scripts/store_cc_token.sh.",
                file=sys.stderr,
                flush=True,
            )
            return None
        if login_health.fallback_token_is_stale():
            print(
                "Genesis: GENESIS_CC_SLOT_OAUTH=always but the stored setup-token "
                "is past its ~1-year life — not injecting a known-dead credential. "
                "Refresh it: `claude setup-token` + scripts/store_cc_token.sh.",
                file=sys.stderr,
                flush=True,
            )
            return None
        return _NOTICE_ALWAYS

    # conditional (default): reuse the shared login-dead gate. Wrap the probe so
    # the ONE case where it actually runs (login already hard-dead) emits a
    # progress line — a healthy login short-circuits before the probe and stays
    # silent, so a new slot never hangs mysteriously while auth is checked.
    cc_path = os.environ.get("GENESIS_CC_BIN") or "claude"

    async def probe() -> bool:
        print(
            "Genesis: primary CC login expired — checking auth status…",
            file=sys.stderr,
            flush=True,
        )
        return await login_health.probe_logged_out(cc_path)

    env = await login_health.fallback_env_if_login_dead(probe=probe, cc_path=cc_path)
    return _NOTICE_CONDITIONAL if env is not None else None


def main() -> int:
    try:
        notice = asyncio.run(_decide())
    except Exception:  # noqa: BLE001 — fail-closed: never inject on error
        return 1
    if notice is None:
        return 1
    # STDOUT carries ONLY the human notice (never the token); cc-slot.sh captures
    # it and the pane echoes it to stderr.
    print(notice)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
