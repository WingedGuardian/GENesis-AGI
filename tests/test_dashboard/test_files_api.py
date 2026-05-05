"""Tests for the file browser download endpoint."""

from __future__ import annotations

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def app(tmp_path):
    """Create a test Flask app with allowed roots pointing to tmp_path."""
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True

    # Patch allowed roots to use tmp_path
    import genesis.dashboard.routes.files as files_mod

    original_roots = files_mod._ALLOWED_ROOTS
    files_mod._ALLOWED_ROOTS = [tmp_path]
    yield app
    files_mod._ALLOWED_ROOTS = original_roots


@pytest.fixture()
def client(app):
    return app.test_client()


def test_download_valid_file(client, tmp_path):
    """Download a file that exists within allowed roots."""
    test_file = tmp_path / "hello.txt"
    test_file.write_text("hello world")

    resp = client.get(f"/api/genesis/files/download?path={test_file}")
    assert resp.status_code == 200
    assert resp.data == b"hello world"
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert "hello.txt" in resp.headers.get("Content-Disposition", "")


def test_download_missing_path_param(client):
    """Download without path parameter returns 400."""
    resp = client.get("/api/genesis/files/download")
    assert resp.status_code == 400


def test_download_nonexistent_file(client, tmp_path):
    """Download a file that does not exist returns 404."""
    resp = client.get(f"/api/genesis/files/download?path={tmp_path / 'nope.txt'}")
    assert resp.status_code == 404


def test_download_blocked_file(client, tmp_path):
    """Download a blocked filename (secrets.env) returns 403."""
    blocked = tmp_path / "secrets.env"
    blocked.write_text("SECRET=abc")

    resp = client.get(f"/api/genesis/files/download?path={blocked}")
    assert resp.status_code == 403


def test_download_path_traversal(client, tmp_path):
    """Path traversal outside allowed roots returns 403."""
    resp = client.get("/api/genesis/files/download?path=/etc/passwd")
    assert resp.status_code == 403


def test_download_directory(client, tmp_path):
    """Attempting to download a directory returns 404 (not a file)."""
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    resp = client.get(f"/api/genesis/files/download?path={subdir}")
    assert resp.status_code == 404


def test_download_binary_file(client, tmp_path):
    """Download a binary file preserves content exactly."""
    binary_content = bytes(range(256))
    test_file = tmp_path / "data.bin"
    test_file.write_bytes(binary_content)

    resp = client.get(f"/api/genesis/files/download?path={test_file}")
    assert resp.status_code == 200
    assert resp.data == binary_content
    assert resp.content_type == "application/octet-stream"


def test_download_too_large(client, tmp_path):
    """Files exceeding the upload size limit return 413."""
    import genesis.dashboard.routes.files as files_mod

    original_limit = files_mod._MAX_UPLOAD_SIZE
    files_mod._MAX_UPLOAD_SIZE = 100  # 100 bytes for testing
    try:
        big_file = tmp_path / "big.txt"
        big_file.write_bytes(b"x" * 200)

        resp = client.get(f"/api/genesis/files/download?path={big_file}")
        assert resp.status_code == 413
    finally:
        files_mod._MAX_UPLOAD_SIZE = original_limit


def test_download_symlink_to_blocked(client, tmp_path):
    """Symlink to a blocked file is still blocked (resolve follows symlinks)."""
    blocked = tmp_path / "secrets.env"
    blocked.write_text("SECRET=abc")
    link = tmp_path / "sneaky.txt"
    link.symlink_to(blocked)

    resp = client.get(f"/api/genesis/files/download?path={link}")
    assert resp.status_code == 403
