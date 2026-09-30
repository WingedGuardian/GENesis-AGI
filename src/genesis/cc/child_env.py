"""Environment pins every Genesis-dispatched Claude Code child carries.

A dispatched session (CCInvoker, the headless judge, the experimentation
router) builds its env from ``os.environ`` of whatever started the server.
Anything in that env that changes how Claude Code behaves reaches the child
unless it is pinned here.

``CLAUDE_CODE_ENABLE_FUNCTION_HOOKS`` is pinned to ``0``. Claude Code resolves
the flag as the env var if set, otherwise a server-side feature flag that is
off today (read in the 2.1.280 bundle). Removing the var would leave dispatched
sessions to that server default, so a rollout would turn plugin JS modules on
in every background session with such a plugin enabled. ``0`` was measured to
keep modules off. Interactive slots opt in separately through the cc-slot
lever (``scripts/cc-slot.sh``).
"""

from __future__ import annotations

from collections.abc import MutableMapping

FUNCTION_HOOKS_ENV = "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"


def pin_dispatched_env(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Apply the dispatched-session pins to ``env`` in place and return it."""
    env[FUNCTION_HOOKS_ENV] = "0"
    return env
