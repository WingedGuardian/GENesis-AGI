"""Tests for the file browser upload and download endpoints."""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

import pytest
from flask import Flask

import genesis.dashboard.routes.files as files_mod
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


def test_upload_subdir_collides_with_existing_file(client, tmp_path):
    """A relpath subdir colliding with an existing file returns a clean 400, not 500."""
    # Pre-create a regular file where the folder upload wants a directory.
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "data").write_text("i am a file, not a dir")

    resp = _post_upload(client, b"x", "x.txt", relpath="data/x.txt")
    assert resp.status_code == 400
    assert "save" in resp.get_json()["error"].lower()


def test_upload_blocked_subdir_creates_no_orphan_dir(client, tmp_path):
    """A relpath whose subdir is blocked is rejected WITHOUT creating the dir."""
    resp = _post_upload(client, b"x", "x.txt", relpath="topsecret/x.txt")
    assert resp.status_code == 403
    # The rejected upload must not have created the directory on disk.
    assert not (tmp_path / "uploads" / "topsecret").exists()


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


# ── SQLite artifacts must be unreachable through every file route ──────────
#
# Not a privacy rule. These routes run inside genesis-server, which holds POSIX
# locks on the live database, and POSIX releases ALL of a process's locks on a
# file when that process closes ANY descriptor to it. So a single READ through
# this API drops the server's lock and seeds the WAL split-brain that malformed
# a production database in under two minutes. The -shm is 32 KB, comfortably
# under _MAX_FILE_SIZE, so the size cap never stood in the way.


@pytest.mark.parametrize(
    "name",
    [
        "genesis.db",
        "genesis.db-wal",
        "genesis.db-shm",
        "genesis.db-journal",
        "queue.sqlite",
        "index.sqlite3",
        "GENESIS.DB",  # case-insensitive
        "other.sqlite-shm",
    ],
)
def test_sqlite_artifacts_are_refused_by_read(client, tmp_path, name):
    (tmp_path / name).write_bytes(b"SQLite format 3\x00")
    resp = client.get(f"/api/genesis/files/read?path={tmp_path / name}")
    assert resp.status_code == 403, f"{name} was readable — this opens it in-process"


def _db_file(tmp_path, name="genesis.db"):
    db = tmp_path / name
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 4096)
    return db


@pytest.mark.parametrize("route,method", [
    ("/api/genesis/files/read", "get"),
    ("/api/genesis/files/download", "get"),
    ("/api/genesis/files/delete", "delete"),
    ("/api/genesis/files/write", "put"),
])
def test_every_path_route_refuses_a_database(client, tmp_path, route, method):
    """Assert on the ROUTES, not just the helper — and cover the MUTATING ones.

    An earlier revision of this test covered only read/download/delete. An
    adversarial review then narrowed the block to
    ``... and request.method in ("GET", "DELETE")``, kept the whole file GREEN at
    34 passed, and truncated a database through ``PUT /files/write`` from 8192
    bytes to 5. A refusal test that omits the routes which WRITE is not testing
    the thing that loses data.
    """
    db = _db_file(tmp_path)
    before = db.stat().st_size

    if method == "put":
        resp = client.put(route, json={"path": str(db), "content": "x"})
    else:
        resp = getattr(client, method)(f"{route}?path={db}")

    assert resp.status_code == 403, f"{route} reached the database"
    assert db.exists(), f"{route} deleted the database despite refusing"
    # The size assertion is what makes the write case non-vacuous: a 403 alone
    # would not catch a route that refuses AFTER writing.
    assert db.stat().st_size == before, f"{route} MODIFIED the database"


def test_create_and_rename_refuse_database_destinations(client, tmp_path):
    """The two routes that CONSTRUCT a destination rather than receiving one."""
    resp = client.post(
        "/api/genesis/files/create",
        json={"path": str(tmp_path / "new.db"), "content": "x"},
    )
    assert resp.status_code == 403
    assert not (tmp_path / "new.db").exists()

    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    resp = client.post(
        "/api/genesis/files/rename",
        json={"path": str(plain), "new_name": "new.db"},
    )
    assert resp.status_code == 403
    assert plain.exists(), "rename moved the file despite refusing"


def test_hardlink_to_a_database_is_refused(client, tmp_path):
    """resolve() cannot see a hardlink — only the identity check can.

    A hardlink is not an alias, it is a second name for the same inode, so the
    resolved NAME is innocent. MEASURED before the identity check existed: such
    a file read 200, downloaded 200, and was truncated 8192 -> 9 by a write.
    """
    db = _db_file(tmp_path)
    link = tmp_path / "notes.txt"
    try:
        os.link(db, link)
    except OSError:
        pytest.skip("hardlinks unsupported here")

    fh = open(db, "rb")  # noqa: SIM115 — the point is to HOLD it open
    try:
        before = db.stat().st_size
        assert client.get(f"/api/genesis/files/read?path={link}").status_code == 403
        resp = client.put(
            "/api/genesis/files/write", json={"path": str(link), "content": "x"}
        )
        assert resp.status_code == 403
        assert db.stat().st_size == before, "the database was truncated via a hardlink"
    finally:
        fh.close()


def test_a_held_database_is_refused_under_any_name(client, tmp_path):
    """The identity check, isolated: no suffix, no recognisable name.

    This is the case the suffix list provably misses — a database configured
    through GENESIS_DB_PATH with no extension, or a Chromium profile DB named
    ``Cookies``. It is refused because we HOLD it, not because of what it is
    called.
    """
    odd = tmp_path / "Cookies"
    odd.write_bytes(b"SQLite format 3\x00")
    assert not files_mod._is_sqlite_artifact(odd.name), "fixture no longer isolates identity"

    fh = open(odd, "rb")  # noqa: SIM115 — holding it open IS the condition
    try:
        assert client.get(f"/api/genesis/files/read?path={odd}").status_code == 403
    finally:
        fh.close()

    # And once nothing holds it, the name-only rule does not claim it.
    assert client.get(f"/api/genesis/files/read?path={odd}").status_code == 200


def test_renaming_a_directory_containing_a_database_is_refused(client, tmp_path):
    """Moving the parent relocates the database without ever opening it.

    Outside the literal "never open a database" rule, same data loss: the server
    keeps writing through descriptors whose directory entry moved, and the next
    connection creates a fresh empty database at the original path. MEASURED
    reachable (200) before the guard.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _db_file(data_dir)

    resp = client.post(
        "/api/genesis/files/rename",
        json={"path": str(data_dir), "new_name": "data-old"},
    )
    assert resp.status_code == 403
    assert data_dir.exists(), "the data directory was moved"
    assert not (tmp_path / "data-old").exists()


def test_renaming_an_ordinary_directory_still_works(client, tmp_path):
    """Control for the guard above — it must not freeze the file browser."""
    plain_dir = tmp_path / "notes"
    plain_dir.mkdir()
    (plain_dir / "a.md").write_text("x")

    resp = client.post(
        "/api/genesis/files/rename",
        json={"path": str(plain_dir), "new_name": "notes-old"},
    )
    assert resp.status_code == 200
    assert (tmp_path / "notes-old").is_dir()


def test_symlink_to_a_database_is_refused(client, tmp_path):
    """The check runs on the RESOLVED name, so an innocent-looking link fails."""
    real = tmp_path / "genesis.db"
    real.write_bytes(b"SQLite format 3\x00")
    link = tmp_path / "notes.txt"
    link.symlink_to(real)

    resp = client.get(f"/api/genesis/files/read?path={link}")
    assert resp.status_code == 403


def test_ordinary_files_beside_a_database_still_work(client, tmp_path):
    """Control: the rule must block the database CLASS, not the directory.

    Without this, a rule that refused everything would pass every assertion
    above while breaking the file browser entirely.
    """
    (tmp_path / "genesis.db").write_bytes(b"SQLite format 3\x00")
    notes = tmp_path / "notes.md"
    notes.write_text("still readable")

    resp = client.get(f"/api/genesis/files/read?path={notes}")
    assert resp.status_code == 200
    assert resp.get_json()["content"] == "still readable"


@pytest.mark.parametrize("name", ["report.db.txt", "database.md", "sqlite-notes.txt"])
def test_lookalike_names_are_not_over_blocked(client, tmp_path, name):
    """Names that merely mention a database are ordinary files."""
    (tmp_path / name).write_text("ok")
    resp = client.get(f"/api/genesis/files/read?path={tmp_path / name}")
    assert resp.status_code == 200
