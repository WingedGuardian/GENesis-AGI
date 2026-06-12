"""Tests for the file browser upload and download endpoints."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def app(tmp_path):
    """Create a test Flask app with allowed roots pointing to tmp_path."""
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True

    # Patch allowed roots AND the uploads dir to use tmp_path, so upload tests
    # never touch the real ~/.genesis/uploads.
    import genesis.dashboard.routes.files as files_mod

    original_roots = files_mod._ALLOWED_ROOTS
    original_upload = files_mod._UPLOAD_DIR
    files_mod._ALLOWED_ROOTS = [tmp_path]
    files_mod._UPLOAD_DIR = tmp_path / "uploads"
    yield app
    files_mod._ALLOWED_ROOTS = original_roots
    files_mod._UPLOAD_DIR = original_upload


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


# ── Upload endpoint ───────────────────────────────────────────────────


def _post_upload(client, content: bytes, filename: str, relpath: str | None = None):
    """Helper to POST a multipart file upload, optionally with a relpath."""
    data = {"file": (BytesIO(content), filename)}
    if relpath is not None:
        data["relpath"] = relpath
    return client.post(
        "/api/genesis/files/upload", data=data, content_type="multipart/form-data"
    )


def test_upload_single_file(client, tmp_path):
    """A plain file (no relpath) lands directly in the uploads root."""
    resp = _post_upload(client, b"hello world", "notes.txt")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["filename"] == "notes.txt"
    dest = tmp_path / "uploads" / "notes.txt"
    assert dest.read_bytes() == b"hello world"


def test_upload_preserves_folder_structure(client, tmp_path):
    """A relpath recreates the folder tree under the uploads root."""
    resp = _post_upload(
        client, b"data", "notes.txt", relpath="FDE Project Challenge/data/notes.txt"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["filename"] == "FDE Project Challenge/data/notes.txt"
    dest = tmp_path / "uploads" / "FDE Project Challenge" / "data" / "notes.txt"
    assert dest.read_bytes() == b"data"


def test_upload_relpath_traversal_stays_contained(client, tmp_path):
    """Traversal segments (..) are dropped; the file cannot escape uploads."""
    resp = _post_upload(client, b"x", "evil.txt", relpath="../../etc/evil.txt")
    assert resp.status_code == 200
    body = resp.get_json()
    # ".." segments dropped → lands at uploads/etc/evil.txt, still contained.
    uploads = (tmp_path / "uploads").resolve()
    assert uploads in Path(body["path"]).resolve().parents
    assert (tmp_path / "uploads" / "etc" / "evil.txt").exists()
    # Nothing was written outside the uploads sandbox.
    assert not (tmp_path / "etc").exists()


def test_upload_absolute_relpath_reinterpreted_under_uploads(client, tmp_path):
    """An absolute-looking relpath is treated as relative to the uploads root."""
    resp = _post_upload(client, b"x", "passwd", relpath="/etc/passwd")
    assert resp.status_code == 200
    assert (tmp_path / "uploads" / "etc" / "passwd").exists()


def test_upload_deduplicates_within_folder(client, tmp_path):
    """Uploading the same relpath twice keeps both via -1 suffix."""
    _post_upload(client, b"first", "notes.txt", relpath="Proj/notes.txt")
    resp2 = _post_upload(client, b"second", "notes.txt", relpath="Proj/notes.txt")
    assert resp2.status_code == 200
    assert (tmp_path / "uploads" / "Proj" / "notes.txt").read_bytes() == b"first"
    assert (tmp_path / "uploads" / "Proj" / "notes-1.txt").read_bytes() == b"second"


def test_upload_blocked_leaf_name(client, tmp_path):
    """A blocked filename (secrets.env) as the leaf is rejected with 403."""
    resp = _post_upload(client, b"SECRET=1", "secrets.env", relpath="Proj/secrets.env")
    assert resp.status_code == 403


def test_upload_too_large(client, tmp_path):
    """A file exceeding the upload size limit returns 413."""
    import genesis.dashboard.routes.files as files_mod

    original_limit = files_mod._MAX_UPLOAD_SIZE
    files_mod._MAX_UPLOAD_SIZE = 100
    try:
        resp = _post_upload(client, b"x" * 200, "big.txt")
        assert resp.status_code == 413
    finally:
        files_mod._MAX_UPLOAD_SIZE = original_limit
