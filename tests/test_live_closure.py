"""Tests for the credential and state closure (tests/_live_closure.py).

Every probe is harmless even if the closure were absent: nothing here reads a
real secret, and the only network attempt targets a name under ``.invalid``
through ``GIT_SSH_COMMAND=false``, which never opens a connection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _live_closure as closure

_SECRETS_HALF_OFF = pytest.mark.skipif(
    closure._S.live_e2e, reason="the secrets half is off under GENESIS_LIVE_E2E=1"
)


@_SECRETS_HALF_OFF
def test_secrets_path_names_a_file_that_does_not_exist_in_this_test(tmp_path):
    from genesis.env import secrets_path

    path = os.environ["SECRETS_PATH"]
    assert path == str(tmp_path / "no-secrets.env")
    assert not os.path.exists(path)
    assert str(secrets_path()) == path


def test_the_session_wide_secrets_path_is_the_closures_own():
    assert closure._S.base and os.path.isdir(closure._S.base)
    assert not os.path.exists(os.path.join(closure._S.base, "no-secrets.env"))


@_SECRETS_HALF_OFF
def test_no_credential_variable_survived_the_closure():
    # Bookkeeping: what install() left, not os.environ now, so a test that leaks a
    # variable later in the run cannot fail this one. The behaviour itself is
    # checked end to end by test_a_run_removes_keys_and_names_them.
    assert closure._S.remaining == ()


def test_the_credential_name_rule():
    env = {
        "FOO_API_KEY": "", "API_KEY_BAR": "", "X_API_KEY_Y": "", "CLAUDE_CODE_OAUTH_TOKEN": "",
        "AWS_SECRET_ACCESS_KEY": "", "SSH_AUTH_SOCK": "", "GH_TOKEN": "", "GITHUB_TOKEN": "",
        "AWS_ACCESS_KEY_ID": "", "DASHBOARD_PASSWORD": "", "BACKUP_PASSPHRASE": "",
        "NAS_PASS": "", "CHAT_WEBHOOK_URL": "", "SSH_PRIVATE_KEY": "", "GCP_CREDENTIALS": "",
        # kept: not credentials, or the run's own switches
        "TOKEN_OPTIMIZER_MODE": "", "PYTEST_ADDOPTS_TOKEN": "", "GENESIS_LIVE_E2E": "",
        "KEYBOARD": "", "PATH": "", "PASSAGE_DIR": "", "AWS_DEFAULT_REGION": "",
        "USER_TIMEZONE": "", "GUARDIAN_SECRETS": "", "GENESIS_SECRETS_PATH": "",
    }  # fmt: skip
    assert closure.credential_names(env) == sorted([
        "API_KEY_BAR", "AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "FOO_API_KEY",
        "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "X_API_KEY_Y", "AWS_ACCESS_KEY_ID",
        "DASHBOARD_PASSWORD", "BACKUP_PASSPHRASE", "NAS_PASS", "CHAT_WEBHOOK_URL",
        "SSH_PRIVATE_KEY", "GCP_CREDENTIALS",
    ])  # fmt: skip


def test_gh_has_no_stored_login_and_git_cannot_prompt_or_use_ssh():
    gh_dir = Path(os.environ["GH_CONFIG_DIR"])
    assert gh_dir.is_dir() and not any(gh_dir.iterdir())
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
    # The ssh transport runs `false` (seen in the trace), never a real ssh: a
    # name lookup failure would print the same "Could not read" line.
    out = subprocess.run(
        ["git", "ls-remote", "git@example.invalid:owner/repo.git"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GIT_TRACE": "1"},
    )
    assert out.returncode != 0
    assert " false git@example.invalid" in out.stderr, out.stderr
    gh = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('GH_TOKEN', '<none>'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert gh.stdout.strip() == "<none>"


def test_the_memory_hook_url_is_a_closed_port():
    assert os.environ["GENESIS_PROACTIVE_HOOK_URL"] == "http://127.0.0.1:9"


def test_the_mcp_log_directory_does_not_exist():
    path = os.environ["GENESIS_MCP_LOG_DIR"]
    assert path.startswith(closure._S.base)
    assert not os.path.exists(path)


def test_children_resolve_the_per_test_database(tmp_path):
    out = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['GENESIS_DB_PATH'])"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == str(tmp_path / "isolated-genesis.db")


_SUBRUN_CONFTEST = """
# Imported before the closure installs, as the box lock in tests/conftest.py
# does: its directory constant is bound to the real home and must be moved.
from genesis.util import process_lock  # noqa: F401

pytest_plugins = ["tests._live_closure"]


def pytest_configure(config):
    from tests import _live_closure

    _live_closure.install(config)


def pytest_unconfigure(config):
    from tests import _live_closure

    _live_closure.uninstall_files(config)
"""

_SUBRUN_TESTS = """
import os
from pathlib import Path

from genesis.util import process_lock

# Bound at import, during collection: after the closure is installed.
IMPORT_HOME = Path.home()


def test_key_state():
    print("KEY=" + os.environ.get("FENCE_PROBE_API_KEY", "<absent>"))
    print("IMPORT_HOME=" + str(IMPORT_HOME))
    print("PID_DIR=" + str(process_lock._DEFAULT_PID_DIR))
"""


def _subrun(tmp_path: Path, live_e2e: bool) -> str:
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_sub.py").write_text(_SUBRUN_TESTS)
    repo = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(repo), str(repo / "src")]),
        "FENCE_PROBE_API_KEY": "probe-value",
        # The parent's HOME is this session's private one; hand the child the
        # real home so the opt-in run's "unchanged" is measured against it.
        "HOME": closure.REAL_HOME,
    }
    env.pop("PYTEST_CURRENT_TEST", None)
    if live_e2e:
        env["GENESIS_LIVE_E2E"] = "1"
    else:
        env.pop("GENESIS_LIVE_E2E", None)
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


_SUBRUN_IMPORT_TIME = """
import genesis.db.connection as connection
import genesis.mcp.health.campaign_tools as campaign_tools
import genesis.mcp.health.task_tools as task_tools
from tests import _live_closure


def test_bound_paths():
    print("BASE=" + _live_closure._S.base)
    for bound in (connection.DEFAULT_DB_PATH, task_tools._DB_PATH, campaign_tools._DB_PATH):
        print("BOUND=" + str(bound))
"""


def test_a_module_imported_at_collection_binds_the_closures_database(tmp_path):
    """Collection runs before any fixture, so a module that resolves the database
    path at import binds whatever the environment says then: the closure's file,
    never the checkout's data/genesis.db (the live database in the main checkout)."""
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_sub.py").write_text(_SUBRUN_IMPORT_TIME)
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(repo), str(repo / "src")])}
    for name in ("PYTEST_CURRENT_TEST", "GENESIS_DB_PATH", "GENESIS_LIVE_E2E"):
        env.pop(name, None)
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    base = next(line.split("=", 1)[1] for line in lines if line.startswith("BASE="))
    bound = [line.split("=", 1)[1] for line in lines if line.startswith("BOUND=")]
    assert bound == [str(Path(base) / "import-time-genesis.db")] * 3, out.stdout


def _value(text: str, key: str) -> str:
    return next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith(key + "="))


def test_a_run_removes_keys_and_names_them(tmp_path):
    text = _subrun(tmp_path, live_e2e=False)
    assert "KEY=<absent>" in text
    assert "credential variables removed" in text and "FENCE_PROBE_API_KEY" in text
    assert "probe-value" not in text  # names only, never values


def test_a_run_gets_a_private_home_bound_before_collection(tmp_path):
    """A module that resolves ``Path.home()`` when collected binds the session's
    private home, and a value bound earlier (the process lock's directory) moved
    with it. The run's summary says so."""
    text = _subrun(tmp_path, live_e2e=False)
    home = _value(text, "IMPORT_HOME")
    assert home != closure.REAL_HOME and "genesis-test-home-" in home, text
    assert _value(text, "PID_DIR") == str(Path(home) / ".genesis"), text
    assert "HOME redirected" in text


def test_an_opt_in_live_run_keeps_its_keys(tmp_path):
    text = _subrun(tmp_path, live_e2e=True)
    assert "KEY=probe-value" in text
    assert "GENESIS_LIVE_E2E=1, secrets left in place" in text
    assert _value(text, "IMPORT_HOME") == closure.REAL_HOME  # HOME untouched too


@pytest.mark.skipif(
    not closure._S.home, reason="HOME not redirected (GENESIS_LIVE_E2E=1 or setup failed)"
)
def test_this_session_runs_with_a_private_home_outside_basetemp(request):
    """pytest empties basetemp on first use, so the home must sit beside it."""
    home = Path(closure._S.home)
    assert Path.home() == home and str(home) != closure.REAL_HOME
    assert (home / "tmp").is_dir()
    basetemp = Path(request.config._tmp_path_factory.getbasetemp())
    assert home not in basetemp.parents and basetemp not in home.parents


def test_the_box_lock_was_taken_before_the_redirect(request):
    """Placed after the lock: resolved under the private home (no ``.genesis``),
    the lock path would be None and the run would silently take no lock."""
    from genesis.util import pytest_lock

    lock = request.config._genesis_pytest_lock
    if lock.acquired:
        path = Path(lock._path)
        assert not closure._S.home or Path(closure._S.home) not in path.parents
        if not os.environ.get(pytest_lock.PATH_ENV):
            assert Path(closure.REAL_HOME, ".genesis") in path.parents, path
        return
    excused = [
        name
        for name in (pytest_lock.HELD_ENV, pytest_lock.DISABLE_ENV, pytest_lock.PATH_ENV)
        if os.environ.get(name)
    ]
    if excused or not Path(closure.REAL_HOME, ".genesis").is_dir():
        pytest.skip(f"no lock expected here ({excused or 'no install'})")
    pytest.fail("an install exists and nothing excused the box lock, yet none was taken")


_SUBRUN_EARLY_CONFTEST = """
# A test* subdirectory conftest of a directory argument: pytest imports it
# BEFORE pytest_configure, so these modules bind their paths first.
import genesis.cc.direct_session  # noqa: F401
import genesis.cc.types  # noqa: F401
import genesis.db.connection  # noqa: F401
"""

_SUBRUN_EARLY_TEST = """
from pathlib import Path

import inspect

import genesis.cc.direct_session as direct_session
import genesis.cc.types as types
import genesis.db.connection as connection
from tests import _live_closure


def test_bound_dir():
    print("HOME=" + str(Path.home()))
    print("BOUND=" + str(types._BACKGROUND_SESSION_DIR))
    print("TMP_BOUND=" + str(direct_session._BG_CC_TMP_ROOT))
    print("BASE=" + _live_closure._S.base)
    print("DB_CONST=" + str(connection.DEFAULT_DB_PATH))
    default = inspect.signature(connection.init_db).parameters["path"].default
    print("DB_DEFAULT=" + str(default))
"""


def test_a_path_bound_before_configure_is_moved_into_the_home(tmp_path):
    """CI's bare ``pytest`` (a directory) imports ``tests/test*/conftest.py`` before
    configure; a state path a module bound then is moved into the test home and
    named in the summary, and the database path it bound is already the
    closure's."""
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    sub = tmp_path / "test_early"
    sub.mkdir()
    (sub / "conftest.py").write_text(_SUBRUN_EARLY_CONFTEST)
    (sub / "test_x.py").write_text(_SUBRUN_EARLY_TEST)
    repo = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(repo), str(repo / "src")]),
        "HOME": closure.REAL_HOME,
    }
    # TMPDIR in the real ~/tmp, as on an install whose Claude Code sessions use it:
    # the closure's own directory then sits under a state root of the home, and
    # ~/tmp state bound before configure must still move.
    env["TMPDIR"] = str(Path(closure.REAL_HOME, "tmp"))
    for name in ("PYTEST_CURRENT_TEST", "GENESIS_LIVE_E2E", "GENESIS_DB_PATH", "SECRETS_PATH"):
        env.pop(name, None)
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    home = _value(out.stdout, "HOME")
    assert home != closure.REAL_HOME, out.stdout
    assert _value(out.stdout, "BOUND") == str(Path(home, ".genesis", "background-sessions"))
    assert "genesis.cc.types._BACKGROUND_SESSION_DIR" in out.stdout
    assert _value(out.stdout, "TMP_BOUND") == str(Path(home, "tmp", "bg-cc-sessions"))
    # The database path, bound at import AND as a default argument, which no
    # rebase reaches: it must be the closure's file because the closure set
    # GENESIS_DB_PATH when it was imported, before this conftest ran.
    expected_db = str(Path(_value(out.stdout, "BASE"), "import-time-genesis.db"))
    assert _value(out.stdout, "DB_CONST") == expected_db, out.stdout
    assert _value(out.stdout, "DB_DEFAULT") == expected_db, out.stdout
