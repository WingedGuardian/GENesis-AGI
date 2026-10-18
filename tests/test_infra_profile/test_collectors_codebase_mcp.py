"""codebase-memory-mcp setup facts, their verdict, and the posture rule they feed.

The alert must fire exactly when the server is REGISTERED (the checkout's
.mcp.json or the user-scope ~/.claude.json), NOT opted out with the kill-switch
file, and NOT working (managed settings missing or invalid, or the query unit
not enabled). "Could not check" must read neither as fine nor as an alert.
Everything runs against a tmp HOME, a tmp checkout with a fake
`codebase_managed.py status`, and a systemctl that always fails; the real
~/.genesis is never read.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import runpy
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from genesis.awareness import loop as _loop
from genesis.infra_profile import codebase_mcp
from genesis.infra_profile.collectors import CONTAINER_COLLECTORS
from genesis.infra_profile.collectors import codebase_mcp_facts as facts_mod
from genesis.infra_profile.collectors.codebase_mcp_facts import collect_codebase_mcp
from tests.test_infra_profile._unix_socket import bind_unix_socket

REPO = Path(__file__).resolve().parents[2]
SLUG = "codebase_mcp_registered_unconfigured"

FAKE_STATUS = """import json, os, sys
if os.environ.get("FAKE_STATUS_MODE") == "crash":
    sys.exit(2)
print(os.environ["FAKE_STATUS"])
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp checkout (fake `codebase_managed.py status`) + tmp HOME. Returns a
    configurator. systemctl on PATH always fails: nothing may consult it
    directly — unit state comes from the managed status JSON."""
    root = tmp_path / "checkout"
    (root / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(REPO / "scripts/lib/cbm_disable_file.sh", root / "scripts/lib/")
    (root / "scripts" / "codebase_managed.py").write_text(FAKE_STATUS)
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text("#!/bin/sh\nexit 1\n")
    (fakebin / "systemctl").chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{fakebin}:/usr/bin:/bin")
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-runtime"))
    monkeypatch.delenv("CODEBASE_MEMORY_MCP_DISABLE_FILE", raising=False)
    monkeypatch.delenv("FAKE_STATUS_MODE", raising=False)
    # No user manager unless a test provides one.
    monkeypatch.setattr(facts_mod, "_user_manager_present", lambda: False, raising=False)
    default_flag = home / ".genesis" / "codebase-memory-mcp.disabled"

    def configure(
        *,
        registered=True,
        user_scope=False,
        config=False,
        enabled=False,
        sentinel=False,
        settings_error=None,
        settings_sentinel=None,
        unit=None,
    ):
        servers = {"genesis-health": {"command": "x"}}
        if registered:
            servers["codebase-memory-mcp"] = {"command": "x"}
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
        user = {"codebase-memory-mcp": {"command": "x"}} if user_scope else {}
        (home / ".claude.json").write_text(json.dumps({"mcpServers": user}))
        cfg = home / ".genesis" / "config" / "codebase-managed.json"
        if config:
            cfg.write_text("{}")
        elif cfg.exists():
            cfg.unlink()
        state = unit if unit is not None else ("enabled" if enabled else "disabled")
        if state == "unreachable":
            service = None
        elif state == "not-found":
            service = {"LoadState": "not-found", "UnitFileState": ""}
        else:
            service = {"LoadState": "loaded", "UnitFileState": state}
        status = {
            "settings": {"version": 2, "sentinel": str(settings_sentinel or default_flag)},
            "settings_error": settings_error,
            "service": service,
            "manager_error": "bus unreachable" if service is None else None,
        }
        monkeypatch.setenv("FAKE_STATUS", json.dumps(status))
        if sentinel:
            default_flag.write_text("")
        elif default_flag.exists():
            default_flag.unlink()
        return root, home

    return configure


def _rule(facts: dict, status: str = "ok") -> list[str]:
    return _loop._infra_missing_protections(
        {"sections": {"codebase_mcp": {"status": status, "facts": facts}}}
    )


def _collect():
    return asyncio.run(collect_codebase_mcp())


@pytest.mark.parametrize(
    ("registered", "config", "enabled", "sentinel"),
    list(itertools.product((True, False), repeat=4)),
)
def test_every_combination_reaches_the_rule(env, registered, config, enabled, sentinel):
    """The REAL collector output into the REAL rule, for all 16 combinations."""
    env(registered=registered, config=config, enabled=enabled, sentinel=sentinel)
    result = _collect()
    assert result.status == "ok", result.error
    facts = result.facts
    assert facts["registered"] is registered
    if registered:
        assert facts["config_present"] is config
        assert facts["disabled"] is sentinel

    expected = registered and not sentinel and (not config or not enabled)
    assert codebase_mcp.needs_setup(facts) is expected
    assert (SLUG in _rule(facts)) is expected


def test_no_registration_files_is_not_registered_and_silent(env):
    root, home = env()
    (root / ".mcp.json").unlink()
    (home / ".claude.json").unlink()
    result = _collect()
    assert result.status == "ok"
    assert result.facts["registered"] is False
    assert SLUG not in _rule(result.facts)


@pytest.mark.parametrize("which", ["project", "user"])
def test_an_unparseable_registration_file_degrades_the_section(env, which):
    """Unparseable is not "not registered": retain, do not guess."""
    root, home = env()
    path = root / ".mcp.json" if which == "project" else home / ".claude.json"
    path.write_text("{not json")
    assert _collect().status != "ok"


def test_a_fifo_registration_file_is_refused_without_hanging(env):
    """A FIFO with no writer would block a plain read forever. Bounded by
    running the collector under a deadline rather than trusting it."""
    root, _home = env()
    (root / ".mcp.json").unlink()
    os.mkfifo(root / ".mcp.json")

    def unblock():
        # A blocked reader (the old code) gets EOF; with no reader this is ENXIO.
        with contextlib.suppress(OSError):
            os.close(os.open(root / ".mcp.json", os.O_WRONLY | os.O_NONBLOCK))

    timer = threading.Timer(15, unblock)
    timer.start()
    try:
        result = _collect()
    finally:
        timer.cancel()
    assert result.status != "ok"
    assert "not a regular file" in (result.error or "")


def test_user_scope_registration_alone_alerts(env):
    """bootstrap/install also register at user scope (~/.claude.json)."""
    env(registered=False, user_scope=True)
    facts = _collect().facts
    assert facts["registered"] is True and facts["registered_user"] is True
    assert SLUG in _rule(facts)


def test_invalid_settings_alert_instead_of_reading_ok(env):
    """A settings file the managed tool rejects is not "configured"."""
    env(config=True, enabled=True, settings_error="requires immutable schema 2")
    facts = _collect().facts
    assert facts["config_valid"] is False
    assert SLUG in _rule(facts)
    line = codebase_mcp.notice(facts)
    assert "is invalid (requires immutable schema 2)" in line


def test_the_sentinel_saved_in_settings_is_the_opt_out(env, tmp_path):
    custom = tmp_path / "off" / "cbm"
    custom.parent.mkdir()
    custom.write_text("")
    env(config=True, enabled=False, settings_sentinel=custom)
    facts = _collect().facts
    assert facts["disable_file"] == str(custom)
    assert facts["disabled"] is True
    assert SLUG not in _rule(facts)
    # ...and an armed default path does NOT stand in for the configured one.
    env(config=True, enabled=False, settings_sentinel=tmp_path / "unarmed", sentinel=True)
    assert SLUG in _rule(_collect().facts)


def test_opted_out_while_the_manager_is_unreachable_is_silent(env, monkeypatch, tmp_path):
    """The opt-out short-circuits before any unit state is consulted, even by
    the strict collector, so an old alert resolves instead of being held. A
    REAL user-manager socket, so a collector that still probed the unit would
    degrade the section (and retain the old alert) here."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    bind_unix_socket(runtime / "bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(facts_mod, "_user_manager_present", lambda: True, raising=False)
    env(config=True, unit="unreachable", sentinel=True)
    result = _collect()
    assert result.status == "ok", result.error
    assert SLUG not in _rule(result.facts)


def test_an_unreachable_manager_degrades_when_one_exists(env, monkeypatch):
    monkeypatch.setattr(facts_mod, "_user_manager_present", lambda: True, raising=False)
    env(config=True, unit="unreachable")
    assert _collect().status != "ok"


def test_no_user_manager_is_unknown_not_fine_and_not_an_alert(env):
    env(config=True, unit="unreachable")
    result = _collect()
    assert result.status == "ok"
    assert result.facts["unit_file_state"] is None
    assert codebase_mcp.needs_setup(result.facts) is None
    assert codebase_mcp.notice(result.facts) != "Codebase: OK"
    assert SLUG not in _rule(result.facts)


def test_a_crashing_status_degrades_the_section(env, monkeypatch):
    env(config=True)
    monkeypatch.setenv("FAKE_STATUS_MODE", "crash")
    assert _collect().status != "ok"


def test_the_user_manager_check_uses_the_normalized_environment(monkeypatch, tmp_path):
    """No XDG_RUNTIME_DIR in the server env must not read as "no manager"."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    bind_unix_socket(runtime / "bus")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(facts_mod, "systemctl_env", lambda: {"XDG_RUNTIME_DIR": str(runtime)})
    assert facts_mod._user_manager_present() is True


def test_managed_status_reaches_the_manager_without_bus_variables(monkeypatch):
    """The unit state is sourced from `status`, whose systemctl call must carry
    the normalized XDG_RUNTIME_DIR/DBUS environment."""
    seen: dict = {}

    def fake_check_output(argv, **kwargs):
        seen.update(kwargs.get("env") or {})
        return "UnitFileState=disabled\n"

    old_path = sys.path[:]
    try:
        managed = runpy.run_path(str(REPO / "scripts/codebase_managed.py"))
    finally:
        sys.path[:] = old_path
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    result = managed["status"](None)
    assert result["service"] == {"UnitFileState": "disabled"}
    assert seen.get("XDG_RUNTIME_DIR") and seen.get("DBUS_SESSION_BUS_ADDRESS")


@pytest.mark.parametrize("state", ["not-found", "enabled-runtime", "masked", "static"])
def test_anything_but_persistent_enablement_alerts(env, state):
    """The query unit refuses unless UnitFileState is exactly `enabled`."""
    env(config=True, unit=state)
    facts = _collect().facts
    assert facts["unit_file_state"] == state
    assert SLUG in _rule(facts)


def test_an_unresolvable_kill_switch_path_is_unknown(env, monkeypatch):
    """The shared resolver refuses a relative override; that is not "no opt-out"."""
    env(config=False)
    monkeypatch.setenv("CODEBASE_MEMORY_MCP_DISABLE_FILE", "relative/flag")
    facts = _collect().facts
    assert facts["disabled"] is None
    assert codebase_mcp.needs_setup(facts) is None
    assert SLUG not in _rule(facts)


def test_the_kill_switch_override_is_honoured_without_settings(env, tmp_path, monkeypatch):
    env(config=False)
    flag = tmp_path / "elsewhere.disabled"
    flag.write_text("")
    monkeypatch.setenv("CODEBASE_MEMORY_MCP_DISABLE_FILE", str(flag))
    facts = _collect().facts
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
    facts = _collect().facts
    line = codebase_mcp.notice(facts)
    assert line.startswith("Codebase: NOT SET UP")
    assert "codebase-managed.json is missing" in line
    assert codebase_mcp.DOC_SECTION in line
    assert facts["disable_file"] in line

    env(registered=True, config=True, enabled=True)
    assert codebase_mcp.notice(_collect().facts) == "Codebase: OK"
    env(registered=True, config=False, sentinel=True)
    assert codebase_mcp.notice(_collect().facts) == ""
    env(registered=False)
    assert codebase_mcp.notice(_collect().facts) == ""


def test_collector_is_registered():
    assert collect_codebase_mcp in CONTAINER_COLLECTORS
