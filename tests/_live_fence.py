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
   process exists, when the program is:

   - one of ``DENY_NAMES`` (claude, systemd-run, systemctl, sudo, gh, loginctl;
     ``claude.exe``/``claude-code`` count as claude) resolving anywhere but a
     test's own temp dir or the fence's stubs. A bare name resolves through the
     call's own ``PATH`` (including a ``PATH=`` the command line sets), so it
     normally finds a stub: ``claude`` (except a bare ``--version``/``--help``)
     and ``sudo`` stubs are refused, so code that finds a stub never mistakes it
     for the real thing; the ``ANSWERED_NAMES`` stubs (and those two ``claude``
     reads) run and answer as the real tool does on a CI runner, where nothing
     is reachable (no bus: exit 1; gh unauthenticated: exit 4), and are
     reported, not failed. A stub exists only for a tool installed on this
     machine, so ``shutil.which`` still says "not installed" where it is not;
   - a deploy script (``scripts/{update,deploy_code_only,restore,backup,
     bootstrap}.sh``) run against live state: with the real ``HOME``, or acting
     on the deployed checkout. Where a script acts is computed per script:
     ``update.sh`` and ``deploy_code_only.sh`` act on their root override
     (``GENESIS_UPDATE_ORIG_DIR``, ``GENESIS_DEPLOY_ROOT``) or else on the
     checkout they live in; ``backup.sh`` and ``restore.sh`` on ``GENESIS_DIR``
     or else ``$HOME/genesis``; ``bootstrap.sh`` on the checkout it lives in.
     Tests run these scripts with a private ``HOME`` and a fixture tree all the
     time, which stays allowed. ``bash -n`` (parse only) is allowed. A command
     named like a deploy script whose file cannot be found (a path the shell
     builds at run time) is judged by the ``HOME`` rule alone.

   Each command is judged as far as the repo's own parser
   (``scripts/hooks/shell_parse.py``) can read it: wrappers (env, timeout,
   setsid, sudo and the like) are stripped and judged themselves, ``-c`` strings
   and here-strings fed to a shell are opened, and every resulting command gets
   both checks, under every ``PATH`` the line sets. Heredoc bodies that only
   feed data to a command (``cat <<EOF``) are dropped first, since the parser
   would otherwise read their lines as commands. Under the ``user_manager``
   opt-out, the command an in-process ``systemd-run`` would start is judged too.
   What the parser does not open (``eval``, a command held in a variable, a
   function body, a script piped or redirected into a shell, ``find -exec``,
   ``su``/``runuser``/``flock``/``script``/``unshare``/``setpriv``) is judged
   only by the PATH stubs, so a bare name there is still caught and an absolute
   path is not.

   The refusal is ``LiveStateViolation``, a ``BaseException``, so a caller's
   ``except OSError`` / ``except Exception`` fallback cannot catch it and try a
   second route (MEASURED: it propagates through both, through an asyncio task
   and through ``asyncio.to_thread``, and a refused ``Popen`` starts nothing).
   In a forked child (``os.spawn*``, a hand-rolled fork-exec) the refusal is
   recorded and the child exits 127 instead, so it never unwinds into a second
   copy of the pytest session.

2. **PATH shims** for processes a child starts (a script under test, a detached
   orchestrator): a directory of refusing, logging stubs goes first on ``PATH``,
   ``GENESIS_CC_BIN`` points at the ``claude`` stub, and the session-bus
   variables point at a path that does not exist (the #2863 fence, generalized
   to the whole suite). NOT caught here, the stated residuals the OS-level
   sandbox exists for: an absolute path in a child (``/usr/bin/systemctl``); a
   child given a hand-built ``env`` without the fence's ``PATH`` and bus
   variables (scripts that default ``XDG_RUNTIME_DIR`` when it is unset then
   reach the real user manager); a ``systemd-run`` a child starts in a
   ``user_manager`` test.

Every refusal, from either layer, is appended to one log with the test's node
id. The test that caused it fails at teardown even if the code swallowed the
exception; a hit that arrives after its test ended (a detached child) fails the
session.

Opt-outs, each listed in the terminal summary: a test marked ``user_manager``
may run the real ``systemd-run`` (for a transient ``--user`` scope or a unit
check) and keeps the bus variables, while what it starts is still judged; a
test marked ``real_tool("gh")`` deliberately runs the named installed tools
(their own ``--help``, a kernel probe) and gets no stub for those names. Deploy
scripts against live state stay refused for both.
"""

from __future__ import annotations

import importlib.util
import os
import pwd
import re
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
DEPLOY_SCRIPTS = frozenset(
    {"update.sh", "deploy_code_only.sh", "restore.sh", "backup.sh", "bootstrap.sh"}
)
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
# A heredoc fed to one of these runs its body.
_INTERPRETERS = _SHELLS | {"python", "python3", "node", "perl", "ruby", "source", "."}
# Root overrides: where a deploy script acts instead of the checkout it lives in
# (a test seam: a fixture tree in place of the checkout).
_ROOT_OVERRIDES = {
    "deploy_code_only.sh": "GENESIS_DEPLOY_ROOT",
    "update.sh": "GENESIS_UPDATE_ORIG_DIR",
}
# These two act on ${GENESIS_DIR:-$HOME/genesis} (backup.sh, restore.sh), not
# on where they live.
_GENESIS_DIR_SCRIPTS = frozenset({"backup.sh", "restore.sh"})
# systemd-run options that take their value as the next argument (systemd 255).
_SYSTEMD_RUN_VALUE_OPTS = frozenset(
    {
        "-H", "--host", "-M", "--machine", "-u", "--unit", "-p", "--property",
        "-E", "--setenv", "--description", "--slice", "--expand-environment",
        "--service-type", "--uid", "--gid", "--nice", "--working-directory",
        "--path-property", "--socket-property", "--on-active", "--on-boot",
        "--on-startup", "--on-unit-active", "--on-unit-inactive", "--on-calendar",
        "--timer-property", "--job-mode",
    }
)  # fmt: skip
_EVENTS = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system"})
# Exactly these argv tails of `claude` only READ the binary (CI: not installed),
# so the stub answers them; every other `claude` call is refused.
_CLAUDE_READS = (["--version"], ["--help"])
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
# Captured at import: tests monkeypatch shutil.which, and the fence must keep
# resolving programs the way exec will.
_which = shutil.which


class LiveStateViolation(BaseException):  # noqa: N818 - named for what it is
    """A test tried to run a real command against this machine."""


class _State:
    installed = False
    pid = 0  # the pytest process; a refusal in a forked child exits instead
    real_home = ""
    orig_path = ""  # PATH before the fence put its stubs in front
    shim_dir = ""
    all_stub_dir = ""  # every DENY_NAMES stub, for the fence's own tests
    base = ""  # the fence's own files: every stub directory lives under it
    log = ""
    stubbed: frozenset[str] = frozenset()  # names with a stub: installed here
    allowed_roots: tuple[str, ...] = ()
    opted_out = False  # the running test is marked user_manager
    real_tool_names: frozenset[str] = frozenset()  # what this real_tool test may run
    live_roots: frozenset[str] = frozenset()  # the deployed checkout on this machine
    real_tool_tests: list[str] = []
    opted_out_tests: list[str] = []
    late_hits: list[str] = []
    answered: list[str] = []  # stub answers: reported, never failed
    saved_bus: dict[str, str | None] = {}


_S = _State()


# ── decision ────────────────────────────────────────────────────────────────


def _real(path: str) -> str:
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


def _with_assignments(env, words) -> dict | Mapping | None:
    """``env`` plus the ``NAME=value`` words a command line sets in front of a
    command (``env PATH=/usr/bin systemctl``, ``PATH=/x cmd``)."""
    assigned = {w.split("=", 1)[0]: w.split("=", 1)[1] for w in words if _ASSIGN.match(w)}
    if not assigned:
        return env
    base = os.environ if env is None else env
    merged = {os.fsdecode(k): os.fsdecode(v) for k, v in base.items()} if base else {}
    merged.update(assigned)
    return merged


def _line_paths(line: str, env) -> list:
    """Environments to resolve names under: the call's own, plus one per
    ``PATH=`` the line sets anywhere (on a wrapper, inside a ``-c`` string, by
    ``export``), and the default PATH when ``env -i``/``env -u PATH`` clears it.
    A name is judged under each, since any of them may be the one in force."""
    values = [v.strip("'\"") for v in re.findall(r"(?:^|[\s;&|(`'\"])PATH=([^\s;&|)`]+)", line)]
    if re.search(r"\benv\s+(?:-\w*i\b|--ignore-environment|-u\s*PATH\b|--unset[= ]PATH\b)", line):
        values.append(os.defpath)
    return [_with_assignments(env, [f"PATH={v}"]) for v in dict.fromkeys(values)]


def _resolve(program: str, env, cwd=None) -> str | None:
    """The file a program name runs: a path (relative to ``cwd``) when it has a
    '/', else via the call's PATH. None when there is no such file: it cannot run,
    and the code under test should see the same FileNotFoundError it would on CI."""
    if "/" in program:
        path = program if os.path.isabs(program) else os.path.join(cwd or os.getcwd(), program)
        return path if os.path.exists(path) else None
    search = _env_get(env, "PATH") or os.defpath
    return _which(program, path=search)


def _shell_parse():
    """The repo's own shell parser (scripts/hooks/shell_parse.py, stdlib-only),
    loaded once under a private module name so it cannot shadow the `shell_parse`
    other tests import."""
    global _SP  # noqa: PLW0603
    if _SP is None:
        path = Path(__file__).resolve().parent.parent / "scripts" / "hooks" / "shell_parse.py"
        spec = importlib.util.spec_from_file_location("_live_fence_shell_parse", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _SP = module
    return _SP


_SP = None


def _heredoc_ops(line: str) -> list[int]:
    """Offsets of the ``<<`` heredoc operators in one line of shell: not inside
    quotes, a ``#`` comment or an arithmetic ``$((...))``, and not ``<<<``."""
    found: list[int] = []
    quote = ""
    arith = 0
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if line.startswith("$((", i):
            arith += 1
            i += 3
            continue
        if arith and line.startswith("))", i):
            arith -= 1
            i += 2
            continue
        if c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t;&|("):
            break
        elif not arith and line.startswith("<<", i):
            if line.startswith("<<<", i):
                i += 3
                continue
            found.append(i)
            i += 2
            continue
        i += 1
    return found


def _runs_code(words: str) -> bool:
    """Whether a command line names a shell or interpreter anywhere, or pipes on:
    a heredoc or here-string on that line may be code, not data."""
    if "|" in words:
        return True
    return any(os.path.basename(w) in _INTERPRETERS for w in re.findall(r"[\w./+-]+", words))


def _drop_heredoc_bodies(text: str) -> str:
    """``text`` without the bodies of heredocs that only feed DATA to a command
    (``cat <<EOF``, ``tee f <<'EOF'``): the parser has no heredoc state and would
    read their lines as commands. A body is kept when it may run: its line names
    a shell or interpreter anywhere or pipes onward, or (unquoted delimiter) it
    holds a ``$(...)`` or backtick substitution, which the shell expands."""
    if "<<" not in text or "\n" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        ops = _heredoc_ops(line)
        if not ops:
            continue
        m = _HEREDOC.match(line, ops[0])
        if not m:
            continue
        dash, quote, delim = m.group(1), m.group(2), m.group(3)
        body: list[str] = []
        end = None
        while i < len(lines):
            candidate = lines[i]
            i += 1
            if (candidate.lstrip("\t") if dash else candidate) == delim:
                end = candidate
                break
            body.append(candidate)
        runs = _runs_code(line[: m.start()] + " " + line[m.end() :])
        expands = not quote and any("$(" in b or "`" in b for b in body)
        # A second heredoc on the line: its body follows; keep both (over-read).
        if runs or expands or end is None or len(ops) > 1:
            out.extend(body)
            if end is not None:
                out.append(end)
    return "\n".join(out)


def _here_strings(text: str) -> list[str]:
    """The strings fed by ``<<<`` to a shell or interpreter: code, not data."""
    found = []
    for line in text.split("\n"):
        for m in re.finditer(r"<<<\s*(?:'([^']*)'|\"([^\"]*)\"|(\S+))", line):
            if _runs_code(line[: m.start()]):
                found.append(next(g for g in m.groups() if g is not None))
    return found


def _segments(command: str) -> list[tuple[list[str], list[str]]]:
    """Every command the shell would run for ``command``, as ``(argv, wrappers)``:
    the house parser strips wrappers (env, timeout, setsid, sudo and the like) and
    opens ``-c`` strings; ``wrappers`` are the words in front of that command it
    stripped, so a wrapper that is itself refused (``sudo``) is still seen, and a
    ``PATH=`` set there is honored. An unparseable command yields only its plain
    split (stated residual)."""
    try:
        segs, _blind = _shell_parse().analyze_checked(command)
    except Exception:  # noqa: BLE001 - a parser fault must not break every test
        segs = []
    out = []
    for seg in segs:
        try:
            raw = shlex.split(seg.raw)
        except ValueError:
            raw = []
        if not seg.argv:
            # Only wrappers (`sudo -v`, a bare `sudo`): judge them all.
            if raw:
                out.append(([], raw))
            continue
        cut = raw.index(seg.argv[0]) if seg.argv[0] in raw else 0
        out.append((list(seg.argv), raw[:cut]))
    if not out:
        try:
            out = [(shlex.split(command), [])]
        except ValueError:
            out = []
    return out


def _tool_name(program: str) -> str:
    """The DENY_NAMES entry ``program`` runs, by its name or, for a path, by the
    name of the file it resolves to; else its plain basename."""
    name = os.path.basename(program)
    name = _ALIASES.get(name, name)
    if name not in DENY_NAMES and "/" in program:
        target = os.path.basename(_real(program))
        target = _ALIASES.get(target, target)
        if target in DENY_NAMES:
            return target
    return name


def _deny_reason(program: str, argv: list[str], env, cwd) -> str | None:
    name = _tool_name(program)
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


def _deploy_script(path: str, cwd) -> tuple[str, str | None] | None:
    """``(name, checkout root)`` when ``path`` runs a deploy script; the root is
    None when the file cannot be found (a path the shell builds at run time, a
    relative path from a directory only the shell knows)."""
    full = path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)
    if os.path.exists(full):
        real = Path(_real(full))
        if real.name in DEPLOY_SCRIPTS and real.parent.name == "scripts":
            return real.name, str(real.parent.parent)
        return None
    name = os.path.basename(path)
    return (name, None) if name in DEPLOY_SCRIPTS else None


def _script_args(argv: list[str]) -> list[str]:
    """The script a script-running program would run: a shell's first operand
    (none with ``-c``, whose string is judged as commands; none with ``-n``, parse
    only, when it comes BEFORE the script), what ``source``/``.`` read, and any
    operand of ``systemd-run``."""
    head = os.path.basename(argv[0])
    if head in _SHELLS:
        for i, arg in enumerate(argv[1:], 1):
            if arg.startswith("--") or arg in ("-o", "+o", "-O", "+O"):
                continue
            if arg.startswith(("-", "+")):
                if "c" in arg[1:] or "n" in arg[1:]:
                    return []
                continue
            if argv[i - 1] in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                continue
            return [arg]
        return []
    if head in ("source", "."):
        return argv[1:2]
    if head == "systemd-run":
        return [a for a in argv[1:] if not a.startswith("-")]
    return []


def _deploy_reason(argv: list[str], env, cwd) -> str | None:
    """A deploy script run against live state: by the real HOME, or acting on the
    live install's checkout (see the module docstring for where each acts)."""
    if not argv:
        return None
    for arg in [argv[0], *_script_args(argv)]:
        found = _deploy_script(arg, cwd)
        if found is None:
            continue
        name, root = found
        home = _env_get(env, "HOME")
        if name in _GENESIS_DIR_SCRIPTS:
            target = _env_get(env, "GENESIS_DIR") or (
                os.path.join(home, "genesis") if home else None
            )
        else:
            target = _env_get(env, _ROOT_OVERRIDES.get(name, "")) or root
        target = _real(target) if target else None
        if not home or _real(home) == _real(_S.real_home):
            why = "the real HOME"
        elif target in _S.live_roots:
            why = f"the live install's checkout as its root ({target})"
        else:
            continue
        return (
            f"{name} run with {why}: a deploy script against this machine's live "
            "state (give it a private HOME and point its root at a fixture tree)"
        )
    return None


def _systemd_run_payload(argv: list[str]) -> list[str]:
    """The command a ``systemd-run`` argv starts."""
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return argv[i + 1 :]
        if not arg.startswith("-") or arg == "-":
            return argv[i:]
        if arg in _SYSTEMD_RUN_VALUE_OPTS:
            i += 1
        i += 1
    return []


def judge(program, argv, env, cwd=None) -> str | None:
    """Why this exec must be refused, or None to allow it.

    ``program`` is what the kernel will run (a path or a bare name), ``argv``
    the argument vector, ``env`` the child's environment (None = inherited),
    ``cwd`` its working directory (None = the current one). Every command the
    argv would run is judged, wrappers and ``-c`` strings included.
    """
    if program is None and argv:
        program = argv[0]
    if program is None:
        return None
    program = os.fsdecode(program)
    argv = [os.fsdecode(a) for a in (argv or [])]
    cwd = os.fsdecode(cwd) if cwd is not None else None
    reason = _deny_reason(program, argv, env, cwd)
    if reason:
        return reason
    if not argv:
        return None
    reason = _deploy_reason(argv, env, cwd)
    if reason:
        return reason
    raw_line = shlex.join(argv)
    for code in (c for arg in argv for c in _here_strings(arg)):
        reason = judge("sh", ["sh", "-c", code], env, cwd)
        if reason:
            return reason
    line = shlex.join(_drop_heredoc_bodies(a) for a in argv)
    alt_envs = _line_paths(raw_line, env)
    for seg, wrappers in _segments(line):
        seg_env = _with_assignments(env, wrappers)
        for word in wrappers:
            for e in (seg_env, *alt_envs):
                reason = _deny_reason(word, [word], e, cwd)
                if reason:
                    return reason
        if not seg:
            continue
        for e in (seg_env, *alt_envs):
            reason = _deny_reason(seg[0], seg, e, cwd)
            if reason:
                return reason
        reason = _deploy_reason(seg, seg_env, cwd)
        if reason:
            return reason
        if _S.opted_out and _tool_name(seg[0]) == "systemd-run":
            # A real systemd-run starts its payload in the user manager, under
            # that manager's PATH, where no stub stands in for anything.
            payload = _systemd_run_payload(seg)
            if payload:
                unit_env = dict(_with_assignments(seg_env, ["PATH=" + _S.orig_path]))
                reason = judge(payload[0], payload, unit_env, cwd)
                if reason:
                    return f"systemd-run payload: {reason}"
    return None


def _record(line: str) -> None:
    if not _S.log:
        return
    try:
        fd = os.open(_S.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line.replace("\n", " ") + "\n").encode())
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
        # A shell command line: judged as `sh -c <line>`, which the parser opens.
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
if [ "$(basename "$0")" = claude ] && [ $# -eq 1 ]; then
  case "$1" in --version|--help) tag=answered ;; esac
fi
args=$(printf '%s' "$*" | tr '\\n\\t' '  ')
printf '%s\\t%s\\t%s %s\\n' "${{PYTEST_CURRENT_TEST:-<no test>}}" "$tag" "$(basename "$0")" "$args" >> {log}
if [ "$tag" = answered ]; then
  # What the real tool says on a CI runner, where nothing is reachable.
  case "$(basename "$0")" in
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
        tag = "answered" if name in ANSWERED_NAMES else "shim"
        shim.write_text(_SHIM.format(log=shlex.quote(log), tag=tag))
        shim.chmod(0o755)


def _stub_dir_without(names: frozenset[str]) -> str:
    """A stub directory for every stubbed tool except ``names``, made once."""
    if not names:
        return _S.shim_dir
    own = Path(_S.base) / ("bin-real-" + "-".join(sorted(names)))
    if not own.is_dir():
        _write_shims(own, _S.log, _S.stubbed - names)
    return str(own)


def install(config) -> None:
    """Arm the fence for this process. Idempotent: an audit hook cannot be removed."""
    if _S.installed:
        return
    _S.pid = os.getpid()
    _S.real_home = pwd.getpwuid(os.getuid()).pw_dir
    _S.orig_path = os.environ.get("PATH", "")
    base = Path(tempfile.mkdtemp(prefix="genesis-live-fence-"))
    _S.base = str(base)
    _S.shim_dir = str(base / "bin")
    _S.all_stub_dir = str(base / "bin-all")
    _S.log = str(base / "hits.log")
    Path(_S.log).touch()
    # A stub only where the tool is installed: where it is not, a bare call
    # fails as on CI and `shutil.which` keeps answering "not installed".
    stubbed = {n for n in DENY_NAMES if _which(n, path=_S.orig_path or os.defpath)}
    if os.environ.get("GENESIS_CC_BIN"):
        stubbed.add("claude")
    _S.stubbed = frozenset(stubbed)
    _write_shims(Path(_S.shim_dir), _S.log, _S.stubbed)
    _write_shims(Path(_S.all_stub_dir), _S.log, DENY_NAMES)
    # The deployed install, as a root deploy scripts may not act on. A CI
    # checkout is never here, so the rule is inert there. An install elsewhere
    # names its checkout in GENESIS_LIVE_FENCE_LIVE_ROOT.
    live = {Path(_S.real_home) / "genesis"}
    if os.environ.get("GENESIS_LIVE_FENCE_LIVE_ROOT"):
        live.add(Path(os.environ["GENESIS_LIVE_FENCE_LIVE_ROOT"]))
    _S.live_roots = frozenset(
        _real(str(root)) for root in live if (root / "scripts" / "update.sh").is_file()
    )
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
    config.addinivalue_line(
        "markers",
        "user_manager: the test needs a reachable systemd user manager for a read-only "
        "or self-contained operation (a transient --user scope, a unit check); the live "
        "fence lets it run the real systemd-run, judges what that starts, and keeps "
        "the session-bus variables",
    )
    config.addinivalue_line(
        "markers",
        "real_tool(*names): the test deliberately runs the named installed tools for "
        "real (it checks a table against their own --help, or probes the kernel); the "
        "live fence lets those names run and lists the test",
    )
    _shell_parse()  # load now: never import from inside the hook, mid-test
    sys.addaudithook(_hook)
    _S.installed = True


def uninstall_files(config) -> None:
    base = getattr(config, "_genesis_live_fence_dir", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


# ── per-test enforcement ────────────────────────────────────────────────────
#
# One cursor into the log: everything before it has been attributed. Lines
# between two tests (a detached child refused late) and lines naming another
# test are "late" and fail the session; lines naming the running test fail it.

_cursor = 0


def _node(line: str) -> str:
    # PYTEST_CURRENT_TEST is "<nodeid> (<phase>)", as both layers write it.
    return line.split("\t", 1)[0].rsplit(" (", 1)[0]


def _tag(line: str) -> str:
    parts = line.split("\t", 2)
    return parts[1] if len(parts) > 1 else ""


def _is_answer(line: str) -> bool:
    return _tag(line) == "answered"


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
        self.reasons = [ln for ln in drained if _tag(ln) == "audit"]
        _pending.extend(self._before)
        _pending.extend(ln for ln in drained if _tag(ln) != "audit")
        if exc_type is LiveStateViolation:
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
    mine = [ln for ln in lines if _node(ln) == request.node.nodeid]
    _S.late_hits.extend(ln for ln in lines if _node(ln) != request.node.nodeid)
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
