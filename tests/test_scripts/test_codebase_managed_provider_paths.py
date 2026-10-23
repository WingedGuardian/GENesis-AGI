"""Provider path incompatibility must stop every runtime route before effects."""

import json
import os
import subprocess
import sys
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from tests.test_scripts.test_codebase_managed_config import SCRIPT, config, managed

runtime = managed


@pytest.mark.parametrize("entry", [
    "runtime_config", "runtime_preflight", "enable", "launch",
    "ready", "serve", "client",
])
@pytest.mark.parametrize("key", ["main", "cache"])
@pytest.mark.parametrize("physical_alias", [False, True])
def test_unsupported_provider_path_refuses_before_runtime_effects(
    runtime, tmp_path, monkeypatch, entry, key, physical_alias,
):
    value = config(tmp_path, runtime)
    unsupported = tmp_path / "native\\identity"
    if physical_alias:
        unsupported.mkdir()
        alias = tmp_path / "plain-alias"
        alias.symlink_to(unsupported, target_is_directory=True)
        value[key] = str(alias)
    else:
        value[key] = str(unsupported)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(value))
    # Structural reads remain available for diagnosis and retained-state tools.
    assert runtime.read_settings(settings, require_build=False) == value
    target = getattr(runtime, entry)
    namespace = target.__globals__
    effects = []
    for name in (
        "verify_cache", "verified_binary", "require_enabled", "show",
        "require_quiescent", "inspect_sources", "refresh_sources", "native_install",
        "verify_frontend_boundary", "verify_query_boundary",
        "check_backend", "frontend_command",
    ):
        effect = Mock(name=name, side_effect=AssertionError(f"unexpected effect: {name}"))
        monkeypatch.setitem(namespace, name, effect)
        effects.append(effect)
    sleep = Mock(side_effect=AssertionError("readiness must not retry"))
    monkeypatch.setattr(namespace["time"], "sleep", sleep)
    # The real settings read, provider guard and preflight remain in the route.
    # Only client admission's unrelated flock is isolated in this unit test.
    monkeypatch.setitem(namespace, "lifecycle_lock", lambda **kw: nullcontext())
    if entry == "runtime_config":
        arguments = (settings,)
    elif entry == "launch":
        arguments = (value, settings)
    elif entry == "client":
        arguments = (settings, "test-client.service")
    else:
        arguments = (value,)
    with pytest.raises(ValueError, match=f"backslash in {key} paths"):
        target(*arguments)
    for effect in effects:
        effect.assert_not_called()
    sleep.assert_not_called()

@pytest.mark.parametrize("key", ["main", "cache"])
def test_ready_cli_rejects_provider_identity_before_manager_or_provider(
    managed, tmp_path, key,
):
    value = config(tmp_path, managed)
    value[key] += "\\unsupported"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(value))
    state = tmp_path / "isolated-state"
    env = dict(os.environ, HOME=str(tmp_path / "home"), GENESIS_HOME=str(state))
    before = path.read_bytes()
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--config", str(path), "ready"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 1
    assert f"backslash in {key} paths" in result.stderr
    assert path.read_bytes() == before
    assert not state.exists()


@pytest.mark.parametrize("argv,expected", [
    (["status"], {"command": "status", "config": None}),
    (["--config", "/settings", "ready"], {"command": "ready", "config": "/settings"}),
    (["serve"], {"command": "serve"}),
    (["launch"], {"command": "launch"}),
    (["enable"], {"command": "enable"}),
    (["disable"], {"command": "disable"}),
    (["remove"], {"command": "remove"}),
    (["client", "--unit", "client.service"], {"command": "client", "unit": "client.service"}),
    (["verify-uninstall-locks", "3", "4", "5"], {"command": "verify-uninstall-locks", "fds": [3, 4, 5]}),
    (["uninstall", "--", "--dry-run"], {"command": "uninstall", "arguments": ["--", "--dry-run"]}),
    (["configure", "--main", "/repo", "--binary", "/binary", "--state", "/state", "--sentinel", "/sentinel"],
     {"command": "configure", "main": "/repo", "binary": "/binary", "state": "/state", "sentinel": "/sentinel"}),
])
def test_extracted_parser_preserves_existing_command_contract(managed, argv, expected):
    parsed = vars(managed.parse_arguments(argv))
    assert {key: parsed[key] for key in expected} == expected


@pytest.mark.parametrize("argv", [[], ["client"], ["configure"], ["verify-uninstall-locks", "x", "4", "5"]])
def test_extracted_parser_still_refuses_incomplete_arguments(managed, argv):
    with pytest.raises(SystemExit) as error:
        managed.parse_arguments(argv)
    assert error.value.code == 2


def test_extracted_parser_keeps_operator_help_description(managed, capsys):
    with pytest.raises(SystemExit) as error:
        managed.parse_arguments(["--help"])
    assert error.value.code == 0
    assert "Stage immutable Codebase settings" in capsys.readouterr().out
