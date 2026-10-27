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

Everything but ``SECRETS_PATH`` and ``GENESIS_DB_PATH`` is set in
``pytest_configure``: a ``tests/test*/conftest.py`` that read a credential, ran
git, or imported the memory hook or the health MCP's browser module would see
the real values first (none does).

A test that sets its own value (``monkeypatch.setenv``) still wins: the closure
is applied before any test runs. With ``GENESIS_LIVE_E2E=1`` the opt-in live
tests need real credentials, so ``SECRETS_PATH`` and the credential removal are
skipped and the summary says so; the git, gh, database, hook-URL and log
closures stay on.

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
credential under a name the rule does not match; credentials a program finds
through the home directory rather than the environment (``~/.ssh``, tool
config files: the private-HOME closure is the answer there); a child started
with an environment that does not carry these variables, including the tests
that strip every ``GIT_*`` variable from a child's environment (its git then
reads the home's user config again); a repository-local ``core.askPass`` or
``credential.helper``; and every credential name class listed in
``docs/reference/testing.md`` ("Not covered").
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

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


class _State:
    installed = False
    base = ""  # the closure's own directory, made at import
    live_e2e = False
    removed: tuple[str, ...] = ()


_S = _State()

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
    if _S.live_e2e:
        tr.section("live closure: GENESIS_LIVE_E2E=1, secrets left in place")
        return
    if _S.removed:
        tr.section("live closure: credential variables removed for this run")
        tr.write_line(", ".join(_S.removed))
