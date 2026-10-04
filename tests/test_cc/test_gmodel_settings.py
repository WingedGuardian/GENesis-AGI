"""Launch checks for what the --settings pins cannot hold: managed settings and CLI flags."""
import json

import pytest

from genesis.cc import gmodel_routes, roster
from genesis.cc import gmodel_settings as settings

_SECRET = "selected-secret"


@pytest.fixture
def selected():
    return gmodel_routes.SelectedRoute(
        "kimi-k3", "api", "https://api.moonshot.ai/anthropic", "MOONSHOT_API_KEY",
        "kimi-k3[1m]", 1048576, "high",
    )


@pytest.fixture
def pins(selected):
    return gmodel_routes.route_env_pins(selected, _SECRET)


def managed(tmp_path, document, name="managed-settings.json"):
    path = tmp_path / "managed" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return path


def check(tmp_path, selected, pins):
    settings.validate_managed_settings(selected, pins, managed_dir=tmp_path / "managed")


def test_no_managed_settings_is_clean(tmp_path, selected, pins):
    check(tmp_path, selected, pins)


@pytest.mark.parametrize("key", sorted(set(gmodel_routes.route_env_pins(
    gmodel_routes.SelectedRoute("x", "api", "https://e.invalid", "K", "m", 1048576), "t"))))
def test_managed_env_override_of_any_pin_refused_without_values(tmp_path, selected, pins, key):
    # A value no pin accepts: non-off for switches, non-empty for empty pins.
    value = "0" if key == "MAX_THINKING_TOKENS" or pins[key] == "1" else "1"
    path = managed(tmp_path, {"env": {key: value}})
    with pytest.raises(roster.RosterError) as error:
        check(tmp_path, selected, pins)
    assert str(path) in str(error.value)
    assert f"env.{key}" in str(error.value)
    assert _SECRET not in str(error.value)


@pytest.mark.parametrize("key,value", [
    ("CLAUDE_CODE_USE_BEDROCK", ""), ("CLAUDE_CODE_USE_BEDROCK", "0"),
    ("DISABLE_AUTO_COMPACT", "false"), ("MAX_THINKING_TOKENS", "32000"),
    ("ANTHROPIC_API_KEY", ""), ("ANTHROPIC_MODEL", "kimi-k3[1m]"),
    # A switch pinned ON agrees with any truthy spelling.
    ("CLAUDE_CODE_ALWAYS_ENABLE_EFFORT", "true"), ("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "yes"),
])
def test_managed_env_agreeing_with_pin_is_clean(tmp_path, selected, pins, key, value):
    managed(tmp_path, {"env": {key: value}})
    check(tmp_path, selected, pins)


def test_unrelated_managed_env_is_clean(tmp_path, selected, pins):
    managed(tmp_path, {"env": {"MY_APP_KEY": "x"}, "permissions": {"allow": ["Read"]}})
    check(tmp_path, selected, pins)


@pytest.mark.parametrize("document,key", [
    ({"fallbackModel": ["claude-sonnet-5"]}, "fallbackModel"),
    ({"alwaysThinkingEnabled": False}, "alwaysThinkingEnabled"),
    ({"autoCompactEnabled": False}, "autoCompactEnabled"),
    ({"maxEffortLevel": "medium"}, "maxEffortLevel"),
    ({"maxEffortLevel": 3}, "maxEffortLevel"),
    ({"modelSettings": {"kimi-k3[1m]": {"maxEffortLevel": "low"}}}, "maxEffortLevel"),
    ({"forceLoginMethod": "gateway"}, "forceLoginMethod"),
    ({"forceLoginGatewayUrl": "https://gateway.invalid"}, "forceLoginGatewayUrl"),
])
def test_managed_keys_above_the_settings_layer_refused(tmp_path, selected, pins, document, key):
    managed(tmp_path, document)
    with pytest.raises(roster.RosterError, match=key):
        check(tmp_path, selected, pins)


@pytest.mark.parametrize("document", [
    {"availableModels": ["kimi-k3[1m]"]}, {"availableModels": ["sonnet", "kimi-k3"]},
    {"deniedModels": ["claude-opus-5-5"]}, {"fallbackModel": ["kimi-k3[1m]"]},
    # Codex 4176543946 / the preflight rule: Claude Code itself refuses these at
    # startup (forceLogin*: authentication docs, "Restrict login to your
    # organization") or replaces the model with a warning (availableModels /
    # deniedModels: model-config docs, "Restrict model selection"), so gmodel
    # leaves them to Claude Code instead of guessing its matching rules.
    {"availableModels": ["sonnet", "opus"]}, {"availableModels": ["kimi-k"]},
    {"availableModels": "kimi-k3"}, {"deniedModels": ["kimi-k3"]},
    {"forceLoginMethod": "console"}, {"forceLoginOrgUUID": "00000000-0000-0000-0000-000000000000"},
    {"forceLoginOrgUUID": ["00000000-0000-0000-0000-000000000000"]},
    {"maxEffortLevel": "max"}, {"alwaysThinkingEnabled": True},
    # A per-model entry replaces the file-wide cap for that model only.
    {"maxEffortLevel": "low", "modelSettings": {"kimi-k3[1m]": {"maxEffortLevel": "max"}}},
    {"modelSettings": {"claude-opus-5-5": {"maxEffortLevel": "low"}}},
    {"forceLoginMethod": "claudeai"},
    # ANTHROPIC_MODEL (pinned) outranks a managed `model` key.
    {"model": "claude-opus-5-5"},
])
def test_managed_keys_compatible_with_route_are_clean(tmp_path, selected, pins, document):
    managed(tmp_path, document)
    check(tmp_path, selected, pins)


def test_managed_dropins_checked_and_hidden_ignored(tmp_path, selected, pins):
    path = managed(tmp_path, {"env": {"ANTHROPIC_BASE_URL": "https://other.invalid"}},
                   "managed-settings.d/10-routing.json")
    with pytest.raises(roster.RosterError, match=str(path)):
        check(tmp_path, selected, pins)
    path.unlink()
    managed(tmp_path, {"env": {"ANTHROPIC_BASE_URL": "https://other.invalid"}},
            "managed-settings.d/.hidden.json")
    check(tmp_path, selected, pins)


@pytest.mark.parametrize("raw", ['{"secret":"leaked-secret"', '["leaked-secret"]', "null"])
def test_unreadable_managed_file_fails_closed_without_contents(tmp_path, selected, pins, raw):
    path = tmp_path / "managed" / "managed-settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    with pytest.raises(roster.RosterError) as error:
        check(tmp_path, selected, pins)
    assert "leaked-secret" not in str(error.value)


@pytest.mark.parametrize("flag", ["--model", "--fallback-model", "--effort", "--autocompact"])
@pytest.mark.parametrize("equal", [False, True])
def test_cli_pin_conflicts(selected, flag, equal):
    args = [flag + "=leaked-secret"] if equal else [flag, "leaked-secret"]
    with pytest.raises(roster.RosterError, match=flag) as error:
        settings.validate_cli(args, selected)
    assert "leaked-secret" not in str(error.value)


def test_matching_cli_values_accepted(selected):
    settings.validate_cli(["--model", "kimi-k3[1m]", "--effort", "high",
                           "--autocompact", "1048576"], selected)


@pytest.mark.parametrize("effort", ["max", "xhigh", "medium"])
def test_effort_must_match_exactly(selected, effort):
    # CLAUDE_CODE_EFFORT_LEVEL is pinned and outranks --effort, so a different
    # value would be silently ignored; `max` is not an alias of `high`.
    with pytest.raises(roster.RosterError, match="--effort"):
        settings.validate_cli(["--effort", effort], selected)


@pytest.mark.parametrize("args", [
    ["--settings", '{"env":{"ANTHROPIC_AUTH_TOKEN":"leaked-secret"}}'],
    ["--settings=/some/file.json"], ["--bare"], ["--remote"], ["--remote", "session"],
    ["--remote=session"], ["--cloud"], ["--teleport"],
])
def test_flags_that_escape_or_replace_the_pins_refused(selected, args):
    with pytest.raises(roster.RosterError, match=args[0].split("=")[0]) as error:
        settings.validate_cli(args, selected)
    assert "leaked-secret" not in str(error.value)


def test_prompt_option_values_and_separator_are_not_parsed(selected):
    settings.validate_cli([
        "--append-system-prompt", "--model", "--system-prompt", "--settings",
        "--setting-sources", "user", "--", "--model", "unrelated",
    ], selected)


def test_missing_option_value_refused(selected):
    with pytest.raises(roster.RosterError):
        settings.validate_cli(["--model"], selected)


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


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def test_effort_cap_in_any_settings_file_is_reported_not_refused(tmp_path, selected):
    home = tmp_path / "home"
    _write(home / ".claude" / "settings.json", {"maxEffortLevel": "medium"})
    _write(tmp_path / "proj" / ".claude" / "settings.local.json",
           {"modelSettings": {"kimi-k3[1m]": {"maxEffortLevel": "low"}}})
    _write(tmp_path / "proj" / ".claude" / "settings.json", {"maxEffortLevel": "max"})
    warnings = settings.effort_cap_warnings(selected, cwd=tmp_path / "proj", environ={"HOME": str(home)})
    assert len(warnings) == 2
    assert any("settings.json caps effort at medium" in w for w in warnings)
    assert any("settings.local.json caps effort at low" in w for w in warnings)


def test_effort_cap_reader_honours_config_dir_and_skips_unreadable(tmp_path, selected):
    config = tmp_path / "custom"
    _write(config / "settings.json", {"maxEffortLevel": "low"})
    (tmp_path / "proj" / ".claude").mkdir(parents=True)
    (tmp_path / "proj" / ".claude" / "settings.json").write_text("{broken")
    warnings = settings.effort_cap_warnings(
        selected, cwd=tmp_path / "proj",
        environ={"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(config)})
    assert warnings == [f"{config / 'settings.json'} caps effort at low; this route asks for high"]
