"""Dashboard deletes go to the Genesis trash (#2926 PR 5).

The suite-wide ``_isolate_trash_root`` fixture puts the trash at
``tmp_path / "genesis-trash"``, inside the allowed root these tests use, so the
browser's refusal to open a trash is exercised for real.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from flask import Flask

import genesis.dashboard.routes.config as config_mod
import genesis.dashboard.routes.files as files_mod
from genesis.dashboard.api import blueprint
from genesis.trash import ITEM, list_entries


@pytest.fixture()
def client(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setattr(files_mod, "_UPLOAD_DIR", tmp_path / "uploads")
    # Keep the isolated database out of the allowed root (see test_files_api).
    monkeypatch.setattr(
        "genesis.env.genesis_db_path", lambda: tmp_path.parent / "db_area" / "genesis.db"
    )
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "gh"))
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setattr(config_mod, "_MEMORY_DIR", mem)
    return app.test_client()


def _entry_for(caller: str):
    [entry] = [e for e in list_entries() if e.tombstone and e.tombstone.caller == caller]
    return entry


# ── file browser ─────────────────────────────────────────────────────────────


def test_a_browser_delete_moves_the_file_to_the_trash(client, tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("keep me")
    resp = client.delete(f"/api/genesis/files/delete?path={f}")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert not f.exists()
    entry = _entry_for("dashboard.files.delete")
    assert body["trash_entry"] == entry.path.name
    assert (entry.path / ITEM).read_text() == "keep me"
    assert entry.tombstone.original_path == str(f)


def test_a_refused_trash_returns_409_and_leaves_the_file(client, tmp_path):
    f = tmp_path / "gh" / "cc-tmp" / "scratch.txt"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    resp = client.delete(f"/api/genesis/files/delete?path={f}")
    assert resp.status_code == 409
    assert "temp volume" in resp.get_json()["error"]
    assert f.read_text() == "x"


def test_another_volume_is_refused_by_the_trash(client, tmp_path, monkeypatch):
    f = tmp_path / "shared.txt"
    f.write_text("x")
    real_stat = os.stat
    trash_parent = (tmp_path / "genesis-trash").parent

    def stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        if Path(p) == trash_parent:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1) + tuple(st)[3:])
        return st

    monkeypatch.setattr("genesis.trash.os.stat", stat)
    resp = client.delete(f"/api/genesis/files/delete?path={f}")
    assert resp.status_code == 409
    assert "another volume" in resp.get_json()["error"]
    assert f.exists()
    assert list_entries() == []


def test_a_symlink_path_trashes_its_target_as_before(client, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("t")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    resp = client.delete(f"/api/genesis/files/delete?path={link}")
    assert resp.status_code == 200
    assert not target.exists()  # the route resolves the path, as it always has
    assert (_entry_for("dashboard.files.delete").path / ITEM).read_text() == "t"


def test_a_directory_is_still_refused(client, tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    assert client.delete(f"/api/genesis/files/delete?path={d}").status_code == 400
    assert d.is_dir()


@pytest.mark.parametrize("store", ["home", "worktree"])
def test_the_browser_cannot_open_or_list_a_trash(client, tmp_path, monkeypatch, store):
    monkeypatch.setattr(files_mod, "_worktree_trash_dir", lambda: tmp_path / "wt-trash")
    if store == "home":
        top = tmp_path / "genesis-trash"
        inside = top / "20261007T000000Z-x" / ITEM
    else:
        top = tmp_path / "wt-trash"
        inside = top / "w" / "notes.txt"
    inside.parent.mkdir(parents=True)
    inside.write_text("secret-ish")
    assert client.get(f"/api/genesis/files/read?path={inside}").status_code == 403
    assert client.delete(f"/api/genesis/files/delete?path={inside}").status_code == 403
    assert inside.exists()
    listed = client.get(f"/api/genesis/files?path={tmp_path}").get_json()["entries"]
    assert top.name not in [e["name"] for e in listed]
    assert "memory" in [e["name"] for e in listed]  # the listing itself worked


# ── memory files ─────────────────────────────────────────────────────────────


def test_a_memory_delete_moves_the_file_to_the_trash(client, tmp_path):
    f = tmp_path / "memory" / "feedback_x.md"
    f.write_text("a rule")
    resp = client.delete("/api/genesis/config-files/memory/feedback_x.md")
    assert resp.status_code == 200, resp.get_json()
    assert not f.exists()
    entry = _entry_for("dashboard.config.memory_delete")
    assert resp.get_json()["trash_entry"] == entry.path.name
    assert (entry.path / ITEM).read_text() == "a rule"


@pytest.mark.parametrize("name", ["memory/MEMORY.md", "memory/./MEMORY.md"])
def test_the_memory_index_is_never_deleted(client, tmp_path, name):
    idx = tmp_path / "memory" / "MEMORY.md"
    idx.write_text("index")
    assert client.delete(f"/api/genesis/config-files/{name}").status_code == 403
    assert idx.read_text() == "index"
    assert list_entries() == []


def test_a_memory_symlink_is_trashed_as_the_link(client, tmp_path):
    real = tmp_path / "memory" / "real.md"
    real.write_text("r")
    link = tmp_path / "memory" / "alias.md"
    link.symlink_to(real)
    resp = client.delete("/api/genesis/config-files/memory/alias.md")
    assert resp.status_code == 200
    assert real.read_text() == "r"
    assert not os.path.lexists(link)
    assert os.path.islink(_entry_for("dashboard.config.memory_delete").path / ITEM)


def test_a_non_memory_delete_is_still_refused(client):
    assert client.delete("/api/genesis/config-files/outreach.yaml").status_code == 403
