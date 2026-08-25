"""Inbox-suite fixtures.

Counter-store isolation: the response writer persists its numbering
high-water marks OUTSIDE the watch path (``~/.genesis/state/``) so that a
vault-sync wipe of the watch directory cannot reset numbering (the 2026-07-29
incident). Every test that writes a response would otherwise touch the REAL
install's counter store — same durable-state class as ``_isolate_alert_queue``
in the top-level conftest, so isolate it for the whole inbox suite.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_counter_store(tmp_path):
    """Redirect the writer's counter store to tmp for ALL inbox tests.

    Fixture-owned MonkeyPatch (not the shared ``monkeypatch`` fixture) so a
    test calling ``monkeypatch.undo()`` mid-body cannot re-expose the real
    ``~/.genesis/state/inbox-counters.json`` — mirrors ``_isolate_alert_queue``.
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(
        "genesis.inbox.writer._counter_store_path",
        lambda: tmp_path / "state" / "inbox-counters.json",
    )
    yield
    mp.undo()
