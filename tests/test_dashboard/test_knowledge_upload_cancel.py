"""Upload cancel only ever removes the upload's own file.

``knowledge_upload_cancel`` reads a path back from the DB. The file is written
as ``_UPLOAD_DIR/<upload_id>/<name>`` at upload time; these tests pin that a row
pointing anywhere else, or reaching its file through a symlink, deletes nothing.
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


# Upload ids are uuid4 strings; the route refuses to address anything else.
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


def _cancel(client, file_path, upload_id=U1):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = MagicMock()
    row = {"id": upload_id, "status": "pending", "file_path": str(file_path)}
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.knowledge_uploads.get", new_callable=AsyncMock, return_value=row),
        patch("genesis.db.crud.knowledge_uploads.delete", new_callable=AsyncMock) as mock_delete,
    ):
        MockRT.instance.return_value = rt
        resp = client.delete(f"/api/genesis/knowledge/upload/{upload_id}")
    return resp, mock_delete


def test_cancel_removes_file_and_empty_upload_subdir(client, inbox):
    sub = inbox / U1
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
    sneaky = inbox / U1 / ".." / ".." / "keep.txt"

    resp, mock_delete = _cancel(client, sneaky)

    assert resp.status_code == 409
    assert victim.exists()
    mock_delete.assert_not_awaited()


def test_cancel_never_removes_the_upload_root_itself(client, inbox):
    """A file directly in the root is no upload's own file (uploads live in
    <root>/<upload_id>/): refused, and the root and file are left alone."""
    target = inbox / "doc.txt"
    target.write_text("x")

    resp, mock_delete = _cancel(client, target)

    assert resp.status_code == 409
    assert target.exists()
    assert inbox.exists()
    mock_delete.assert_not_awaited()


def test_cancel_refuses_another_uploads_real_file(client, inbox):
    """A corrupted row for u1 naming u2's real file (no symlink at all) must not
    delete u2's data."""
    theirs = inbox / U2 / "theirs.txt"
    theirs.parent.mkdir()
    theirs.write_text("x")

    resp, mock_delete = _cancel(client, theirs)

    assert resp.status_code == 409
    assert theirs.exists()
    mock_delete.assert_not_awaited()


def test_cancel_refuses_a_symlink_loop(client, inbox):
    """realpath returns a looping link unresolved, so it must be caught as a link."""
    sub = inbox / U1
    sub.mkdir()
    loop = sub / "doc.txt"
    loop.symlink_to(loop)

    resp, mock_delete = _cancel(client, loop)

    assert resp.status_code == 409
    assert loop.is_symlink()
    mock_delete.assert_not_awaited()


def test_cancel_of_an_already_removed_file_still_completes(client, inbox):
    """The file being gone already is the goal state, not a 500."""
    sub = inbox / U1
    sub.mkdir()

    resp, mock_delete = _cancel(client, sub / "doc.txt")

    assert resp.status_code == 200
    assert not sub.exists()
    mock_delete.assert_awaited_once()


@pytest.mark.parametrize("which", ["root", "subdir"])
def test_cancel_refuses_a_directory_path(client, inbox, which):
    """A row naming the upload root or a subdirectory is refused, not a 500 from unlink."""
    sub = inbox / U1
    sub.mkdir()
    (sub / "doc.txt").write_text("x")
    target = inbox if which == "root" else sub

    resp, mock_delete = _cancel(client, target)

    assert resp.status_code == 409
    assert (sub / "doc.txt").exists()
    mock_delete.assert_not_awaited()


def test_cancel_refuses_a_symlink_inside_that_points_outside(client, inbox, tmp_path):
    victim = tmp_path / "keep.txt"
    victim.write_text("x")
    link = inbox / "link.txt"
    link.symlink_to(victim)

    resp, mock_delete = _cancel(client, link)

    assert resp.status_code == 409
    assert victim.exists()
    assert link.is_symlink()
    mock_delete.assert_not_awaited()


def test_cancel_refuses_a_symlink_outside_that_points_inside(client, inbox, tmp_path):
    """A stored symlink outside the root that resolves to an upload inside it is
    not that upload's own path: refused, and neither file nor link is touched."""
    inside = inbox / "doc.txt"
    inside.write_text("x")
    link = tmp_path / "outside-link.txt"
    link.symlink_to(inside)

    resp, mock_delete = _cancel(client, link)

    assert resp.status_code == 409
    assert inside.exists()
    assert link.is_symlink()
    mock_delete.assert_not_awaited()


def test_cancel_refuses_a_symlink_to_another_upload(client, inbox):
    """An entry inside the root replaced by a symlink to ANOTHER upload's file:
    following it would delete the other upload's data. Refused instead."""
    other = inbox / U2 / "theirs.txt"
    other.parent.mkdir()
    other.write_text("x")
    link = inbox / U1 / "mine.txt"
    link.parent.mkdir()
    link.symlink_to(other)

    resp, mock_delete = _cancel(client, link)

    assert resp.status_code == 409
    assert other.exists()
    assert link.is_symlink()
    mock_delete.assert_not_awaited()


def test_cancel_refuses_a_symlinked_directory_inside(client, inbox):
    """A symlinked subdirectory in the stored path is a symlink too."""
    real = inbox / "real"
    real.mkdir()
    (real / "doc.txt").write_text("x")
    (inbox / "alias").symlink_to(real)

    resp, mock_delete = _cancel(client, inbox / "alias" / "doc.txt")

    assert resp.status_code == 409
    assert (real / "doc.txt").exists()
    mock_delete.assert_not_awaited()


def test_cancel_works_when_the_upload_root_itself_is_reached_through_a_symlink(
    client, tmp_path, monkeypatch
):
    """A symlinked HOME puts a symlink ABOVE the root; that is not the stored
    entry's doing, so an ordinary cancel still succeeds."""
    from genesis.dashboard.routes import knowledge_upload as upload_routes

    real_root = tmp_path / "real-inbox"
    real_root.mkdir()
    alias_root = tmp_path / "alias-inbox"
    alias_root.symlink_to(real_root)
    monkeypatch.setattr(upload_routes, "_UPLOAD_DIR", alias_root)
    sub = alias_root / U1
    sub.mkdir()
    (sub / "doc.txt").write_text("x")

    resp, mock_delete = _cancel(client, sub / "doc.txt")

    assert resp.status_code == 200
    assert not (real_root / U1).exists()
    mock_delete.assert_awaited_once()


def test_cancel_refuses_a_self_referential_upload_directory(client, inbox):
    """<root>/<upload_id> as a link to itself: a 409, not an ELOOP 500."""
    sub = inbox / U1
    sub.symlink_to(sub)

    resp, mock_delete = _cancel(client, sub / "doc.txt")

    assert resp.status_code == 409
    assert sub.is_symlink()
    mock_delete.assert_not_awaited()


def test_cancel_refuses_an_upload_directory_linked_to_another_upload(client, inbox):
    """The stored path is spelled correctly, but its directory is a link to
    another upload's directory: that upload's file must survive."""
    theirs = inbox / U2 / "doc.txt"
    theirs.parent.mkdir()
    theirs.write_text("x")
    (inbox / U1).symlink_to(inbox / U2)

    resp, mock_delete = _cancel(client, inbox / U1 / "doc.txt")

    assert resp.status_code == 409
    assert theirs.exists()
    assert (inbox / U1).is_symlink()
    mock_delete.assert_not_awaited()


def test_cancel_refuses_an_upload_id_that_is_not_a_uuid(client, inbox):
    """The upload route only mints uuid4 ids; no other id addresses a directory."""
    sub = inbox / "not-a-uuid"
    sub.mkdir()
    target = sub / "doc.txt"
    target.write_text("x")

    resp, mock_delete = _cancel(client, target, upload_id="not-a-uuid")

    assert resp.status_code == 409
    assert target.exists()
    mock_delete.assert_not_awaited()


def test_cancel_completes_when_the_upload_directory_is_already_gone(client, inbox):
    resp, mock_delete = _cancel(client, inbox / U1 / "doc.txt")

    assert resp.status_code == 200
    mock_delete.assert_awaited_once()


def test_cancel_leaves_other_files_in_the_upload_directory(client, inbox):
    sub = inbox / U1
    sub.mkdir()
    (sub / "doc.txt").write_text("x")
    (sub / "other.txt").write_text("y")

    resp, mock_delete = _cancel(client, sub / "doc.txt")

    assert resp.status_code == 200
    assert not (sub / "doc.txt").exists()
    assert (sub / "other.txt").exists()
    mock_delete.assert_awaited_once()


@pytest.mark.parametrize("tail", ["/.", "/..", "/", "/doc.txt\x00", "/doc.txt\n", "/ doc.txt"])
def test_cancel_refuses_a_stored_path_ending_in_a_sentinel_name(client, inbox, tail):
    """`<root>/<id>/.`, `..` or a trailing slash name no file the directory lists;
    treating them as "already removed" would drop the row and leave the file."""
    sub = inbox / U1
    sub.mkdir()
    target = sub / "doc.txt"
    target.write_text("x")

    resp, mock_delete = _cancel(client, str(sub) + tail)

    assert resp.status_code == 409
    assert target.exists()
    mock_delete.assert_not_awaited()


@pytest.mark.parametrize("stored", ["doc.TXT", "missing.txt"])
def test_cancel_refuses_a_stored_name_absent_while_another_file_remains(client, inbox, stored):
    """The row names a file that is not listed, but the upload's directory still
    holds one: "already removed" would drop the row and orphan the real file."""
    sub = inbox / U1
    sub.mkdir()
    real = sub / "doc.txt"
    real.write_text("x")

    resp, mock_delete = _cancel(client, sub / stored)

    assert resp.status_code == 409
    assert real.exists()
    mock_delete.assert_not_awaited()

