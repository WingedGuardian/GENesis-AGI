"""Selected billing/model pins survive discovered and supplied settings."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.cc import gmodel_settings as settings
from genesis.cc import roster


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_MANAGED_DIR", tmp_path / "managed")
    monkeypatch.setattr(settings, "_local_roots", lambda cwd: [cwd])
    return {
        "HOME": str(tmp_path / "home"),
        "ANTHROPIC_MODEL": "kimi-k3[1m]",
        "ANTHROPIC_BASE_URL": "https://api.moonshot.ai/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "selected-secret",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1048576",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1048576",
        "CLAUDE_CODE_EFFORT_LEVEL": "high",
    }


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def test_unrelated_settings_preserved(tmp_path, env):
    write(Path(env["HOME"]) / ".claude/settings.json", {"permissions": {"allow": ["Read"]}})
    write(tmp_path / ".claude/settings.json", {"env": {"MY_APP_KEY": "unrelated"}})
    settings.validate_settings([], env, cwd=tmp_path)


@pytest.mark.parametrize("key", sorted(settings._PROTECTED_ENV))
def test_env_conflicts_redact_values(tmp_path, env, key):
    path = write(tmp_path / ".claude/settings.json", {"env": {key: "leaked-secret"}})
    with pytest.raises(roster.RosterError) as error:
        settings.validate_settings([], env, cwd=tmp_path)
    assert str(path) in str(error.value)
    assert key in str(error.value)
    assert "leaked-secret" not in str(error.value)
    assert "selected-secret" not in str(error.value)


def test_matching_pins_and_empty_api_key(tmp_path, env):
    write(tmp_path / ".claude/settings.local.json", {
        "env": {**{k: v for k, v in env.items() if k in settings._PROTECTED_ENV},
                "ANTHROPIC_API_KEY": ""},
        "model": env["ANTHROPIC_MODEL"], "fallbackModel": [env["ANTHROPIC_MODEL"]],
        "effortLevel": "max",
    })
    settings.validate_settings(["--model", env["ANTHROPIC_MODEL"]], env, cwd=tmp_path)


@pytest.mark.parametrize("flag", ["--model", "--fallback-model", "--effort", "--autocompact"])
@pytest.mark.parametrize("equal", [False, True])
def test_cli_pin_conflicts(tmp_path, env, flag, equal):
    args = [flag + "=leaked-secret"] if equal else [flag, "leaked-secret"]
    with pytest.raises(roster.RosterError, match=flag) as error:
        settings.validate_settings(args, env, cwd=tmp_path)
    assert "leaked-secret" not in str(error.value)


def test_prompt_option_values_and_separator_are_not_parsed(tmp_path, env):
    settings.validate_settings([
        "--append-system-prompt", "--model", "--system-prompt", "--settings",
        "--", "--model", "unrelated",
    ], env, cwd=tmp_path)


def test_setting_sources_and_custom_user_dir(tmp_path, env):
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "custom")
    write(tmp_path / "custom/settings.json", {"model": "other"})
    write(tmp_path / ".claude/settings.json", {"model": env["ANTHROPIC_MODEL"]})
    settings.validate_settings(["--setting-sources", "project"], env, cwd=tmp_path)
    with pytest.raises(roster.RosterError, match="custom/settings.json"):
        settings.validate_settings(["--setting-sources=user"], env, cwd=tmp_path)


def test_both_cwd_and_git_root_local_files_checked(tmp_path, env, monkeypatch):
    child = tmp_path / "nested"
    child.mkdir()
    monkeypatch.setattr(settings, "_local_roots", lambda cwd: [cwd, tmp_path])
    path = write(tmp_path / ".claude/settings.local.json", {"model": "other"})
    with pytest.raises(roster.RosterError, match=str(path)):
        settings.validate_settings([], env, cwd=child)
    settings.validate_settings(["--setting-sources", "project"], env, cwd=child)


def test_managed_dropin_always_checked(tmp_path, env):
    path = write(tmp_path / "managed/managed-settings.d/10-routing.json", {"model": "other"})
    with pytest.raises(roster.RosterError, match=str(path)):
        settings.validate_settings(["--setting-sources", ""], env, cwd=tmp_path)
    path.unlink()
    write(tmp_path / "managed/managed-settings.d/.hidden.json", {"model": "other"})
    settings.validate_settings([], env, cwd=tmp_path)


@pytest.mark.parametrize("inline", [False, True])
def test_supplied_settings_even_when_sources_disabled(tmp_path, env, inline):
    doc = {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "leaked-secret"}}
    value = json.dumps(doc) if inline else str(write(tmp_path / "supplied.json", doc))
    with pytest.raises(roster.RosterError, match="CLAUDE_CODE_OAUTH_TOKEN") as error:
        settings.validate_settings(["--setting-sources", "", "--settings", value], env, cwd=tmp_path)
    assert "leaked-secret" not in str(error.value)


def test_bad_json_does_not_leak_decoder_content(tmp_path, env):
    with pytest.raises(roster.RosterError, match="inline --settings") as error:
        settings.validate_settings(["--settings", '{"secret":"leaked-secret"'], env, cwd=tmp_path)
    assert "leaked-secret" not in str(error.value)


def test_inline_array_error_does_not_print_contents(tmp_path, env):
    with pytest.raises(roster.RosterError, match="inline --settings") as error:
        settings.validate_settings(["--settings", '["leaked-secret"]'], env, cwd=tmp_path)
    assert "leaked-secret" not in str(error.value)


@pytest.mark.parametrize("doc", [None, ["other"], {"env": []}, {"fallbackModel": ["other"]}])
def test_invalid_objects_and_fallback_pins(tmp_path, env, doc):
    write(tmp_path / ".claude/settings.json", doc)
    with pytest.raises(roster.RosterError):
        settings.validate_settings([], env, cwd=tmp_path)


def test_missing_supplied_file_and_bad_sources(tmp_path, env):
    for args in (["--settings", "missing.json"], ["--setting-sources", "other"], ["--model"]):
        with pytest.raises(roster.RosterError):
            settings.validate_settings(args, env, cwd=tmp_path)


def test_git_root_discovery_uses_main_checkout(monkeypatch, tmp_path):
    main = tmp_path / "main"
    work = tmp_path / "linked"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=f"{work}\n{main / '.git'}\n"))
    assert settings._local_roots(work) == [work, main]


@pytest.mark.parametrize("args, expected", [
    (["--route", "api", "--model", "selected"], ("api", ["--model", "selected"])),
    (["--route=subscription", "--resume", "session"], ("subscription", ["--resume", "session"])),
    (["--append-system-prompt", "--route", "--route", "api"],
     ("api", ["--append-system-prompt", "--route"])),
    (["--", "--route", "api"], (None, ["--", "--route", "api"])),
])
def test_extract_route_preserves_passthrough(args, expected):
    assert settings.extract_route(args) == expected


@pytest.mark.parametrize("args", [["--route"], ["--route="], ["--route", "--resume"],
                                  ["--route", "api", "--route", "api"]])
def test_extract_route_rejects_missing_or_duplicate(args):
    with pytest.raises(roster.RosterError):
        settings.extract_route(args)


def test_restricted_skips_discovered_settings_not_supplied(tmp_path, env):
    write(tmp_path / ".claude/settings.json", {"model": "other"})
    settings.validate_settings(["--restricted"], env, cwd=tmp_path)
    with pytest.raises(roster.RosterError):
        settings.validate_settings(["--restricted", "--settings", '{"model":"other"}'],
                                   env, cwd=tmp_path)


def test_disabled_thinking_refused(tmp_path, env):
    with pytest.raises(roster.RosterError, match="alwaysThinkingEnabled"):
        settings.validate_settings(["--settings", '{"alwaysThinkingEnabled":false}'], env, cwd=tmp_path)
    with pytest.raises(roster.RosterError, match="env.MAX_THINKING_TOKENS"):
        settings.validate_settings(["--settings", '{"env":{"MAX_THINKING_TOKENS":"0"}}'],
                                   env, cwd=tmp_path)
    env["MAX_THINKING_TOKENS"] = "0"
    with pytest.raises(roster.RosterError, match="MAX_THINKING_TOKENS"):
        settings.validate_settings([], env, cwd=tmp_path)


@pytest.mark.parametrize("args", [["--remote"], ["--remote", "session"], ["--remote=session"]])
def test_deprecated_cloud_alias_refused(tmp_path, env, args):
    with pytest.raises(roster.RosterError, match="--remote"):
        settings.validate_settings(args, env, cwd=tmp_path)


@pytest.mark.parametrize("source", ["environment", "settings"])
@pytest.mark.parametrize("value", ["1", "true", "0", "false", "off", ""])
def test_simple_mode_auth_sources(tmp_path, env, source, value):
    args = []
    if source == "environment":
        env["CLAUDE_CODE_SIMPLE"] = value
    else:
        args = ["--settings", json.dumps({"env": {"CLAUDE_CODE_SIMPLE": value}})]
    if value in {"1", "true"}:
        with pytest.raises(roster.RosterError, match="CLAUDE_CODE_SIMPLE"):
            settings.validate_settings(args, env, cwd=tmp_path)
    else:
        settings.validate_settings(args, env, cwd=tmp_path)
    env.pop("ANTHROPIC_AUTH_TOKEN")
    env["ANTHROPIC_API_KEY"] = "selected-secret"
    settings.validate_settings(args, env, cwd=tmp_path)
