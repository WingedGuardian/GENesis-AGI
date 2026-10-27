"""Tests for the live closure (tests/_live_closure.py).

Every probe is harmless even if the closure were absent: nothing here opens a
real database, posts to a server or reads a credential. The only network attempt
targets a name under ``.invalid`` through ``GIT_SSH_COMMAND=false``, which never
opens a connection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _live_closure as closure

_REPO = Path(__file__).resolve().parent.parent
_SECRETS_HALF_OFF = pytest.mark.skipif(
    closure._S.live_e2e, reason="the secrets half is off under GENESIS_LIVE_E2E=1"
)
# Everything the closure sets: a sub-run starts without them, so what it shows is
# its own closure at work, not values inherited from this run.
_CLOSURE_VARS = (
    "PYTEST_CURRENT_TEST", "GENESIS_LIVE_E2E", "GENESIS_DB_PATH", "SECRETS_PATH",
    "GENESIS_PROACTIVE_HOOK_URL", "GENESIS_MCP_LOG_DIR", "GH_CONFIG_DIR",
    "GIT_TERMINAL_PROMPT", "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_SYSTEM",
)  # fmt: skip


def test_the_root_conftest_names_the_closure_in_pytest_plugins(request):
    """Registered through ``pytest_plugins`` (so imported before any
    ``tests/test*/conftest.py``), not merely imported by ``pytest_configure``,
    which would be too late for paths bound at import."""
    assert request.config.pluginmanager.get_plugin("tests._live_closure") is not None


@_SECRETS_HALF_OFF
def test_secrets_path_names_a_file_that_does_not_exist_in_this_test(tmp_path):
    from genesis.env import secrets_path

    path = os.environ["SECRETS_PATH"]
    assert path == str(tmp_path / "no-secrets.env")
    assert not os.path.exists(path)
    assert str(secrets_path()) == path


def test_the_session_wide_secrets_path_is_the_closures_own():
    assert closure._S.base and os.path.isdir(closure._S.base)
    assert not os.path.exists(closure._NO_SECRETS)
    assert closure._NO_SECRETS.startswith(closure._S.base)


def test_the_credential_name_rule():
    env = {
        "FOO_API_KEY": "", "API_KEY_BAR": "", "X_API_KEY_Y": "", "CLAUDE_CODE_OAUTH_TOKEN": "",
        "AWS_SECRET_ACCESS_KEY": "", "SSH_AUTH_SOCK": "", "GH_TOKEN": "", "GITHUB_TOKEN": "",
        "AWS_ACCESS_KEY_ID": "", "DASHBOARD_PASSWORD": "", "BACKUP_PASSPHRASE": "",
        "NAS_PASS": "", "CHAT_WEBHOOK_URL": "", "SSH_PRIVATE_KEY": "", "GCP_CREDENTIALS": "",
        "API_KEY": "", "DOCKER_AUTH_CONFIG": "", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
        "VSCODE_GIT_IPC_HANDLE": "", "VSCODE_GIT_ASKPASS_MAIN": "",
        # kept: not credentials, or the run's own switches
        "TOKEN_OPTIMIZER_MODE": "", "PYTEST_ADDOPTS_TOKEN": "", "GENESIS_LIVE_E2E": "",
        "KEYBOARD": "", "PATH": "", "PASSAGE_DIR": "", "AWS_DEFAULT_REGION": "",
        "USER_TIMEZONE": "", "GUARDIAN_SECRETS": "", "GENESIS_SECRETS_PATH": "",
        "API_KEYS_DIR": "", "AUTH_CONFIG_MODE": "",
    }  # fmt: skip
    assert closure.credential_names(env) == sorted([
        "API_KEY_BAR", "AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "FOO_API_KEY",
        "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "X_API_KEY_Y", "AWS_ACCESS_KEY_ID",
        "DASHBOARD_PASSWORD", "BACKUP_PASSPHRASE", "NAS_PASS", "CHAT_WEBHOOK_URL",
        "SSH_PRIVATE_KEY", "GCP_CREDENTIALS", "API_KEY", "DOCKER_AUTH_CONFIG",
        "GIT_ASKPASS", "SSH_ASKPASS", "VSCODE_GIT_IPC_HANDLE", "VSCODE_GIT_ASKPASS_MAIN",
    ])  # fmt: skip


def test_gh_has_no_stored_login_and_git_cannot_prompt_or_use_ssh():
    gh_dir = Path(os.environ["GH_CONFIG_DIR"])
    assert gh_dir.is_dir() and not any(gh_dir.iterdir())
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
    assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
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
print("EARLY_SECRETS=" + os.environ.get("SECRETS_PATH", "<unset>"))
if os.environ.get("CLOSURE_PROBE_RESET_DB"):
    # Something resetting the variable between the plugin's import and configure.
    os.environ["GENESIS_DB_PATH"] = os.environ["CLOSURE_PROBE_RESET_DB"]
"""

_SUBRUN_EARLY_TEST = """
import inspect
import os
import subprocess

import genesis.db.connection as connection
import genesis.mcp.health.task_tools as task_tools
from tests import _live_closure


def _git(*args, env=None):
    out = subprocess.run(["git", *args], capture_output=True, text=True, env=env)
    return out.stdout.strip().replace("\\n", "|")


def test_bound():
    print("BASE=" + _live_closure._S.base)
    print("DB_CONST=" + str(connection.DEFAULT_DB_PATH))
    print("DB_DEFAULT=" + str(inspect.signature(connection.init_db).parameters["path"].default))
    print("TASK_DB=" + str(task_tools._DB_PATH))
    print("HOOK_URL=" + os.environ["GENESIS_PROACTIVE_HOOK_URL"])
    for name in (
        "CLOSURE_PROBE_API_KEY", "API_KEY", "DOCKER_AUTH_CONFIG", "GIT_ASKPASS",
        "SSH_ASKPASS", "VSCODE_GIT_IPC_HANDLE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    ):  # fmt: skip
        print(name + "=" + os.environ.get(name, "<absent>"))
    print("GIT_CONFIG_GLOBAL=" + os.environ.get("GIT_CONFIG_GLOBAL", "<unset>"))
    print("GIT_GLOBAL=" + _git("config", "--global", "--list"))
    print("GIT_SYSTEM=" + _git("config", "--system", "--list"))
    # Names and origins only: if the closure ever failed, the assertion message
    # (and a public CI log) would otherwise show this machine's config values.
    print("GIT_EFFECTIVE=" + _git("config", "--list", "--show-origin", "--name-only"))
    print("GIT_HELPER=" + _git("config", "--get", "credential.helper"))
    # Guard-the-guard: without the closure's git variables the same child does
    # read the fixture's helpers, so the fixture really plants them.
    closed = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM")
    unclosed = {k: v for k, v in os.environ.items() if k not in closed}
    if os.environ.get("CLOSURE_PROBE_SYSTEM_CFG"):
        unclosed["GIT_CONFIG_SYSTEM"] = os.environ["CLOSURE_PROBE_SYSTEM_CFG"]
    print("GIT_HELPER_UNCLOSED=" + _git("config", "--get", "credential.helper", env=unclosed))
    print("GIT_SYSTEM_UNCLOSED=" + _git("config", "--system", "--list", env=unclosed))
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
    for name in _CLOSURE_VARS:
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
    # SECRETS_PATH likewise, for a module that loads secrets.env at import.
    no_secrets = str(Path(_value(out.stdout, "BASE")) / "no-secrets.env")
    assert _value(out.stdout, "EARLY_SECRETS") == no_secrets, out.stdout
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
    live memory hook or this machine's git configuration: the credential half
    is skipped, and the summary says so; the other closures stay on."""
    out = _subrun(tmp_path, {"GENESIS_LIVE_E2E": "1", "CLOSURE_PROBE_API_KEY": "probe-value"})
    expected = str(Path(_value(out.stdout, "BASE")) / "import-time-genesis.db")
    assert _value(out.stdout, "DB_CONST") == expected, out.stdout
    assert _value(out.stdout, "HOOK_URL") == "http://127.0.0.1:9"
    assert _value(out.stdout, "CLOSURE_PROBE_API_KEY") == "probe-value"
    assert "GENESIS_LIVE_E2E=1, secrets left in place" in out.stdout
    assert _value(out.stdout, "GIT_CONFIG_GLOBAL") == os.devnull


def test_the_closure_directory_is_removed_at_unconfigure(tmp_path):
    out = _subrun(tmp_path)
    assert not os.path.exists(_value(out.stdout, "BASE"))


def test_a_run_removes_keys_and_names_them(tmp_path):
    out = _subrun(
        tmp_path,
        {"CLOSURE_PROBE_API_KEY": "probe-value", "API_KEY": "bare-value",
         "DOCKER_AUTH_CONFIG": "docker-value"},
    )  # fmt: skip
    for name in ("CLOSURE_PROBE_API_KEY", "API_KEY", "DOCKER_AUTH_CONFIG"):
        assert _value(out.stdout, name) == "<absent>", out.stdout
    summary = out.stdout.split("credential variables removed", 1)[1]
    for name in ("CLOSURE_PROBE_API_KEY", "API_KEY", "DOCKER_AUTH_CONFIG"):
        assert name in summary, out.stdout
    for value in ("probe-value", "bare-value", "docker-value"):
        assert value not in out.stdout + out.stderr  # names only, never values


def test_git_reads_no_user_or_system_config_whatever_home_and_xdg_say(tmp_path):
    """A credential helper in ``$XDG_CONFIG_HOME/git/config``, in
    ``$HOME/.gitconfig`` and in a system file is invisible to a child, even to an
    explicit ``git config --system`` (which reads the system file under
    ``GIT_CONFIG_NOSYSTEM=1`` alone: MEASURED, git 2.43)."""
    fixture = tmp_path / "fixture"
    helper = "[credential]\n\thelper = store --file=/nonexistent/creds\n"
    (fixture / "xdg" / "git").mkdir(parents=True)
    (fixture / "xdg" / "git" / "config").write_text(helper)
    (fixture / "home").mkdir()
    (fixture / "home" / ".gitconfig").write_text(helper)
    (fixture / "system.cfg").write_text(helper)
    run = tmp_path / "run"
    run.mkdir()
    out = _subrun(
        run,
        {
            "XDG_CONFIG_HOME": str(fixture / "xdg"),
            "HOME": str(fixture / "home"),
            # The closure must override an inherited system file, not just set one.
            "GIT_CONFIG_SYSTEM": str(fixture / "system.cfg"),
            "CLOSURE_PROBE_SYSTEM_CFG": str(fixture / "system.cfg"),
        },
    )
    unclosed_system = "credential.helper=store --file=/nonexistent/creds"
    assert _value(out.stdout, "GIT_HELPER_UNCLOSED") == "store --file=/nonexistent/creds"
    assert _value(out.stdout, "GIT_SYSTEM_UNCLOSED") == unclosed_system, out.stdout
    assert _value(out.stdout, "GIT_HELPER") == "", out.stdout
    assert _value(out.stdout, "GIT_GLOBAL") == "", out.stdout
    assert _value(out.stdout, "GIT_SYSTEM") == "", out.stdout
    assert _value(out.stdout, "GIT_EFFECTIVE") == "", out.stdout


def test_git_askpass_helpers_and_inherited_config_are_removed(tmp_path):
    """An askpass program answers git's credential prompt even under
    ``GIT_TERMINAL_PROMPT=0``, and inherited ``-c`` config can name a helper
    whatever the config files say: none of them reach a child."""
    out = _subrun(
        tmp_path,
        {
            "GIT_ASKPASS": "/nonexistent/askpass",
            "SSH_ASKPASS": "/nonexistent/ssh-askpass",
            "VSCODE_GIT_IPC_HANDLE": "/nonexistent/ipc.sock",
            "GIT_CONFIG_PARAMETERS": "'credential.helper'='store --file=/nonexistent/creds'",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "store --file=/nonexistent/creds",
        },
    )
    for name in (
        "GIT_ASKPASS", "SSH_ASKPASS", "VSCODE_GIT_IPC_HANDLE", "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    ):  # fmt: skip
        assert _value(out.stdout, name) == "<absent>", out.stdout
    assert _value(out.stdout, "GIT_HELPER") == "", out.stdout
