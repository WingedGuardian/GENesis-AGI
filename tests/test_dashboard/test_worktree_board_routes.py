"""Tests for the worktree board dashboard routes.

The route is deliberately thin: it renders the cache that
``scripts/worktree_lifecycle.py`` wrote and re-derives nothing, because a board
that disagreed with the reaper would be worse than no board. So what is pinned
here is the wiring and the HONESTY of the degraded paths — a missing or corrupt
cache must read as "no board", never as "no worktrees", and a refresh failure
must still render whatever the cache holds rather than blanking the modal.

The one safety property worth a test of its own: the refresh endpoint can only
ever invoke the reaper in report-only mode. If that argv ever gains a reaping
flag, the dashboard becomes able to destroy work on a POST.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint
from genesis.dashboard.routes import worktrees as wt


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Never read the operator's real board, archive, or tombstone index."""
    monkeypatch.setattr(wt, "BOARD_CACHE", tmp_path / "board.json")
    monkeypatch.setattr(wt, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(wt, "TOMBSTONE_INDEX", tmp_path / "tombstones.jsonl")


def _write_board(tmp_path, rows, generated_at="2026-09-10T12:00:00+00:00"):
    (tmp_path / "board.json").write_text(
        json.dumps({"generated_at": generated_at, "worktrees": rows})
    )


ROWS = [
    {
        "path": "/w/a",
        "branch": "feat/a",
        "state": "at_risk",
        "action": "none",
        "age_days": 9.0,
        "merged": False,
        "merge_method": "",
        "dirty": None,
        "reason": "unmerged, 9d cold",
    },
    {
        "path": "/w/b",
        "branch": "feat/b",
        "state": "reap_merged",
        "action": "trash",
        "age_days": 20.0,
        "merged": True,
        "merge_method": "pr",
        "dirty": False,
        "reason": "merged via pr",
    },
    {
        "path": "/w/c",
        "branch": "feat/c",
        "state": "fresh",
        "action": "none",
        "age_days": 1.0,
        "merged": False,
        "merge_method": "",
        "dirty": None,
        "reason": "activity 1d ago",
    },
]


def test_board_renders_counts_and_actions(client, tmp_path):
    _write_board(tmp_path, ROWS)
    body = client.get("/api/genesis/worktrees").get_json()
    assert body["available"] is True
    assert body["total"] == 3
    assert body["counts"] == {"at_risk": 1, "reap_merged": 1, "fresh": 1}
    assert body["due_for_archiving"] == 1  # only the row whose action is "trash"
    assert set(body["state_labels"]) >= {"at_risk", "reap_merged", "fresh"}


def test_missing_cache_is_unavailable_not_empty(client):
    """No cache must NOT read as "zero worktrees" — the distinction is the point."""
    body = client.get("/api/genesis/worktrees").get_json()
    assert body["available"] is False
    assert body["worktrees"] == []
    assert body["total"] == 0


@pytest.mark.parametrize(
    "payload",
    ["", "{", "null", "[]", '{"worktrees": "nope"}', '{"generated_at": 5}'],
)
def test_corrupt_cache_degrades_without_raising(client, tmp_path, payload):
    """A truncated or hand-edited cache must never 500 the dashboard."""
    (tmp_path / "board.json").write_text(payload)
    resp = client.get("/api/genesis/worktrees")
    assert resp.status_code == 200
    assert resp.get_json()["worktrees"] == []


def test_trash_summary_counts_archives_not_sidecars(client, tmp_path):
    """Sidecar metadata and dotfiles are not trash ENTRIES; archives are."""
    trash = tmp_path / "trash"
    trash.mkdir()
    (trash / "a-20260910.tar.gz").write_bytes(b"x" * 100)
    (trash / "a-20260910.meta.json").write_text("{}")
    (trash / ".extract-scratch").mkdir()
    (trash / "legacy-dir-20260801").mkdir()  # a pre-compression entry
    (tmp_path / "tombstones.jsonl").write_text('{"a":1}\n{"b":2}\n\n')

    body = client.get("/api/genesis/worktrees").get_json()
    trash_info = body["trash"]
    assert trash_info["available"] is True
    assert trash_info["entries"] == 2, "archive + legacy dir; sidecar/dotfile excluded"
    assert trash_info["archived"] == 1
    assert trash_info["bytes"] == 100
    assert trash_info["tombstones"] == 2, "blank lines are not rows"


# ─── the safety property: refresh can only ever REPORT ───────────────────────


def test_refresh_invokes_the_reaper_in_report_only_mode(client, tmp_path):
    """The POST must not be able to reap. Assert the argv, not the intent.

    A reaping run is the default (bare `worktree_lifecycle.py`), so the absence
    of `--report-json` here would silently turn a dashboard button into a
    destructive action against every stale worktree on the box.
    """
    _write_board(tmp_path, ROWS)
    with patch.object(wt.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        resp = client.post("/api/genesis/worktrees/refresh", json={"no_network": True})

    assert resp.status_code == 200
    argv = run.call_args[0][0]
    assert "--report-json" in argv, f"refresh must be report-only, got {argv}"
    assert "--no-network" in argv
    assert "--dry-run" not in argv  # not needed; report-only already mutates nothing
    assert not any(a in argv for a in ("--recover", "--list-trash"))


def test_refresh_omits_no_network_when_not_requested(client, tmp_path):
    _write_board(tmp_path, ROWS)
    with patch.object(wt.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        client.post("/api/genesis/worktrees/refresh", json={})
    assert "--no-network" not in run.call_args[0][0]


def test_refresh_failure_still_returns_the_existing_board(client, tmp_path):
    """A failed refresh must not blank the modal — stale data beats no data."""
    _write_board(tmp_path, ROWS)
    with patch.object(wt.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=1, stdout="", stderr="git exploded")
        resp = client.post("/api/genesis/worktrees/refresh", json={})

    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False
    assert "git exploded" in body["error"]
    assert body["total"] == 3, "the cached board must survive a failed refresh"


def test_refresh_timeout_reports_504_and_keeps_the_board(client, tmp_path):
    _write_board(tmp_path, ROWS)
    with patch.object(wt.subprocess, "run") as run:
        run.side_effect = wt.subprocess.TimeoutExpired(cmd="x", timeout=180)
        resp = client.post("/api/genesis/worktrees/refresh", json={})

    assert resp.status_code == 504
    body = resp.get_json()
    assert body["ok"] is False
    assert body["total"] == 3


def test_refresh_reports_a_missing_reaper_rather_than_crashing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "_repo_root", lambda: tmp_path / "nowhere")
    resp = client.post("/api/genesis/worktrees/refresh", json={})
    assert resp.status_code == 500
    assert "not found" in resp.get_json()["error"]


def test_absolute_home_paths_never_reach_the_open_endpoint(client, tmp_path):
    """S7: this GET is unauthenticated by the dashboard's design.

    The blueprint gate exempts /api/ and the mutation gate exempts GET, and the
    dashboard is reachable from any IP — so publishing absolute worktree paths
    would put the OS username and full directory layout on an open endpoint for
    every branch at once. The parent directory carries the operational value;
    the home prefix carries only the username.
    """
    # Build under the REAL home the route will resolve, whatever the test env
    # sets it to — hardcoding /home/ubuntu here would silently test the
    # outside-home fallback instead of the stripping path.
    home = Path.home()
    rows = [
        {**r, "path": str(home / "genesis-worktrees" / r["branch"].split("/")[-1])}
        for r in ROWS
    ]
    _write_board(tmp_path, rows)
    body = client.get("/api/genesis/worktrees").get_json()
    blob = json.dumps(body)

    assert str(home) not in blob, "the home prefix must be stripped"
    assert "path" not in body["worktrees"][0], "the raw path field must be gone"
    locations = [w["location"] for w in body["worktrees"]]
    assert locations == ["genesis-worktrees/a", "genesis-worktrees/b", "genesis-worktrees/c"]
    assert all("/" in loc for loc in locations), (
        "the parent directory is the part with operational value; keep it"
    )


def test_a_path_outside_home_degrades_to_its_basename(client, tmp_path):
    """A worktree somewhere unexpected must not leak its full path either."""
    _write_board(tmp_path, [
        {"path": "/srv/elsewhere/deep/wt-x", "branch": "b", "state": "fresh",
         "action": "none", "age_days": 1.0, "merged": False, "merge_method": "",
         "dirty": None, "reason": "r"},
    ])
    body = client.get("/api/genesis/worktrees").get_json()
    assert body["worktrees"][0]["location"] == "wt-x"
    assert "/srv/elsewhere" not in json.dumps(body)
