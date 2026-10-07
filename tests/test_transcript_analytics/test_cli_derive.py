"""CLI snapshot behavior belongs to the CLI layer, not the store PR."""

import json

import pytest

from genesis.transcript_analytics import derive, store


def _asst(mid, ts, out):
    return {
        "type": "assistant",
        "sessionId": "s",
        "timestamp": ts,
        "message": {
            "id": mid,
            "model": "m",
            "stop_reason": "end_turn",
            "content": [],
            "usage": {"output_tokens": out},
        },
    }


@pytest.fixture
def built(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    path = projects / "session.jsonl"
    path.write_text(json.dumps(_asst("m1", "2026-10-01T00:00:00Z", 5)) + "\n")
    store.ingest(projects, data)
    return projects, data, path


def test_ingest_cli_derives_only_when_something_changed(built, monkeypatch, capsys):
    from genesis.transcript_analytics import cli, config, resources

    monkeypatch.setattr(config, "load", lambda: config.Config(enabled=True))
    monkeypatch.setattr(resources, "ensure_capped", lambda *args: None)

    projects, data, f = built
    calls = []
    monkeypatch.setattr(
        derive, "build", lambda d, **k: calls.append(d) or {"dir": "x", "views": []}
    )
    args = ["--projects", str(projects), "--data", str(data), "ingest"]
    assert cli.main(args) == 0 and len(calls) == 1  # no snapshot yet -> build
    monkeypatch.setattr(derive, "is_current", lambda d: True)
    with open(f, "a") as fh:
        fh.write(json.dumps(_asst("m3", "2026-10-01T00:00:03Z", 1)) + "\n")
    # Freshness is derived from the inputs, not from whether THIS run rebuilt (re-audit SF-1):
    assert cli.main(args) == 0 and len(calls) == 1  # snapshot reports current -> no derive
    monkeypatch.setattr(derive, "is_current", lambda d: False)
    assert (
        cli.main(args) == 0 and len(calls) == 2
    )  # snapshot stale -> derive, even with nothing rebuilt


def test_reaudit_sf3_ingest_survives_a_derive_failure(built, monkeypatch, capsys):
    from genesis.transcript_analytics import cli, config, resources

    monkeypatch.setattr(config, "load", lambda: config.Config(enabled=True))
    monkeypatch.setattr(resources, "ensure_capped", lambda *args: None)

    projects, data, f = built

    def boom(d, **k):
        raise RuntimeError("synthetic derive failure")

    monkeypatch.setattr(derive, "build", boom)
    rc = cli.main(["--projects", str(projects), "--data", str(data), "ingest"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and "synthetic derive failure" in out["derived"] and out["sources"] == 1
