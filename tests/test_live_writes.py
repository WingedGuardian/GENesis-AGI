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
    if not audit._S.installed:
        pytest.skip(f"the audit is not armed here ({audit._S.disarmed or 'not installed'})")
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
    monkeypatch.setattr(audit._S, "faults", 0)
    monkeypatch.setattr(audit._S, "credential_inodes", frozenset())
    return fake


def _hits():
    return {(label, op, Path(path).name) for _node, label, op, path in audit._S.hits}


@pytest.mark.parametrize("operation", ["remove", "rename", "mkdir", "rmdir", "rmtree", "link", "symlink"])
def test_name_operations_resolve_symlink_parent_before_dotdot(home, operation):
    live = home / ".genesis"
    (live / "nested").mkdir()
    allowed = home.parent / "allowed"
    link = allowed / "link"
    link.symlink_to(live / "nested", target_is_directory=True)
    victim = live / "victim"
    target = str(link / ".." / "victim")
    source = allowed / "source"
    source.write_bytes(b"owned")
    if operation in {"remove", "rename"}:
        victim.write_bytes(b"owned")
    elif operation in {"rmdir", "rmtree"}:
        victim.mkdir()
    audit._S.hits.clear()

    if operation == "remove":
        os.remove(target)
    elif operation == "rename":
        os.rename(target, allowed / "renamed")
    elif operation == "mkdir":
        os.mkdir(target)
    elif operation == "rmdir":
        os.rmdir(target)
    elif operation == "rmtree":
        shutil.rmtree(target)
    elif operation == "link":
        os.link(source, target)
    else:
        os.symlink(source, target)

    event = "shutil.rmtree" if operation == "rmtree" else f"os.{operation}"
    assert ("~/.genesis", event, "victim") in _hits()
    if operation in {"remove", "rename", "rmdir", "rmtree"}:
        assert not victim.exists()
    elif operation == "mkdir":
        assert victim.is_dir()
    else:
        assert victim.read_bytes() == b"owned"


def test_descriptor_truncate_records_its_protected_target(home):
    target = home / ".genesis" / "descriptor"
    target.write_bytes(b"owned")
    with target.open("r+b") as handle:
        audit._S.hits.clear()
        os.ftruncate(handle.fileno(), 1)
    assert target.read_bytes() == b"o"
    assert ("~/.genesis", "os.truncate", "descriptor") in _hits()


def test_valid_descriptor_without_proc_visibility_counts_uncertainty(home, monkeypatch):
    target = home / ".genesis" / "hidden-descriptor"
    target.write_bytes(b"owned")
    with target.open("r+b") as handle:
        descriptor = handle.fileno()
        readlink = audit.os.readlink

        def unavailable(path, *args, **kwargs):
            if path == f"/proc/self/fd/{descriptor}":
                raise FileNotFoundError(path)
            return readlink(path, *args, **kwargs)

        monkeypatch.setattr(audit.os, "readlink", unavailable)
        audit._S.hits.clear()
        os.ftruncate(descriptor, 1)
    assert target.read_bytes() == b"o"
    assert not audit._S.hits
    assert audit._S.faults == 1


def test_invalid_descriptor_is_not_an_unknown_write(home):
    audit._hook("os.truncate", (-1, 1))
    assert not audit._S.hits
    assert audit._S.faults == 0


def test_hook_uses_private_os_and_transitive_path_bindings(home, monkeypatch):
    directory = home / ".genesis" / "empty-dir"
    directory.mkdir()
    nested = home / ".genesis" / "nested"
    nested.mkdir()
    (home / ".genesis" / "pointer").symlink_to(nested, target_is_directory=True)
    modules = {name: sys.modules[name] for name in ("os", "posixpath", "genericpath")}
    names = ("stat", "lstat", "listdir", "getcwd", "readlink")
    helpers = {name: getattr(os, name) for name in names}
    audit._observer_os()
    assert all(sys.modules[name] is module for name, module in modules.items())
    assert all(getattr(os, name) is helper for name, helper in helpers.items())
    monkeypatch.chdir(home / ".genesis")
    audit._S.hits.clear()

    def global_helper_reached(*args, **kwargs):
        raise AssertionError("observer reached a mutable global OS helper")

    with pytest.MonkeyPatch.context() as global_patch:
        for name in names:
            global_patch.setattr(os, name, global_helper_reached)
        audit._hook("open", ("pointer/../private-os", "w", os.O_WRONLY))
        audit._hook("os.rmdir", (str(directory), -1))
        with pytest.raises(AssertionError, match="mutable global OS helper"):
            os.getcwd()

    assert ("~/.genesis", "write", "private-os") in _hits()
    assert ("~/.genesis", "os.rmdir", "empty-dir") in _hits()
    assert audit._S.faults == 0
    assert all(getattr(os, name) is helper for name, helper in helpers.items())


@pytest.mark.parametrize("failure", [RuntimeError, pytest.fail.Exception])
def test_judging_fault_is_counted_without_escaping(home, monkeypatch, failure):
    monkeypatch.setattr(audit._S, "faults", 0, raising=False)

    def broken(*args):
        raise failure("owned judging fault")

    with pytest.MonkeyPatch.context() as injection:
        injection.setattr(audit, "_judge", broken)
        audit._hook("os.mkdir", ("owned", 0o700, -1))
    assert audit._S.faults == 1


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_hook_preserves_operator_interrupts(home, monkeypatch, signal):
    def interrupted(*args):
        raise signal()

    monkeypatch.setattr(audit, "_judge", interrupted)
    with pytest.raises(signal):
        audit._hook("os.mkdir", ("owned", 0o700, -1))


def test_fault_count_reaches_controller_and_summary(home, monkeypatch):
    monkeypatch.setattr(audit._S, "faults", 3, raising=False)
    monkeypatch.setattr(audit._S, "merged_workers", set())
    monkeypatch.setattr(audit._S, "unreported", [])
    config = type("C", (), {"workeroutput": {}})()
    audit.pytest_sessionfinish(type("S", (), {"config": config})(), 0)
    audit._S.faults = 0
    node = type("N", (), {"gateway": type("G", (), {"id": "gw0"})(), "workeroutput": config.workeroutput})()
    audit.pytest_testnodedown(node, None)
    assert audit._S.faults == 3
    monkeypatch.setattr(audit._S, "hits", {})
    monkeypatch.delenv("GENESIS_LIVE_WRITES_REPORT", raising=False)
    lines = []
    reporter = type("R", (), {"config": object(), "write_line": lambda self, line, **kw: lines.append(line)})()
    audit.pytest_terminal_summary(reporter)
    assert any("3 events could not be classified" in line for line in lines)


def test_the_audit_is_armed():
    if audit._S.disarmed:
        pytest.skip(f"the audit is disarmed here: {audit._S.disarmed}")
    assert audit._S.installed


def test_the_session_allows_its_test_home_and_the_lock_it_took(request):
    """Installed after the closure: its test home, when it gives the session one
    (under the real home in ``~/tmp/pytest/<pid>``), is allowed, and the lock file
    is the one this run holds, not one re-resolved under a test home."""
    from tests import _live_closure

    test_home = getattr(_live_closure._S, "home", None)
    if test_home:
        # The home itself, not a parent: with the basetemp redirect the per-PID
        # leaf already contains it, which would hide a missing entry; without it
        # (CI, an explicit --basetemp) the home is the only root that covers it.
        assert os.path.realpath(test_home) in audit._S.allowed_roots
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


@pytest.mark.parametrize(
    ("query", "read_only"),
    [
        # Deliberately narrow recognized forms, not an engine boolean parser.
        ("mode=ro", True),
        ("mode=memory", True),
        ("immutable=1", True),
        ("immutable=TRUE", False),
        ("immutable=on", False),
        ("immutable=2", False),
        ("immutable=1&immutable=0", False),
        ("journal_mode=rollback", False),
        ("cache=shared&nomode=ro", False),
        ("immutable=0", False),
        ("immutable=no", False),
        ("immutable=", False),
        ("immutable", False),
        ("immutable=0&immutable=1", False),
        ("MODE=ro", False),
    ],
)
def test_a_sqlite_uri_is_read_only_only_by_its_exact_parameters(query, read_only):
    assert audit._read_only_uri(query) is read_only



@pytest.mark.parametrize("query", [
    "immutable=256", "immutable=512", "immutable=4294967297", "immutable=2147483648",
    "immutable=²", "immutable=١", "%69mmutable=0&immutable=1",
])
def test_writable_sqlite_query_is_not_silently_exempted(home, query):
    target = home / ".genesis" / "query.db"
    sqlite3.connect(target).close()
    audit._S.hits.clear()
    with sqlite3.connect(f"file:{target}?{query}", uri=True) as connection:
        connection.execute("CREATE TABLE owned(value)")
        connection.execute("INSERT INTO owned VALUES (1)")
    with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as reader:
        assert reader.execute("SELECT value FROM owned").fetchall() == [(1,)]
    assert ("~/.genesis", "sqlite connect", str(target)) in {
        (label, operation, path) for _node, label, operation, path in audit._S.hits
    }
    assert audit._S.faults == 0


@pytest.mark.parametrize("suffix", ["extra", "é"])
def test_memory_name_prefix_is_a_disk_file(home, monkeypatch, suffix):
    directory = home / ".genesis"
    monkeypatch.chdir(directory)
    target = directory / f":memory:{suffix}"
    audit._S.hits.clear()
    with sqlite3.connect(f"file::memory:{suffix}", uri=True) as connection:
        connection.execute("CREATE TABLE owned(value)")
    assert target.is_file()
    assert ("~/.genesis", "sqlite connect", str(target)) in {
        (label, operation, path) for _node, label, operation, path in audit._S.hits
    }
    assert audit._S.faults == 0


def test_sqlite_fragment_cannot_supply_a_readonly_query(home):
    target = home / ".genesis" / "fragment.db"
    sqlite3.connect(target).close()
    audit._S.hits.clear()
    with sqlite3.connect(f"file:{target}#ignored?mode=ro", uri=True) as connection:
        connection.execute("CREATE TABLE owned(value)")
    with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as reader:
        assert reader.execute("SELECT name FROM sqlite_master").fetchall() == [("owned",)]
    assert ("~/.genesis", "sqlite connect", str(target)) in {
        (label, operation, path) for _node, label, operation, path in audit._S.hits
    }


@pytest.mark.parametrize("uri", [
    "file::memory:", "file::memory:?cache=shared", "file::memory:#fragment",
    "file::memory:#ignored?mode=rw",
])
def test_exact_memory_uri_never_changes_a_disk_target(home, monkeypatch, uri):
    directory = home / ".genesis"
    monkeypatch.chdir(directory)
    audit._S.hits.clear()
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("CREATE TABLE owned(value)")
        connection.execute("INSERT INTO owned VALUES (1)")
        assert connection.execute("SELECT value FROM owned").fetchall() == [(1,)]
        assert connection.execute("PRAGMA database_list").fetchone()[2] == ""
    assert list(directory.iterdir()) == []
    assert audit._S.hits == {}
    assert audit._S.faults == 0



@pytest.mark.parametrize("query", ["mode=ro", "immutable=1"])
def test_plain_readonly_sqlite_form_refuses_disk_writes(home, query):
    target = home / ".genesis" / "readonly.db"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE owned(value)")
    before = target.read_bytes()
    audit._S.hits.clear()
    with sqlite3.connect(f"file:{target}?{query}", uri=True) as connection:
        with pytest.raises(sqlite3.OperationalError) as failure:
            connection.execute("INSERT INTO owned VALUES (1)")
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    assert target.read_bytes() == before
    assert audit._S.hits == {}


@pytest.mark.parametrize("exists", [False, True])
def test_plain_memory_mode_uses_memory_even_for_a_disk_name(home, exists):
    target = home / ".genesis" / "memory.db"
    if exists:
        with sqlite3.connect(target) as connection:
            connection.execute("CREATE TABLE disk_only(value)")
    before = target.read_bytes() if exists else None
    audit._S.hits.clear()
    with sqlite3.connect(f"file:{target}?mode=memory", uri=True) as connection:
        connection.execute("CREATE TABLE owned(value)")
        connection.execute("INSERT INTO owned VALUES (1)")
        assert connection.execute("SELECT value FROM owned").fetchall() == [(1,)]
        assert connection.execute("PRAGMA database_list").fetchone()[2] == ""
    assert (target.read_bytes() if target.exists() else None) == before
    assert audit._S.hits == {}


def test_an_immutable_uri_to_a_missing_file_is_a_write(home):
    """SQLite creates the missing file even with immutable=1 (measured)."""
    db = home / "genesis" / "new.db"
    sqlite3.connect(f"file:{db}?immutable=1", uri=True).close()
    assert ("the deployed checkout (~/genesis)", "sqlite connect", "new.db") in _hits()
    audit._S.hits.clear()
    sqlite3.connect(f"file:{db}?immutable=1", uri=True).close()  # now it exists
    assert {h for h in _hits() if h[1] == "sqlite connect"} == set()


def test_a_relative_os_open_read_of_a_credential_file_is_recorded(home, monkeypatch):
    (home / "genesis" / "secrets.env").write_text("K=v\n")
    monkeypatch.chdir(home / "genesis")
    os.close(os.open("secrets.env", os.O_RDONLY))
    assert (
        "credential file read (relative os.open; judged against cwd)",
        "read",
        "secrets.env",
    ) in _hits()


def test_a_write_to_this_checkout_outside_the_home_is_recorded(home, tmp_path, monkeypatch):
    """The checkout need not sit under the passwd home (a container mounts it
    elsewhere); its class root is consulted before the home filter."""
    checkout = tmp_path / "workspace" / "repo"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(
        audit._S, "classes", ((os.path.realpath(checkout), "this checkout"), *audit._S.classes)
    )
    (checkout / "state.json").write_text("{}")
    (tmp_path / "workspace" / "beside.txt").write_text("x")  # neither home nor checkout
    assert _hits() == {("this checkout", "write", "state.json")}


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


def test_refused_mkdir_rmdir_remove_and_rmtree_are_not_writes(home):
    """Each fails in the kernel and changes nothing: mkdir of an existing file,
    rmdir of a file, unlink of a directory, rmtree of a file."""
    g = home / ".genesis"
    (g / "plain").write_text("x")
    (g / "adir").mkdir()
    audit._S.hits.clear()
    with pytest.raises(FileExistsError):
        os.mkdir(g / "plain")
    with pytest.raises(NotADirectoryError):
        os.rmdir(g / "plain")
    with pytest.raises(OSError):
        os.remove(g / "adir")
    with pytest.raises(OSError):
        shutil.rmtree(g / "plain")
    assert _hits() == set()
    assert (g / "plain").read_text() == "x" and (g / "adir").is_dir()


def test_mkdir_of_an_existing_file_is_not_a_write(home):
    (home / ".genesis" / "plain").write_text("x")
    audit._S.hits.clear()
    with pytest.raises(FileExistsError):
        os.mkdir(home / ".genesis" / "plain")
    assert _hits() == set()


def test_a_relative_os_open_truncating_an_existing_file_is_a_write(home, monkeypatch):
    """No dir_fd, cwd under the home: truncating an existing file there is a real
    write, judged against the cwd like a new one."""
    target = home / "genesis" / "tracked.py"
    target.write_text("x = 1\n")
    monkeypatch.chdir(home / "genesis")
    audit._S.hits.clear()
    os.close(os.open("tracked.py", os.O_WRONLY | os.O_TRUNC))
    assert target.read_text() == ""
    assert _hits() == {
        ("relative os.open (dir_fd unknown; judged against cwd)", "write", "tracked.py")
    }


def test_a_relative_os_open_that_only_reopens_an_existing_file_is_not_a_write(home, monkeypatch):
    (home / "genesis" / "lock").write_text("")
    monkeypatch.chdir(home / "genesis")
    audit._S.hits.clear()
    os.close(os.open("lock", os.O_CREAT | os.O_RDONLY))
    assert _hits() == set()





























def test_a_credential_file_read_under_another_name_is_recorded(home, tmp_path, monkeypatch):
    """Through a symlink and a hard link whose names say nothing: recorded."""
    secrets = home / "genesis" / "secrets.env"
    secrets.write_text("K=v\n")
    st = os.stat(secrets)
    monkeypatch.setattr(audit._S, "credential_inodes", frozenset({(st.st_dev, st.st_ino)}))
    link = tmp_path / "allowed" / "current-creds"
    link.symlink_to(secrets)
    hard = home / "elsewhere" / "copy-of-creds"
    os.link(secrets, hard)
    audit._S.hits.clear()
    link.read_text()
    assert ("credential file read", "read", "secrets.env") in _hits()
    audit._S.hits.clear()
    hard.read_text()
    assert ("credential file read", "read", "copy-of-creds") in _hits()


def test_a_fault_in_the_credential_lookup_never_breaks_a_read(home, tmp_path, monkeypatch):
    """The read-side credential lookup runs inside the hook's guard: a test that
    makes os.stat raise (as tests/test_hooks/test_push_publish_ask_switch.py
    does) still opens its file, and the lookup's own stat is not re-audited."""
    secrets = home / "genesis" / "secrets.env"
    secrets.write_text("K=v\n")
    st = os.stat(secrets)
    monkeypatch.setattr(audit._S, "credential_inodes", frozenset({(st.st_dev, st.st_ino)}))
    body = tmp_path / "body.txt"
    body.write_text("x")
    real_stat = os.stat

    def refusing_stat(*args, **kwargs):
        raise AssertionError("stat by path")

    os.stat = refusing_stat
    try:
        fd = os.open(body, os.O_RDONLY)
    finally:
        os.stat = real_stat
    os.close(fd)




def test_install_without_a_known_real_home_stays_disarmed(monkeypatch):
    """A numeric UID with no passwd entry must not abort the run.

    The fence resolves the real home (falling back to an absolute HOME); when it
    has none, there is no home to judge writes against, so the audit stays off.
    """
    from tests import _live_fence

    hooks = []
    monkeypatch.setattr(audit._S, "installed", False)
    monkeypatch.setattr(audit._S, "disarmed", "")
    monkeypatch.setattr(audit.sys, "addaudithook", hooks.append)
    config = type("C", (), {"option": type("O", (), {"basetemp": None})()})()
    # Armed fence that found no usable home.
    monkeypatch.setattr(_live_fence._S, "installed", True)
    monkeypatch.setattr(_live_fence._S, "real_home", None)
    audit.install(config)
    # No fence: no passwd entry and a relative HOME.
    monkeypatch.setattr(_live_fence._S, "installed", False)
    monkeypatch.setattr(audit.pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    monkeypatch.setenv("HOME", "relative/home")
    audit.install(config)
    assert hooks == [] and not audit._S.installed
    # Off is said out loud, so an empty summary never reads as "no writes".
    lines = []
    reporter = type(
        "R",
        (),
        {"config": object(), "write_line": lambda self, line, **kw: lines.append(line)},
    )()
    audit.pytest_terminal_summary(reporter)
    assert lines == ["live writes: disarmed (no real home known)"]


def test_install_without_the_fence_takes_passwd_then_an_absolute_home(tmp_path, monkeypatch):
    """The passwd entry wins over HOME: an inner pytest inherits the outer
    closure's test HOME, and judging writes against it would hide them."""
    from tests import _live_fence

    passwd_home, env_home = tmp_path / "a", tmp_path / "b"
    passwd_home.mkdir()
    env_home.mkdir()
    monkeypatch.setattr(audit.sys, "addaudithook", lambda hook: None)
    monkeypatch.setattr(_live_fence._S, "installed", False)
    monkeypatch.setattr(_live_fence._S, "real_home", None)
    for name in ("real_home", "allowed_roots", "allowed_files", "classes", "credential_files"):
        monkeypatch.setattr(audit._S, name, getattr(audit._S, name))
    monkeypatch.setattr(audit._S, "credential_inodes", frozenset())
    monkeypatch.setattr(audit._S, "disarmed", "")
    monkeypatch.setenv("HOME", str(env_home))
    config = type("C", (), {"option": type("O", (), {"basetemp": None})()})()

    monkeypatch.setattr(audit._S, "installed", False)
    monkeypatch.setattr(
        audit.pwd, "getpwuid", lambda uid: type("P", (), {"pw_dir": str(passwd_home)})()
    )
    audit.install(config)
    assert audit._S.installed and audit._S.real_home == os.path.realpath(passwd_home)

    # No passwd entry: an absolute HOME arms it.
    monkeypatch.setattr(audit._S, "installed", False)
    monkeypatch.setattr(audit.pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    audit.install(config)
    assert audit._S.installed and audit._S.real_home == os.path.realpath(env_home)
    assert not audit._S.disarmed


def test_a_report_with_an_undecodable_path_never_fails_a_run(tmp_path):
    """A path whose bytes are not UTF-8 is written to the report as its bytes."""
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_sub.py").write_text(_SUBRUN_UNDECODABLE)
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
    assert b"/bad-\xff-name" in report.read_bytes(), text


_SUBRUN_UNDECODABLE = """
import os

from tests import _live_writes


def test_writes_an_undecodable_name(tmp_path):
    _live_writes._S.real_home = str(tmp_path)
    _live_writes._S.allowed_roots = ()
    with open(os.path.join(os.fsencode(tmp_path), b"bad-\\xff-name"), "w") as f:
        f.write("x")
"""


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


def test_an_xdist_run_reports_the_workers_writes(tmp_path):
    """CI runs the suite on xdist workers: their hits must reach the controller's
    summary and report, which a worker's own summary never does."""
    pytest.importorskip("xdist")
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    for name in ("test_one.py", "test_two.py"):  # --dist=loadfile: one file per worker
        (tmp_path / name).write_text(_SUBRUN_TESTS)
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
            "-n",
            "2",
            "--dist=loadfile",
            str(tmp_path / "test_one.py"),
            str(tmp_path / "test_two.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    text = out.stdout + out.stderr
    assert out.returncode == 0, text
    lines = report.read_text().splitlines()
    assert sorted(line.split("\t")[0].split("::")[0] for line in lines) == [
        "test_one.py",
        "test_two.py",
    ], text
    assert "live writes: writes outside the test's temp dirs" in text
    assert "no report from xdist worker" not in text


def test_a_worker_hands_over_an_undecodable_path_intact(monkeypatch):
    """The handoff crosses xdist's channel, which refuses a str that is not
    UTF-8 encodable: the path must arrive as it was recorded."""
    gateway_base = pytest.importorskip("execnet.gateway_base")
    key = ("t::a", "~/.genesis", "write", os.fsdecode(b"/h/bad-\xff-name"))
    monkeypatch.setattr(audit._S, "hits", {key: None})
    monkeypatch.setattr(audit._S, "disarmed", "")
    config = type("C", (), {"workeroutput": {}})()
    audit.pytest_sessionfinish(type("S", (), {"config": config})(), 0)
    sent = gateway_base.loads(gateway_base.dumps(config.workeroutput))  # the channel
    monkeypatch.setattr(audit._S, "hits", {})
    monkeypatch.setattr(audit._S, "merged_workers", set())
    monkeypatch.setattr(audit._S, "unreported", [])
    gateway = type("G", (), {"id": "gw0"})()
    audit.pytest_testnodedown(type("N", (), {"gateway": gateway, "workeroutput": sent})(), None)
    assert list(audit._S.hits) == [key] and audit._S.unreported == []


def test_a_worker_that_never_reported_is_named(monkeypatch):
    monkeypatch.setattr(audit._S, "merged_workers", set())
    monkeypatch.setattr(audit._S, "unreported", [])
    monkeypatch.setattr(audit._S, "hits", {})
    gateway = type("G", (), {"id": "gw3"})()
    audit.pytest_testnodedown(type("N", (), {"gateway": gateway, "workeroutput": {}})(), "crashed")
    audit.pytest_testnodedown(type("N", (), {"gateway": gateway, "workeroutput": {}})(), "crashed")
    assert audit._S.unreported == ["gw3 (crashed)"]  # reported down twice, named once


def test_the_summary_iterates_a_snapshot_of_the_hits(tmp_path, monkeypatch):
    """A thread the run left behind may still record during the summary; the
    summary must not iterate the live dict (that raises and fails the run)."""

    class Racing(dict):
        """Another thread records a hit as soon as anything iterates the dict."""

        def __iter__(self):
            it = super().__iter__()
            self[("t::late", "~/.genesis", "write", f"/late{len(self)}")] = None
            return it

    monkeypatch.setattr(audit._S, "installed", True)
    monkeypatch.setattr(audit._S, "unreported", [])
    monkeypatch.setattr(audit._S, "hits", Racing({("t::a", "~/.genesis", "write", "/x"): None}))
    monkeypatch.setenv("GENESIS_LIVE_WRITES_REPORT", str(tmp_path / "report.tsv"))
    lines = []
    reporter = type(
        "R",
        (),
        {
            "config": type("C", (), {})(),
            "section": lambda self, *a, **k: None,
            "write_line": lambda self, line, **k: lines.append(line),
        },
    )()
    audit.pytest_terminal_summary(reporter)  # must not raise
    assert (tmp_path / "report.tsv").read_text() == "t::a\t~/.genesis\twrite\t/x\n"


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
