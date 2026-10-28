"""The live closure: tests and their children run without this machine's live state.

The live fence (``tests/_live_fence.py``) stops a test from starting a real
process. This plugin, armed for every run by ``tests/conftest.py`` right after
the fence, closes what a process can still reach WITHOUT starting anything: the
credentials, paths and endpoints it inherits through the environment. It sets
them in the process environment, so every child a test starts inherits the
closure too:

- ``SECRETS_PATH`` names a file that does not exist, so every loader of
  ``secrets.env`` that honours it (``genesis.env.secrets_path()`` and what
  resolves through it) loads nothing. Each test then gets its own such path
  under its ``tmp_path``, so a script that WRITES to ``SECRETS_PATH``
  (``restore.sh``) cannot leave a file the next test loads. The session-wide
  value, set at import, covers what loads at collection time.
- Credential-shaped variables are removed: ``SSH_AUTH_SOCK``, ``GH_TOKEN``,
  ``GITHUB_TOKEN``, ``GIT_ASKPASS``, ``SSH_ASKPASS``, a bare ``API_KEY``, names
  starting ``VSCODE_GIT_`` (an IDE terminal's route to its own GitHub login), and
  any name containing ``_API_KEY``, ``_SECRET``, ``PASSWORD``, ``PASSPHRASE``,
  ``WEBHOOK``, ``CREDENTIAL``, ``_ACCESS_KEY`` or ``_PRIVATE_KEY``, starting
  ``API_KEY_``, or ending ``_TOKEN``, ``_PASS``, ``_KEY_ID`` or ``_AUTH_CONFIG``.
  Their NAMES (never values) are listed in the run summary.
- ``GH_CONFIG_DIR`` points at an empty directory (no stored gh login, and so no
  git credential helper backed by it). ``GIT_TERMINAL_PROMPT=0`` and
  ``GIT_SSH_COMMAND=false`` stop git from prompting or reaching an ssh agent.
  ``GIT_CONFIG_GLOBAL=/dev/null``, ``GIT_CONFIG_NOSYSTEM=1`` and
  ``GIT_CONFIG_SYSTEM=/dev/null`` close git's user and system configuration
  (``~/.gitconfig``, ``$XDG_CONFIG_HOME/git/config``, ``/etc/gitconfig``) and any
  credential helper they name, whatever ``HOME`` or ``XDG_CONFIG_HOME`` say; the
  ``GIT_CONFIG_PARAMETERS``/``GIT_CONFIG_COUNT``/``_KEY_n``/``_VALUE_n`` config
  git takes from the environment is removed. A test that needs a git identity
  sets it itself, as it must on CI.
- ``GENESIS_DB_PATH`` names a file under the closure's own directory from the
  moment this plugin is IMPORTED (see below), and ``tests/conftest.py``
  (``_isolate_genesis_db_path``) points it at a per-test file for the code a
  test runs and the children it starts. Unset, ``genesis.env.genesis_db_path()``
  resolves to the checkout's ``data/genesis.db``, which in the deployed checkout
  is the live database.
- ``GENESIS_PROACTIVE_HOOK_URL`` points at a closed port, so a test that runs
  the memory hook as a child never posts to the live server.
- ``GENESIS_MCP_LOG_DIR`` names a directory that does not exist, so importing the
  health MCP's browser module opens no log in the shared ``~/tmp``.
- ``HOME`` names a private directory for the session, with the four XDG base
  directories inside it, so everything resolved through ``Path.home()``, ``~``
  or XDG (tokens, the Guardian's shared policy file, caches, stamps, logs,
  ``~/tmp`` scratch, ``~/.claude``, ``~/.genesis``, tool credential files) uses
  it instead of the real home. It is made when this plugin is imported, before
  any ``genesis`` module loads, so no module binds the real home first (the
  summary names any that had; none is expected). It sits in the real home's
  big-tmp directory where the suite's basetemp redirect applies, else in the
  default temp dir; ``tests/conftest.py`` places the box lock and basetemp with
  ``HOME`` set back to the real home and removes this one at unconfigure.
  ``tests._live_fence.real_home()`` names the real home for tests that must;
  ``PLAYWRIGHT_BROWSERS_PATH`` keeps naming the real browser cache.

Everything but ``SECRETS_PATH``, ``GENESIS_DB_PATH`` and ``HOME`` is set in
``pytest_configure``: a ``tests/test*/conftest.py`` that read a credential, ran
git, or imported the memory hook or the health MCP's browser module would see
the real values first (none does).

A test that sets its own value (``monkeypatch.setenv``) still wins: the closure
is applied before any test runs. With ``GENESIS_LIVE_E2E=1`` the opt-in live
tests need real credentials and act on the live install's state, so
``SECRETS_PATH``, the credential removal and the HOME redirect are skipped and
the summary says so; the git, gh, database, hook-URL and log closures stay on.

Why at import: ``tests/conftest.py`` names this plugin in ``pytest_plugins``, so
pytest imports it while registering the root conftest, before it imports any
``tests/test*/conftest.py``. A directory run (CI's bare ``pytest`` included)
imports those BEFORE ``pytest_configure``, and a module imported there binds the
database path at import (``DEFAULT_DB_PATH = genesis_db_path()``) and as default
arguments (``def init_db(path=DEFAULT_DB_PATH)``), which nothing can change
afterwards. MEASURED on the rework's predecessor (#3333): set in configure, a
full directory run had 10 voice tests open the checkout's ``data/genesis.db``.
``SECRETS_PATH`` likewise: a module that loads ``secrets.env`` at import would
read the real file before configure. The import-time database file is one per
session, so a default-argument connect in one test can leave rows another reads;
it is still never the live database.

Not covered (stated residuals): a reader that builds its own path to
``secrets.env`` or the database instead of honouring these variables; a
credential under a name the rule does not match; a path resolved from the
password database rather than HOME (OpenSSH finds ``~/.ssh`` that way), and a
child started with no ``HOME`` at all (Python and bash then fall back to the
real home); an explicit ``GENESIS_HOME``; ``tempfile`` output, which follows
``TMPDIR``; a child started with an environment that does not carry these
variables; a repository-local ``core.askPass`` or
``credential.helper``; and every credential name class listed in
``docs/reference/testing.md`` ("Not covered").
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from tests import _live_fence

_CREDENTIAL_NAME = re.compile(
    r"_API_KEY|^API_KEY_|^API_KEY$|_TOKEN$|_SECRET|PASSWORD|PASSPHRASE|_PASS$|WEBHOOK"
    r"|CREDENTIAL|_ACCESS_KEY|_PRIVATE_KEY|_KEY_ID$|_AUTH_CONFIG$"
)
# The askpass programs answer git's and ssh's credential prompts even under
# GIT_TERMINAL_PROMPT=0 (MEASURED); an IDE terminal routes VSCODE_GIT_* to its
# own GitHub login.
_EXACT = frozenset({"SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"})
_CREDENTIAL_PREFIX = ("VSCODE_GIT_",)
# Config git takes from the environment instead of a file (`git -c` sets the
# first): it can name a credential helper whatever the config files say.
_GIT_ENV_CONFIG = re.compile(r"^GIT_CONFIG_(PARAMETERS|COUNT|KEY_\d+|VALUE_\d+)$")
# Never removed: the run's own switches, and variables that hold a PATH to a
# secrets file (dropping one that points at a sandbox would send its reader
# back to the real default).
_NEVER = re.compile(r"^(PYTEST_|GENESIS_LIVE_)|^(GUARDIAN_SECRETS|GENESIS_SECRETS_PATH)$")
_CLOSED_PORT_URL = "http://127.0.0.1:9"  # discard port: nothing listens


_XDG_ROOTS = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_CACHE_HOME": ".cache",
    "XDG_STATE_HOME": ".local/state",
}


class _State:
    installed = False
    base = ""  # the closure's own directory, made at import
    live_e2e = False
    removed: tuple[str, ...] = ()
    home = ""  # the session's test HOME, "" when not redirected
    real_home: str | None = None
    # HOME as the run started (None when unset). The box lock and basetemp are
    # placed from this, as Path.home() places them without the closure.
    home_before: str | None = None
    # genesis modules already loaded when this plugin was imported: each could
    # have bound the real home before the redirect. Expected empty.
    genesis_at_import: tuple[str, ...] = ()


_S = _State()


def _load_tmp_policy():
    """``genesis/util/tmp.py`` by file path, under a private name.

    Importing it as ``genesis.util.tmp`` would first run
    ``genesis/util/__init__.py``, whose ``process_lock`` binds ``Path.home()``
    at import, before the redirect below. The file imports only os and pathlib."""
    path = Path(__file__).resolve().parent.parent / "src" / "genesis" / "util" / "tmp.py"
    spec = importlib.util.spec_from_file_location("_genesis_live_closure_tmp_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _home_parent(real: str | None) -> str | None:
    """Where the test home goes: the real home's big-tmp directory
    (``GENESIS_BIG_TMP``, else ``<real home>/tmp``, created when missing as
    ``big_tmp_dir()`` does) when the suite's basetemp redirect applies there,
    else the default temp dir (``None``).

    ``real`` is the home ``Path.home()`` gave as the run started. An explicit
    ``--basetemp`` is not known when this runs (no config object at plugin
    import), so it is judged as absent."""
    if real is None:
        return None
    try:
        policy = _load_tmp_policy()
        if not policy.should_redirect_pytest_basetemp(
            current_basetemp=None,
            ci_env=os.environ.get("CI"),
            tmpdir_env=os.environ.get("TMPDIR"),
        ):
            return None
    except Exception as exc:  # noqa: BLE001 - placement only; the default temp dir is safe
        sys.stderr.write(f"genesis: test HOME placed in the default temp dir ({exc!r})\n")
        return None
    big = os.environ.get("GENESIS_BIG_TMP") or os.path.join(real, "tmp")
    try:
        os.makedirs(big, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"genesis: test HOME placed in the default temp dir ({exc})\n")
        return None
    return big


def _starting_home() -> str | None:
    """``Path.home()`` as the run started: HOME when set and absolute, else the
    passwd entry (``real_home()``)."""
    if _S.home_before and os.path.isabs(_S.home_before):
        return _S.home_before
    return _S.real_home


def _redirect_home() -> None:
    """Point HOME, and the four XDG base directories, at a private directory for
    the session (see the module notes). On failure the run keeps the real home
    and says so: the status quo, never a failed collection."""
    try:
        parent = _home_parent(_starting_home())
        home = Path(tempfile.mkdtemp(prefix="genesis-test-home-", dir=parent))
        (home / "tmp").mkdir()
    except OSError as exc:
        sys.stderr.write(f"genesis: test HOME not redirected ({exc}); tests use the real home\n")
        return
    started = _starting_home()
    if started is not None:
        # Playwright resolves its cache from HOME, as the lock and basetemp do.
        cache = Path(started) / ".cache" / "ms-playwright"
        if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and cache.is_dir():
            # Read-only use: the UI tests still find their browser.
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
    os.environ["HOME"] = str(home)
    for name, rel in _XDG_ROOTS.items():
        os.environ[name] = str(home / rel)
    _S.home = str(home)


# Set when this plugin is IMPORTED, not in install(): see the module docstring.
_S.base = tempfile.mkdtemp(prefix="genesis-live-closure-")
# Removed at exit when install() never ran (a run the box lock refuses exits in
# pytest_configure first); otherwise uninstall_files() owns it. Never
# unconditional: a forked child of a test that exits through sys.exit runs
# atexit too, and would delete the directory the parent is still using.
atexit.register(lambda: _S.installed or shutil.rmtree(_S.base, ignore_errors=True))
_IMPORT_TIME_DB = str(Path(_S.base) / "import-time-genesis.db")
_NO_SECRETS = str(Path(_S.base) / "no-secrets.env")
os.environ["GENESIS_DB_PATH"] = _IMPORT_TIME_DB
if os.environ.get("GENESIS_LIVE_E2E") != "1":
    os.environ["SECRETS_PATH"] = _NO_SECRETS
_S.real_home = _live_fence.real_home()
_S.home_before = os.environ.get("HOME")
if os.environ.get("GENESIS_LIVE_E2E") != "1":
    _redirect_home()
# Taken after the redirect, so a genesis import made while placing it counts too.
_S.genesis_at_import = tuple(
    sorted(m for m in sys.modules if m == "genesis" or m.startswith("genesis."))
)
if _S.genesis_at_import:
    sys.stderr.write(
        "genesis: modules loaded before the live closure redirected HOME may hold "
        "the real home: " + ", ".join(_S.genesis_at_import) + "\n"
    )
# The same fallback as the closure's own directory; tests/conftest.py removes it
# in pytest_unconfigure (read-only trees such as git objects need its forced form).
atexit.register(lambda: _S.installed or not _S.home or shutil.rmtree(_S.home, ignore_errors=True))


def credential_names(environ) -> list[str]:
    """The credential-shaped names in ``environ`` that the closure removes."""
    return sorted(
        k
        for k in environ
        if not _NEVER.match(k)
        and (k in _EXACT or k.startswith(_CREDENTIAL_PREFIX) or _CREDENTIAL_NAME.search(k))
    )


def install(config) -> None:
    """Apply the closure to this process's environment. Idempotent."""
    if _S.installed:
        return
    _S.live_e2e = os.environ.get("GENESIS_LIVE_E2E") == "1"
    if not _S.live_e2e:
        os.environ["SECRETS_PATH"] = _NO_SECRETS
        names = credential_names(os.environ)
        for name in names:
            del os.environ[name]
        _S.removed = tuple(names)
    no_gh = Path(_S.base) / "no-gh"
    no_gh.mkdir(exist_ok=True)
    os.environ["GH_CONFIG_DIR"] = str(no_gh)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["GIT_SSH_COMMAND"] = "false"
    os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
    # NOSYSTEM alone still lets an explicit `git config --system` read the system
    # file (MEASURED, git 2.43); GIT_CONFIG_SYSTEM closes that too.
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_CONFIG_SYSTEM"] = os.devnull
    for name in [k for k in os.environ if _GIT_ENV_CONFIG.match(k)]:
        del os.environ[name]
    # Set at import. If something changed it since, paths bound in between
    # follow the other value: say so rather than hide it.
    if os.environ.get("GENESIS_DB_PATH") != _IMPORT_TIME_DB:
        sys.stderr.write(
            f"genesis: GENESIS_DB_PATH changed after the live closure set it "
            f"({os.environ.get('GENESIS_DB_PATH')!r}); restoring {_IMPORT_TIME_DB}\n"
        )
    os.environ["GENESIS_DB_PATH"] = _IMPORT_TIME_DB
    os.environ["GENESIS_PROACTIVE_HOOK_URL"] = _CLOSED_PORT_URL
    # A directory that does not exist: importing the health MCP's browser module
    # then attaches no log file, instead of appending every genesis.* line of this
    # run to the MCP servers' shared ~/tmp/mcp_health.log.
    os.environ["GENESIS_MCP_LOG_DIR"] = str(Path(_S.base) / "no-mcp-log")
    config._genesis_live_closure_dir = _S.base
    if _S.home:
        config._genesis_live_home_cleanup = _S.home
    _S.installed = True


def uninstall_files(config) -> None:
    """Remove the closure's directory. A detached child that outlives pytest is
    then left with paths that do not exist (``SECRETS_PATH``, ``GH_CONFIG_DIR``),
    which fails closed: no credentials."""
    path = getattr(config, "_genesis_live_closure_dir", None)
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
    if _S.genesis_at_import:
        tr.section("live closure: modules loaded before the HOME redirect", red=True)
        tr.write_line(", ".join(_S.genesis_at_import))
    if _S.live_e2e:
        tr.section("live closure: GENESIS_LIVE_E2E=1, secrets and HOME left in place")
        return
    if _S.home:
        tr.section("live closure: HOME redirected for this run")
        tr.write_line(f"HOME={_S.home} (XDG base directories inside it)")
    if _S.removed:
        tr.section("live closure: credential variables removed for this run")
        tr.write_line(", ".join(_S.removed))
