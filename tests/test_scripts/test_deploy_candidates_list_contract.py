"""The engine's `list` output is a contract: deploy_health counts candidates from
it (it may not read the deploy manifest itself, by the reader policy in
test_deploy_candidates_manifest.py), so each shape `list` prints must parse to
the right count, and nothing else may parse as zero."""

from __future__ import annotations

import importlib

dh = importlib.import_module("genesis.observability.snapshots.deploy_health")


def test_no_manifest_parses_as_zero(dc, dc_ready, capsys):
    capsys.readouterr()
    assert dc_ready.run(dc, "list") == 0
    assert dh._listed_candidates((0, capsys.readouterr().out)) == 0


def test_listed_candidates_parse_to_their_count(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"list-a.txt": "a\n"})
    w.candidate("feat/b", {"list-b.txt": "b\n"})
    assert w.add(dc, "feat/a") == 0
    assert w.add(dc, "feat/b") == 0
    capsys.readouterr()
    assert w.run(dc, "list") == 0
    assert dh._listed_candidates((0, capsys.readouterr().out)) == 2
    # Emptied by drops, the manifest stays on disk listing nothing.
    assert w.run(dc, "drop", "feat/a") == 0
    assert w.run(dc, "drop", "feat/b") == 0
    assert w.manifest_path.exists()
    capsys.readouterr()
    assert w.run(dc, "list") == 0
    assert dh._listed_candidates((0, capsys.readouterr().out)) == 0


def test_a_refused_list_is_unknown(dc, dc_ready, capsys):
    w = dc_ready
    w.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    w.manifest_path.write_text("{not json")
    capsys.readouterr()
    rc = w.run(dc, "list")
    assert rc != 0
    assert dh._listed_candidates((rc, capsys.readouterr().out)) is None
