"""Shared fixtures for outreach tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_quiet_hours(monkeypatch):
    """Pin the governance quiet-hours check OFF so outreach tests are
    deterministic.

    ``GovernanceGate._in_quiet_hours`` reads the real wall clock
    (``datetime.now(user_timezone())``), so any test that exercises the ALLOW
    path flakes when CI happens to run inside a configured quiet window. The
    previous workaround — a 1-minute ``23:58-23:59`` "disable" window — only
    moved the landmine (it tripped whenever CI ran in that minute). Patching the
    check itself removes the wall-clock dependency entirely. Tests that assert
    quiet-hours behaviour re-patch ``_in_quiet_hours`` to ``True`` explicitly.
    """
    monkeypatch.setattr(
        "genesis.outreach.governance.GovernanceGate._in_quiet_hours",
        lambda self: False,
    )
