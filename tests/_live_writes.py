"""The write audit: every write a test makes to the real home outside its temp dirs.

The live fence stops real commands, the closure removes credentials, and the
outbound fence refuses live connections. None of them sees a test that writes a
file into the live install directly: a config under ``~/.genesis``, a row in the
real database, a file in the deployed checkout. This plugin, armed after the
other three, REPORTS those writes. It changes nothing and fails nothing: its
list is the to-do list for making each such test write under its own
``tmp_path``, after which the audit can become a refusal.

A PEP 578 audit hook judges, in this interpreter:

- ``open`` with write intent (a mode with ``w``/``a``/``x``/``+``, or flags with
  ``O_WRONLY``/``O_RDWR``/``O_CREAT``/``O_TRUNC``/``O_APPEND``);
- ``os.remove``, ``os.rename`` (and ``os.replace``), ``os.mkdir``, ``os.rmdir``,
  ``shutil.rmtree``, ``os.truncate``, ``os.symlink``, ``os.link``;
- ``sqlite3.connect`` to a file (it creates and writes one), unless opened
  read-only (``mode=ro`` / ``immutable=1`` URI);
- reads of the real credential files (the deployed ``secrets.env``,
  ``~/.claude/.credentials.json``, ``~/.config/gh/hosts.yml``,
  ``~/.genesis/cc_oauth_token.env``).

A write counts when it lands under the real home and outside the allowed
places: the temp directory (``tempfile.gettempdir()``), pytest's basetemp, the
box lock file, ``/dev``, and ``__pycache__`` / ``.pytest_cache`` directories.
Paths given relative to a directory descriptor are resolved through
``/proc/self/fd``. Each write is classed by where it lands (the deployed
checkout, this checkout, ``~/.genesis``, ``~/.claude``, elsewhere in the home)
and attributed to the running test.

Output: a terminal-summary section, and, with ``GENESIS_LIVE_WRITES_REPORT=<file>``,
one tab-separated line per (test, class, operation, path), written on every run.

A hit is a write that happens: an operation the kernel will refuse or that
changes nothing (``mkdir`` of an existing directory, ``unlink`` of a missing
file, ``rmdir`` of a non-empty one, reopening an existing file without
truncating or writing) is not recorded. ``os.open`` of a relative name is
judged against the cwd and labelled so (its audit event omits ``dir_fd``).
Attribution is by ``PYTEST_CURRENT_TEST``: a write during collection or a
session fixture's teardown counts as "outside any test", and a background
thread an earlier test left running is charged to the test running now.

Not covered, by design: writes made by child processes (a script a test runs),
writes done in C without an audit event, metadata changes (``os.chmod``,
``os.utime``, ``os.setxattr``, ``Path.touch`` of an existing file),
``os.mkfifo``/``os.mknod``, SQLite writes through a connection opened earlier,
and ``file://localhost`` or percent-encoded SQLite URIs. The OS-level sandbox
is the boundary for those.
"""

from __future__ import annotations

import os
import pwd
import sys
import tempfile
import threading
from pathlib import Path

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_EVENTS = frozenset(
    {
        "open",
        "os.remove",
        "os.rename",
        "os.mkdir",
        "os.rmdir",
        "shutil.rmtree",
        "os.truncate",
        "os.symlink",
        "os.link",
        "sqlite3.connect",
    }
)


class _State:
    installed = False
    real_home = ""
    allowed_roots: tuple[str, ...] = ()
    allowed_files: frozenset[str] = frozenset()
    classes: tuple[tuple[str, str], ...] = ()  # (root, label), most specific first
    credential_files: frozenset[str] = frozenset()
    hits: dict[tuple[str, str, str, str], None] = {}  # insertion-ordered set


_S = _State()
_busy = threading.local()


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _fspath(value) -> str | None:
    if isinstance(value, int) or value is None:
        return None
    try:
        path = os.fspath(value)
    except TypeError:
        return None
    return os.fsdecode(path) if isinstance(path, bytes) else path


def _absolute(value, dir_fd=None, *, follow=True) -> str | None:
    """The absolute, resolved path. ``follow=False`` resolves only the parent:
    removing, renaming or linking acts on the NAME, so a symlink a test made in
    its own temp dir is that symlink, not the file it points at."""
    path = _fspath(value)
    if not path:
        return None
    if not os.path.isabs(path):
        base = os.getcwd()
        if isinstance(dir_fd, int) and dir_fd >= 0:
            try:
                base = os.readlink(f"/proc/self/fd/{dir_fd}")
            except OSError:
                return None
        path = os.path.join(base, path)
    try:
        if not follow:
            parent, name = os.path.split(os.path.abspath(path))
            return os.path.join(os.path.realpath(parent), name)
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.abspath(path)


def _allowed(path: str) -> bool:
    if path in _S.allowed_files or _under(path, "/dev"):
        return True
    parts = path.split("/")
    if "__pycache__" in parts or ".pytest_cache" in parts:
        return True
    return any(_under(path, root) for root in _S.allowed_roots)


def _label(path: str) -> str | None:
    """The class of a write that counts, or None when it does not."""
    if not _under(path, _S.real_home) or _allowed(path):
        return None
    for root, label in _S.classes:
        if _under(path, root):
            return label
    return "elsewhere in the home"


def _node() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "<outside a test>").rsplit(" (", 1)[0]


def _record(label: str, op: str, path: str) -> None:
    _S.hits[(_node(), label, op, path)] = None


def _sqlite_target(database) -> str | None:
    path = _fspath(database)
    if not path or path == ":memory:" or path.startswith("file::memory:"):
        return None
    if path.startswith("file:"):
        body, _, query = path[5:].partition("?")
        if "mode=ro" in query or "immutable=1" in query or "mode=memory" in query:
            return None
        path = body
    return _absolute(path)


def _judge(event: str, args: tuple) -> None:
    if event == "open":
        path, mode, flags = args[0], args[1], args[2]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            mode is None and isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        name = _fspath(path)
        if mode is None and name and not os.path.isabs(name):
            # os.open's audit event omits dir_fd, so a relative name may be
            # relative to a directory descriptor rather than the cwd. Judge it
            # against the cwd, but label it so a reader knows it is a guess.
            if writes:
                guess = _absolute(name)
                if guess and _label(guess) and not os.path.exists(guess):
                    _record("relative os.open (dir_fd unknown; judged against cwd)", "write", guess)
            return
        target = _absolute(path)
        if target is None:
            return
        if not writes:
            if target in _S.credential_files:
                _record("credential file read", "read", target)
            return
        if (
            mode is None
            and isinstance(flags, int)
            and not flags & (os.O_TRUNC | os.O_APPEND | os.O_WRONLY | os.O_RDWR)
            and os.path.exists(target)
        ):
            return  # O_CREAT|O_RDONLY on an existing file: nothing changes
        label = _label(target)
        if label:
            _record(label, "write", target)
        return
    if event == "sqlite3.connect":
        target = _sqlite_target(args[0])
        label = target and _label(target)
        if label:
            _record(label, "sqlite connect", target)
        return
    if event == "os.rename":
        pairs = ((args[0], args[2]), (args[1], args[3]))
    elif event == "os.link":
        pairs = ((args[1], args[3]),)
    elif event == "os.symlink":
        pairs = ((args[1], args[2]),)
    elif event == "os.truncate":
        pairs = ((args[0], None),)
    else:  # os.remove, os.mkdir, os.rmdir, shutil.rmtree: path first, dir_fd last
        pairs = ((args[0], args[-1]),)
    for i, (value, dir_fd) in enumerate(pairs):
        # A write THROUGH a path (truncate) follows it; one ON a name does not.
        target = _absolute(value, dir_fd, follow=event == "os.truncate")
        label = target and _label(target)
        if label and not _no_op(event, i, target):
            _record(label, event, target)


def _no_op(event: str, index: int, target: str) -> bool:
    """True when the call cannot change anything: the audit event fires before
    the system call, so an attempt the kernel refuses (or that is a no-op, such
    as ``mkdir(exist_ok=True)`` or ``unlink(missing_ok=True)``) is not a write."""
    if event == "os.mkdir":
        return os.path.isdir(target)
    if event in ("os.remove", "os.rmdir", "shutil.rmtree") or (
        event == "os.rename" and index == 0
    ):
        if not os.path.lexists(target):
            return True
        if event == "os.rmdir" and os.path.isdir(target):
            try:
                return bool(os.listdir(target))
            except OSError:
                return True
    return False


def _hook(event: str, args: tuple) -> None:
    if event not in _EVENTS or getattr(_busy, "on", False):
        return
    if event == "open":
        mode, flags = args[1], args[2]
        reads = (isinstance(mode, str) and not any(c in mode for c in "wax+")) or (
            mode is None and isinstance(flags, int) and not flags & _WRITE_FLAGS
        )
        # Cheapest exit first: an import or ordinary read is not a write, and
        # only a credential file's name is worth resolving on a read.
        if reads:
            name = _fspath(args[0])
            if not name or not name.endswith((
                "secrets.env", ".credentials.json", "hosts.yml", "cc_oauth_token.env",
            )):  # fmt: skip
                return
    _busy.on = True
    try:
        _judge(event, args)
    except Exception:  # noqa: BLE001, S110 - an audit fault must never break the code under test
        pass
    finally:
        _busy.on = False


def install(config) -> None:
    """Arm the write audit. Idempotent: an audit hook cannot be removed."""
    if _S.installed:
        return
    from genesis.util.pytest_lock import default_lock_path

    home = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    _S.real_home = home
    roots = {os.path.realpath(tempfile.gettempdir())}
    if config.option.basetemp:
        roots.add(os.path.realpath(str(config.option.basetemp)))
    _S.allowed_roots = tuple(sorted(roots))
    lock = default_lock_path()
    _S.allowed_files = frozenset({os.path.realpath(str(lock))} if lock else ())
    repo = os.path.realpath(str(Path(__file__).resolve().parent.parent))
    deployed = os.path.realpath(os.path.join(home, "genesis"))
    classes = [
        (deployed, "the deployed checkout (~/genesis)"),
        (os.path.join(home, ".genesis"), "~/.genesis"),
        (os.path.join(home, ".claude"), "~/.claude"),
    ]
    if repo != deployed:
        classes.append((repo, "this checkout"))
    # Most specific root first, so a checkout inside another path wins.
    _S.classes = tuple(sorted(classes, key=lambda c: -len(c[0])))
    _S.credential_files = frozenset(
        os.path.realpath(os.path.join(home, rel))
        for rel in (
            "genesis/secrets.env",
            ".claude/.credentials.json",
            ".config/gh/hosts.yml",
            ".genesis/cc_oauth_token.env",
        )
    )
    sys.addaudithook(_hook)
    _S.installed = True


def pytest_terminal_summary(terminalreporter):
    if not _S.installed:
        return
    tr = terminalreporter
    if _S.hits:
        by_class: dict[str, set[str]] = {}
        for node, label, _op, _path in _S.hits:
            by_class.setdefault(label, set()).add(node)
        tr.section("live fence: writes outside the test's temp dirs (reported, not failed)")
        for label, nodes in sorted(by_class.items()):
            outside = "<outside a test>" in nodes
            count = len(nodes) - outside
            tr.write_line(f"{label}: {count} tests" + (", and outside any test" if outside else ""))
    report = os.environ.get("GENESIS_LIVE_WRITES_REPORT")
    if report:
        try:
            Path(report).write_text("".join("\t".join(key) + "\n" for key in _S.hits))
        except OSError as exc:  # report-only: a bad report path never fails a run
            tr.write_line(f"live writes: could not write the report {report}: {exc}", yellow=True)
