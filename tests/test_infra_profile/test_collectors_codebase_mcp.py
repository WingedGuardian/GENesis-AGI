"""codebase-memory-mcp setup facts, their verdict, and the posture rule they feed.

The alert must fire exactly when the server is REGISTERED in the checkout's
.mcp.json, NOT opted out with the kill-switch file, and NOT working (managed
settings missing, or the query unit not enabled). "Could not check" must read
neither as fine nor as an alert. Everything runs against a tmp HOME, a tmp
checkout and a fake systemctl; the real ~/.genesis is never read.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
from pathlib import Path

import pytest

from genesis.awareness import loop as _loop
from genesis.infra_profile import codebase_mcp
from genesis.infra_profile.collectors import CONTAINER_COLLECTORS
from genesis.infra_profile.collectors.codebase_mcp_facts import collect_codebase_mcp
from tests.test_infra_profile._unix_socket import bind_unix_socket

REPO = Path(__file__).resolve().parents[2]
SLUG = "codebase_mcp_registered_unconfigured"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp checkout + tmp HOME + fake systemctl. Returns a configurator."""
    root = tmp_path / "checkout"
    (root / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(REPO / "scripts/lib/cbm_disable_file.sh", root / "scripts/lib/")
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    systemctl = fakebin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        '[ -n "$FAKE_SYSTEMCTL_FAIL" ] && exit 1\n'
        'if [ "$FAKE_UNIT_STATE" = "not-found" ]; then\n'
        "  printf 'LoadState=not-found\\nUnitFileState=\\n'\n"
        "else\n"
        "  printf 'LoadState=loaded\\nUnitFileState=%s\\n' \"$FAKE_UNIT_STATE\"\n"
        "fi\n"
    )
    systemctl.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{fakebin}:/usr/bin:/bin")
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(root))
    monkeypatch.delenv("CODEBASE_MEMORY_MCP_DISABLE_FILE", raising=False)
    monkeypatch.delenv("FAKE_SYSTEMCTL_FAIL", raising=False)
    monkeypatch.setenv("FAKE_UNIT_STATE", "disabled")
    # No user manager unless a test binds one.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-runtime"))

    def configure(*, registered=True, config=False, enabled=False, sentinel=False):
        servers = {"genesis-health": {"command": "x"}}
        if registered:
            servers["codebase-memory-mcp"] = {"command": "x"}
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
        cfg = home / ".genesis" / "config" / "codebase-managed.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        if config:
            cfg.write_text("{}")
        elif cfg.exists():
            cfg.unlink()
        monkeypatch.setenv("FAKE_UNIT_STATE", "enabled" if enabled else "disabled")
        flag = home / ".genesis" / "codebase-memory-mcp.disabled"
        if sentinel:
            flag.write_text("")
        elif flag.exists():
            flag.unlink()
        return root, home

    return configure


def _rule(facts: dict, status: str = "ok") -> list[str]:
    return _loop._infra_missing_protections(
        {"sections": {"codebase_mcp": {"status": status, "facts": facts}}}
    )


@pytest.mark.parametrize(
    ("registered", "config", "enabled", "sentinel"),
    list(itertools.product((True, False), repeat=4)),
)
def test_every_combination_reaches_the_rule(env, registered, config, enabled, sentinel):
    """The REAL collector output into the REAL rule, for all 16 combinations."""
    env(registered=registered, config=config, enabled=enabled, sentinel=sentinel)
    result = asyncio.run(collect_codebase_mcp())
    assert result.status == "ok", result.error
    facts = result.facts
    assert facts["registered"] is registered
    assert facts["config_present"] is config
    assert facts["unit_file_state"] == ("enabled" if enabled else "disabled")
    assert facts["disabled"] is sentinel

    expected = registered and not sentinel and (not config or not enabled)
    assert codebase_mcp.needs_setup(facts) is expected
    assert (SLUG in _rule(facts)) is expected


def test_no_mcp_json_is_not_registered_and_silent(env, tmp_path):
    root, _home = env()
    (root / ".mcp.json").unlink()
    result = asyncio.run(collect_codebase_mcp())
    assert result.status == "ok"
    assert result.facts["registered"] is False
    assert SLUG not in _rule(result.facts)


def test_an_unreadable_mcp_json_degrades_the_section(env):
    """Unparseable is not "not registered": retain, do not guess."""
    root, _home = env()
    (root / ".mcp.json").write_text("{not json")
    assert asyncio.run(collect_codebase_mcp()).status != "ok"


def test_a_failing_systemctl_with_a_user_manager_degrades_the_section(env, tmp_path, monkeypatch):
    env(config=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    bind_unix_socket(runtime / "bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL", "1")
    assert asyncio.run(collect_codebase_mcp()).status != "ok"


def test_no_user_manager_is_unknown_not_fine_and_not_an_alert(env, monkeypatch):
    """Configured, but no manager to ask whether the unit is enabled."""
    env(config=True)
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL", "1")
    result = asyncio.run(collect_codebase_mcp())
    assert result.status == "ok"
    assert result.facts["unit_file_state"] is None
    assert codebase_mcp.needs_setup(result.facts) is None
    assert SLUG not in _rule(result.facts)


def test_a_missing_config_alerts_even_when_the_unit_state_is_unknown(env, monkeypatch):
    env(config=False)
    monkeypatch.setattr(
        codebase_mcp.shutil, "which", lambda name: None if name == "systemctl" else "/bin/bash"
    )
    result = asyncio.run(collect_codebase_mcp())
    assert result.facts["unit_file_state"] is None
    assert SLUG in _rule(result.facts)


@pytest.mark.parametrize("state", ["not-found", "enabled-runtime", "masked", "static"])
def test_anything_but_persistent_enablement_alerts(env, monkeypatch, state):
    """The query unit refuses unless UnitFileState is exactly `enabled`."""
    env(config=True)
    monkeypatch.setenv("FAKE_UNIT_STATE", state)
    facts = asyncio.run(collect_codebase_mcp()).facts
    assert facts["unit_file_state"] == state
    assert SLUG in _rule(facts)


def test_an_unresolvable_kill_switch_path_is_unknown(env, monkeypatch):
    """The shared resolver refuses a relative override; that is not "no opt-out"."""
    env(config=False)
    monkeypatch.setenv("CODEBASE_MEMORY_MCP_DISABLE_FILE", "relative/flag")
    facts = asyncio.run(collect_codebase_mcp()).facts
    assert facts["disabled"] is None
    assert codebase_mcp.needs_setup(facts) is None
    assert SLUG not in _rule(facts)


def test_the_kill_switch_override_is_honoured(env, tmp_path, monkeypatch):
    env(config=False)
    flag = tmp_path / "elsewhere.disabled"
    flag.write_text("")
    monkeypatch.setenv("CODEBASE_MEMORY_MCP_DISABLE_FILE", str(flag))
    facts = asyncio.run(collect_codebase_mcp()).facts
    assert facts["disable_file"] == str(flag)
    assert SLUG not in _rule(facts)


def test_absent_or_failed_section_is_silent():
    assert SLUG not in _loop._infra_missing_protections({"sections": {}})
    alerting = {"registered": True, "disabled": False, "config_present": False}
    assert SLUG not in _rule(alerting, status="error")


def test_retained_alerting_facts_hold_the_all_clear():
    alerting = {"registered": True, "disabled": False, "config_present": False}
    prof = {"sections": {"codebase_mcp": {"status": "error", "facts": alerting}}}
    assert "codebase_mcp" in _loop._infra_unverifiable_planes(prof)
    quiet = {"registered": False}
    prof = {"sections": {"codebase_mcp": {"status": "error", "facts": quiet}}}
    assert "codebase_mcp" not in _loop._infra_unverifiable_planes(prof)


def test_the_detail_text_names_the_problem_the_remedy_and_the_opt_out():
    text = _loop._INFRA_POSTURE_DETAIL[SLUG]
    assert "codebase-managed.json" in text and "genesis-cbm-query.service" in text
    assert "configure" in text and "enable" in text
    assert "If you see the 'registered but unconfigured' alert" in text
    assert "codebase-memory-mcp.disabled" in text


def test_the_doc_section_the_texts_cite_exists():
    doc = (REPO / codebase_mcp.DOC).read_text()
    assert f"## {codebase_mcp.DOC_SECTION}" in doc


def test_notice_lines(env):
    env(registered=True, config=False, sentinel=False)
    facts = asyncio.run(collect_codebase_mcp()).facts
    line = codebase_mcp.notice(facts)
    assert line.startswith("Codebase: NOT SET UP")
    assert "codebase-managed.json is missing" in line
    assert codebase_mcp.DOC_SECTION in line
    assert facts["disable_file"] in line

    env(registered=True, config=True, enabled=True)
    assert codebase_mcp.notice(asyncio.run(collect_codebase_mcp()).facts) == "Codebase: OK"
    env(registered=True, config=False, sentinel=True)
    assert codebase_mcp.notice(asyncio.run(collect_codebase_mcp()).facts) == ""
    env(registered=False)
    assert codebase_mcp.notice(asyncio.run(collect_codebase_mcp()).facts) == ""


def test_collector_is_registered():
    assert collect_codebase_mcp in CONTAINER_COLLECTORS
