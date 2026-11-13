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
- ``sqlite3.connect`` to a file, except recognized literal URI query forms
  ``mode=ro``, ``mode=memory``, or ``immutable=1`` for an existing target;
- reads of the real credential files (``~/.config/genesis/secrets.env``,
  the deployed ``secrets.env``,
  ``~/.claude/.credentials.json``, ``~/.config/gh/hosts.yml``,
  ``~/.genesis/cc_oauth_token.env``).

A write counts when it lands under the real home (or in this checkout, wherever
it sits) and outside the allowed places: the temp directory (``tempfile.gettempdir()``), pytest's basetemp and its
per-process leaf, the live closure's test HOME (when it sets one), the box lock
file this run took,
``/dev``, and ``__pycache__`` / ``.pytest_cache`` directories.
Paths given relative to a directory descriptor are resolved through
``/proc/self/fd``. Each write is classed by where it lands (the deployed
checkout, this checkout, ``~/.genesis``, ``~/.claude``, elsewhere in the home)
and attributed to the running test.

Output: a terminal-summary section, and, with ``GENESIS_LIVE_WRITES_REPORT=<file>``,
one tab-separated line per (test, class, operation, path), written on every run.
Each field escapes backslash/tab/LF/CR; tests._live_writes_report.decode reads
the reversible bytes. Empty/parseable TSV alone never establishes completeness.
Under xdist each worker hands its hits to the controller at session end
(``workeroutput``, as the live fence does), and only the controller reports; a
worker that never reported is named in the summary. The ordinary trylast worker
snapshot includes default/tryfirst sessionfinish hooks, but not later same-priority
hooks, wrapper teardown, unconfigure or writes after the snapshot.

Hits classify pre-operation events, not syscall completion. Known no-ops such
as an existing ``mkdir``, missing ``unlink``, non-empty ``rmdir``, or an
unchanged reopen are filtered. Other refused operations and readonly SQLite
queries outside the recognized literal forms can still be reported.
Known exclusive/nofollow refusals, metadata-only O_PATH, missing rename/link
sources, occupied link destinations and same-inode renames are also filtered.
Protected hardlink sources are observed as ``os.link source`` exposure, even
without a later content write. Source symlink targets are checked conservatively
because the link event omits follow_symlinks. No alias history is maintained.
``os.open`` of a relative name is judged against the cwd and labelled so (its
audit event omits ``dir_fd``).
Attribution is by ``PYTEST_CURRENT_TEST``: a write during collection or a
session fixture's teardown counts as "outside any test", and a background
thread an earlier test left running is charged to the test running now.

Not covered, by design: writes made by child processes (a script a test runs),
writes done in C without an audit event, metadata changes (``os.chmod``,
``os.utime``, ``os.setxattr``, ``Path.touch`` of an existing file),
``os.mkfifo``/``os.mknod``, SQLite writes through a connection opened earlier,
and ``file://localhost`` or percent-encoded SQLite URIs. The OS-level sandbox
is the boundary for those.
Without stdlib ``ALLOW_MISSING`` (unless backported), missing credential
targets produce explicit unknown events; existing identities still work.
"""

from __future__ import annotations

import errno
import genericpath
import importlib.util
import os as _os
import pwd
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType

import pytest

from genesis.secrets_location import home_config_location
from tests import _live_writes_fs, _live_writes_report


def _observer_os():
    """Capture functions, including stdlib path helpers' transitive bindings.

    Tests keep the normal mutable OS modules. These private module instances
    reuse their standard loader and resolver without changing sys.modules.
    """
    # Module reprs do not expand environ or other captured values on failure.
    snapshot = ModuleType("os")
    snapshot.__dict__.update(vars(_os))

    def private_module(module):
        copied = importlib.util.module_from_spec(module.__spec__)
        module.__spec__.loader.exec_module(copied)
        copied.os = snapshot
        return copied

    private_generic = private_module(genericpath)
    snapshot.path = private_module(_os.path)
    snapshot.path.genericpath = private_generic
    for name in private_generic.__all__:
        setattr(snapshot.path, name, getattr(private_generic, name))
    return snapshot


os = _observer_os()

# Older Python lacks missing-aware resolution: strict=True leaves uncertainty
# visible through the hook rather than silently accepting a guessed target.
_MISSING_STRICT = getattr(os.path, "ALLOW_MISSING", True)
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
    disarmed = ""  # why install() left the audit off; the summary says so
    real_home = ""
    allowed_roots: tuple[str, ...] = ()
    allowed_files: frozenset[str] = frozenset()
    classes: tuple[tuple[str, str], ...] = ()  # (root, label), most specific first
    credential_files: frozenset[str] = frozenset()
    credential_inodes: frozenset[tuple[int, int]] = frozenset()  # initial identities
    hits: dict[tuple[str, str, str, str], None] = {}  # insertion-ordered set
    merged_workers: set[str] = set()  # xdist workers whose hits were merged
    unreported: list[str] = []  # xdist workers whose hits never arrived
    faults = 0  # unknown classifications must not look like a clean census
    census_id: str | None = None
    exitstatus: int | None = None
    expected_workers: set[str] = set()
    worker_status: dict[str, dict | None] = {}


_S = _State()
_busy = threading.local()
_fault_lock = threading.Lock()


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
    if isinstance(value, int):
        try:
            path = os.readlink(f"/proc/self/fd/{value}")
        except FileNotFoundError:
            try:
                os.fstat(value)
            except OSError as error:
                if error.errno == errno.EBADF:
                    return None  # established invalid/closed descriptor
                raise
            raise  # valid descriptor, unknown target: count the judging fault
        if not os.path.isabs(path):
            return None  # pipes and sockets are not filesystem paths
    else:
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
            parent, name = os.path.split(path)
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
    if _allowed(path):
        return None
    # The class roots first: this checkout need not sit under the home.
    for root, label in _S.classes:
        if _under(path, root):
            return label
    return "elsewhere in the home" if _under(path, _S.real_home) else None


def _node() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "<outside a test>").rsplit(" (", 1)[0]


def _record(label: str, op: str, path: str) -> None:
    _S.hits[(_node(), label, op, path)] = None


def _sqlite_target(database) -> str | None:
    path = _fspath(database)
    if not path or path == ":memory:":
        return None
    if path.startswith("file:"):
        body, _, query = path[5:].partition("#")[0].partition("?")
        if body == ":memory:":
            return None
        target = _absolute(body)
        if _read_only_uri(query, exists=bool(target) and os.path.lexists(target)):
            return None
        return target
    return _absolute(path)


def _read_only_uri(query: str, *, exists: bool = True) -> bool:
    """Recognize only measured literal queries on URI-enabled SQLite.

    Other queries are possible-write observations, including readonly forms
    outside this set. Do not emulate SQLite's escaping, duplicate parameters
    or integer booleans. Even literal immutable=1 creates a missing file.
    """
    return query in {"mode=ro", "mode=memory"} or (query == "immutable=1" and exists)


def _reopens_unchanged(flags, target: str) -> bool:
    """An ``os.open`` that only creates (``O_CREAT`` without a write, truncate or
    append flag) changes nothing when the target already exists."""
    return (
        isinstance(flags, int)
        and not flags & (os.O_TRUNC | os.O_APPEND | os.O_WRONLY | os.O_RDWR)
        and os.path.exists(target)
    )


def _judge(event: str, args: tuple) -> None:
    if event == "open":
        path, mode, flags = args[0], args[1], args[2]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            mode is None and isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        reads = (isinstance(mode, str) and ("r" in mode or "+" in mode)) or (
            mode is None and isinstance(flags, int)
            and flags & os.O_ACCMODE in (os.O_RDONLY, os.O_RDWR)
            and not flags & getattr(os, "O_PATH", 0)
        )
        name = _fspath(path)
        # The event omits dir_fd. Preserve the existing cwd-guess labels.
        relative = mode is None and name and not os.path.isabs(name)
        target = _absolute(path)
        if target is None:
            return
        mutates = writes and not (mode is None and _reopens_unchanged(flags, target))
        write_label = _label(target) if mutates else None
        if relative and write_label:
            write_label = "relative os.open (dir_fd unknown; judged against cwd)"
        if write_label:
            _record(write_label, "write", target)
        # Keep an established write even if credential metadata is unavailable.
        if not reads and (write_label or not mutates):
            return
        credential = target in _S.credential_files or _is_credential_file(target)
        if reads and credential:
            label = ("credential file read (relative os.open; judged against cwd)"
                     if relative else "credential file read")
            _record(label, "read", target)
        if mutates and not write_label and credential:
            _record("credential file write", "write", target)
        return
    if event == "sqlite3.connect":
        target = _sqlite_target(args[0])
        label = target and _label(target)
        if label:
            _record(label, "sqlite connect", target)
        return
    if event in ("os.rename", "os.link"):
        _judge_names(event, args)
        return
    if event == "os.symlink":
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


def _unknown():
    with _fault_lock:
        _S.faults += 1


def _judge_names(event, args):
    try:
        effects = _live_writes_fs.name_effects(os, _absolute, event, args)
    except (OSError, ValueError):
        _unknown()
        # Metadata uncertainty must not erase an established destination hit.
        effects = _live_writes_fs.uncertain_name_effects(_absolute, event, args)
    for target, operation in effects:
        label = target and _label(target)
        if label:
            _record(label, operation, target)


def _no_op(event: str, index: int, target: str) -> bool:
    """True when the call cannot change anything: the audit event fires before
    the system call, so an attempt the kernel refuses (or that is a no-op, such
    as ``mkdir(exist_ok=True)`` or ``unlink(missing_ok=True)``) is not a write."""
    if event == "os.mkdir":
        # Any existing name, directory or not (a file, a dangling symlink): EEXIST.
        return os.path.lexists(target)
    if event in ("os.remove", "os.rmdir", "shutil.rmtree"):
        if not os.path.lexists(target):
            return True
        is_dir = os.path.isdir(target) and not os.path.islink(target)
        if event in ("os.rmdir", "shutil.rmtree") and not is_dir:
            return True  # ENOTDIR/OSError: neither removes a file or a symlink
        if event == "os.remove" and is_dir:
            return True  # EISDIR/EPERM: unlink never removes a directory
        if event == "os.rmdir":
            try:
                return bool(os.listdir(target))
            except OSError:
                return True
    return False


def _is_missing_credential_target(name: str) -> bool:
    """Resolve missing named targets while preserving nonabsence errors."""
    target = os.path.realpath(name, strict=_MISSING_STRICT)
    unavailable = None
    for credential in _S.credential_files:
        try:
            current = os.path.realpath(credential, strict=_MISSING_STRICT)
        except NotADirectoryError:
            continue
        except (OSError, ValueError) as error:
            unavailable = error
            continue
        if current == target:
            return True
    if unavailable is not None:
        raise unavailable
    return False


def _is_credential_file(name: str) -> bool:
    """Match initial identities or the current logical credential names.

    Fresh metadata handles replacement/late creation without a stale cache or
    an accumulating inode history. No credential contents are read.
    """
    try:
        st = os.stat(name)
    except FileNotFoundError:
        # A dangling logical credential symlink can create this named target.
        # Unlike permissive _absolute, this cannot silently absorb EACCES/EIO.
        return _is_missing_credential_target(name)
    except NotADirectoryError:
        return False
    identity = (st.st_dev, st.st_ino)
    if identity in _S.credential_inodes:
        return True
    unavailable = None
    for credential in _S.credential_files:
        try:
            current = os.stat(credential)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, ValueError) as error:
            unavailable = error
            continue
        if identity == (current.st_dev, current.st_ino):
            return True
    if unavailable is not None:
        raise unavailable  # an unestablished negative judgment is unknown
    return False


def _ordinary_read(args: tuple) -> bool:
    """Whether an ``open`` event is a read of anything but a credential file.

    Cheapest exit first: an import or ordinary read is not a write, and only a
    credential file is worth resolving on a read: by its name, or by device and
    inode when it is reached under another name."""
    mode, flags = args[1], args[2]
    reads = (isinstance(mode, str) and not any(c in mode for c in "wax+")) or (
        mode is None and isinstance(flags, int) and not flags & _WRITE_FLAGS
    )
    if not reads:
        return False
    name = _fspath(args[0])
    if not name:
        return False  # integer descriptors need the existing proc-fd lookup
    return not name.endswith((
        "secrets.env", ".credentials.json", "hosts.yml", "cc_oauth_token.env",
    )) and not _is_credential_file(name)  # fmt: skip


def _hook(event: str, args: tuple) -> None:
    if event not in _EVENTS or getattr(_busy, "on", False):
        return
    # Everything past the event check runs under the guard and the exception
    # boundary, the read-side credential lookup included: its os.stat can raise
    # (a test that patches it) and is itself an audited open.
    _busy.on = True
    try:
        if event == "open":
            try:
                if _live_writes_fs.open_no_effect(os, _absolute, args):
                    return
            except (OSError, ValueError):
                _unknown()  # retain conservative observations as well
        if event == "open" and _ordinary_read(args):
            return
        _judge(event, args)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException:  # pytest outcomes also must not escape an observer
        _unknown()
    finally:
        _busy.on = False


def install(config) -> None:
    """Arm the write audit. Idempotent: an audit hook cannot be removed."""
    if _S.installed:
        return
    _S.census_id = os.environ.get("GENESIS_LIVE_WRITES_RUN_ID")
    from tests import _live_closure, _live_fence

    # The fence (installed first in the suite) owns the real home. Without it
    # (a run that arms only this audit), resolve it the way the fence does: the
    # passwd entry, else an absolute HOME (numeric-UID containers need not have
    # an /etc/passwd entry). With no known home there is nothing to judge
    # writes against, so the audit stays off rather than aborting the run, and
    # the terminal summary says it was off (no hits is not the same as unwatched).
    home = _live_fence._S.real_home
    if not _live_fence._S.installed:
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            home = os.environ.get("HOME")
    if not home or not os.path.isabs(home):
        _S.disarmed = "no real home known"
        return
    home = os.path.realpath(home)
    _S.real_home = home
    roots = {os.path.realpath(tempfile.gettempdir())}
    # basetemp, its per-process leaf, and the closure's test HOME when the closure
    # gives the session one (it may sit under the real home, in ~/tmp/pytest/<pid>).
    for root in (
        config.option.basetemp,
        getattr(config, "_genesis_basetemp_cleanup", None),
        getattr(_live_closure._S, "home", None),
    ):
        if root:
            roots.add(os.path.realpath(str(root)))
    _S.allowed_roots = tuple(sorted(roots))
    # The lock this run took. Once the closure moves HOME, default_lock_path()
    # would resolve under the test home, where there is no lock. (Its holder record is
    # written through an fd opened before this hook, so today nothing reports it
    # either way; the entry names the right file for any later open.)
    lock = getattr(getattr(config, "_genesis_pytest_lock", None), "_path", None)
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
    _S.credential_files = frozenset({os.fspath(home_config_location(Path(home)))}) | frozenset(
        os.path.join(home, rel)
        for rel in (
            "genesis/secrets.env",
            ".claude/.credentials.json",
            ".config/gh/hosts.yml",
            ".genesis/cc_oauth_token.env",
        )
    )
    inodes = set()
    for path in _S.credential_files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        inodes.add((st.st_dev, st.st_ino))
    _S.credential_inodes = frozenset(inodes)
    sys.addaudithook(_hook)
    _S.installed = True


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    # An xdist worker's terminal summary never reaches the run, so it hands its
    # hits to the controller (pytest_testnodedown), as the live fence does.
    # Earlier ordinary hooks may update Session after the argument was copied.
    _S.exitstatus = int(getattr(session, "exitstatus", exitstatus))
    if hasattr(session.config, "workeroutput"):
        session.config.workeroutput["genesis_live_writes"] = {
            # As bytes: xdist's channel refuses a str it cannot encode as UTF-8
            # (MEASURED: execnet DumpError), which an undecodable path is.
            "hits": [
                [field.encode("utf-8", "surrogateescape") for field in key]
                for key in dict.copy(_S.hits)
            ],
            "disarmed": _S.disarmed,
            "faults": _S.faults,
        }
        if _S.census_id:
            session.config.workeroutput["genesis_live_writes"]["census"] = _live_writes_report.observation_state(_S)


def _worker_id(node):
    return (getattr(node, "gateway", None) and getattr(node.gateway, "id", None)) or "a worker"


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    _S.expected_workers.add(_worker_id(node))


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    """The controller merges each worker's hits, so an xdist run reports what a
    serial one would. A worker that went down without reporting is named in the
    summary: its writes are unknown, not absent."""
    worker = _worker_id(node)
    if worker in _S.merged_workers:
        return  # xdist can report one worker down twice (an interrupted run)
    _S.merged_workers.add(worker)
    got = (getattr(node, "workeroutput", None) or {}).get("genesis_live_writes")
    if got is None:
        _S.unreported.append(f"{worker} ({error})" if error else worker)
        return
    census = got.get("census")
    _S.worker_status[worker] = dict.copy(census) if isinstance(census, dict) else None
    for key in got["hits"]:
        _S.hits[tuple(field.decode("utf-8", "surrogateescape") for field in key)] = None
    if got["disarmed"]:
        _S.unreported.append(f"{worker} (disarmed: {got['disarmed']})")
    with _fault_lock:
        _S.faults += got.get("faults", 0)


def pytest_terminal_summary(terminalreporter):
    if hasattr(terminalreporter.config, "workeroutput"):
        return  # a worker: its hits go to the controller, which reports once
    if not _S.installed:
        if _S.disarmed:
            terminalreporter.write_line(f"live writes: disarmed ({_S.disarmed})", yellow=True)
        else:
            terminalreporter.write_line("live writes: not installed (unknown, not clean)", yellow=True)
    tr = terminalreporter
    if _S.faults:
        tr.write_line(f"live writes: {_S.faults} events could not be classified (unknown, not clean)", yellow=True)
    # One snapshot: a thread the run left behind can still record while this runs,
    # and iterating the live dict would raise and fail a run this only reports on.
    # dict.copy() takes it in one step under the GIL, without iterating the dict.
    hits = list(dict.copy(_S.hits))
    if hits:
        by_class: dict[str, set[str]] = {}
        for node, label, _op, _path in hits:
            by_class.setdefault(label, set()).add(node)
        tr.section("live writes: writes outside the test's temp dirs (reported, not failed)")
        for label, nodes in sorted(by_class.items()):
            outside = "<outside a test>" in nodes
            count = len(nodes) - outside
            tr.write_line(f"{label}: {count} tests" + (", and outside any test" if outside else ""))
    for worker in _S.unreported:
        tr.write_line(f"live writes: no report from xdist worker {worker}", yellow=True)
    report = os.environ.get("GENESIS_LIVE_WRITES_REPORT")
    if report:
        try:
            # surrogateescape: a path os.fsdecode() could not decode is written back
            # as its original bytes instead of failing the run.
            _live_writes_report.publish(report, hits, _live_writes_report.census_state(_S))
        except (OSError, UnicodeError) as exc:  # report-only: never fails a run
            tr.write_line(f"live writes: could not write the report {report}: {exc}", yellow=True)
