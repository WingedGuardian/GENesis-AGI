"""The credential and state closure: tests run without this machine's secrets.

A test that reaches a real credential can act with it: call a paid API, read a
private repository, push with a stored token. On CI there are none, so the same
test fails or skips there. This plugin, armed for every run by
``tests/conftest.py`` right after the live fence, takes them away locally too,
in the process environment, so every child process inherits the closure:

- ``SECRETS_PATH`` names a file that does not exist, so every loader of
  ``secrets.env`` that honours it (``genesis.env.secrets_path()`` and what
  resolves through it) loads nothing.
  Each test then gets its own such path under its ``tmp_path``, so a script
  that WRITES to ``SECRETS_PATH`` (``restore.sh``) cannot leave a file the next
  test loads. The session-wide value covers what loads at collection time.
- Credential-shaped variables are removed: ``SSH_AUTH_SOCK``, ``GH_TOKEN``,
  ``GITHUB_TOKEN`` and any name containing ``_API_KEY``, ``_SECRET``,
  ``PASSWORD``, ``PASSPHRASE``, ``WEBHOOK``, ``CREDENTIAL``, ``_ACCESS_KEY`` or
  ``_PRIVATE_KEY``, starting ``API_KEY_``, or ending ``_TOKEN``, ``_PASS`` or
  ``_KEY_ID``. Their NAMES (never values) are listed in the run summary.
- ``GH_CONFIG_DIR`` points at an empty directory (no stored gh login, and so no
  git credential helper backed by it), ``GIT_TERMINAL_PROMPT=0`` and
  ``GIT_SSH_COMMAND=false`` stop git from prompting or reaching an ssh agent.
- ``GENESIS_PROACTIVE_HOOK_URL`` points at a closed port, so a test that runs
  the memory hook as a child never posts to the live server.
- ``GENESIS_MCP_LOG_DIR`` names a directory that does not exist, so importing the
  health MCP's browser module opens no log in the shared ``~/tmp``.
- ``HOME`` names a private directory for the whole session, so everything that
  resolves a path through ``Path.home()`` or ``~`` (the internal API token, the
  Guardian's shared CLI policy, caches, stamps, logs, ``~/tmp`` scratch,
  ``~/.claude`` and ``~/.genesis`` credential files) uses it instead of the real
  home. It sits beside basetemp in the per-process leaf ``tests/conftest.py``
  creates, so the reaper and the session cleanup remove it; without that leaf
  (CI, an explicit ``--basetemp``) it is a temp directory beside the explicit
  basetemp, or in the default temp dir. It starts with only ``tmp/``: no
  ``.genesis`` at the start, so a nested run does not take it for an install.
  ``REAL_HOME`` (from the password database) is the home for tests that must name
  it. State paths ``genesis`` modules bound to the real home before this point
  are moved in (``_rebase_bound_paths``) and named in the summary;
  ``PLAYWRIGHT_BROWSERS_PATH`` keeps naming the real browser cache (read-only use)
  so the UI tests still find their browser.

A test that sets its own value (``monkeypatch.setenv``) still wins: the closure
is applied before any test runs. With ``GENESIS_LIVE_E2E=1`` the opt-in
live tests need real credentials, so the secrets half is skipped and the summary
says so; the git, gh and hook-URL closures stay on.
The HOME redirect is skipped too: those tests act on the live install's state.

``GENESIS_DB_PATH`` names a file under the closure's own directory from the
moment this plugin is imported (with the root conftest, before pytest imports
any ``tests/test*/conftest.py``), so a module that resolves the database path
when it is IMPORTED, at module level or as a default argument, binds that file
rather than the checkout's ``data/genesis.db``, the live database in the main
checkout. ``SECRETS_PATH`` is set at the same moment (unless ``GENESIS_LIVE_E2E=1``).
``tests/conftest.py`` (``_isolate_genesis_db_path``) then points it at a per-test
file for the code a test runs and the children it starts. The import-time file is
one per session: a default-argument connect in one test can leave rows another
reads, which is still never the live database.

Not covered (stated residuals):

- a reader that builds its own path to ``secrets.env``: the import-time loaders
  in the Claude Code hooks and ``gmodel`` (their own checkout's file), the eval
  harnesses' and the guardian credential bridge's ``~/genesis/secrets.env``
  fallback, and the guardian passphrase resolver and credential self-heal
  (targets relative to the home their caller names). #3332 makes all but the
  last two honour ``SECRETS_PATH``;
- a path resolved from the password database rather than HOME (OpenSSH's
  ``~/.ssh``), a child process started with an environment that has no ``HOME``
  (Python and bash then fall back to the password database), and an explicit
  ``GENESIS_HOME``;
- a home path a module bound before the redirect as a string, inside a
  container, or outside the ``genesis`` package;
- paths named by other variables: ``tempfile`` writes to ``TMPDIR``, which may
  sit in the real home.

The OS-level sandbox is the boundary for those.
"""

from __future__ import annotations

import atexit
import os
import pwd
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePath

import pytest

_CREDENTIAL_NAME = re.compile(
    r"_API_KEY|^API_KEY_|_TOKEN$|_SECRET|PASSWORD|PASSPHRASE|_PASS$|WEBHOOK"
    r"|CREDENTIAL|_ACCESS_KEY|_PRIVATE_KEY|_KEY_ID$"
)
_EXACT = frozenset({"SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN"})
# Never removed: the run's own switches, and variables that hold a PATH to a
# secrets file (dropping one that points at a sandbox would send its reader
# back to the real default).
_NEVER = re.compile(r"^(PYTEST_|GENESIS_LIVE_)|^(GUARDIAN_SECRETS|GENESIS_SECRETS_PATH)$")
_CLOSED_PORT_URL = "http://127.0.0.1:9"  # discard port: nothing listens
# The home of the user running the suite, whatever HOME says.
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
# Where the real home keeps state (never code): paths under these that ``genesis``
# modules bound before the redirect are moved into the test home.
_STATE_ROOTS = (".genesis", ".claude", "tmp", ".cache", ".config", ".local/share", ".local/state")


class _State:
    installed = False
    base = ""
    removed: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()
    live_e2e = False
    home = ""  # the session's test HOME, "" when not redirected
    rebased: tuple[str, ...] = ()  # module paths moved into it


_S = _State()

# The database path is set when this plugin is IMPORTED, not in install().
# tests/conftest.py names it in ``pytest_plugins``, so pytest imports it while
# registering the root conftest, before it imports any ``tests/test*/conftest.py``
# (which a directory run, CI's bare ``pytest`` included, loads BEFORE
# pytest_configure). A module imported there binds the path at import
# (``DEFAULT_DB_PATH = genesis_db_path()``) and as default arguments
# (``def get_db(path=DEFAULT_DB_PATH)``), which nothing can rebase afterwards.
# MEASURED: set in install(), a full directory run connected to the checkout's
# data/genesis.db from 10 voice tests; in the deployed checkout that is the live
# database.
# SECRETS_PATH likewise: a module that loads secrets.env at import would read
# the real file before install() ran.
_S.base = tempfile.mkdtemp(prefix="genesis-live-closure-")
# Removed at exit when install() never ran (a run the box lock refuses exits in
# pytest_configure first); otherwise the session cleanup owns it.
atexit.register(lambda: _S.installed or shutil.rmtree(_S.base, ignore_errors=True))
os.environ["GENESIS_DB_PATH"] = str(Path(_S.base) / "import-time-genesis.db")
if os.environ.get("GENESIS_LIVE_E2E") != "1":
    os.environ["SECRETS_PATH"] = str(Path(_S.base) / "no-secrets.env")


def credential_names(environ) -> list[str]:
    """The credential-shaped names in ``environ`` that the closure removes."""
    return sorted(
        k for k in environ if not _NEVER.match(k) and (k in _EXACT or _CREDENTIAL_NAME.search(k))
    )


def install(config) -> None:
    """Apply the closure to this process's environment. Idempotent."""
    if _S.installed:
        return
    no_gh = Path(_S.base) / "no-gh"  # _S.base: made at import, see above
    no_gh.mkdir()
    _S.live_e2e = os.environ.get("GENESIS_LIVE_E2E") == "1"
    if not _S.live_e2e:
        os.environ["SECRETS_PATH"] = str(Path(_S.base) / "no-secrets.env")
        names = credential_names(os.environ)
        for name in names:
            del os.environ[name]
        _S.removed = tuple(names)
        _S.remaining = tuple(credential_names(os.environ))
    os.environ["GH_CONFIG_DIR"] = str(no_gh)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["GIT_SSH_COMMAND"] = "false"
    os.environ["GENESIS_PROACTIVE_HOOK_URL"] = _CLOSED_PORT_URL
    # Set at import (see above). If something changed it since, paths bound in
    # between follow the other value: say so rather than hide it.
    db = str(Path(_S.base) / "import-time-genesis.db")
    if os.environ.get("GENESIS_DB_PATH") != db:
        sys.stderr.write(
            f"genesis: GENESIS_DB_PATH changed after the live closure set it "
            f"({os.environ.get('GENESIS_DB_PATH')!r}); restoring {db}\n"
        )
    os.environ["GENESIS_DB_PATH"] = db
    # A directory that does not exist: importing the health MCP's browser module
    # then attaches no log file, instead of appending every genesis.* line of this
    # run to the MCP servers' shared ~/tmp/mcp_health.log.
    os.environ["GENESIS_MCP_LOG_DIR"] = str(Path(_S.base) / "no-mcp-log")
    if not _S.live_e2e:
        _redirect_home(config)
    config._genesis_live_closure_dir = _S.base
    _S.installed = True


def _redirect_home(config) -> None:
    """Point HOME at a private directory for the session (see the module notes).

    Runs after the box lock and basetemp are placed, both of which resolve HOME:
    redirected before them, the lock would look for an install under the empty
    home and silently not lock at all."""
    leaf = getattr(config, "_genesis_basetemp_cleanup", None)
    try:
        if leaf:
            # tests/conftest.py removed any leftover from a dead run with this PID.
            home = Path(leaf) / "home"
            home.mkdir(exist_ok=True)
        else:
            home = _unredirected_home(config)
            config._genesis_live_home_cleanup = str(home)
        (home / "tmp").mkdir(exist_ok=True)
    except OSError as exc:
        # The status quo (the real home) rather than a failed collection; say so.
        sys.stderr.write(f"genesis: test HOME not redirected ({exc}); tests use the real home\n")
        return
    cache = Path(REAL_HOME) / ".cache" / "ms-playwright"
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and cache.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
    os.environ["HOME"] = str(home)
    # Not tempfile.gettempdir(): it is often ~/tmp itself (TMPDIR), which would
    # exempt every state path bound under ~/tmp.
    private = (_S.base, getattr(config, "_genesis_basetemp_cleanup", None))
    _S.rebased = tuple(_rebase_bound_paths(home, private))
    _S.home = str(home)


def _unredirected_home(config) -> Path:
    """The home when basetemp is not redirected (CI, or an explicit --basetemp).

    Beside an explicit basetemp, on the volume its caller chose for temp (the
    install's sandbox passes ``/tmp/bt``), else in the default temp dir (a CI
    runner's on-disk ``/tmp``). Not inside the closure's own directory, which sits
    in ``TMPDIR``, a small policed one in a Claude Code session. Falls back there
    only when the chosen parent cannot be written."""
    basetemp = config.option.basetemp
    parent = str(Path(basetemp).resolve().parent) if basetemp else None
    try:
        return Path(tempfile.mkdtemp(prefix="genesis-test-home-", dir=parent))
    except OSError:
        home = Path(_S.base) / "home"
        home.mkdir()
        return home


def _rebase_bound_paths(home: Path, private=()) -> list[str]:
    """Move state paths ``genesis`` modules bound to the real home before this ran.

    pytest imports every ``tests/test*/conftest.py`` of a directory argument (CI's
    bare ``pytest`` included) before ``pytest_configure``, so the modules those
    import have already resolved ``Path.home()``. MEASURED on ``pytest tests/``:
    74 ``genesis`` modules loaded at this point, 9 module-level paths under the
    real home, 3 of them state (the process-lock directory, the background-session
    directory, the circuit-breaker state file); the rest are the checkout, which
    must not move. So only paths under a state root of the real home are rebased,
    each named in the run summary. A path held as a string or inside a container,
    or bound by a module outside ``genesis``, is not reached.

    ``private`` names directories that are already this run's own (the closure's
    directory and the per-process leaf; the test home is added here), which may sit under a state
    root (``~/tmp``): a path under one is left alone. MEASURED: without that, the
    import-time database path (under the closure's directory in ``~/tmp``) was
    moved, and stopped matching the default arguments bound to it."""
    roots = [Path(REAL_HOME, r) for r in _STATE_ROOTS]
    keep = [Path(os.path.realpath(p)) for p in (*private, home) if p]
    moved = []
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "genesis" or name.startswith("genesis.")):
            continue
        for attr, value in list(vars(module).items()):
            if not isinstance(value, PurePath):
                continue
            real = Path(os.path.realpath(value))
            if any(real == k or k in real.parents for k in keep):
                continue
            if any(value == root or root in value.parents for root in roots):
                setattr(module, attr, type(value)(home, value.relative_to(REAL_HOME)))
                moved.append(f"{name}.{attr}")
    return sorted(moved)


def uninstall_files(config) -> None:
    for attr in ("_genesis_live_home_cleanup", "_genesis_live_closure_dir"):
        path = getattr(config, attr, None)
        if path:
            shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _live_closure_secrets_path(tmp_path):
    """A per-test nonexistent ``SECRETS_PATH``: one test's write is never the
    next test's load.

    A private MonkeyPatch, never the shared ``monkeypatch`` fixture: requesting
    that from an autouse fixture sets it up first and tears it down LAST, after
    conftest's mock-leak guard, which then sees a test's own still-unrestored
    ``monkeypatch.setattr`` as a leak."""
    mp = pytest.MonkeyPatch()
    if _S.installed and not _S.live_e2e:
        mp.setenv("SECRETS_PATH", str(tmp_path / "no-secrets.env"))
    try:
        yield
    finally:
        mp.undo()


def pytest_terminal_summary(terminalreporter):
    if not _S.installed:
        return
    tr = terminalreporter
    if _S.live_e2e:
        tr.section("live closure: GENESIS_LIVE_E2E=1, secrets left in place")
        return
    if _S.home:
        tr.section("live closure: HOME redirected for this run")
        tr.write_line(f"HOME={_S.home} (the real home is {REAL_HOME})")
        if _S.rebased:
            tr.write_line("module paths bound earlier, moved in: " + ", ".join(_S.rebased))
    if _S.removed:
        tr.section("live closure: credential variables removed for this run")
        tr.write_line(", ".join(_S.removed))
