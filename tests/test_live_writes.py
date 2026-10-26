"""Tests for the write audit (tests/_live_writes.py).

Each test points the audit at a fake home under its own ``tmp_path``, so every
write it makes, and every write it expects to be recorded, stays in that
directory. Nothing here touches the real home.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _live_writes as audit


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fake home with a deployed checkout and ~/.genesis; a sibling temp dir."""
    fake = tmp_path / "home"
    (fake / "genesis").mkdir(parents=True)
    (fake / ".genesis").mkdir()
    (fake / "elsewhere").mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    root = os.path.realpath(fake)
    monkeypatch.setattr(audit._S, "real_home", root)
    monkeypatch.setattr(audit._S, "allowed_roots", (os.path.realpath(allowed),))
    monkeypatch.setattr(audit._S, "allowed_files", frozenset())
    monkeypatch.setattr(
        audit._S,
        "classes",
        (
            (os.path.join(root, ".genesis"), "~/.genesis"),
            (os.path.join(root, "genesis"), "the deployed checkout (~/genesis)"),
        ),
    )
    monkeypatch.setattr(
        audit._S, "credential_files", frozenset({os.path.join(root, "genesis", "secrets.env")})
    )
    monkeypatch.setattr(audit._S, "hits", {})
    return fake


def _hits():
    return {(label, op, Path(path).name) for _node, label, op, path in audit._S.hits}


def test_the_audit_is_armed():
    assert audit._S.installed


def test_the_session_allows_its_test_home_and_the_lock_it_took(request):
    """Installed after the closure moved HOME: the test home (under the real home
    in ``~/tmp/pytest/<pid>``) is allowed, and the lock file is the one this run
    holds, not one re-resolved under the test home."""
    from tests import _live_closure

    if _live_closure._S.home:
        # The home itself, not a parent: with the basetemp redirect the per-PID
        # leaf already contains it, which would hide a missing entry; without it
        # (CI, an explicit --basetemp) the home is the only root that covers it.
        assert os.path.realpath(_live_closure._S.home) in audit._S.allowed_roots
    lock = request.config._genesis_pytest_lock
    if lock.acquired:
        assert audit._S.allowed_files == frozenset({os.path.realpath(str(lock._path))})


def test_a_write_into_the_home_is_recorded_and_classed(home):
    (home / ".genesis" / "settings.yaml").write_text("x")
    (home / "genesis" / "data.json").write_text("x")
    (home / "elsewhere" / "note.txt").write_text("x")
    assert _hits() == {
        ("~/.genesis", "write", "settings.yaml"),
        ("the deployed checkout (~/genesis)", "write", "data.json"),
        ("elsewhere in the home", "write", "note.txt"),
    }


def test_the_test_that_wrote_is_named(home):
    (home / ".genesis" / "x").write_text("x")
    (node,) = {node for node, *_ in audit._S.hits}
    assert node.endswith("test_the_test_that_wrote_is_named")


def test_a_read_is_not_a_write(home):
    target = home / ".genesis" / "settings.yaml"
    target.write_text("x")
    audit._S.hits.clear()
    target.read_text()
    with open(target, "rb"):
        pass
    assert _hits() == set()


def test_os_open_with_write_flags_is_a_write(home):
    fd = os.open(home / ".genesis" / "raw", os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    assert ("~/.genesis", "write", "raw") in _hits()


def test_rename_mkdir_rmdir_remove_and_rmtree_are_writes(home):
    g = home / ".genesis"
    (g / "a").write_text("x")
    audit._S.hits.clear()
    os.replace(g / "a", g / "b")
    os.mkdir(g / "d")
    os.rmdir(g / "d")
    os.remove(g / "b")
    (g / "tree").mkdir()
    audit._S.hits.clear()
    shutil.rmtree(g / "tree")
    assert ("~/.genesis", "shutil.rmtree", "tree") in _hits()


def test_each_operation_is_named(home):
    g = home / ".genesis"
    (g / "a").write_text("x")
    os.replace(g / "a", g / "b")
    os.mkdir(g / "d")
    os.rmdir(g / "d")
    os.remove(g / "b")
    ops = {op for _label, op, _name in _hits()}
    assert {"write", "os.rename", "os.mkdir", "os.rmdir", "os.remove"} <= ops


def test_a_path_relative_to_a_directory_descriptor_is_resolved(home):
    g = home / ".genesis"
    (g / "victim").write_text("x")
    audit._S.hits.clear()
    fd = os.open(g, os.O_RDONLY)
    try:
        os.remove("victim", dir_fd=fd)
    finally:
        os.close(fd)
    assert ("~/.genesis", "os.remove", "victim") in _hits()


def test_a_sqlite_file_is_a_write_but_read_only_and_memory_are_not(home):
    db = home / "genesis" / "app.db"
    sqlite3.connect(db).close()
    assert ("the deployed checkout (~/genesis)", "sqlite connect", "app.db") in _hits()
    audit._S.hits.clear()
    sqlite3.connect(f"file:{db}?mode=ro", uri=True).close()
    sqlite3.connect(":memory:").close()
    assert {h for h in _hits() if h[1] == "sqlite connect"} == set()


def test_the_allowed_places_are_not_recorded(home, tmp_path):
    (tmp_path / "allowed" / "scratch").write_text("x")
    pycache = home / "genesis" / "__pycache__"
    pycache.mkdir()
    (pycache / "m.cpython-312.pyc").write_bytes(b"x")
    (home / "genesis" / ".pytest_cache").mkdir()
    assert _hits() == set()


def test_a_write_outside_the_home_is_not_recorded(home, tmp_path):
    (tmp_path / "outside").write_text("x")  # tmp_path is beside the fake home
    assert _hits() == set()


def test_removing_a_symlink_to_a_home_file_is_not_a_write_to_that_file(home, tmp_path):
    """Removing, renaming or linking acts on the NAME: a test's own symlink into
    the home is that symlink, not the file it points at."""
    target = home / "genesis" / "src"
    target.mkdir()
    link = tmp_path / "allowed" / "src"
    link.symlink_to(target)
    audit._S.hits.clear()
    os.remove(link)
    assert _hits() == set()
    assert target.is_dir()


def test_a_relative_os_open_is_judged_against_the_cwd_and_labelled(home, tmp_path, monkeypatch):
    """os.open's audit event omits dir_fd, so a relative name is judged against
    the cwd and labelled as a guess: here it actually landed in the dir_fd's
    directory, which the label tells a reader to check."""
    sandbox = tmp_path / "allowed"
    monkeypatch.chdir(home / "genesis")  # a cwd inside the home: a guess would misattribute
    fd = os.open(sandbox, os.O_RDONLY)
    try:
        os.close(os.open("made", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
    finally:
        os.close(fd)
    assert (sandbox / "made").exists()
    assert _hits() == {("relative os.open (dir_fd unknown; judged against cwd)", "write", "made")}


def test_attempts_that_change_nothing_are_not_writes(home):
    """Audit events fire before the call: a no-op or refused attempt is not a write."""
    g = home / ".genesis"
    (g / "keep").mkdir()
    (g / "full").mkdir()
    (g / "full" / "f").write_text("x")
    (g / "lock").write_text("")
    audit._S.hits.clear()
    (g / "keep").mkdir(exist_ok=True)  # already there
    (g / "absent").unlink(missing_ok=True)  # nothing to remove
    shutil.rmtree(g / "absent-tree", ignore_errors=True)
    with contextlib.suppress(OSError):
        os.rmdir(g / "full")  # not empty: refused
    os.close(os.open(g / "lock", os.O_CREAT | os.O_RDONLY))  # reopened, unchanged
    assert _hits() == set()


def test_an_unwritable_report_path_never_fails_a_run(tmp_path):
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_sub.py").write_text(_SUBRUN_TESTS)
    repo = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "PYTHONPATH": f"{repo}{os.pathsep}{repo / 'src'}",
        "GENESIS_LIVE_WRITES_REPORT": str(tmp_path / "missing-dir" / "report.tsv"),
    }
    env.pop("PYTEST_CURRENT_TEST", None)
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(tmp_path / "test_sub.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = out.stdout + out.stderr
    assert out.returncode == 0, text
    assert "could not write the report" in text, text


def test_a_credential_file_read_is_recorded(home):
    secrets = home / "genesis" / "secrets.env"
    secrets.write_text("K=v\n")
    audit._S.hits.clear()
    secrets.read_text()
    assert ("credential file read", "read", "secrets.env") in _hits()


_SUBRUN_CONFTEST = """
pytest_plugins = ["tests._live_writes"]


def pytest_configure(config):
    from tests import _live_writes

    _live_writes.install(config)
"""

_SUBRUN_TESTS = """
import os

from tests import _live_writes


def test_writes_into_a_fake_home(tmp_path):
    _live_writes._S.real_home = str(tmp_path)
    _live_writes._S.allowed_roots = ()
    (tmp_path / "f").write_text("x")
"""


def test_a_run_reports_writes_without_failing_the_test(tmp_path):
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_sub.py").write_text(_SUBRUN_TESTS)
    report = tmp_path / "report.tsv"
    repo = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "PYTHONPATH": f"{repo}{os.pathsep}{repo / 'src'}",
        "GENESIS_LIVE_WRITES_REPORT": str(report),
    }
    env.pop("PYTEST_CURRENT_TEST", None)
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(tmp_path / "test_sub.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = out.stdout + out.stderr
    assert out.returncode == 0, text
    assert "writes outside the test's temp dirs (reported, not failed)" in text, text
    lines = report.read_text().splitlines()
    assert any(line.endswith("/f") and "\twrite\t" in line for line in lines), lines
