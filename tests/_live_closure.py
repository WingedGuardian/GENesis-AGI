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

A test that sets its own value (``monkeypatch.setenv``) still wins: the closure
is applied before any test runs. With ``GENESIS_LIVE_E2E=1`` the opt-in
live tests need real credentials, so the secrets half is skipped and the summary
says so; the git, gh and hook-URL closures stay on.

``GENESIS_DB_PATH`` names a file under the closure's own directory from
``pytest_configure`` on, so a module that resolves the database path when it is
IMPORTED (during collection, before any fixture runs) binds that file rather than
the checkout's ``data/genesis.db``, the live database in the main checkout.
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
- credentials kept in other files (``~/.claude``, ``~/.genesis``, ``~/.ssh``,
  ``~/.git-credentials``; ``~/.config/gh`` is covered by ``GH_CONFIG_DIR``), and
  state paths other than the database (``GENESIS_HOME`` is not redirected).

The OS-level sandbox is the boundary for those.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

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


class _State:
    installed = False
    base = ""
    removed: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()
    live_e2e = False


_S = _State()


def credential_names(environ) -> list[str]:
    """The credential-shaped names in ``environ`` that the closure removes."""
    return sorted(
        k for k in environ if not _NEVER.match(k) and (k in _EXACT or _CREDENTIAL_NAME.search(k))
    )


def install(config) -> None:
    """Apply the closure to this process's environment. Idempotent."""
    if _S.installed:
        return
    _S.base = tempfile.mkdtemp(prefix="genesis-live-closure-")
    no_gh = Path(_S.base) / "no-gh"
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
    # Before collection: modules that resolve the database path at import
    # (``_DB_PATH = genesis_db_path()``) bind this, not the checkout's live file.
    os.environ["GENESIS_DB_PATH"] = str(Path(_S.base) / "import-time-genesis.db")
    # A directory that does not exist: importing the health MCP's browser module
    # then attaches no log file, instead of appending every genesis.* line of this
    # run to the MCP servers' shared ~/tmp/mcp_health.log.
    os.environ["GENESIS_MCP_LOG_DIR"] = str(Path(_S.base) / "no-mcp-log")
    config._genesis_live_closure_dir = _S.base
    _S.installed = True


def uninstall_files(config) -> None:
    base = getattr(config, "_genesis_live_closure_dir", None)
    if base:
        shutil.rmtree(base, ignore_errors=True)


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
