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


def test_refresh_snapshot_flag_parses_and_is_forwarded(monkeypatch):
    seen = {}

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["restore", "--refresh-snapshot", "--force"])
    assert args.func(args) == 7
    assert "--refresh-snapshot" in seen["cmd"]
    assert "--force" in seen["cmd"]


def test_transcript_preferences_parse_and_forward_verbatim(monkeypatch):
    seen = {}
    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers(dest="command"))
    preferences = ["-project/a.jsonl=legacy", "p/agent.jsonl=v2",
                   "p/plain.jsonl=legacy-plain", "p/encrypted.jsonl=legacy-encrypted"]
    args = parser.parse_args(["restore", *("--transcript-preference=" + p for p in preferences)])
    assert args.func(args) == 0
    assert seen["cmd"][-8:] == [value for preference in preferences for value in ("--transcript-preference", preference)]
