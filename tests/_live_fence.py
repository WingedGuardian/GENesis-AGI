"""The live fence: a test may not run real commands against the machine it runs on.

A test whose fixture redirects only some of the state a code path touches can
still reach that path's real side effects: an update route that starts a real
orchestrator, which starts a real ``claude`` session, which runs the deployed
checkout's ``scripts/update.sh``. Nothing else in the suite stops a test from
starting a real process, so this plugin does.

It is a guardrail, not a security boundary (code in the test process can get
around anything done in-process). Two layers, because a process can be started
two ways:

1. **An audit hook** (PEP 578) sees every process THIS interpreter starts
   (``subprocess.Popen`` incl. asyncio and ``shell=True``, ``os.posix_spawn``,
   ``os.exec*`` incl. ``os.spawn*``, ``os.system``) and refuses, before any
   process exists, when the PROGRAM it will run — the resolved file, never its
   arguments or a shell string — is one of ``DENY_NAMES`` (claude, systemd-run,
   systemctl, sudo, gh, loginctl; ``claude.exe``/``claude-code`` count as
   claude) resolving anywhere but a test's own temp dir or the fence's stubs.
   A bare name resolves through the call's own ``PATH``, so it normally finds
   a stub: ``claude`` (except a bare ``--version``/``--help``) and ``sudo``
   stubs are refused, so code that finds a stub never mistakes it for the real
   thing; the ``ANSWERED_NAMES`` stubs (and those two ``claude`` reads) run and
   answer as the real tool does on a CI runner, where nothing is reachable (no
   bus: exit 1; gh unauthenticated: exit 4), and are reported, not failed. A
   stub exists only for a tool installed on this machine, so ``shutil.which``
   still says "not installed" where it is not.

   The refusal is ``LiveStateViolation``, a ``BaseException``, so a caller's
   ``except OSError`` / ``except Exception`` fallback cannot catch it and try a
   second route (MEASURED: it propagates through both, through an asyncio task
   and through ``asyncio.to_thread``, and a refused ``Popen`` starts nothing).
   In a forked child (``os.spawn*``, a hand-rolled fork-exec) the refusal is
   recorded and the child exits 127 instead, so it never unwinds into a second
   copy of the pytest session.

   Arguments are never judged: which file ``bash -c <string>`` or a heredoc
   would run is unknowable in-process, and every attempt to predict it leaked
   launch forms. A deploy script run through any shell spelling instead refuses
   ITSELF: the fence exports ``GENESIS_TEST_FENCE_LIVE_ROOTS`` (this checkout's
   real root, the real home's ``~/.genesis``/``~/genesis`` whether or not they
   exist yet, and
   a root named in ``GENESIS_LIVE_FENCE_LIVE_ROOT``) and
   ``GENESIS_TEST_FENCE_LOG`` (this log), and
   ``scripts/lib/live_system_guard.sh``'s ``refuse_if_test_fenced_live`` makes
   each deploy script refuse — ``REFUSED (test fence): ...`` on stderr, the same
   line appended here, exit 97 — when its acting root, ``HOME``,
   ``GENESIS_HOME`` or ``HOME/.genesis`` is at or under a listed root, or a
   path it must check cannot be resolved provably.

2. **PATH shims** for processes a child starts (a script under test, a detached
   orchestrator): a directory of refusing, logging stubs goes first on ``PATH``,
   ``GENESIS_CC_BIN`` points at the ``claude`` stub, and the session-bus
   variables point at a path that does not exist (the #2863 fence, generalized
   to the whole suite). NOT caught here, the stated residuals the OS-level
   sandbox exists for: an absolute path in a child (``/usr/bin/systemctl``); a
   child given a hand-built ``env`` without the fence's ``PATH`` and bus
   variables (scripts that default ``XDG_RUNTIME_DIR`` when it is unset then
   reach the real user manager); whatever a child shell string or a real
   ``systemd-run`` in a ``user_manager`` test goes on to start.

Every refusal, from either layer, is appended to one log with the test's node
id. The test that caused it fails at teardown even if the code swallowed the
exception; a hit drained after its test ended (a detached child) fails the
session. Reporting ends at the final session drain. Shim files remain in their
unique temp directory for surviving children, until normal temp cleanup.

Opt-outs, each listed in the terminal summary: a test marked ``user_manager``
may run the real ``systemd-run`` (for a transient ``--user`` scope or a unit
check) and keeps the bus variables; a test marked ``real_tool("gh")``
deliberately runs the named installed tools (their own ``--help``, a kernel
probe) and gets no stub for those names.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pytest

DENY_NAMES = frozenset({"claude", "systemd-run", "systemctl", "sudo", "gh", "loginctl"})
# Other names the same programs install under (the repo's own list:
# cc/slot_liveness.py): the claude launcher is a symlink to `claude.exe`.
_ALIASES = {"claude.exe": "claude", "claude-code": "claude"}
_STUB_NAMES = DENY_NAMES | _ALIASES.keys()
# Of those, the tools hundreds of existing tests reach only to READ live state
# (a user-manager query, a GitHub GET). Called by bare name they get the stub,
# which answers as the real tool does on CI (nothing reachable) and is reported,
# not failed. Called by a real path outside tmp they are refused like the rest.
# MEASURED 2026-10-13, over the 144 test files that both start processes and
# name one of these tools: strict refusal failed or errored 676 tests, and every
# one of the 554 calls captured in the six worst files was a read.
ANSWERED_NAMES = frozenset({"systemctl", "systemd-run", "gh", "loginctl"})
# What a ``user_manager`` test may run for real.
USER_MANAGER_ALLOWED = frozenset({"systemd-run"})
_EVENTS = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system"})
# Exactly these argv tails of `claude` only READ the binary (CI: not installed),
# so the stub answers them; every other `claude` call is refused.
_CLAUDE_READS = (["--version"], ["--help"])
# First words of a deploy script's own refusal line (see the module docstring).
_REFUSAL_PREFIX = "REFUSED"
# Captured at import: tests monkeypatch shutil.which, and the fence must keep
# resolving programs the way exec will.
_which = shutil.which


class LiveStateViolation(BaseException):  # noqa: N818 - named for what it is
    """A test tried to run a real command against this machine."""


class _State:
    installed = False
    pid = 0  # the pytest process; a refusal in a forked child exits instead
    real_home: str | None = None
    orig_path = ""  # PATH before the fence put its stubs in front
    shim_dir = ""
    all_stub_dir = ""  # every DENY_NAMES stub, for the fence's own tests
    base = ""  # the fence's own files: every stub directory lives under it
    log = ""
    stubbed: frozenset[str] = frozenset()  # names with a stub: installed here
    allowed_roots: tuple[str, ...] = ()
    opted_out = False  # the running test is marked user_manager
    real_tool_names: frozenset[str] = frozenset()  # what this real_tool test may run
    real_tool_tests: list[str] = []
    opted_out_tests: list[str] = []
    late_hits: list[str] = []
    answered: list[str] = []  # stub answers: reported, never failed
    saved_bus: dict[str, str | None] = {}


_S = _State()


# ── decision ────────────────────────────────────────────────────────────────


def _real(path: str, cwd=None) -> str:
    if cwd is not None and not os.path.isabs(path):
        path = os.path.join(cwd, path)
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _env_get(env, key: str):
    if env is None:
        return os.environ.get(key)
    if isinstance(env, Mapping):
        value = env.get(key)
        if value is None:
            value = env.get(key.encode())
        return os.fsdecode(value) if isinstance(value, bytes) else value
    return None


def _resolve(program: str, env, cwd=None) -> str | None:
    """The file a program name runs: a path (relative to ``cwd``) when it has a
    '/', else via the call's PATH. None when there is no such file: it cannot run,
    and the code under test should see the same FileNotFoundError it would on CI."""
    if "/" in program:
        path = program if os.path.isabs(program) else os.path.join(cwd or os.getcwd(), program)
        return path if os.path.exists(path) else None
    search = _env_get(env, "PATH")
    if search is None:
        search = os.defpath
    # Empty and relative PATH components are relative to the child's cwd,
    # including an explicitly empty PATH (which is not the default PATH).
    search = os.pathsep.join(
        part if os.path.isabs(part) else os.path.join(cwd or os.getcwd(), part)
        for part in search.split(os.pathsep)
    )
    return _which(program, path=search)


def _tool_name(program: str, env, cwd=None) -> str:
    """The DENY_NAMES entry ``program`` runs, by its name or by the name of the
    file it resolves to (a slash path against ``cwd``, a bare name against the
    child's PATH); else its plain basename."""
    name = os.path.basename(program)
    name = _ALIASES.get(name, name)
    if name not in DENY_NAMES:
        resolved = program if "/" in program else _resolve(program, env, cwd)
        if resolved is not None:
            target = os.path.basename(_real(resolved, cwd))
            target = _ALIASES.get(target, target)
            if target in DENY_NAMES:
                return target
    return name


def _deny_reason(program: str, argv: list[str], env, cwd) -> str | None:
    name = _tool_name(program, env, cwd)
    if name not in DENY_NAMES or name in _S.real_tool_names:
        return None
    if _S.opted_out and name in USER_MANAGER_ALLOWED:
        return None
    resolved = _resolve(program, env, cwd)
    if resolved is None:
        return None  # not installed here: it cannot run, and CI is the same
    real = _real(resolved)
    if _S.base and _under(real, _real(_S.base)):
        if name in ANSWERED_NAMES or (name == "claude" and argv[1:] in _CLAUDE_READS):
            return None  # the stub answers, as CI would, and records it
        return f"{name}: found the fence's stub (a real `{name}` would have run here)"
    if not any(_under(real, root) for root in _S.allowed_roots):
        return f"{name}: resolves to {real}, outside the test's temp dirs"
    return None


def judge(program, argv, env, cwd=None) -> str | None:
    """Why this exec must be refused, or None to allow it.

    ``program`` is what the kernel will run (a path or a bare name), ``argv``
    the argument vector, ``env`` the child's environment (None = inherited),
    ``cwd`` its working directory (None = the current one). Only the resolved
    program is judged — never its arguments or a shell string.
    """
    if program is None and argv:
        program = argv[0]
    if program is None:
        return None
    program = os.fsdecode(program)
    argv = [os.fsdecode(a) for a in (argv or [])]
    cwd = os.fsdecode(cwd) if cwd is not None else None
    return _deny_reason(program, argv, env, cwd)


def _record(line: str) -> None:
    if not _S.log:
        return
    try:
        fd = os.open(_S.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line.replace("\n", " ") + "\n").encode("utf-8", "backslashreplace"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _hook(event: str, args: tuple) -> None:
    if event not in _EVENTS:
        return
    cwd = None
    if event == "subprocess.Popen":
        program, argv, cwd, env = args
        argv = [argv] if isinstance(argv, (str, bytes)) else list(argv or [])
    elif event == "os.system":
        # A shell command line: judged on the shell it starts (`sh`). The shim
        # refuses whatever deny-listed name that shell goes on to run.
        program, argv, env = "sh", ["sh", "-c", os.fsdecode(args[0])], None
    else:  # os.posix_spawn, os.exec
        program, argv, env = args
        argv = list(argv or [])
    reason = judge(program, argv, env, cwd)
    if reason is None:
        return
    # Raw, as the shell stub writes it: _node() strips the phase once.
    node = os.environ.get("PYTEST_CURRENT_TEST", "<no test>")
    shown = " ".join(os.fsdecode(a) for a in argv[:8])[:300]
    _record(f"{node}\taudit\t{reason}\targv: {shown}")
    if os.getpid() != _S.pid:
        # A forked child about to exec: raising would unwind it into a second
        # copy of this pytest session. The log line fails the test.
        os._exit(127)
    raise LiveStateViolation(
        f"live fence: refused to run {reason}. Tests must not run real commands on "
        "this machine; stub the call, or give the program a path inside tmp_path."
    )


# ── install ─────────────────────────────────────────────────────────────────

_SHIM = """#!/bin/sh
tag={tag}
tool={tool}
if [ "$tool" = claude ] && [ $# -eq 1 ]; then
  case "$1" in --version|--help) tag=answered ;; esac
fi
args=$(printf '%s' "$*" | tr '\\n\\t' '  ')
printf '%s\\t%s\\t%s %s\\n' "${{PYTEST_CURRENT_TEST:-<no test>}}" "$tag" "$(basename "$0")" "$args" >> {log}
if [ "$tag" = answered ]; then
  # What the real tool says on a CI runner, where nothing is reachable.
  case "$tool" in
    gh) echo "To get started with GitHub CLI, please run:  gh auth login" >&2; exit 4 ;;
    claude) exit 127 ;;
    *) echo "Failed to connect to bus: No such file or directory" >&2; exit 1 ;;
  esac
fi
echo "live fence: $(basename "$0") is not available to tests (stub it, or put a fake in tmp_path)" >&2
exit 3
"""


def _write_shims(directory: Path, log: str, names) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        shim = directory / name
        tool = _ALIASES.get(name, name)
        tag = "answered" if tool in ANSWERED_NAMES else "shim"
        shim.write_text(_SHIM.format(log=shlex.quote(log), tag=tag, tool=shlex.quote(tool)))
        shim.chmod(0o755)


def _stub_dir_without(names: frozenset[str]) -> str:
    """A stub directory for every stubbed tool except ``names``, made once."""
    if not names:
        return _S.shim_dir
    own = Path(_S.base) / ("bin-real-" + "-".join(sorted(names)))
    if not own.is_dir():
        _write_shims(own, _S.log, {n for n in _S.stubbed if _ALIASES.get(n, n) not in names})
    return str(own)


# HOME as the run started. tests/_live_closure.py imports this module before it
# redirects HOME, so this is never the session's private home.
_HOME_AT_IMPORT = os.environ.get("HOME")


def real_home() -> str | None:
    """The home of the user running the suite, whatever HOME says now.

    From the password database. Numeric-UID containers need not have an
    /etc/passwd entry, so HOME as the run started is the fallback (never the
    live HOME, which the live closure has redirected by then); it is usable
    identity only when absolute, otherwise None (deploys then remain refused).
    Tests that must name the real home call this."""
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        home = _HOME_AT_IMPORT
    return home if home and os.path.isabs(home) else None


def install(config) -> None:
    """Arm the fence for this process. Idempotent: an audit hook cannot be removed."""
    if _S.installed:
        return
    _S.pid = os.getpid()
    _S.real_home = real_home()
    _S.orig_path = os.environ.get("PATH", os.defpath)
    base = Path(tempfile.mkdtemp(prefix="genesis-live-fence-"))
    _S.base = str(base)
    _S.shim_dir = str(base / "bin")
    _S.all_stub_dir = str(base / "bin-all")
    _S.log = str(base / "hits.log")
    Path(_S.log).touch()
    # A stub only where the tool is installed: where it is not, a bare call
    # fails as on CI and `shutil.which` keeps answering "not installed".
    stubbed = {n for n in _STUB_NAMES if _resolve(n, {"PATH": _S.orig_path}, os.getcwd())}
    if os.environ.get("GENESIS_CC_BIN"):
        stubbed.add("claude")
    _S.stubbed = frozenset(stubbed)
    _write_shims(Path(_S.shim_dir), _S.log, _S.stubbed)
    _write_shims(Path(_S.all_stub_dir), _S.log, _STUB_NAMES)
    roots = {_real(tempfile.gettempdir())}
    if config.option.basetemp:
        roots.add(_real(str(config.option.basetemp)))
    _S.allowed_roots = tuple(sorted(roots))
    config._genesis_live_fence_dir = str(base)

    os.environ["PATH"] = f"{_S.shim_dir}{os.pathsep}{_S.orig_path}"
    if "claude" in _S.stubbed:
        os.environ["GENESIS_CC_BIN"] = str(Path(_S.shim_dir) / "claude")
    nobus = base / "no-bus"
    _S.saved_bus = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}
    os.environ["XDG_RUNTIME_DIR"] = str(nobus)
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={nobus}/bus"
    # The deploy scripts' own guard (scripts/lib/live_system_guard.sh's
    # refuse_if_test_fenced_live) refuses any run whose acting root is a listed
    # root: this checkout's real root, the real home's ~/.genesis and ~/genesis
    # (listed even when absent at install, since a test may create one mid-run),
    # and an install elsewhere named in
    # GENESIS_LIVE_FENCE_LIVE_ROOT. Its REFUSED line lands in this log, so the
    # test that caused it fails at teardown.
    live = {_real(str(Path(__file__).resolve().parent.parent))}
    if _S.real_home:
        live.update(
            _real(str(root))
            for root in (Path(_S.real_home) / ".genesis", Path(_S.real_home) / "genesis")
        )
    if os.environ.get("GENESIS_LIVE_FENCE_LIVE_ROOT"):
        live.add(_real(os.environ["GENESIS_LIVE_FENCE_LIVE_ROOT"]))
    os.environ["GENESIS_TEST_FENCE_LIVE_ROOTS"] = ":".join(sorted(live))
    os.environ["GENESIS_TEST_FENCE_LOG"] = _S.log
    config.addinivalue_line(
        "markers",
        "user_manager: the test needs a reachable systemd user manager for a read-only "
        "or self-contained operation (a transient --user scope, a unit check); the live "
        "fence lets it run the real systemd-run and keeps the session-bus variables",
    )
    config.addinivalue_line(
        "markers",
        "real_tool(*names): the test deliberately runs the named installed tools for "
        "real (it checks a table against their own --help, or probes the kernel); the "
        "live fence lets those names run and lists the test",
    )
    sys.addaudithook(_hook)
    _S.installed = True


def uninstall_files(config) -> None:
    """Keep the file layer armed for children that outlive pytest.

    Inherited PATH readers must not fall through to installed tools when the
    parent finishes. Unique scratch directories remain for normal temp cleanup;
    reporting ends at the session's final drain, while the shims still refuse.
    The audit hook also remains installed, so same-process reuse stays armed.
    """


# ── per-test enforcement ────────────────────────────────────────────────────
#
# One cursor into the log: everything before it has been attributed. Lines
# between two tests (a detached child refused late) and lines naming another
# test are "late" and fail the session; lines naming the running test fail it.
# A deploy script's own `REFUSED (test fence):` line carries no node id (the
# spec fixes its text), so it attributes to the test it drains under.

_cursor = 0


def _node(line: str) -> str:
    # PYTEST_CURRENT_TEST is "<nodeid> (<phase>)", as both layers write it.
    return line.split("\t", 1)[0].rsplit(" (", 1)[0]


def _tag(line: str) -> str:
    parts = line.split("\t", 2)
    return parts[1] if len(parts) > 1 else ""


def _is_answer(line: str) -> bool:
    return _tag(line) == "answered"


def _is_refusal(line: str) -> bool:
    return line.startswith(_REFUSAL_PREFIX)


def _drain() -> list[str]:
    """Log lines since the cursor; advances the cursor past them."""
    global _cursor  # noqa: PLW0603
    try:
        with open(_S.log, "rb") as f:
            f.seek(_cursor)
            data = f.read()
    except OSError:
        return []
    _cursor += len(data)
    return data.decode("utf-8", "replace").splitlines()


class _Expect:
    """``with expect_violation() as v: ...`` for the fence's own tests: the block
    must be refused. Its in-process refusals do not fail the test (``v.reasons``
    has them); anything else it logged is judged at teardown as usual."""

    def __enter__(self):
        self._before = _drain()
        return self

    def __exit__(self, exc_type, exc, tb):
        drained = _drain()
        self.reasons = [ln for ln in drained if _tag(ln) == "audit" or _is_refusal(ln)]
        _pending.extend(self._before)
        _pending.extend(ln for ln in drained if _tag(ln) != "audit" and not _is_refusal(ln))
        if exc_type is not None and exc_type is not LiveStateViolation:
            return None  # a real failure inside the block: never swallow it
        if exc_type is LiveStateViolation or self.reasons:
            return True
        raise AssertionError(f"expected the live fence to refuse; it did not ({exc_type})")


_pending: list[str] = []  # in-test lines drained early by an _Expect block


def expect_violation() -> _Expect:
    return _Expect()


@pytest.fixture(autouse=True)
def _live_fence(request):
    if not _S.installed:
        yield
        return
    # Fixture-owned, not the shared `monkeypatch`: requesting that would set it
    # up before a test's tmp_path and so tear it down AFTER tmp_path's cleanup,
    # leaving the test's own os.* patches live during pytest's rmtree.
    monkeypatch = pytest.MonkeyPatch()
    between = _drain()  # refused between tests
    _S.answered.extend(ln for ln in between if _is_answer(ln))
    _S.late_hits.extend(ln for ln in between if not _is_answer(ln))
    _pending.clear()
    real = frozenset()
    marker = request.node.get_closest_marker("real_tool")
    if marker is not None:
        # The test deliberately runs the NAMED tools for real (their help text, a
        # kernel probe): no stub and no refusal for those names only. Deploy
        # scripts against live state stay refused. Listed in the summary.
        names = frozenset(marker.args) & DENY_NAMES
        if not names:
            raise pytest.UsageError(f"{request.node.nodeid}: real_tool needs tool names")
        _S.real_tool_names = names
        _S.real_tool_tests.append(f"{request.node.nodeid} ({', '.join(sorted(names))})")
        real |= names
    if request.node.get_closest_marker("user_manager"):
        _S.opted_out = True
        _S.opted_out_tests.append(request.node.nodeid)
        real |= USER_MANAGER_ALLOWED
        for key, value in _S.saved_bus.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
    if real:
        # Swap exactly the fence's own PATH entry for a directory without those
        # stubs (one directory, whichever markers apply).
        parts = os.environ.get("PATH", "").split(os.pathsep)
        parts = [_stub_dir_without(real) if p == _S.shim_dir else p for p in parts]
        monkeypatch.setenv("PATH", os.pathsep.join(parts))
    try:
        yield
    finally:
        _S.opted_out = False
        _S.real_tool_names = frozenset()
        monkeypatch.undo()
    lines = _pending + _drain()
    _pending.clear()
    _S.answered.extend(ln for ln in lines if _is_answer(ln))
    lines = [ln for ln in lines if not _is_answer(ln)]
    mine = [ln for ln in lines if _node(ln) == request.node.nodeid or _is_refusal(ln)]
    _S.late_hits.extend(ln for ln in lines if _node(ln) != request.node.nodeid and not _is_refusal(ln))
    if mine:
        pytest.fail(
            "live fence: this test ran (or tried to run) a real command:\n  " + "\n  ".join(mine),
            pytrace=False,
        )


def pytest_sessionfinish(session, exitstatus):
    if not _S.installed:
        return
    rest = _drain()
    _S.answered.extend(ln for ln in rest if _is_answer(ln))
    _S.late_hits.extend(ln for ln in rest if not _is_answer(ln))
    if _S.late_hits and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter):
    if not _S.installed:
        return
    tr = terminalreporter
    if _S.late_hits:
        tr.section("live fence: refusals outside their test", red=True)
        for line in _S.late_hits:
            tr.write_line(line)
    if _S.answered:
        by_name: dict[str, set[str]] = {}
        for line in _S.answered:
            parts = line.split("\t", 2)
            prog = parts[2].split(" ", 1)[0] if len(parts) > 2 else "?"
            by_name.setdefault(prog, set()).add(_node(line))
        tr.section("live fence: live-state reads answered by a stub (not failed)")
        for prog, nodes in sorted(by_name.items()):
            tr.write_line(f"{prog}: {len(nodes)} tests")
        report = os.environ.get("GENESIS_LIVE_FENCE_REPORT")
        if report:
            Path(report).write_text("\n".join(_S.answered) + "\n")
    if _S.real_tool_tests:
        tr.section("live fence: real_tool tests (installed tools ran for real)")
        for node in _S.real_tool_tests:
            tr.write_line(node)
    if _S.opted_out_tests:
        tr.section("live fence: user_manager tests (real systemd-run allowed)")
        for node in _S.opted_out_tests:
            tr.write_line(node)
