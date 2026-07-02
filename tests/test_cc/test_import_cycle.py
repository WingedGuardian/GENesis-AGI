"""Regression guard: genesis.cc.contingency <-> genesis.awareness.loop import cycle.

A layering inversion (awareness.loop importing a CC-domain constant from
cc.contingency, combined with an eager awareness/__init__ that pulled in
awareness.loop) once made ``import genesis.cc.contingency`` fail with an
ImportError ("cannot import name 'RATE_LIMIT_DEFERRAL_TTL_S' from partially
initialized module") under certain import orders. The full test suite masked it
because an earlier test imported awareness.loop first.

These tests import the modules in a FRESH interpreter so that sys.modules caching
cannot hide a re-introduced cycle, and assert both import orders succeed.
"""

from __future__ import annotations

import subprocess
import sys


def _import_in_fresh_process(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )


def test_import_contingency_first_no_cycle() -> None:
    # This exact order used to raise the partially-initialized-module ImportError.
    proc = _import_in_fresh_process(
        "import genesis.cc.contingency; import genesis.awareness.loop"
    )
    assert proc.returncode == 0, proc.stderr


def test_import_awareness_loop_first_no_cycle() -> None:
    proc = _import_in_fresh_process(
        "import genesis.awareness.loop; import genesis.cc.contingency"
    )
    assert proc.returncode == 0, proc.stderr


def test_awareness_lazy_public_api_preserved() -> None:
    # De-eagering awareness/__init__ (PEP 562 __getattr__) must not drop the
    # documented package-level re-exports.
    proc = _import_in_fresh_process(
        "from genesis.awareness import "
        "AwarenessLoop, Depth, JobRetryRegistry, SignalReading, TickResult"
    )
    assert proc.returncode == 0, proc.stderr


def test_rate_limit_ttl_constant_single_source() -> None:
    from genesis.cc.constants import RATE_LIMIT_DEFERRAL_TTL_S as from_constants
    from genesis.cc.contingency import RATE_LIMIT_DEFERRAL_TTL_S as from_contingency

    assert from_constants == from_contingency == 14400
