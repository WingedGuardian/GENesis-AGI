"""Engine availability: the package imports and the harness skips contenders
cleanly when their libs are absent (prod venv / CI) — never errors on collection."""

from __future__ import annotations

import importlib.util

from genesis.eval.graph_bakeoff.engines.falkor import FalkorEngine
from genesis.eval.graph_bakeoff.engines.ladybug import LadybugEngine
from genesis.eval.graph_bakeoff.engines.nx_incremental import NxIncrementalEngine


def test_control_available_contenders_skip_in_prod_venv():
    # The control runs in-process (networkx is a prod dep).
    assert NxIncrementalEngine.available() is True
    # Ladybug lives in the throwaway venv only -> absent here, reported (not raised).
    assert LadybugEngine.available() is False
    # falkordblite is a Genesis dependency (it ships the graph engine), so its
    # package is present wherever that wheel installed: the probe must say so.
    assert FalkorEngine.available() is (importlib.util.find_spec("redislite") is not None)


def test_available_never_raises():
    # available() must be a pure find_spec probe — safe to call anywhere.
    for eng in (NxIncrementalEngine, LadybugEngine, FalkorEngine):
        assert isinstance(eng.available(), bool)


def test_stub_stats_report_smoke_status():
    assert LadybugEngine().stats()["status"] == "S1-stub"
    assert FalkorEngine().stats()["status"] == "S1-stub"
