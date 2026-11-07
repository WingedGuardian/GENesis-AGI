"""Compatibility facade for the shared stdlib E2E declaration parser."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from genesis.session_awareness import e2e_declaration as _shared  # noqa: E402
from genesis.session_awareness.e2e_declaration import (  # noqa: E402, F401
    E2E_CUTOFF_ISO,
    GUIDANCE,
    MARKER,
    e2e_steps,
    is_pre_cutoff,
    parse_e2e,
)


def __getattr__(name):
    # Existing script consumers also inspect scanner constants/private helpers.
    return getattr(_shared, name)


def _load_sibling_readable_body():
    """``readable_body`` from ``check_cc_pin_receipts.py`` — the SAME scanner the pin
    gate uses, so the two body-readers can never disagree about what is visible.

    Falls back to the local scanner below if that module cannot be loaded (it carries
    imports this one deliberately does not need). Degrading the STRIPPING is safe in
    the direction that matters: the local copy implements the same rules."""
    import sys

    name = "_cc_pin_receipts_for_e2e"
    try:
        path = Path(__file__).resolve().parent / "check_cc_pin_receipts.py"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            # REGISTERED BEFORE exec: that module's dataclasses resolve their own
            # module out of sys.modules, so exec-ing an unregistered module raises
            # `AttributeError: 'NoneType' object has no attribute '__dict__'`.
            # Without this line the load ALWAYS failed and the local fallback
            # silently ran instead — so the docstring's "the SAME scanner the pin
            # gate uses, so the two can never disagree" was false in every run
            # (Kimi P2, 2026-09-06). The guard's own _load_pin_receipt_checker
            # carries this line and says why; I read that comment and did not copy
            # it. On failure the half-initialised entry is removed so a later
            # import of the real module is not poisoned.
            sys.modules[name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop(name, None)
                raise
            fn = getattr(mod, "readable_body", None)
            if callable(fn):
                return fn
    except Exception:
        pass
    return None


def readable_body(body: str, *, keep_blank: bool = False) -> str:
    """Preserve the script API's optional pin-scanner integration."""
    fn = _load_sibling_readable_body()
    if fn:
        return fn(body[:_shared._MAX_BODY], keep_blank=keep_blank)
    return _shared.readable_body(body, keep_blank=keep_blank)
