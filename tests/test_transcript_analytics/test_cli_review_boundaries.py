"""CLI diagnostics and bounded result consumption preserve query evidence."""

import csv
import errno
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.transcript_analytics import cli, config, query, store


@pytest.mark.parametrize("number", [errno.EIO, errno.EACCES, errno.ELOOP, errno.ENOENT])
def test_status_isolates_initial_source_stat_errors(tmp_path, monkeypatch, number):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    data.mkdir()
    bad, good = projects / "bad.jsonl", projects / "good.jsonl"
    bad.write_text("{}\n")
    good.write_text("{}\n")
    monkeypatch.setattr(store, "discover", lambda _: [(bad, bad.name), (good, good.name)])
    real_stat = os.stat
    def inspect(path, *args, **kwargs):
        if path == bad:
            raise OSError(number, "synthetic stat error")
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(cli.os, "stat", inspect)
    out = cli._status(SimpleNamespace(projects=projects, data=data))
    assert out["stale_or_unbuilt"] == (1 if number == errno.ENOENT else 2)
    assert len(out["source_errors"]) == int(number != errno.ENOENT)
    if out["source_errors"]:
        assert out["source_errors"][0]["source"] == store.source_identity(bad.name)


@pytest.mark.parametrize(
    "kind",
    [
        "outside",
        "nested",
        "directory",
        "root-link",
        "root-file",
        "dangling",
        "loop",
        "valid",
        "stale",
        "absent",
        "empty",
        "bad-json",
    ],
)
def test_status_uses_reader_selector_boundary_without_reading_pages(tmp_path, monkeypatch, kind):
    data, root = tmp_path / "data", tmp_path / "data/derived"
    data.mkdir()
    manifest = {
        "views_version": query.VIEWS_VERSION,
        "inputs_signature": "captured",
        "views": list(query._DERIVED),
        "schemas": {},
        "marker": "synthetic",
    }
    if kind != "absent":
        if kind == "root-file":
            root.write_text("not a directory")
        else:
            target = tmp_path / "outside" if kind == "root-link" else root
            target.mkdir()
            if kind == "root-link":
                root.symlink_to(target, target_is_directory=True)
            if kind != "empty":
                generation = (
                    tmp_path / "external"
                    if kind == "outside"
                    else target / "nested/deep"
                    if kind == "nested"
                    else target / "generation"
                )
                generation.mkdir(parents=True)
                selector = target / "current"
                if kind == "directory":
                    selector.mkdir()
                    generation = selector
                else:
                    selector.symlink_to(
                        "missing"
                        if kind == "dangling"
                        else "current"
                        if kind == "loop"
                        else generation,
                        target_is_directory=True,
                    )
                if kind == "stale":
                    manifest["views_version"] = "stale"
                (generation / "MANIFEST.json").write_text(
                    "broken" if kind == "bad-json" else json.dumps(manifest)
                )
    monkeypatch.setattr(store, "inputs_signature", lambda *a, **k: "captured")
    monkeypatch.setattr(query, "connect", lambda *a, **k: pytest.fail("status loaded cache pages"))
    observed, current = cli._snapshot_state(data, selection=([], []))
    if kind in {"valid", "stale"}:
        assert observed == manifest and current is (kind == "valid")
    else:
        assert observed is None
        assert current is (None if kind in {"absent", "empty"} else False)


def _published(tmp_path, manifest_text=None):
    """A derived/current selector over one generation holding MANIFEST.json."""
    data = tmp_path / "data"
    generation = data / "derived" / "generation"
    generation.mkdir(parents=True)
    (data / "derived" / "current").symlink_to("generation", target_is_directory=True)
    if manifest_text is not None:
        (generation / "MANIFEST.json").write_text(manifest_text)
    return data, generation


def test_status_does_not_block_on_a_special_manifest(tmp_path, monkeypatch):
    import threading

    data, generation = _published(tmp_path)
    os.mkfifo(generation / "MANIFEST.json")
    monkeypatch.setattr(store, "inputs_signature", lambda *a, **k: "captured")
    result = []
    reader = threading.Thread(
        target=lambda: result.append(cli._snapshot_state(data, selection=([], []))), daemon=True
    )
    reader.start()
    reader.join(timeout=10)
    assert not reader.is_alive(), "status blocked on a FIFO manifest"
    assert result == [(None, False)]


@pytest.mark.parametrize(
    "change",
    [{"views": ["tool_calls"]}, {"schemas": None}, {"views_version": "older"}],
    ids=["views-list", "schemas", "views-version"],
)
def test_status_calls_current_only_what_the_reader_would_load(tmp_path, monkeypatch, change):
    manifest = {
        "views_version": query.VIEWS_VERSION,
        "inputs_signature": "captured",
        "views": list(query._DERIVED),
        "schemas": {},
    }
    manifest.update(change)
    data, _ = _published(tmp_path, json.dumps(manifest))
    monkeypatch.setattr(store, "inputs_signature", lambda *a, **k: "captured")
    observed, current = cli._snapshot_state(data, selection=([], []))
    assert observed == manifest and current is False
    with pytest.raises(ValueError, match="not current"):
        query._snapshot(data, ([], []))


@pytest.mark.parametrize("claude", [False, True])
@pytest.mark.parametrize("genesis", [False, True])
def test_shipped_path_defaults_follow_canonical_homes(tmp_path, monkeypatch, claude, genesis):
    from genesis import _config_overlay

    for key, enabled, path in [
        ("CLAUDE_HOME", claude, tmp_path / "claude"),
        ("GENESIS_HOME", genesis, tmp_path / "genesis"),
    ]:
        if enabled:
            monkeypatch.setenv(key, str(path))
        else:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GENESIS_TRANSCRIPT_ANALYTICS_DISABLED", raising=False)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: tmp_path)
    base = Path(__file__).resolve().parents[2] / "config/transcript_analytics.yaml"
    cfg = config.load(base)
    assert cfg.enabled is False
    assert (
        cfg.projects_dir
        == (tmp_path / "claude" if claude else Path.home() / ".claude") / "projects"
    )
    assert (
        cfg.data_dir
        == (tmp_path / "genesis" if genesis else Path.home() / ".genesis") / "analytics/transcripts"
    )
    explicit = config.from_values(
        {"projects_dir": "~/.claude/projects", "data_dir": "~/.genesis/analytics/transcripts"}
    )
    assert explicit.projects_dir == Path.home() / ".claude/projects"
    assert explicit.data_dir == Path.home() / ".genesis/analytics/transcripts"


@pytest.fixture
def result_reader(monkeypatch):
    def prepare(count=2050, *, allow_fetchall=False):
        class Rows:
            columns = ["value"]
            position = 0

            def fetchall(self):
                assert allow_fetchall, "CLI materialized its complete user result"
                self.position = count
                return [(i,) for i in range(count)]

            def fetchmany(self, size):
                assert 0 < size <= 4096
                start, self.position = self.position, min(count, self.position + size)
                return [(i,) for i in range(start, self.position)]

        class Connection:
            closed, executions = False, 0
            rows = Rows()

            def sql(self, sql, params=None):
                self.executions += 1
                return self.rows

            def execute(self, *args):
                assert self.rows.position == count, "metadata interrupted result consumption"
                return self

            def fetchone(self):
                return (0,)

            def close(self):
                self.closed = True

        con, manifests = Connection(), []
        query._READS[con] = {"coverage": {}, "selection": ([], []), "inputs_signature": "captured"}
        monkeypatch.setattr(query, "connect", lambda *a, **k: con)
        monkeypatch.setattr(query, "turn_identity_coverage", lambda c: c.execute("identity") and {})
        monkeypatch.setattr(query, "_scrub_labels", lambda *a: {})
        monkeypatch.setattr(query, "_write_manifest", lambda path, value: manifests.append(value))
        monkeypatch.setattr(store, "inventory", lambda data: {})
        monkeypatch.setattr(store, "semantics", lambda: {})
        return con, manifests

    return prepare


@pytest.mark.parametrize("limit", [0, 1, 1023, 1024, 1025, 3000])
@pytest.mark.parametrize("as_csv", [False, True])
@pytest.mark.parametrize("count", [0, 2050])
def test_cli_batches_user_results_and_preserves_exact_manifest(
    result_reader, capsys, tmp_path, limit, as_csv, count
):
    con, manifests = result_reader(count)
    args = SimpleNamespace(
        data=tmp_path,
        query="synthetic query",
        live=True,
        manifest=tmp_path / "audit.json",
        window=None,
        baseline=None,
        csv=as_csv,
        max_rows=limit,
        cell_width=30,
    )
    assert cli.cmd_sql(args) == 0
    output = capsys.readouterr().out
    assert con.closed and con.executions == 1 and con.rows.position == count
    assert manifests[0]["result_rows"] == count
    assert manifests[0]["denominators"] == dict.fromkeys(["tool_calls", "turns", "sessions"], 0)
    if as_csv:
        assert list(csv.reader(io.StringIO(output))) == [
            ["value"],
            *[[str(i)] for i in range(count)],
        ]
    else:
        assert output.splitlines()[-1].startswith(f"({count} rows")
        assert len(output.splitlines()) == min(limit, count) + 3


def test_public_query_still_returns_the_complete_list(result_reader, tmp_path):
    con, manifests = result_reader(9, allow_fetchall=True)
    assert query.run_query(
        tmp_path, "synthetic query", live=True, manifest_path=tmp_path / "audit.json"
    ) == (
        ["value"],
        [(i,) for i in range(9)],
    )
    assert con.closed and manifests[0]["result_rows"] == 9


@pytest.mark.parametrize("method,target", [
    ("is_symlink", "root"), ("exists", "root"), ("is_dir", "root"),
    ("is_symlink", "current"), ("exists", "current"),
])
@pytest.mark.parametrize("error", [PermissionError(13, "synthetic classification"), OSError(5, "synthetic classification")])
def test_status_classification_io_failure_stays_unreadable(tmp_path, monkeypatch, method, target, error):
    root = tmp_path / "derived"
    root.mkdir()
    probe = root if target == "root" else root / "current"
    original = getattr(Path, method)

    def failed_resolver(data):
        raise ValueError("synthetic malformed snapshot")

    def failed_probe(path, *args, **kwargs):
        if path == probe:
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(query, "_snapshot_directory", failed_resolver)
    monkeypatch.setattr(Path, method, failed_probe)
    assert cli._snapshot_state(tmp_path, selection=([], [])) == (None, False)


def test_deferred_csv_flush_failure_prevents_manifest(result_reader, tmp_path, monkeypatch):
    import sys

    class DeferredFailure(io.StringIO):
        def flush(self):
            raise OSError("synthetic deferred flush")

    con, manifests = result_reader()
    args = SimpleNamespace(data=tmp_path, query="synthetic query", live=True, manifest=tmp_path / "audit.json",
                           window=None, baseline=None, csv=True, max_rows=1, cell_width=30)
    with monkeypatch.context() as patch_stdout:
        patch_stdout.setattr(sys, "stdout", DeferredFailure())
        with pytest.raises(OSError, match="synthetic deferred flush"):
            cli.cmd_sql(args)
    assert con.closed and not manifests


@pytest.mark.parametrize("failure", ["fetch", "write"])
def test_consumption_failure_closes_connection_without_success_manifest(
    result_reader, tmp_path, monkeypatch, failure
):
    import sys

    con, manifests = result_reader()
    fetch = con.rows.fetchmany

    def failed_fetch(size):
        if con.rows.position:
            raise OSError("synthetic interrupted fetch")
        return fetch(size)

    class FailedWriter(io.StringIO):
        writes = 0

        def write(self, value):
            self.writes += 1
            if self.writes > 2:
                raise OSError("synthetic interrupted write")
            return super().write(value)

    if failure == "fetch":
        monkeypatch.setattr(con.rows, "fetchmany", failed_fetch)
    else:
        monkeypatch.setattr(sys, "stdout", FailedWriter())
    args = SimpleNamespace(
        data=tmp_path,
        query="synthetic query",
        live=True,
        manifest=tmp_path / "audit.json",
        window=None,
        baseline=None,
        csv=failure == "write",
        max_rows=1,
        cell_width=30,
    )
    with pytest.raises(OSError, match="synthetic interrupted"):
        cli.cmd_sql(args)
    assert con.closed and not manifests


def test_no_relation_preserves_empty_result_and_manifest(
    result_reader, tmp_path, monkeypatch, capsys
):
    con, manifests = result_reader(0)
    monkeypatch.setattr(con, "sql", lambda *a, **k: None)
    args = SimpleNamespace(
        data=tmp_path,
        query="synthetic statement",
        live=True,
        manifest=tmp_path / "audit.json",
        window=None,
        baseline=None,
        csv=False,
        max_rows=1,
        cell_width=30,
    )
    assert cli.cmd_sql(args) == 0 and capsys.readouterr().out == ""
    assert con.closed and manifests[0]["result_rows"] == 0


@pytest.mark.parametrize("as_csv", [False, True])
def test_native_consumption_executes_volatile_query_once(tmp_path, monkeypatch, as_csv, capsys):
    original = query.connect
    connections = []

    def connect(*args, **kwargs):
        con = original(*args, **kwargs, memory_limit="128MB", threads=1)
        con.execute("CREATE SEQUENCE probe START 1")
        connections.append(con)
        return con

    args = SimpleNamespace(csv=as_csv, max_rows=3)

    def consume(rel):
        payload, total = cli._sql_rows(rel, args)
        assert connections[0].execute("SELECT currval('probe')").fetchone() == (2050,)
        return payload, total

    monkeypatch.setattr(query, "connect", connect)
    manifest = tmp_path / "manifest.json"
    cols, shown, total = query._query(
        tmp_path / "data",
        "SELECT nextval('probe') AS value FROM range(2050)",
        consume=consume,
        live=True,
        manifest_path=manifest,
    )
    assert cols == ["value"] and total == 2050 and len(connections) == 1
    assert json.loads(manifest.read_text())["result_rows"] == 2050
    if as_csv:
        assert shown == []
        assert list(csv.reader(io.StringIO(capsys.readouterr().out))) == [
            ["value"],
            *[[str(i)] for i in range(1, 2051)],
        ]
    else:
        assert shown == [(1,), (2,), (3,)]
