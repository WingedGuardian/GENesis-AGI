"""Settings-surface view of the worktree ownership lever.

The lever itself lives in ``scripts/hooks/worktree_claim.py``, not here, and this
module deliberately re-exports from there rather than restating it. That module
has to be stdlib-only -- it is imported by a PreToolUse hook on the latency path
and by a daily sweeper that may run under the system interpreter when the venv is
absent -- so it cannot import ``genesis``. Defining ``MODES`` and the defaults a
second time on this side would give the settings validator its own copy to drift
away from the one the mechanism actually obeys, and a validator that accepts a
mode the mechanism rejects is worse than no validator.

Loaded by explicit spec rather than by mutating ``sys.path``, so importing this
module cannot change how anything else in the process resolves imports.
"""

from __future__ import annotations

import importlib.util
import logging
from types import ModuleType

from genesis.env import repo_root

logger = logging.getLogger(__name__)

_MODULE_NAME = "genesis._worktree_claim_lever"

# Mirrors scripts/hooks/worktree_claim.py. Used only if that module cannot be
# loaded at all (a source checkout missing scripts/), so that the settings
# surface degrades to describing the lever instead of failing to import.
_FALLBACK_MODES: tuple[str, ...] = ("off", "advisory")
_FALLBACK_DEFAULTS: dict[str, object] = {"enabled": True, "mode": "advisory"}


def _load() -> ModuleType | None:
    path = repo_root() / "scripts" / "hooks" / "worktree_claim.py"
    try:
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        logger.warning("worktree ownership lever not loadable at %s", path, exc_info=True)
        return None


_LEVER = _load()

MODES: tuple[str, ...] = tuple(getattr(_LEVER, "MODES", _FALLBACK_MODES))
DEFAULTS: dict = dict(getattr(_LEVER, "DEFAULTS", _FALLBACK_DEFAULTS))


def load_config() -> dict:
    """The merged config, read fresh per call."""
    if _LEVER is None:
        return dict(_FALLBACK_DEFAULTS)
    return _LEVER.load_config()


def effective_mode() -> str:
    """The mode the mechanism runs under, read live."""
    if _LEVER is None:
        return "advisory"
    return _LEVER.effective_mode()
