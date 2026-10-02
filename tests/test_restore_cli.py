from __future__ import annotations

import argparse
from types import SimpleNamespace

from genesis.restore import cli


def test_database_only_flag_is_forwarded(monkeypatch):
    seen = {}

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    args = argparse.Namespace(
        from_=None,
        dry_run=False,
        force=False,
        database_only=True,
    )

    assert cli.run(args) == 0
    assert seen["cmd"][-1] == "--database-only"
