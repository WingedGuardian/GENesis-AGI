"""Fatal publication diagnostics stop before deriving and preserve exit ABI."""

import json
from types import SimpleNamespace

import pytest

from genesis.transcript_analytics import cli, derive, publication, store


@pytest.mark.parametrize("uncertain", [False, True])
def test_ingest_reports_committed_and_staged_progress_without_deriving(
    tmp_path, monkeypatch, capsys, uncertain
):
    error = (publication.Uncertain if uncertain else publication.Failed)("selector fault")
    error.staged_unpublished = 2
    error.rebuilt_staged = 3
    error.rebuilt_durably_committed = 1
    error.last_durable_revision = "a" * 32
    error.visible_revision = "b" * 32 if uncertain else None

    def fail(*args, **kwargs):
        raise error

    def forbidden(*args, **kwargs):
        pytest.fail("derive must not run after fatal publication")

    monkeypatch.setattr(store, "ingest", fail)
    monkeypatch.setattr(derive, "build", forbidden)
    args = SimpleNamespace(
        since=None,
        timer=False,
        projects=tmp_path / "projects",
        data=tmp_path / "data",
        adopt_projects_root=False,
        no_derive=False,
    )
    assert cli.cmd_ingest(args) == 2
    output = capsys.readouterr()
    assert not output.out
    report = json.loads(output.err)
    assert report["publication_failed"]
    assert report["durability_uncertain"] is uncertain
    assert report["staged_unpublished"] == 2
    assert report["rebuilt_staged"] == 3
    assert report["rebuilt_durably_committed"] == 1
    assert report["last_durable_revision"] == "a" * 32
    assert report["visible_revision"] == ("b" * 32 if uncertain else None)


def test_failure_before_publisher_reports_unknown_progress(tmp_path, monkeypatch, capsys):
    failure = publication.Failed("data preparation failed")
    monkeypatch.setattr(store, "ingest", lambda *a, **k: (_ for _ in ()).throw(failure))
    args = SimpleNamespace(
        since=None,
        timer=False,
        projects=tmp_path / "projects",
        data=tmp_path / "data",
        adopt_projects_root=False,
        no_derive=True,
    )
    assert cli.cmd_ingest(args) == 2
    report = json.loads(capsys.readouterr().err)
    for field in (
        "staged_unpublished",
        "rebuilt_staged",
        "rebuilt_durably_committed",
        "last_durable_revision",
    ):
        assert report[field] is None


@pytest.mark.parametrize("boundary", ["directory", "finalize", "checkpoint", "collect"])
@pytest.mark.parametrize("uncertain", [False, True])
def test_fatal_source_boundaries_report_actual_durable_prefix(
    tmp_path, monkeypatch, capsys, boundary, uncertain
):
    from genesis.transcript_analytics import catalog

    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    for name in ("a.jsonl", "b.jsonl"):
        (projects / name).write_text(
            json.dumps({"type": "assistant", "uuid": name, "message": {"id": name, "content": []}})
            + "\n"
        )
    monkeypatch.setattr(store, "DEFAULT_LOCK", tmp_path / "writer.lock")
    discover = store.discover
    monkeypatch.setattr(store, "discover", lambda root: sorted(discover(root), key=lambda x: x[1]))
    failure = (publication.Uncertain if uncertain else publication.Failed)("injected boundary")
    failure.visible_revision = "f" * 32 if uncertain else None
    extract = store.extract_source
    original_publish = publication.publish

    def fail(*a, **k):
        raise failure

    def extract_second(path, *a, **k):
        if path.name == "b.jsonl":
            if boundary == "directory":
                (data / ".staging" / "sources").rename(data / "saved-stages")
                (data / ".staging" / "sources").write_text("not a directory")
            elif boundary == "finalize":
                monkeypatch.setattr(publication, "finalize", fail)
            elif boundary == "checkpoint":
                monkeypatch.setattr(publication, "publish", fail)
            else:
                monkeypatch.setattr(publication, "collect", fail)
        return extract(path, *a, **k)

    monkeypatch.setattr(store, "extract_source", extract_second)
    args = SimpleNamespace(
        since=None,
        timer=False,
        projects=projects,
        data=data,
        adopt_projects_root=False,
        no_derive=True,
    )
    assert cli.cmd_ingest(args) == 2
    report = json.loads(capsys.readouterr().err)
    selected = catalog.load(data)
    expected = 2 if boundary == "collect" else 1
    assert report["rebuilt_durably_committed"] == expected
    assert report["last_durable_revision"] == selected.revision
    assert selected.collection["rebuilt_committed"] == expected
    assert report["rebuilt_staged"] == (2 if boundary in ("checkpoint", "collect") else 1)
    assert report["staged_unpublished"] == (1 if boundary == "checkpoint" else 0)
    if boundary != "directory":
        assert report["visible_revision"] == failure.visible_revision
    monkeypatch.setattr(publication, "publish", original_publish)


@pytest.mark.parametrize("boundary", ["bootstrap", "discovery", "standalone"])
def test_early_publication_failures_keep_exception_and_known_zero_progress(
    tmp_path, monkeypatch, boundary
):
    from genesis.transcript_analytics import catalog

    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    path = projects / "a.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "uuid": "a", "message": {"content": []}}) + "\n"
    )
    monkeypatch.setattr(store, "DEFAULT_LOCK", tmp_path / "writer.lock")
    failure = publication.Uncertain("injected early failure")
    failure.visible_revision = "f" * 32

    def fail(*a, **k):
        raise failure

    if boundary == "bootstrap":
        monkeypatch.setattr(publication, "publish", fail)
    elif boundary == "discovery":
        monkeypatch.setattr(store, "discover", fail)
    else:
        monkeypatch.setattr(publication, "finalize", fail)
    with pytest.raises(publication.Uncertain) as caught:
        if boundary == "standalone":
            store.build_source(path, "a.jsonl", data, path.stat())
        else:
            store.ingest(projects, data)
    assert caught.value is failure
    assert failure.staged_unpublished == failure.rebuilt_staged == 0
    assert failure.rebuilt_durably_committed == 0
    selected = catalog.load(data)
    assert failure.last_durable_revision == (selected.revision if selected else None)
    assert failure.visible_revision == "f" * 32
