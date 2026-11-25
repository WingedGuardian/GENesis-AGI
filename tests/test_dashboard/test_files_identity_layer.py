"""The generic file editor saves CONVERSATION/STEERING into their local layer (#2862).

Other tracked files in a main checkout still save, with a warning; a worktree
or an untracked file saves silently.
"""

from __future__ import annotations

import subprocess

import pytest
from flask import Flask

import genesis.dashboard.routes.config as config_mod
import genesis.dashboard.routes.files as files_mod
from genesis.dashboard.api import blueprint
from genesis.identity.paths import read_identity

_SHIPPED = "# Steering\n\n---\n\nshipped rule\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    repo = tmp_path / "repo"
    identity = repo / "src" / "genesis" / "identity"
    identity.mkdir(parents=True)
    (identity / "STEERING.md").write_text(_SHIPPED)
    (repo / "notes.md").write_text("tracked\n")
    (repo / "loose.md").write_text("untracked\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "src", "notes.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setattr(config_mod, "_IDENTITY_DIR", identity)
    monkeypatch.setattr(config_mod, "_REPO_ROOT", repo)
    monkeypatch.setattr(
        "genesis.env.genesis_db_path", lambda: tmp_path.parent / "db_area" / "genesis.db"
    )
    return app.test_client(), repo, identity


def _read(client, path):
    return client.get(f"/api/genesis/files/read?path={path}")


def test_steering_reads_resolved_and_saves_into_the_local_layer(env):
    client, _repo, identity = env
    (identity / "STEERING.local.md").write_text("---\n\nlocal rule\n")
    data = _read(client, identity / "STEERING.md").get_json()
    assert data["layered"] is True and "local rule" in data["content"]
    resp = client.put(
        "/api/genesis/files/write",
        json={
            "path": str(identity / "STEERING.md"),
            "content": data["content"].rstrip() + "\n\n---\n\nmy rule\n",
            "version": data["version"],
        },
    )
    assert resp.status_code == 200
    assert (identity / "STEERING.md").read_text() == _SHIPPED
    assert "my rule" in read_identity("STEERING.md", identity)
    assert "warning" not in resp.get_json()


def test_a_missing_version_asks_for_a_reload(env):
    client, _repo, identity = env
    resp = client.put(
        "/api/genesis/files/write", json={"path": str(identity / "STEERING.md"), "content": "x\n"}
    )
    assert resp.status_code == 400 and "reload" in resp.get_json()["error"]
    assert (identity / "STEERING.md").read_text() == _SHIPPED


def test_a_stale_version_is_refused(env):
    client, _repo, identity = env
    resp = client.put(
        "/api/genesis/files/write",
        json={"path": str(identity / "STEERING.md"), "content": "x\n", "version": "old"},
    )
    assert resp.status_code == 409


@pytest.mark.parametrize("op", ["delete", "rename_from", "rename_to", "create_shipped_name"])
def test_the_shipped_layer_file_is_not_deleted_renamed_or_recreated(env, op):
    client, repo, identity = env
    shipped = identity / "STEERING.md"
    if op == "delete":
        resp = client.delete(f"/api/genesis/files/delete?path={shipped}")
    elif op == "rename_from":
        resp = client.post(
            "/api/genesis/files/rename", json={"path": str(shipped), "new_name": "OLD.md"}
        )
    elif op == "rename_to":
        (identity / "other.md").write_text("o\n")
        resp = client.post(
            "/api/genesis/files/rename",
            json={"path": str(identity / "other.md"), "new_name": "CONVERSATION.md"},
        )
    else:
        resp = client.post(
            "/api/genesis/files/create",
            json={"path": str(identity / "CONVERSATION.md"), "content": "x\n"},
        )
    assert resp.status_code == 403
    assert shipped.read_text() == _SHIPPED
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""


@pytest.mark.parametrize("hop", [1, 2, 3])
def test_the_identity_dir_and_its_parents_are_not_renamed(env, hop):
    client, _repo, identity = env
    source = identity
    for _ in range(hop - 1):
        source = source.parent
    resp = client.post("/api/genesis/files/rename", json={"path": str(source), "new_name": "moved"})
    assert resp.status_code == 403
    assert (identity / "STEERING.md").read_text() == _SHIPPED


def test_the_warning_ignores_a_browser_written_fsmonitor(env, tmp_path):
    client, repo, _identity = env
    marker = tmp_path / "fsmonitor-ran"
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", f"touch {marker}"], check=True)
    resp = client.put(
        "/api/genesis/files/write", json={"path": str(repo / "notes.md"), "content": "edited\n"}
    )
    assert resp.status_code == 200 and "warning" in resp.get_json()
    assert not marker.exists()


def test_a_save_equal_to_head_gives_no_warning(env):
    client, repo, _identity = env
    resp = client.put(
        "/api/genesis/files/write", json={"path": str(repo / "notes.md"), "content": "tracked\n"}
    )
    assert resp.status_code == 200 and "warning" not in resp.get_json()


def test_another_clone_gives_no_warning(env, tmp_path):
    client, repo, _identity = env
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
    resp = client.put(
        "/api/genesis/files/write", json={"path": str(other / "notes.md"), "content": "e\n"}
    )
    assert resp.status_code == 200 and "warning" not in resp.get_json()


@pytest.mark.parametrize("op", ["write", "create", "rename"])
def test_a_local_layer_is_not_touched_directly(env, op):
    client, _repo, identity = env
    local = identity / "STEERING.local.md"
    if op == "write":
        local.write_text("---\n\nr\n")
        resp = client.put("/api/genesis/files/write", json={"path": str(local), "content": "x\n"})
    elif op == "create":
        resp = client.post("/api/genesis/files/create", json={"path": str(local), "content": "x\n"})
    else:
        (identity / "other.md").write_text("o\n")
        resp = client.post(
            "/api/genesis/files/rename",
            json={"path": str(identity / "other.md"), "new_name": "STEERING.local.md"},
        )
    assert resp.status_code == 403


def test_a_tracked_file_in_a_main_checkout_saves_with_a_warning(env):
    client, repo, _identity = env
    resp = client.put(
        "/api/genesis/files/write", json={"path": str(repo / "notes.md"), "content": "edited\n"}
    )
    assert resp.status_code == 200
    assert "next deploy refuses" in resp.get_json()["warning"]
    assert (repo / "notes.md").read_text() == "edited\n"


def test_an_untracked_file_saves_without_a_warning(env):
    client, repo, _identity = env
    resp = client.put(
        "/api/genesis/files/write", json={"path": str(repo / "loose.md"), "content": "e\n"}
    )
    assert resp.status_code == 200 and "warning" not in resp.get_json()


def test_a_worktree_file_saves_without_a_warning(env, tmp_path):
    client, repo, _identity = env
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
    resp = client.put(
        "/api/genesis/files/write", json={"path": str(wt / "notes.md"), "content": "e\n"}
    )
    assert resp.status_code == 200 and "warning" not in resp.get_json()


def test_a_worktree_steering_file_is_edited_in_place(env, tmp_path):
    """Only the server's own identity directory is layered; a worktree edits its tracked copy."""
    client, repo, _identity = env
    wt = tmp_path / "wt2"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
    target = wt / "src" / "genesis" / "identity" / "STEERING.md"
    resp = client.put(
        "/api/genesis/files/write", json={"path": str(target), "content": "wt edit\n"}
    )
    assert resp.status_code == 200
    assert target.read_text() == "wt edit\n"
