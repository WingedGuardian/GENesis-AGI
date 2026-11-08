"""Owner-mode Codex gets Claude Code's Genesis tools; unattended Codex keeps the floor.

``scripts/codex_owner_mcp.py`` reads the dispatched markers Codex forwards
(``env_vars`` in ``.codex/config.toml``) before sanitizing the environment.
Unmarked: the ordinary server, labelled ``codex``. Marked: exactly the external
floor the old launcher started, and no outreach or recon server. The project
config pre-approves only the read tools (owner ruling: reads run silently,
anything else asks), and every name it lists must be a registered tool.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "codex_owner_mcp_under_test",
    ROOT / "scripts" / "codex_owner_mcp.py",
)
owner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner)

# The unattended floor keeps the external launcher's own path; owner mode runs
# the MAIN checkout's server, as Claude Code does.
LAUNCHER = str(ROOT / ".claude" / "mcp" / "run-mcp-server")
OWNER_LAUNCHER = str(owner.runtime_root() / ".claude" / "mcp" / "run-mcp-server")
BASE = {"HOME": "/h", "PATH": "/bin"}


def test_unmarked_codex_gets_the_ordinary_server_labelled_codex():
    argv, env = owner.plan(["--server", "memory"], BASE)
    assert argv == [OWNER_LAUNCHER, "--server", "memory"]
    assert env["GENESIS_CALLER_PROFILE"] == "codex"
    assert env["GENESIS_REPO_ROOT"]


@pytest.mark.parametrize(
    "markers",
    [
        {"GENESIS_CC_SESSION": "1"},
        {"GENESIS_CC_SESSION": "true"},  # unrecognised reads as unattended
        {"GENESIS_SESSION_ORIGIN": "external_untrusted"},
    ],
)
def test_marked_codex_keeps_the_external_floor(markers):
    argv, env = owner.plan(["--server", "health"], {**BASE, **markers})
    assert argv == [LAUNCHER, "--external-client", "--server", "health"]
    assert "GENESIS_CALLER_PROFILE" not in env
    assert "GENESIS_CC_SESSION" not in env and "GENESIS_SESSION_ORIGIN" not in env


@pytest.mark.parametrize("value", ["", "0"])
def test_an_empty_or_zero_marker_is_the_owner(value):
    argv, env = owner.plan(["--server", "health"], {**BASE, "GENESIS_CC_SESSION": value})
    assert "--external-client" not in argv
    assert env["GENESIS_CALLER_PROFILE"] == "codex"


@pytest.mark.parametrize("server", ["outreach", "recon"])
def test_marked_codex_gets_no_outreach_or_recon_server(server):
    assert owner.plan(["--server", server], {**BASE, "GENESIS_CC_SESSION": "1"}) is None
    argv, _ = owner.plan(["--server", server], BASE)
    assert argv == [OWNER_LAUNCHER, "--server", server]


def test_an_inherited_label_never_survives_into_the_floor():
    _, env = owner.plan(
        ["--server", "memory"],
        {**BASE, "GENESIS_CC_SESSION": "1", "GENESIS_CALLER_PROFILE": "codex"},
    )
    assert "GENESIS_CALLER_PROFILE" not in env


def test_server_name_reads_both_spellings():
    assert owner.server_name(["--server", "recon"]) == "recon"
    assert owner.server_name(["--server=outreach"]) == "outreach"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--server"],
        ["--server", "health", "--external-client", "validator"],
        ["--server", "memory", "--server", "health"],
        ["--external-client", "--server", "health"],
        ["--serv", "health"],
        ["--server", "browser"],
        ["--server="],
    ],
)
def test_anything_but_one_named_server_is_refused(args):
    # Refused before any environment is read: an extra argument would otherwise
    # reach the server, whose own parser reads repeats and prefixes differently.
    with pytest.raises(SystemExit) as stop:
        owner.plan(args, BASE)
    assert stop.value.code not in (0, None)


def test_a_missing_owner_launcher_starts_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(owner, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(owner.os, "environ", {**BASE})
    monkeypatch.setattr(owner.os, "execve", lambda *a: pytest.fail("exec'd a missing launcher"))
    with pytest.raises(SystemExit) as stop:
        owner.main(["--server", "health"])
    assert "launcher not found" in str(stop.value.code)


def test_main_execs_the_plan_and_exits_cleanly_when_there_is_none(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(owner.os, "execve", lambda *a: captured.setdefault("argv", a[1]))
    monkeypatch.setattr(owner.os, "environ", {**BASE})
    owner.main(["--server", "health"])
    assert captured["argv"] == [OWNER_LAUNCHER, "--server", "health"]

    monkeypatch.setattr(owner.os, "environ", {**BASE, "GENESIS_CC_SESSION": "1"})
    with pytest.raises(SystemExit) as stop:
        owner.main(["--server", "outreach"])
    assert stop.value.code == 0
    assert "not available to an unattended Codex run" in capsys.readouterr().err


def _config() -> dict:
    return tomllib.loads((ROOT / ".codex" / "config.toml").read_text())


def _servers() -> dict:
    return _config()["mcp_servers"]


def test_config_has_only_keys_a_strict_codex_run_accepts():
    # An unrecognised top-level key makes a strict Codex run refuse to start.
    assert set(_config()) <= {"mcp_servers", "hooks"}


@pytest.mark.parametrize("server", ["health", "memory"])
def test_the_unattended_floor_is_pre_approved(server):
    # Unattended runs approve nothing (approval policy never), so every floor tool
    # must be pre-approved or the floor stops working.
    from genesis.mcp.external_profiles import profile_tools

    approved = set(_servers()[f"genesis-{server}"]["tools"])
    assert profile_tools(server, "external") <= approved


def test_the_unattended_floor_is_exactly_the_reviewed_set():
    # The floor is what an unattended Codex run (no human to ask) can call. It
    # changes only by editing this list, so a new tool cannot join it unread.
    from genesis.mcp.external_profiles import profile_tools

    assert profile_tools("health", "external") == {
        "health_status",
        "bootstrap_manifest",
        "subsystem_heartbeats",
        "job_health",
        "provider_activity",
    }
    assert profile_tools("memory", "external") == {
        "memory_recall",
        "memory_expand",
        "memory_core_facts",
        "memory_stats",
        "knowledge_recall",
        "knowledge_status",
        "procedure_recall",
        "observation_query",
    }


def test_project_config_routes_every_genesis_server_through_the_owner_launcher():
    servers = _servers()
    genesis = {name for name in servers if name.startswith("genesis-")}
    assert genesis == {"genesis-health", "genesis-memory", "genesis-outreach", "genesis-recon"}
    for name in genesis:
        server = servers[name]
        assert server["args"] == [
            "scripts/codex_owner_mcp.py",
            "--server",
            name.removeprefix("genesis-"),
        ]
        assert server["command"] == "python3" and server["cwd"] == "."
        assert server["startup_timeout_sec"] == 30
        # Without forwarding, the launcher could not tell an unattended run apart.
        assert set(server["env_vars"]) == {"GENESIS_CC_SESSION", "GENESIS_SESSION_ORIGIN"}
        # Parity: no client-side allowlist; the server decides the tool set.
        assert "enabled_tools" not in server and "disabled_tools" not in server
        # Unlisted tools (including new ones) ask before running.
        assert server["default_tools_approval_mode"] == "prompt"
        assert {t["approval_mode"] for t in server.get("tools", {}).values()} == {"approve"}


async def _registered(server: str) -> set[str]:
    module = {
        "health": "genesis.mcp.health_mcp",
        "memory": "genesis.mcp.memory_mcp",
        "outreach": "genesis.mcp.outreach_mcp",
        "recon": "genesis.mcp.recon_mcp",
    }[server]
    mcp = importlib.import_module(module).mcp
    return {tool.name for tool in await mcp.list_tools()}


@pytest.mark.parametrize("server", ["health", "memory", "outreach", "recon"])
async def test_every_pre_approved_tool_is_a_registered_tool(server):
    approved = set(_servers()[f"genesis-{server}"].get("tools", {}))
    assert approved, "each server pre-approves at least its read tools"
    assert approved <= await _registered(server)


# Tools that write, send, spend or spawn. None may be pre-approved; this list is
# a tripwire for the obvious ones, not the full classification.
_NEVER_PRE_APPROVED = {
    "health": {
        "browser_fill_card",
        "browser_navigate",
        "web_agent",
        "deliberate",
        "settings_update",
        "direct_session_run",
        "task_submit",
        "follow_up_create",
        "ego_decision",
        "ego_proposal_resolve",
        "web_fetch",
        "web_search",
    },
    "memory": {
        "memory_store",
        "reference_store",
        "reference_delete",
        "knowledge_ingest_source",
        "procedure_store",
        "entity_adjudication_apply",
        "observation_write",
        "resume_review",
    },
    "outreach": {"outreach_send", "outreach_send_and_wait", "marketing_send", "provision_grow"},
    "recon": {"recon_store_finding", "recon_triage"},
}


@pytest.mark.parametrize("server", sorted(_NEVER_PRE_APPROVED))
def test_side_effect_tools_are_never_pre_approved(server):
    approved = set(_servers()[f"genesis-{server}"].get("tools", {}))
    assert not approved & _NEVER_PRE_APPROVED[server]


def test_owner_launcher_reuses_the_external_launcher_helpers():
    assert owner.sanitized_environment.__module__ == "codex_external_mcp"
    assert owner.launcher_path.__module__ == "codex_external_mcp"


def test_client_session_name(monkeypatch):
    from genesis.caller_context import client_session_name

    monkeypatch.delenv("GENESIS_CALLER_PROFILE", raising=False)
    assert client_session_name() == "Claude Code session"
    monkeypatch.setenv("GENESIS_CALLER_PROFILE", "codex")
    assert client_session_name() == "Codex session"


def test_labelled_calls_never_consume_a_pending_plan_bookmark(monkeypatch, tmp_path):
    memory = importlib.import_module("genesis.mcp.memory")
    marker = tmp_path / "pending.json"
    marker.write_text("{}")
    spawned = []
    monkeypatch.setattr(memory, "_PLAN_BOOKMARK_PENDING", marker)
    monkeypatch.setattr(memory, "_process_pending_bookmarks", True)
    monkeypatch.setattr(memory, "_store", object())
    monkeypatch.setattr(memory, "_retriever", object())
    monkeypatch.setattr(memory, "_db", object())
    monkeypatch.setattr(
        "genesis.db.request_database.spawn_database_task",
        lambda *a, **k: spawned.append(k.get("name")) or a[1].close(),
    )
    monkeypatch.setenv("GENESIS_CALLER_PROFILE", "codex")
    memory._require_init()
    assert spawned == []
    monkeypatch.delenv("GENESIS_CALLER_PROFILE")
    memory._require_init()
    assert spawned == ["plan-bookmark-pending"]
