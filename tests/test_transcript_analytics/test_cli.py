import json
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.transcript_analytics import cli, config


def test_disabled_and_help_never_import_analytics_dependencies(tmp_path):
    # Import blocker is stronger than having optional libraries installed locally.
    source = str(Path(__file__).resolve().parents[2] / "src")
    code = """
import sys
sys.path.insert(0, sys.argv[1])
class Block:
 def find_spec(self, fullname, *args):
  if fullname.split('.')[0] in ('duckdb', 'pyarrow'): raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from genesis.transcript_analytics import cli, config
config.load = lambda: config.Config()
assert cli.main(['status']) == 0
try: cli.main(['--help'])
except SystemExit as e: assert e.code == 0
"""
    result = subprocess.run([sys.executable, "-c", code, source], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_disabled_collection_requires_optin(monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda: config.Config())
    assert cli.main(["ingest"]) == 2
    assert json.loads(capsys.readouterr().out)["enabled"] is False
    assert cli.main(["ingest", "--timer"]) == 0


def test_invalid_config_does_not_collect(monkeypatch):
    def bad():
        raise ValueError("synthetic invalid configuration")

    monkeypatch.setattr(config, "load", bad)
    assert cli.main(["ingest"]) == 2


@pytest.mark.parametrize("missing", [("duckdb",), ("pyarrow",), ("duckdb", "pyarrow"), ()])
def test_dependency_guidance_matches_actual_extra_installation(monkeypatch, capsys, tmp_path, missing):
    import importlib.util
    from unittest.mock import Mock

    from genesis.transcript_analytics import resources

    monkeypatch.setattr(config, "load", lambda: config.Config(enabled=True, data_dir=tmp_path))
    real_find = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name in missing else real_find(name))
    admission = Mock(return_value=75)
    monkeypatch.setattr(resources, "ensure_capped", admission)
    result = cli.main(["ingest"])
    if missing:
        assert result == 2
        admission.assert_not_called()
        report = json.loads(capsys.readouterr().out)
        assert report["unavailable"] == list(missing)
        assert "transcript-analytics" in report["action"]
        assert "docs/reference/transcript-analytics.md" in report["action"]
        assert "bootstrap" not in report["action"]
    else:
        assert result == 75
        admission.assert_called_once()


def test_ingest_returns_deferred_when_snapshot_lock_busy(tmp_path, monkeypatch, capsys):
    import argparse

    from genesis.transcript_analytics import derive, store

    monkeypatch.setattr(store, "ingest", lambda *a, **kw: {"failed": 0})
    monkeypatch.setattr(derive, "is_current", lambda *a: False)

    def busy(*args, **kwargs):
        assert kwargs == {"if_stale": True}
        raise store.Busy("busy")

    monkeypatch.setattr(derive, "build", busy)
    args = argparse.Namespace(
        timer=False, projects=tmp_path, data=tmp_path, since=None, no_derive=False
    )
    assert cli.cmd_ingest(args) == 75
    assert json.loads(capsys.readouterr().out)["derived"] == "skipped: lock busy"


def test_ingest_root_adoption_is_explicit(tmp_path, monkeypatch, capsys):
    import argparse

    from genesis.transcript_analytics import store

    calls = []

    def ingest(*a, **kw):
        calls.append(kw)
        return {"failed": 0}

    monkeypatch.setattr(store, "ingest", ingest)
    args = argparse.Namespace(
        timer=False,
        projects=tmp_path,
        data=tmp_path,
        since=None,
        no_derive=True,
        adopt_projects_root=True,
    )
    assert cli.cmd_ingest(args) == 0
    assert calls == [{"since_days": None, "adopt_projects_root": True}]


def test_status_tolerates_corrupt_marker_stats_and_counts_snapshots(tmp_path, monkeypatch, capsys):
    import argparse

    import pyarrow as pa
    import pyarrow.parquet as pq

    from genesis.transcript_analytics import query, store

    data = tmp_path / "store"
    data.mkdir()
    bad = data / f"{store.MARKER}__{'a' * 16}.parquet"
    bad.write_bytes(b"not parquet")
    stats = data / f"{store.MARKER}__{'b' * 16}.parquet"
    table = pa.table({"value": [1]}).replace_schema_metadata({b"ta.stats": b'{"malformed":"bad"}'})
    pq.write_table(table, stats)
    (data / "derived" / "old").mkdir(parents=True)
    snapshot = data / "derived" / "old" / "snapshot.parquet"
    snapshot.write_bytes(b"x" * 2**20)
    (data / "derived" / "current").symlink_to("old", target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large.parquet").write_bytes(b"x" * 2**20)
    (data / "linked").symlink_to(outside, target_is_directory=True)
    (data / "file.parquet").symlink_to(outside / "large.parquet")
    monkeypatch.setattr(store, "discover", lambda *a: [])
    monkeypatch.setattr(store, "compatible_sources", lambda *a: ([], [{"reason": "corrupt"}]))
    monkeypatch.setattr(query, "snapshot_manifest", lambda *a: {})
    monkeypatch.setattr(query, "snapshot_compatible", lambda *a: False)
    monkeypatch.setattr(query, "derived_current", lambda *a: False)
    assert cli.cmd_status(argparse.Namespace(data=data, projects=tmp_path)) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["marker_errors"]) == 2
    assert out["malformed_lines"] == 0
    assert out["snapshot_store_mb"] == 1.0
    assert out["store_mb"] == 1.0
    assert out["coverage"]["excluded"]


def test_status_does_not_descend_into_symlink_data_root(tmp_path, monkeypatch, capsys):
    import argparse

    from genesis.transcript_analytics import query, store

    outside = tmp_path / "outside"
    (outside / "derived" / "old").mkdir(parents=True)
    (outside / "derived" / "old" / "view.parquet").write_bytes(b"x" * 2**20)
    data = tmp_path / "linked"
    data.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(store, "discover", lambda *a: [])
    monkeypatch.setattr(store, "compatible_sources", lambda *a: ([], []))
    monkeypatch.setattr(query, "snapshot_manifest", lambda *a: {})
    monkeypatch.setattr(query, "snapshot_compatible", lambda *a: False)
    monkeypatch.setattr(query, "derived_current", lambda *a: False)
    cli.cmd_status(argparse.Namespace(data=data, projects=tmp_path))
    out = json.loads(capsys.readouterr().out)
    assert out["store_mb"] == out["source_store_mb"] == out["snapshot_store_mb"] == 0
    assert out["size_errors"] == ["data directory is a symlink"]


def test_status_classifies_source_and_snapshot_bytes_in_one_walk(tmp_path, monkeypatch):
    source = tmp_path / "fragments__x.parquet"
    source.write_bytes(b"source")
    (tmp_path / "derived" / "old").mkdir(parents=True)
    (tmp_path / "derived" / "old" / "turns.parquet").write_bytes(b"snapshot")
    walks = []
    original = cli.os.walk

    def once(*args, **kwargs):
        walks.append(args)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(cli.os, "walk", once)
    assert cli._parquet_bytes(tmp_path) == (6, 8, [])
    assert len(walks) == 1


@pytest.mark.parametrize("window", ["0", "-1", "nan", "inf", "-inf"])
def test_ingestion_window_parser_rejects_invalid_values(window):
    import argparse

    parser = argparse.ArgumentParser()
    cli._configure(parser)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["ingest", "--since", window])
    assert exc.value.code == 2


@pytest.mark.parametrize("window", [0, -1, float("nan"), float("inf"), True])
def test_direct_ingest_handler_rejects_window_before_store(window, tmp_path, monkeypatch):
    import argparse

    from genesis.transcript_analytics import store

    calls = []
    monkeypatch.setattr(store, "ingest", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(argparse.ArgumentTypeError):
        cli.cmd_ingest(argparse.Namespace(since=window))
    assert not calls


def test_status_reports_unreadable_source_fingerprint(tmp_path, monkeypatch, capsys):
    import argparse

    from genesis.transcript_analytics import query, store

    source = tmp_path / "session.jsonl"
    source.write_text("{}\n")
    monkeypatch.setattr(store, "discover", lambda *args: [(source, source.name)])

    def unavailable(*args):
        raise ValueError("agent metadata is a symlink")

    monkeypatch.setattr(store, "source_fingerprint", unavailable)
    monkeypatch.setattr(store, "compatible_sources", lambda *args: ([], []))
    monkeypatch.setattr(query, "snapshot_manifest", lambda *args: {})
    monkeypatch.setattr(query, "snapshot_compatible", lambda *args: False)
    monkeypatch.setattr(query, "derived_current", lambda *args: False)
    assert cli.cmd_status(argparse.Namespace(data=tmp_path, projects=tmp_path)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stale_or_unbuilt"] == 1
    assert out["source_errors"][0]["source"] == source.name
