"""The live closure: tests and their children run without this machine's live state.

The live fence (``tests/_live_fence.py``) stops a test from starting a real
process. This plugin, armed for every run by ``tests/conftest.py`` right after
the fence, closes what a process can still reach WITHOUT starting anything: the
paths and endpoints it inherits through the environment. It sets them in the
process environment, so every child a test starts inherits the closure too:

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

The last two are set in ``pytest_configure``: a ``tests/test*/conftest.py`` that
imported the memory hook or the health MCP's browser module would act on the
real values first (none does).

A test that sets its own value (``monkeypatch.setenv``) still wins: the closure
is applied before any test runs. ``GENESIS_LIVE_E2E=1`` (the opt-in live tests)
changes nothing here: those tests need real credentials, never the live
database or the live memory hook.

Why at import: ``tests/conftest.py`` names this plugin in ``pytest_plugins``, so
pytest imports it while registering the root conftest, before it imports any
``tests/test*/conftest.py``. A directory run (CI's bare ``pytest`` included)
imports those BEFORE ``pytest_configure``, and a module imported there binds the
database path at import (``DEFAULT_DB_PATH = genesis_db_path()``) and as default
arguments (``def init_db(path=DEFAULT_DB_PATH)``), which nothing can change
afterwards. MEASURED on the rework's predecessor (#3333): set in configure, a
full directory run had 10 voice tests open the checkout's ``data/genesis.db``.
The import-time file is one per session, so a default-argument connect in one
test can leave rows another reads; it is still never the live database.

Not covered: a reader that builds its own database path instead of resolving
``genesis_db_path()``, and a child started with an environment that does not
carry these variables.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_CLOSED_PORT_URL = "http://127.0.0.1:9"  # discard port: nothing listens


class _State:
    installed = False
    base = ""  # the closure's own directory, made at import


_S = _State()

# Set when this plugin is IMPORTED, not in install(): see the module docstring.
_S.base = tempfile.mkdtemp(prefix="genesis-live-closure-")
# Removed at exit when install() never ran (a run the box lock refuses exits in
# pytest_configure first); otherwise uninstall_files() owns it. Never
# unconditional: a forked child of a test that exits through sys.exit runs
# atexit too, and would delete the directory the parent is still using.
atexit.register(lambda: _S.installed or shutil.rmtree(_S.base, ignore_errors=True))
_IMPORT_TIME_DB = str(Path(_S.base) / "import-time-genesis.db")
os.environ["GENESIS_DB_PATH"] = _IMPORT_TIME_DB


def install(config) -> None:
    """Apply the closure to this process's environment. Idempotent."""
    if _S.installed:
        return
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
    then left with paths that do not exist, which fails closed."""
    path = getattr(config, "_genesis_live_closure_dir", None)
    if path:
        shutil.rmtree(path, ignore_errors=True)
