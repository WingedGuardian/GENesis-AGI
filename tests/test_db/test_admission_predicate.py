"""Contract tests for the admission predicate.

Two properties, and both were bought by a defect rather than imagined:

* :func:`database_is_fenced` NEVER raises. Its callers are advisory hooks whose
  only answer to an exception is "skip", so a predicate that raises just moves
  the failure somewhere with less context. The pathological leg is real: on
  CPython 3.12 ``Path.resolve()`` raises ``RuntimeError`` — not ``OSError`` —
  for a symlink loop, which escaped an earlier ``except OSError`` guard.

* ``file:`` URIs resolve to the same admission domain as the plain path. This
  is the load-bearing half of ``_coerce_path``: several script openers connect
  by URI, and the quarantine reader resolves whatever it is handed, so passing
  the raw URI string yields a nonexistent path matching no marker — silently
  un-fencing exactly the read-only callers that use URIs.

The regression guards (fresh install, missing parent, relative spelling) carry
as much weight as the fence cases: a predicate that refuses a healthy database
on an ordinary install is a self-inflicted outage.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from genesis.db import admission
from genesis.db.integrity import DatabaseIntegrityError, quarantine_database


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """A scratch genesis home + a real database file, nothing quarantined."""
    home = tmp_path / "ghome"
    home.mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(home))
    db = tmp_path / "genesis.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    return db


# --- must NOT fence ordinary healthy states ------------------------------


def test_clean_state_is_not_fenced(scratch_db):
    assert admission.database_is_fenced(scratch_db) is False


def test_database_that_does_not_exist_yet_is_not_fenced(scratch_db):
    """Fresh install: the database is created later. That is not a fence."""
    assert admission.database_is_fenced(scratch_db.parent / "not-created-yet.db") is False


def test_database_under_missing_parent_is_not_fenced(scratch_db):
    assert admission.database_is_fenced(scratch_db.parent / "no-dir" / "x.db") is False


def test_relative_spelling_is_not_fenced(scratch_db, monkeypatch):
    monkeypatch.chdir(scratch_db.parent)
    assert admission.database_is_fenced("genesis.db") is False


def test_unrelated_database_is_not_fenced_by_another_quarantine(scratch_db):
    quarantine_database(scratch_db, source="test", detail="probe")
    other = scratch_db.parent / "other.db"
    other.write_bytes(b"SQLite format 3\x00")
    assert admission.database_is_fenced(other) is False


# --- must fence -----------------------------------------------------------


def test_quarantined_database_is_fenced(scratch_db):
    quarantine_database(scratch_db, source="test", detail="probe")
    assert admission.database_is_fenced(scratch_db) is True


@pytest.mark.parametrize(
    "spelling",
    [
        "file:{path}",
        "file:{path}?mode=ro",
        "file://{path}",
    ],
)
def test_file_uri_spellings_reach_the_same_domain(scratch_db, spelling):
    """A URI caller must not slip past a marker the plain path would hit."""
    quarantine_database(scratch_db, source="test", detail="probe")
    assert admission.database_is_fenced(spelling.format(path=scratch_db)) is True


def test_symlink_loop_answers_by_marker_state_and_never_raises(scratch_db):
    """The pathological path, pinned in BOTH marker states.

    On CPython 3.12 ``Path.resolve()`` raises ``RuntimeError`` — not
    ``OSError`` — for a symlink loop, which is what escaped an earlier
    ``except OSError`` guard. MEASURED here, and the two states differ for a
    real reason worth pinning rather than collapsing:

    * No marker: the quarantine reader short-circuits before resolving, so no
      exception arises and the honest answer is "not quarantined". Fencing
      here would refuse a database nothing has flagged.
    * Marker present: resolution is reached and raises, so identity cannot be
      established against the marker — and "cannot establish" is the fence
      condition.

    An earlier version of this test asserted True unconditionally. It was
    wrong, and it was wrong in the direction that hides a self-inflicted
    outage: it would have passed against a predicate that fenced every
    unusual path on a perfectly healthy install.
    """
    loop = scratch_db.parent / "loop.db"
    loop.symlink_to(loop)

    assert admission.database_is_fenced(loop) is False

    quarantine_database(scratch_db, source="test", detail="probe")
    assert admission.database_is_fenced(loop) is True


def test_path_beginning_with_file_colon_is_not_treated_as_a_uri(tmp_path, monkeypatch):
    """``file:literal.db`` is a legal POSIX filename, not a URI.

    Only a STRING may be a URI here. Stringifying a ``Path`` and sniffing the
    prefix checked ``literal.db`` while the connection factories opened the
    literal ``file:literal.db`` — so a quarantine marker on the real file was
    missed and the seam was bypassable by a legal name. Both directions are
    pinned: the Path must fence, and a genuine URI string must still decode.
    """
    home = tmp_path / "ghome"
    home.mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(home))
    (tmp_path / "file:literal.db").write_bytes(b"SQLite format 3\x00")
    quarantine_database(tmp_path / "file:literal.db", source="test", detail="probe")

    # RELATIVE on purpose. An absolute Path stringifies to "/tmp/.../file:..."
    # which does not start with "file:", so it never reaches the URI branch
    # and proves nothing — an earlier version of this test did exactly that
    # and survived the mutation it was written to catch.
    monkeypatch.chdir(tmp_path)
    literal = Path("file:literal.db")

    assert admission.database_is_fenced(literal) is True
    with pytest.raises(DatabaseIntegrityError):
        admission.assert_admitted(literal)


def test_predicate_never_raises_when_the_quarantine_reader_fails(scratch_db, monkeypatch):
    """Any unexpected failure answers True rather than escaping to a hook."""

    def _boom(_path):
        raise RuntimeError("quarantine reader exploded")

    monkeypatch.setattr(admission, "database_is_quarantined", _boom)
    assert admission.database_is_fenced(scratch_db) is True


# --- assert_admitted: the raising seam the factories use ------------------


def test_assert_admitted_passes_when_clear(scratch_db):
    admission.assert_admitted(scratch_db)  # must not raise


def test_assert_admitted_raises_when_quarantined(scratch_db):
    quarantine_database(scratch_db, source="test", detail="probe")
    with pytest.raises(DatabaseIntegrityError):
        admission.assert_admitted(scratch_db)


def test_assert_admitted_honours_uri_spellings(scratch_db):
    """The seam must not be bypassable by handing it a URI.

    Guards the same hole as the predicate test above, on the raising path that
    the connection factories actually call.
    """
    quarantine_database(scratch_db, source="test", detail="probe")
    with pytest.raises(DatabaseIntegrityError):
        admission.assert_admitted(f"file:{scratch_db}?mode=ro")


def test_assert_admitted_error_is_catchable_as_sqlite_databaseerror(scratch_db):
    """Existing handlers must keep working or this seam breaks callers."""
    quarantine_database(scratch_db, source="test", detail="probe")
    with pytest.raises(sqlite3.DatabaseError):
        admission.assert_admitted(scratch_db)
