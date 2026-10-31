"""Tests for the live closure (tests/_live_closure.py).

Every probe is harmless even if the closure were absent: nothing here opens a
real database, posts to a server or reads a credential.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests import _live_closure as closure

_REPO = Path(__file__).resolve().parent.parent


def test_the_root_conftest_names_the_closure_in_pytest_plugins(request):
    """Registered through ``pytest_plugins`` (so imported before any
    ``tests/test*/conftest.py``), not merely imported by ``pytest_configure``,
    which would be too late for paths bound at import."""
    assert request.config.pluginmanager.get_plugin("tests._live_closure") is not None


def test_the_memory_hook_url_is_a_closed_port():
    assert os.environ["GENESIS_PROACTIVE_HOOK_URL"] == "http://127.0.0.1:9"


def test_the_mcp_log_directory_does_not_exist():
    from genesis.util.mcp_logging import mcp_log_directory

    path = os.environ["GENESIS_MCP_LOG_DIR"]
    assert path.startswith(closure._S.base)
    assert not os.path.exists(path)
    assert mcp_log_directory() is None


def test_children_resolve_the_per_test_database(tmp_path):
    out = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['GENESIS_DB_PATH'])"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == str(tmp_path / "isolated-genesis.db")


# A sub-run wired the way tests/conftest.py wires the closure: the plugin named
# in ``pytest_plugins`` of the root conftest, installed in pytest_configure.
_SUBRUN_ROOT_CONFTEST = """
pytest_plugins = ["tests._live_closure"]


def pytest_configure(config):
    from tests import _live_closure

    _live_closure.install(config)


def pytest_unconfigure(config):
    from tests import _live_closure

    _live_closure.uninstall_files(config)
"""

# A ``test*`` subdirectory conftest of a directory argument: pytest imports it
# BEFORE pytest_configure, so these modules resolve the database path first.
_SUBRUN_EARLY_CONFTEST = """
import os

import genesis.db.connection  # noqa: F401
import genesis.mcp.health.task_tools  # noqa: F401

print("EARLY_ENV=" + os.environ.get("GENESIS_DB_PATH", "<unset>"))
if os.environ.get("CLOSURE_PROBE_RESET_DB"):
    # Something resetting the variable between the plugin's import and configure.
    os.environ["GENESIS_DB_PATH"] = os.environ["CLOSURE_PROBE_RESET_DB"]
"""

_SUBRUN_EARLY_TEST = """
import inspect
import os

import genesis.db.connection as connection
import genesis.mcp.health.task_tools as task_tools
from tests import _live_closure


def test_bound():
    print("BASE=" + _live_closure._S.base)
    print("DB_CONST=" + str(connection.DEFAULT_DB_PATH))
    print("DB_DEFAULT=" + str(inspect.signature(connection.init_db).parameters["path"].default))
    print("TASK_DB=" + str(task_tools._DB_PATH))
    print("HOOK_URL=" + os.environ["GENESIS_PROACTIVE_HOOK_URL"])
"""


def _value(text: str, key: str) -> str:
    return next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith(key + "="))


def _subrun(tmp_path: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    (tmp_path / "conftest.py").write_text(_SUBRUN_ROOT_CONFTEST)
    sub = tmp_path / "test_early"
    sub.mkdir()
    (sub / "conftest.py").write_text(_SUBRUN_EARLY_CONFTEST)
    (sub / "test_x.py").write_text(_SUBRUN_EARLY_TEST)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(_REPO), str(_REPO / "src")])}
    for name in ("PYTEST_CURRENT_TEST", "GENESIS_DB_PATH", "GENESIS_LIVE_E2E"):
        env.pop(name, None)
    env.update(extra_env or {})
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    return out


def test_a_module_imported_by_a_subdirectory_conftest_binds_the_import_time_file(tmp_path):
    """A directory run (CI's bare ``pytest``) imports ``tests/test*/conftest.py``
    before configure. A module imported there binds the database path at import
    and as default arguments, which nothing can change afterwards; it must bind
    the closure's file, never the checkout's ``data/genesis.db``."""
    out = _subrun(tmp_path)
    expected = str(Path(_value(out.stdout, "BASE")) / "import-time-genesis.db")
    assert _value(out.stdout, "EARLY_ENV") == expected, out.stdout
    for key in ("DB_CONST", "DB_DEFAULT", "TASK_DB"):
        assert _value(out.stdout, key) == expected, (key, out.stdout)
    assert "data/genesis.db" not in out.stdout


def test_a_database_path_changed_after_import_is_restored_and_named(tmp_path):
    """Something that resets GENESIS_DB_PATH between the plugin's import and
    configure leaves paths bound in between on the other value: say so."""
    out = _subrun(tmp_path, {"CLOSURE_PROBE_RESET_DB": str(tmp_path / "elsewhere.db")})
    expected = str(Path(_value(out.stdout, "BASE")) / "import-time-genesis.db")
    assert "GENESIS_DB_PATH changed after the live closure set it" in out.stderr, out.stderr
    assert _value(out.stdout, "DB_CONST") == expected, out.stdout


def test_an_opt_in_live_run_keeps_the_state_closures(tmp_path):
    """GENESIS_LIVE_E2E=1 needs real credentials, never the live database or the
    live memory hook. Nothing branches on it yet: this guards the credential
    half, which skips itself under it, against taking these closures along."""
    out = _subrun(tmp_path, {"GENESIS_LIVE_E2E": "1"})
    expected = str(Path(_value(out.stdout, "BASE")) / "import-time-genesis.db")
    assert _value(out.stdout, "DB_CONST") == expected, out.stdout
    assert _value(out.stdout, "HOOK_URL") == "http://127.0.0.1:9"


def test_the_closure_directory_is_removed_at_unconfigure(tmp_path):
    out = _subrun(tmp_path)
    assert not os.path.exists(_value(out.stdout, "BASE"))
