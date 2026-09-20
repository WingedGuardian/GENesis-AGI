"""Error-leg and key-identity behaviour of the admission predicate.

Every case here was MEASURED against a real filesystem before it was written
down, several of them because the obvious implementation got them wrong:

* ``Path.exists()`` cannot express this contract — it follows symlinks (so a
  dangling marker reads as absent) and answers False for some errors.
* CPython 3.12's ``Path.resolve(strict=False)`` raises ``RuntimeError`` (not
  ``OSError``) on a symlink loop, so a "catch OSError" fence let the loop
  escape as an exception instead of fencing.
* Fixing that escape by simply ignoring the failed resolution was WORSE: it
  answered "not fenced" for a path whose identity was unknowable — a silent
  fail-OPEN in the one function whose whole contract is fail-closed.

The regression guards (fresh install, missing parent, relative spelling) are
as load-bearing as the fence cases: an over-eager fence that refuses a healthy
database on a normal install is a self-inflicted outage.
"""

from __future__ import annotations

import os

import pytest

from genesis.db import admission


@pytest.fixture
def fenced_env(tmp_path, monkeypatch):
    """A scratch genesis home + a real database file, no markers."""
    home = tmp_path / "ghome"
    home.mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(home))
    db = tmp_path / "genesis.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    return db


def _write_marker(db_path) -> None:
    marker = admission.maintenance_marker_path(db_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")


# --- the fence must NOT fire on ordinary healthy states -------------------


def test_clean_state_is_not_fenced(fenced_env):
    assert admission.database_is_fenced(fenced_env) is False


def test_database_that_does_not_exist_yet_is_not_fenced(fenced_env):
    """Fresh install: the database is created later, that is not a fence."""
    assert admission.database_is_fenced(fenced_env.parent / "not-created-yet.db") is False


def test_database_under_missing_parent_is_not_fenced(fenced_env):
    assert admission.database_is_fenced(fenced_env.parent / "no-dir" / "x.db") is False


def test_relative_spelling_is_not_fenced(fenced_env, monkeypatch):
    monkeypatch.chdir(fenced_env.parent)
    assert admission.database_is_fenced("genesis.db") is False


def test_unrelated_database_is_not_fenced_by_another_marker(fenced_env):
    _write_marker(fenced_env)
    other = fenced_env.parent / "other.db"
    other.write_bytes(b"SQLite format 3\x00")
    assert admission.database_is_fenced(other) is False


# --- the fence must fire, including on every error leg --------------------


def test_marker_presence_fences(fenced_env):
    _write_marker(fenced_env)
    assert admission.database_is_fenced(fenced_env) is True


def test_dangling_symlink_marker_fences(fenced_env):
    """``Path.exists()`` would call this absent; lstat does not."""
    marker = admission.maintenance_marker_path(fenced_env)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.symlink_to(fenced_env.parent / "target-that-does-not-exist")
    assert admission.database_is_fenced(fenced_env) is True


def test_marker_path_as_directory_fences(fenced_env):
    marker = admission.maintenance_marker_path(fenced_env)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.mkdir()
    assert admission.database_is_fenced(fenced_env) is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unsearchable_admission_dir_fences_both_ways(fenced_env):
    """State that cannot be established must read as fenced, marker or not."""
    directory = admission.admission_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for marker_present in (True, False):
        if marker_present:
            _write_marker(fenced_env)
        os.chmod(directory, 0o000)
        try:
            assert admission.database_is_fenced(fenced_env) is True
        finally:
            os.chmod(directory, 0o755)
        admission.maintenance_marker_path(fenced_env).unlink(missing_ok=True)


def test_symlink_loop_fences_rather_than_raising(fenced_env):
    """3.12 raises RuntimeError here; the predicate must still answer True."""
    loop = fenced_env.parent / "loop.db"
    loop.symlink_to(loop)
    assert admission.database_is_fenced(loop) is True


def test_symlink_loop_fences_at_the_INNER_layer_too(fenced_env):
    """Pin ``maintenance_fence_active`` directly, not through the outer net.

    ``database_is_fenced`` ends in a catch-all that fences on any exception,
    which is correct — and which means the test above CANNOT distinguish "the
    key logic fenced deliberately" from "the key logic blew up and the safety
    net caught it". MEASURED: a mutation making the partial-key path fail OPEN
    left the test above green, because the resulting AttributeError fell
    through to that catch-all.

    So assert one layer down, where there is no net: an unresolvable identity
    must be a decision to fence, not an accident that happens to land there.
    """
    loop = fenced_env.parent / "loop2.db"
    loop.symlink_to(loop)
    assert admission.maintenance_fence_active(loop) is True


def test_unresolvable_identity_refuses_to_produce_a_partial_key(fenced_env):
    """Half an identity is not an identity: ``_domain_keys`` must refuse.

    A marker can sit under either spelling, so checking only the spelling that
    happened to compute is checking the wrong half and reporting a confident
    answer. The contract is "raise, and let the fail-closed caller fence".
    """
    loop = fenced_env.parent / "loop3.db"
    loop.symlink_to(loop)
    with pytest.raises(OSError):
        admission._domain_keys(loop)


# --- key identity: the fence must survive the swap it guards --------------


def test_marker_survives_retargeting_a_symlinked_database(fenced_env):
    """The exact moment the maintenance fence exists for.

    A marker written for ``link -> target1`` must still fence after the link
    is repointed, because that repoint IS the replacement the owner is doing.
    Keying on the resolved path alone loses the marker mid-swap.
    """
    target1 = fenced_env.parent / "target1.db"
    target1.write_bytes(b"SQLite format 3\x00")
    target2 = fenced_env.parent / "target2.db"
    target2.write_bytes(b"SQLite format 3\x00")
    link = fenced_env.parent / "live.db"
    link.symlink_to(target1)

    _write_marker(link)
    assert admission.database_is_fenced(link) is True

    link.unlink()
    link.symlink_to(target2)
    assert admission.database_is_fenced(link) is True


def test_marker_on_target_also_fences_the_alias(fenced_env):
    """The opposite hole: one database must not become two fence domains."""
    target = fenced_env.parent / "real.db"
    target.write_bytes(b"SQLite format 3\x00")
    link = fenced_env.parent / "alias.db"
    link.symlink_to(target)

    _write_marker(target)
    assert admission.database_is_fenced(link) is True


def test_regular_file_uses_a_single_key(fenced_env):
    """The dual-key check costs nothing in the ordinary case."""
    assert len(admission.maintenance_marker_paths(fenced_env)) == 1


def test_file_uri_spellings_resolve_to_the_same_domain(fenced_env):
    _write_marker(fenced_env)
    assert admission.database_is_fenced(f"file:{fenced_env}") is True
    assert admission.database_is_fenced(f"file:{fenced_env}?mode=ro") is True


# --- assert_admitted: the raising seam used by the factories --------------


def test_assert_admitted_passes_when_clear(fenced_env):
    admission.assert_admitted(fenced_env)  # must not raise


def test_assert_admitted_raises_fenced_error_on_marker(fenced_env):
    _write_marker(fenced_env)
    with pytest.raises(admission.DatabaseFencedError) as excinfo:
        admission.assert_admitted(fenced_env)
    # The message must point the operator at what to inspect: this exception
    # is read mid-incident, by someone deciding whether to delete a marker.
    assert str(admission.admission_dir()) in str(excinfo.value)


def test_fenced_error_is_catchable_as_the_existing_types(fenced_env):
    """Existing handlers must keep working, or this change breaks callers.

    Every current ``except DatabaseIntegrityError`` / ``except
    sqlite3.DatabaseError`` around a connection factory has to keep catching,
    otherwise adding the maintenance fence turns handled refusals into
    escaping exceptions.
    """
    import sqlite3

    from genesis.db.integrity import DatabaseIntegrityError

    _write_marker(fenced_env)
    with pytest.raises(DatabaseIntegrityError):
        admission.assert_admitted(fenced_env)
    with pytest.raises(sqlite3.DatabaseError):
        admission.assert_admitted(fenced_env)
