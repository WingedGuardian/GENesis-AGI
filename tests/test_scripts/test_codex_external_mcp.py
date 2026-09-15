"""Boundary tests for the Codex external Genesis-MCP launcher."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.codex_external_mcp as subject


def test_sanitized_environment_removes_session_context(monkeypatch) -> None:
    for marker in subject._SESSION_CONTEXT:
        monkeypatch.setenv(marker, "inherited")

    environment = subject.sanitized_environment()

    assert all(marker not in environment for marker in subject._SESSION_CONTEXT)


def test_sanitized_environment_preserves_unrelated_values() -> None:
    environment = subject.sanitized_environment(
        {
            "GENESIS_CC_SESSION": "1",
            "KEEP": "value",
        }
    )

    assert environment["KEEP"] == "value"
    assert environment["GENESIS_REPO_ROOT"] == str(subject.runtime_root())


def test_runtime_root_uses_main_checkout_for_linked_worktree(monkeypatch, tmp_path) -> None:
    main = tmp_path / "main"
    checkout = main / ".claude" / "worktrees" / "feature"
    common = main / ".git"
    monkeypatch.setattr(
        subject.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{common}\n",
        ),
    )

    assert subject.runtime_root(checkout) == main.resolve()


def test_runtime_root_falls_back_when_git_is_unavailable(monkeypatch, tmp_path) -> None:
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 2)

    monkeypatch.setattr(subject.subprocess, "run", unavailable)

    assert subject.runtime_root(tmp_path) == tmp_path.resolve()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("raise", id="git-times-out"),
        pytest.param("empty", id="git-returns-nothing"),
    ],
)
def test_an_unresolved_LINKED_worktree_refuses_rather_than_substituting(
    monkeypatch, tmp_path, failure
) -> None:
    """Falling back to the checkout is right in main and WRONG in a worktree.

    The substitution is silent and self-concealing: the shell launcher runs its
    own unbounded git lookup and still finds the main virtualenv, so the server
    starts — with GENESIS_REPO_ROOT naming the worktree and the DB path
    resolving to its missing or stale copy. Memory is then unavailable or
    pointed at the wrong state, and nothing says so.

    A linked worktree's `.git` is a FILE (a `gitdir:` pointer); main's is a
    directory. That is checked WITHOUT git, because git is what just failed.
    """
    worktree = tmp_path / "feature"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/feature\n")

    if failure == "raise":

        def broken(*args, **kwargs):
            raise subprocess.TimeoutExpired("git", 2)
    else:

        def broken(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(subject.subprocess, "run", broken)

    with pytest.raises(SystemExit) as exc:
        subject.runtime_root(worktree)

    assert "linked worktree" in str(exc.value)


def test_launcher_path_targets_existing_portable_launcher() -> None:
    path = subject.launcher_path()

    assert path.name == "run-mcp-server"
    assert path.parent.name == "mcp"
    assert path.is_file()


def test_main_executes_launcher_with_sanitized_environment(monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.setenv("GENESIS_SESSION_ID", "genesis-session")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-session")
    captured = {}

    def fake_execve(path, argv, environment) -> None:
        captured.update(path=path, argv=argv, environment=environment)

    monkeypatch.setattr(subject.os, "execve", fake_execve)

    subject.main(["--server", "health"])

    assert captured["argv"][-2:] == ["--server", "health"]
    assert all(marker not in captured["environment"] for marker in subject._SESSION_CONTEXT)
    assert captured["environment"]["GENESIS_REPO_ROOT"] == str(subject.runtime_root())


def test_project_config_exposes_only_the_read_oriented_pilot_tools() -> None:
    """The external surface admits no tool a CALLER can ask to write.

    This list is the whole security story for the external client, and it is
    worth stating precisely, because the obvious phrasing is false. Several of
    these tools do write incidentally — recall is read-MOSTLY, and bumps access
    counters on a hit. The invariant is narrower and it is the one that matters:
    **no allowlisted tool takes an argument by which a caller can REQUEST a
    mutation.** Incidental bookkeeping the caller cannot steer is not the same
    as handing it a switch.

    `infrastructure_profile` is deliberately absent for exactly that reason: an
    allowlist grants the TOOL, not its ARGUMENTS, so admitting it would admit
    `refresh=true` — which persists profile files, emits DB drift observations
    and renders shared documents. The tool's own empty-state response hints
    "pass refresh=true", so an agent would reach for it unprompted.

    Enumerated rather than spot-checked: at the time of writing every other
    entry has a read-only signature. Adding a tool here means checking its
    ARGUMENTS for a caller-requestable write, not just its name — and this
    assertion is what forces that check to happen deliberately.
    """
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / ".codex" / "config.toml").read_text())

    assert config["mcp_optional_startup_grace_ms"] == 0

    servers = config["mcp_servers"]

    assert set(servers) == {"genesis-health", "genesis-memory"}
    assert servers["genesis-health"]["enabled_tools"] == [
        "health_status",
        "health_errors",
        "health_alerts",
        "bootstrap_manifest",
        "subsystem_heartbeats",
        "job_health",
        "provider_activity",
    ]
    assert servers["genesis-memory"]["enabled_tools"] == [
        "memory_recall",
        "memory_expand",
        "memory_core_facts",
        "memory_stats",
        "knowledge_recall",
        "knowledge_status",
        "procedure_recall",
        "observation_query",
    ]
    for server in servers.values():
        assert server["command"] == "python3"
        assert server["args"][0] == "scripts/codex_external_mcp.py"
        assert server["cwd"] == "."
        assert server["startup_timeout_sec"] == 30
        assert server["default_tools_approval_mode"] == "approve"


async def test_project_allowlists_only_registered_genesis_tools() -> None:
    """A renamed or removed Genesis tool must make this integration fail closed."""
    from genesis.mcp.health_mcp import mcp as health_mcp
    from genesis.mcp.memory_mcp import mcp as memory_mcp

    root = Path(__file__).resolve().parents[2]
    servers = tomllib.loads((root / ".codex" / "config.toml").read_text())["mcp_servers"]
    registered = {
        "genesis-health": set(await health_mcp.get_tools()),
        "genesis-memory": set(await memory_mcp.get_tools()),
    }

    for server_name, server in servers.items():
        assert set(server["enabled_tools"]) <= registered[server_name]
