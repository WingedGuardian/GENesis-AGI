"""Interactive-slot OAuth decision gate — `python -m genesis.cc.login_gate`.

`scripts/cc-slot.sh` runs this on slot CREATE to decide whether the pane should
inject the stored 1-year setup-token (``CLAUDE_CODE_OAUTH_TOKEN``) so an
interactive session survives a dead ``/login`` without a re-login prompt.

Contract: exit 0 = inject, exit 1 = do NOT inject. Prints NOTHING sensitive
(never the token). Fails CLOSED — any error → exit 1 (the slot keeps its normal
login behavior; a dead login just prompts ``/login`` as it does today).

Decision authority is SHARED, not re-implemented: the ``conditional`` mode calls
``login_health.fallback_env_if_login_dead`` — the same gate CCInvoker uses
(invoker.py) — so the container never grows two divergent notions of "login is
dead" (per the leaky per-consumer-classification lesson).

Lever ``GENESIS_CC_SLOT_OAUTH``:
- ``conditional`` (default): inject ONLY when the interactive login is
  hard-expired AND a live ``claude auth status`` probe confirms logged-out.
  Preserves Remote Control + claude.ai connectors while the login is alive
  (the env var is simply absent then, so CC uses ``/login``).
- ``always``: inject whenever a setup-token exists, bypassing the login gate.
  Overrides even a live login (loses connectors until the lever is set back to
  ``conditional`` and the slot restarts).
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


def _mode() -> str:
    return (os.environ.get("GENESIS_CC_SLOT_OAUTH") or "conditional").strip().lower()


async def _should_inject() -> bool:
    """True iff this slot should inject the setup-token, per the lever."""
    # Imported lazily so the module stays cheap to import and the gate's only
    # heavy dependency (login_health → asyncio/subprocess) loads on demand.
    from genesis.cc import login_health

    mode = _mode()
    if mode == "off":
        return False

    if mode == "always":
        return login_health.read_fallback_token() is not None

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
    return env is not None


def main() -> int:
    try:
        return 0 if asyncio.run(_should_inject()) else 1
    except Exception:  # noqa: BLE001 — fail-closed: never inject on error
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
