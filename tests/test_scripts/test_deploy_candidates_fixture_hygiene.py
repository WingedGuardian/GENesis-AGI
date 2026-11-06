"""Loading the engine in-process must leave the interpreter's import state as it
found it.

`scripts/deploy_candidates.py` strips its own directory from `sys.path` at import
and puts a sibling finder on `sys.meta_path`. That is right for the engine, which
runs as its own process. But the `dc` fixture executes it inside the test process,
so without a restore every later test in the same process lost `scripts/` from
its path. A serial run hid this, because the test files that lazily import a
`scripts/` sibling ran first. Under `pytest -n` the scheduler sends the largest
files first, and three review tests then failed with ModuleNotFoundError.
"""

from __future__ import annotations

import sys

import pytest

from tests.test_scripts._deploy_candidates_world import SCRIPTS, load_engine


def test_loading_the_engine_restores_import_state_on_undo(monkeypatch):
    # The path the engine is loaded into must hold scripts/, or the strip below
    # would prove nothing. Added through monkeypatch, so it is removed again.
    if str(SCRIPTS) not in sys.path:
        monkeypatch.syspath_prepend(str(SCRIPTS))
    path_before = list(sys.path)
    meta_path_before = list(sys.meta_path)

    with pytest.MonkeyPatch.context() as mp:
        load_engine(mp)
        # The strip is real while the engine is loaded, so the restore is a
        # check rather than a no-op.
        assert str(SCRIPTS) not in sys.path
        assert len(sys.meta_path) == len(meta_path_before) + 1

    assert sys.path == path_before
    assert sys.meta_path == meta_path_before
