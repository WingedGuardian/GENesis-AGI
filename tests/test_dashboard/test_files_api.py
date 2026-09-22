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

    original_roots = files_mod._ALLOWED_ROOTS
    original_upload = files_mod._UPLOAD_DIR
    files_mod._ALLOWED_ROOTS = [tmp_path]
    files_mod._UPLOAD_DIR = tmp_path / "uploads"

    # Move the isolated database OUT of the allowed root.
    #
    # conftest's autouse `_isolate_genesis_db_path` points genesis_db_path() at
    # `tmp_path / "isolated-genesis.db"`, and this fixture makes `tmp_path` the
    # allowed root — so without this, the allowed root IS the database's
    # directory and every file in it is correctly refused, failing tests that
    # have nothing to do with databases. Park it in a sibling directory so the
    # two concerns stop overlapping; tests that want the database-area rule
    # exercised point at `db_area` explicitly.
    mp = pytest.MonkeyPatch()
    mp.setattr("genesis.env.genesis_db_path", lambda: tmp_path.parent / "db_area" / "genesis.db")

    yield app

    mp.undo()
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


def test_an_ordinary_held_file_is_still_readable(client, tmp_path):
    """Holding a descriptor is not by itself grounds to refuse.

    Review named the regression this prevents: normal bootstrap installs a
    RotatingFileHandler on ~/genesis/logs/genesis.log, so an identity check that
    matched ANY open inode made the current log return 403 on every route —
    read, download, write, delete and rename. Closing a descriptor is only
    dangerous when the process holds SQLite locks on that inode, so only
    database-shaped descriptors are candidates.
    """
    log = tmp_path / "genesis.log"
    log.write_text("a log line")

    fh = open(log, "a")  # noqa: SIM115 — the open handler IS the condition
    try:
        resp = client.get(f"/api/genesis/files/read?path={log}")
        assert resp.status_code == 200, "an open log file was refused"
        assert resp.get_json()["content"] == "a log line"
    finally:
        fh.close()


def test_a_directory_named_like_a_database_is_still_usable(client, tmp_path):
    """A directory cannot be opened as a database, so the name rule must not claim it."""
    for name in ("project.db", "fixtures.sqlite"):
        d = tmp_path / name
        d.mkdir()
        (d / "note.txt").write_text("x")
        resp = client.get(f"/api/genesis/files?path={d}")
        assert resp.status_code == 200, f"directory {name} was refused"


def test_an_unreadable_subtree_refuses_the_rename(client, tmp_path):
    """Fail CLOSED when the tree cannot be fully inspected.

    Path.rglob silently omits directories it cannot descend into, so the guard
    could return False having never seen part of the tree while claiming to fail
    closed. Review named the case: a live database under a subdirectory that
    later loses search permission.
    """
    tree = tmp_path / "tree"
    private = tree / "private"
    private.mkdir(parents=True)
    (private / "live.db").write_bytes(b"SQLite format 3\x00")
    private.chmod(0o000)
    try:
        resp = client.post(
            "/api/genesis/files/rename",
            json={"path": str(tree), "new_name": "tree-old"},
        )
        if os.geteuid() == 0:
            pytest.skip("running as root — permission bits do not restrict the walk")
        assert resp.status_code == 403, "an uninspectable subtree was waved through"
        assert tree.exists()
    finally:
        private.chmod(0o755)


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


# ── The configured database area ──────────────────────────────────────────
#
# The one AUTHORITATIVE rule: the path comes from config rather than from a
# guess about names, so it covers the database under ANY spelling — including
# the extensionless form genesis.env supports — and every sidecar beside it.


@pytest.fixture()
def db_area(tmp_path, monkeypatch):
    """Point the configured database INSIDE the allowed root, and return it."""
    area = tmp_path / "data"
    area.mkdir()
    db = area / "configured-db-no-extension"  # deliberately unrecognisable by name
    db.write_bytes(b"SQLite format 3\x00")
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)
    return db


@pytest.mark.parametrize("route,method", [
    ("/api/genesis/files/read", "get"),
    ("/api/genesis/files/download", "get"),
    ("/api/genesis/files/delete", "delete"),
    ("/api/genesis/files/write", "put"),
])
def test_the_configured_database_is_refused_under_any_name(client, db_area, route, method):
    """Named so the suffix rule cannot possibly match — config is what catches it."""
    assert not files_mod._is_sqlite_artifact(db_area.name), "fixture no longer isolates config"
    before = db_area.stat().st_size

    if method == "put":
        resp = client.put(route, json={"path": str(db_area), "content": "x"})
    else:
        resp = getattr(client, method)(f"{route}?path={db_area}")

    assert resp.status_code == 403, f"{route} reached the configured database"
    assert db_area.exists() and db_area.stat().st_size == before


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal", "-mj7b3a91e0"])
def test_sidecars_of_the_configured_database_are_refused(client, db_area, sidecar):
    """Sidecars inherit the base name, so a suffix rule cannot see them either.

    This is the case review raised twice: a WAL sidecar does not begin with the
    SQLite magic and its base name carries no recognised suffix. Being BESIDE
    the configured database is what catches it.
    """
    side = db_area.parent / (db_area.name + sidecar)
    side.write_bytes(b"\x37\x7f\x06\x82" + b"\x00" * 64)  # a WAL header, not magic

    resp = client.get(f"/api/genesis/files/read?path={side}")
    assert resp.status_code == 403, f"{sidecar} was reachable"


def test_renaming_the_database_directory_is_refused(client, db_area):
    """Moving the parent relocates the database without ever opening it."""
    resp = client.post(
        "/api/genesis/files/rename",
        json={"path": str(db_area.parent), "new_name": "data-old"},
    )
    assert resp.status_code == 403
    assert db_area.parent.exists()


def test_files_outside_the_database_area_are_unaffected(client, db_area, tmp_path):
    """Control: the rule is scoped to that directory, not to the whole root."""
    notes = tmp_path / "notes.md"
    notes.write_text("still readable")

    resp = client.get(f"/api/genesis/files/read?path={notes}")
    assert resp.status_code == 200
    assert resp.get_json()["content"] == "still readable"
