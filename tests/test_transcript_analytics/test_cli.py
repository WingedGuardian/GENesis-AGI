import json
import subprocess
import sys
from pathlib import Path

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


def test_ingest_returns_deferred_when_snapshot_lock_busy(tmp_path, monkeypatch, capsys):
    import argparse

    from genesis.transcript_analytics import derive, store

    monkeypatch.setattr(store, "ingest", lambda *a, **kw: {"failed": 0})
    monkeypatch.setattr(derive, "is_current", lambda *a: False)

    def busy(*args):
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
    bad = data / f"{store.MARKER}__bad.parquet"
    bad.write_bytes(b"not parquet")
    stats = data / f"{store.MARKER}__stats.parquet"
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
