"""Launch checks for what the --settings pins cannot hold: the CLI grammar and effort caps."""
import json

import pytest

from genesis.cc import gmodel_routes, roster
from genesis.cc import gmodel_settings as settings


@pytest.fixture
def selected():
    return gmodel_routes.SelectedRoute(
        "kimi-k3", "api", "https://api.moonshot.ai/anthropic", "MOONSHOT_API_KEY",
        "kimi-k3[1m]", 1048576, "high",
    )


@pytest.mark.parametrize("args, expected", [
    (["--route", "api", "--model", "selected"], ("api", ["--model", "selected"])),
    (["--route=subscription", "--resume", "session"], ("subscription", ["--resume", "session"])),
    # Only the first position is the launcher's; anything later is Claude's.
    (["--resume", "session", "--route", "api"], (None, ["--resume", "session", "--route", "api"])),
    (["--", "--route", "api"], (None, ["--", "--route", "api"])),
])
def test_extract_route_reads_only_the_first_position(args, expected):
    assert settings.extract_route(args) == expected


@pytest.mark.parametrize("args", [["--route"], ["--route="], ["--route", "--resume"]])
def test_extract_route_rejects_a_missing_value(args):
    with pytest.raises(roster.RosterError):
        settings.extract_route(args)


@pytest.mark.parametrize("args, headless", [
    (["-p", "hi"], True), (["--print=json"], True), (["--bg"], True), (["-cp"], True),
    (["--permission-mode", "plan"], False), (["-c"], False), (["--", "-p"], False),
    (["explain -p and --print"], False),
])
def test_headless_is_a_whole_token_check(args, headless):
    assert settings.is_headless(args) is headless


def test_refusal_never_prints_a_value():
    with pytest.raises(roster.RosterError) as error:
        settings.validate_cli(["--settings={\"env\":{\"ANTHROPIC_AUTH_TOKEN\":\"leaked-secret\"}}"])
    assert "leaked-secret" not in str(error.value)
    settings.validate_cli(["--resume", "session", "--", "--settings", "--model"])


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


@pytest.mark.parametrize("args", [
    ["--append-system-prompt", "--", "--model", "claude-opus-4-6"],
    ["--name", "--", "--settings", "other.json"],
])
def test_delimiter_as_an_option_value_does_not_hide_later_flags(args):
    """Claude Code gives a bare `--` to a value-taking option and keeps parsing
    (MEASURED, CC 2.1.280), so gmodel must keep reading too."""
    with pytest.raises(roster.RosterError):
        settings.validate_cli(args)


def test_delimiter_as_an_option_value_does_not_hide_headless_or_resume():
    assert settings.is_headless(["-n", "--", "--bg"])
    assert settings.resumes(["--name", "--", "--continue"])
    assert settings.resumes(["-cp"])
    # A real delimiter after a value still ends the options.
    assert not settings.is_headless(["--name", "session", "--", "-p"])
