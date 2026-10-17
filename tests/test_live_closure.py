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


def test_key_state():
    print("KEY=" + os.environ.get("FENCE_PROBE_API_KEY", "<absent>"))
"""


def _subrun(tmp_path: Path, live_e2e: bool) -> str:
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_sub.py").write_text(_SUBRUN_TESTS)
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(repo), "FENCE_PROBE_API_KEY": "probe-value"}
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


def test_a_run_removes_keys_and_names_them(tmp_path):
    text = _subrun(tmp_path, live_e2e=False)
    assert "KEY=<absent>" in text
    assert "credential variables removed" in text and "FENCE_PROBE_API_KEY" in text
    assert "probe-value" not in text  # names only, never values


def test_an_opt_in_live_run_keeps_its_keys(tmp_path):
    text = _subrun(tmp_path, live_e2e=True)
    assert "KEY=probe-value" in text
    assert "GENESIS_LIVE_E2E=1, secrets left in place" in text
