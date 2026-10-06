"""Upload cancel re-checks that the stored path is still inside the upload dir.

``knowledge_upload_cancel`` unlinks a path it reads back from the DB. The path
is written under ``_UPLOAD_DIR`` at upload time; these tests pin that a row
pointing anywhere else deletes nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

# Importing the module registers its routes on the shared blueprint.
import genesis.dashboard.routes.knowledge_upload as upload_routes
from genesis.dashboard._blueprint import blueprint


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    root.mkdir()
    monkeypatch.setattr(upload_routes, "_UPLOAD_DIR", root)
    return root


def _cancel(client, file_path):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = MagicMock()
    row = {"id": "u1", "status": "pending", "file_path": str(file_path)}
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.knowledge_uploads.get", new_callable=AsyncMock, return_value=row),
        patch("genesis.db.crud.knowledge_uploads.delete", new_callable=AsyncMock) as mock_delete,
    ):
        MockRT.instance.return_value = rt
        resp = client.delete("/api/genesis/knowledge/upload/u1")
    return resp, mock_delete


def test_cancel_removes_file_and_empty_upload_subdir(client, inbox):
    sub = inbox / "u1"
    sub.mkdir()
    target = sub / "doc.txt"
    target.write_text("x")

    resp, mock_delete = _cancel(client, target)

    assert resp.status_code == 200
    assert not target.exists()
    assert not sub.exists()
    mock_delete.assert_awaited_once()


def test_cancel_refuses_a_path_outside_the_upload_dir(client, inbox, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "keep.txt"
    victim.write_text("x")

    resp, mock_delete = _cancel(client, victim)

    assert resp.status_code == 409
    assert victim.exists()
    mock_delete.assert_not_awaited()


def test_cancel_refuses_traversal_out_of_the_upload_dir(client, inbox, tmp_path):
    victim = tmp_path / "keep.txt"
    victim.write_text("x")
    sneaky = inbox / "u1" / ".." / ".." / "keep.txt"

    resp, mock_delete = _cancel(client, sneaky)

    assert resp.status_code == 409
    assert victim.exists()
    mock_delete.assert_not_awaited()


def test_cancel_never_removes_the_upload_root_itself(client, inbox):
    target = inbox / "doc.txt"
    target.write_text("x")

    resp, _ = _cancel(client, target)

    assert resp.status_code == 200
    assert not target.exists()
    assert inbox.exists()
