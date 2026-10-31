"""The shared-MCP rollout switch (MCP P7): which command a Genesis MCP registration runs.

`scripts/lib/mcp_rollout.py` decides, per server, between the per-session stdio
server (today's behaviour) and the thin adapter to a shared service. These tests
pin the contract that keeps it inert until the shared service and its
prerequisites exist: a code ceiling no config can exceed, a unit that must be
loaded, enabled and running, and Codex (`--external-client`) never routed here.
`.claude/mcp/run-mcp-server`'s fast path is driven as a real subprocess with a
recording python shim. No test calls the real systemctl.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "lib" / "mcp_rollout.py"
LAUNCHER = REPO / ".claude" / "mcp" / "run-mcp-server"
SHIPPED = REPO / "config" / "mcp_shared.json"

_spec = importlib.util.spec_from_file_location("mcp_rollout_under_test", SCRIPT)
rollout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rollout)

STDIO = ["/venv/bin/python", "/repo/scripts/genesis_mcp_server.py", "--server", "memory"]
ADAPTER = [
    "{python}",
    "{root}/adapter.py",
    "--server",
    "{server}",
    "--url",
    "{url}",
    "--stage",
    "{stage}",
]


def _config(stage="memory-floor", adapter=None):
    cfg = json.loads(SHIPPED.read_text())
    cfg["stage"] = stage
    cfg["adapter"] = {"argv": ADAPTER if adapter is None else adapter}
    return cfg


def _ready(_unit):
    return True, "running"


def _decide(argv=STDIO, *, config=None, env=None, ready=_ready, ceiling="writes"):
    return rollout.decide(
        argv,
        config=_config() if config is None else config,
        env=env or {},
        ready=ready,
        ceiling=ceiling,
        python="/venv/bin/python",
        root="/repo",
    )


def test_shipped_config_and_code_ceiling_keep_every_server_on_stdio():
    """The inert contract: shipped stage off, code ceiling off, so the shipped
    install never leaves stdio whatever an overlay asks for."""
    shipped = json.loads(SHIPPED.read_text())
    assert shipped["stage"] == "off"
    assert rollout.MAX_STAGE == "off"
    for server in rollout.STAGE_SERVERS["writes"]:
        assert shipped["servers"][server]["unit"]
        assert shipped["servers"][server]["url"].startswith("http://127.0.0.1:")
    overlay = _config("writes")
    d = rollout.decide(
        STDIO,
        config=overlay,
        env={},
        ready=_ready,
        ceiling=rollout.MAX_STAGE,
        python="/p",
        root="/r",
    )
    assert d.argv == STDIO and "ceiling" in d.reason


def test_ceiling_caps_the_requested_stage():
    d = _decide(config=_config("writes"), ceiling="off")
    assert d.argv == STDIO
    assert "ceiling" in d.reason


def test_stage_includes_server_and_ready_unit_selects_the_adapter():
    d = _decide()
    assert d.argv == [
        "/venv/bin/python",
        "/repo/adapter.py",
        "--server",
        "memory",
        "--url",
        "http://127.0.0.1:8101/mcp",
        "--stage",
        "memory-floor",
    ]
    assert d.shared


def test_server_outside_the_stage_stays_stdio():
    argv = [*STDIO[:-1], "health"]
    d = _decide(argv)
    assert d.argv == argv and not d.shared
    assert d.reason == "health is not shared at stage memory-floor"


def test_unit_not_ready_falls_back_to_stdio_naming_the_unit():
    d = _decide(ready=lambda unit: (False, f"{unit}: inactive"))
    assert d.argv == STDIO
    assert "genesis-mcp-memory.service" in d.reason


@pytest.mark.parametrize("flag", [["--external-client"], ["--external-client", "--profile", "x"]])
def test_external_client_is_never_routed(flag):
    argv = [*STDIO[:2], *flag, *STDIO[2:]]
    d = _decide(argv)
    assert d.argv == argv and not d.shared
    assert "external" in d.reason


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_kill_switch_forces_stdio(value):
    d = _decide(env={"GENESIS_MCP_SHARED_DISABLED": value})
    assert d.argv == STDIO and "DISABLED" in d.reason


def test_kill_switch_zero_is_off():
    assert _decide(env={"GENESIS_MCP_SHARED_DISABLED": "0"}).shared


def test_env_stage_overrides_the_config():
    d = _decide(config=_config("off"), env={"GENESIS_MCP_SHARED_STAGE": "memory-floor"})
    assert d.shared


@pytest.mark.parametrize("bad", ["Writes", "all", "", 3])
def test_invalid_stage_is_an_error_not_a_guess(bad):
    with pytest.raises(rollout.RolloutError):
        _decide(config=_config(bad))


def test_no_adapter_configured_stays_stdio():
    d = _decide(config=_config(adapter=[]))
    assert d.argv == STDIO and "adapter" in d.reason


@pytest.mark.parametrize(
    "args",
    [
        ["--server=memory"],
        ["--server", "memory", "--transport", "stdio"],
        ["--ser", "memory"],
        ["--external", "--server", "memory"],
        ["--server", "discord-bot"],
    ],
)
def test_only_the_registered_form_is_routed(args):
    """A closed form, not a parse: anything but `--server <shared name>` keeps the
    stdio command it was given."""
    argv = [*STDIO[:2], *args]
    d = _decide(argv)
    assert d.argv == argv and not d.shared


def test_ceiling_below_the_request_shares_up_to_the_ceiling():
    cfg = _config("writes")
    assert _decide(config=cfg, ceiling="memory-floor").shared
    health = _decide([*STDIO[:-1], "health"], config=cfg, ceiling="memory-floor")
    assert not health.shared and "capped" in health.reason


def test_load_config_merges_the_user_overlay(tmp_path):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    shutil.copy(SHIPPED, root / "config" / "mcp_shared.json")
    home = tmp_path / "gh"
    (home / "config").mkdir(parents=True)
    (home / "config" / "mcp_shared.json").write_text(
        json.dumps({"stage": "health-reads", "servers": {"memory": {"unit": "x.service"}}})
    )
    cfg = rollout.load_config(root, {"GENESIS_HOME": str(home)})
    assert cfg["stage"] == "health-reads"
    assert cfg["servers"]["memory"]["unit"] == "x.service"
    # Keys the overlay did not name keep the shipped value.
    assert cfg["servers"]["memory"]["url"] == "http://127.0.0.1:8101/mcp"
    assert cfg["servers"]["health"]["unit"] == "genesis-mcp-health.service"


def test_relative_genesis_home_is_refused(tmp_path):
    with pytest.raises(rollout.RolloutError):
        rollout.load_config(REPO, {"GENESIS_HOME": "rel/path"})


@pytest.mark.parametrize(
    ("props", "ready"),
    [
        ("LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=running\n", True),
        ("LoadState=loaded\nUnitFileState=disabled\nActiveState=active\nSubState=running\n", False),
        (
            "LoadState=loaded\nUnitFileState=enabled\nActiveState=activating\nSubState=start\n",
            False,
        ),
        ("LoadState=not-found\nUnitFileState=\nActiveState=inactive\nSubState=dead\n", False),
        ("", False),
    ],
)
def test_unit_ready_requires_loaded_enabled_active_running(props, ready):
    def runner(argv):
        assert argv[:3] == ["systemctl", "--user", "show"]
        return 0, props

    assert rollout.unit_ready("u.service", runner=runner)[0] is ready


def test_unit_ready_is_false_when_systemctl_fails():
    """Even a full "ready" answer does not count when systemctl itself failed."""
    props = "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=running\n"
    assert rollout.unit_ready("u.service", runner=lambda argv: (1, props))[0] is False


def test_launch_execs_stdio_when_the_decision_raises(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(rollout.os, "execv", lambda path, argv: calls.append(argv))

    def boom(*a, **k):
        raise rollout.RolloutError("bad overlay")

    monkeypatch.setattr(rollout, "load_config", boom)
    rollout.main(["launch", "--root", "/repo", "--", *STDIO])
    assert calls == [STDIO]
    assert "bad overlay" in capsys.readouterr().err


def test_launch_execs_stdio_when_the_adapter_cannot_start(monkeypatch, capsys):
    calls = []

    def execv(path, argv):
        calls.append(argv)
        if argv != STDIO:
            raise FileNotFoundError(path)

    monkeypatch.setattr(rollout.os, "execv", execv)
    shared = rollout.Decision(["/nope/adapter"], True, "shared")
    monkeypatch.setattr(rollout, "decide", lambda *a, **k: shared)
    monkeypatch.setattr(rollout, "load_config", lambda *a, **k: {})
    rollout.main(["launch", "--root", "/repo", "--", *STDIO])
    assert calls == [["/nope/adapter"], STDIO]
    assert "cannot start the adapter" in capsys.readouterr().err


def test_units_lists_every_configured_unit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    rollout.main(["units", "--root", str(REPO)])
    out = capsys.readouterr().out.split()
    assert out == [
        "genesis-mcp-health.service",
        "genesis-mcp-memory.service",
        "genesis-mcp-outreach.service",
        "genesis-mcp-recon.service",
    ]


# ── The launcher's fast path ─────────────────────────────────────────


def _launcher_tree(tmp_path):
    """A checkout holding the REAL launcher and helper, with a python shim that
    records how it was called."""
    root = tmp_path / "checkout"
    (root / ".claude" / "mcp").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(LAUNCHER, root / ".claude" / "mcp" / "run-mcp-server")
    shutil.copy(SCRIPT, root / "scripts" / "lib" / "mcp_rollout.py")
    (root / "scripts" / "genesis_mcp_server.py").write_text("")
    log = tmp_path / "python.log"
    shim = root / ".venv" / "bin" / "python"
    shim.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{log}"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return root, log


def _run_launcher(root, tmp_path, *args, env_extra=None):
    home = tmp_path / "gh"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(home),
        **(env_extra or {}),
    }
    return subprocess.run(
        [str(root / ".claude" / "mcp" / "run-mcp-server"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _overlay(tmp_path):
    (tmp_path / "gh" / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "gh" / "config" / "mcp_shared.json").write_text('{"stage": "off"}\n')


def test_launcher_without_opt_in_never_runs_the_helper(tmp_path):
    root, log = _launcher_tree(tmp_path)
    _run_launcher(root, tmp_path, "--server", "memory")
    assert log.read_text().splitlines() == [f"{root}/scripts/genesis_mcp_server.py --server memory"]


def test_launcher_with_an_overlay_routes_through_the_helper(tmp_path):
    root, log = _launcher_tree(tmp_path)
    _overlay(tmp_path)
    _run_launcher(root, tmp_path, "--server", "memory")
    line = log.read_text().splitlines()[0]
    assert line.startswith(f"-I -S {root}/scripts/lib/mcp_rollout.py launch --root {root} -- ")
    assert line.endswith(f"{root}/scripts/genesis_mcp_server.py --server memory")


def test_launcher_with_the_env_stage_routes_through_the_helper(tmp_path):
    root, log = _launcher_tree(tmp_path)
    _run_launcher(
        root, tmp_path, "--server", "memory", env_extra={"GENESIS_MCP_SHARED_STAGE": "off"}
    )
    assert "mcp_rollout.py launch" in log.read_text()


@pytest.mark.parametrize(
    ("args", "env_extra"),
    [
        (["--external-client", "--server", "memory"], {}),
        (["--server", "memory"], {"GENESIS_MCP_SHARED_DISABLED": "1"}),
    ],
)
def test_launcher_bypasses_the_helper_for_codex_and_the_kill_switch(tmp_path, args, env_extra):
    root, log = _launcher_tree(tmp_path)
    _overlay(tmp_path)
    _run_launcher(root, tmp_path, *args, env_extra=env_extra)
    assert "mcp_rollout" not in log.read_text()
