"""Tests for the bench arm builders + the memory-tool forcing function."""

from __future__ import annotations

import ast
import os
import pathlib

import pytest

from genesis.cc.types import CCModel, EffortLevel
from genesis.eval.bench.arms import (
    BENCH_MEMORY_DISALLOW,
    BENCH_MEMORY_READONLY_ALLOWED,
    BENCH_MEMORY_WRITE_DISALLOWED,
    TASK_ENVELOPE,
    build_bare_arm_invocation,
    build_genesis_arm_invocation,
    prepare_bare_config_dir,
    scrub_nested_cc_env,
)
from genesis.eval.bench.types import BenchTask

_MEMORY_MCP_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "genesis" / "mcp" / "memory"
)


def _registered_memory_tools() -> set[str]:
    """Static-AST enumeration of every @mcp.tool()-decorated function in the
    genesis-memory server (pattern: test_recall_inject_coverage.py)."""
    tools: set[str] = set()
    for path in sorted(_MEMORY_MCP_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                func = dec.func if isinstance(dec, ast.Call) else dec
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "tool"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "mcp"
                ):
                    tools.add(node.name)
    return tools


class TestMemoryToolForcingFunction:
    """Every registered memory tool must be consciously classified.

    A new genesis-memory tool added without deciding whether the bench arm
    may use it FAILS here — read tools default to nothing; classify it into
    BENCH_MEMORY_READONLY_ALLOWED or BENCH_MEMORY_WRITE_DISALLOWED in
    eval/bench/arms.py (and if it writes Qdrant inside a read path, guard it
    with env.memory_writebacks_off like retrieval stage 11).
    """

    def test_enumeration_finds_the_server(self):
        tools = _registered_memory_tools()
        assert "memory_recall" in tools  # sanity: enumeration works
        assert len(tools) >= 25

    def test_every_tool_classified_exactly_once(self):
        tools = _registered_memory_tools()
        classified = BENCH_MEMORY_READONLY_ALLOWED | BENCH_MEMORY_WRITE_DISALLOWED
        unclassified = tools - classified
        assert not unclassified, (
            f"unclassified genesis-memory tool(s): {sorted(unclassified)} — "
            "classify in eval/bench/arms.py (see this test's docstring)"
        )
        stale = classified - tools
        assert not stale, (
            f"classified but no longer registered: {sorted(stale)} — "
            "remove from eval/bench/arms.py"
        )

    def test_sets_disjoint(self):
        overlap = BENCH_MEMORY_READONLY_ALLOWED & BENCH_MEMORY_WRITE_DISALLOWED
        assert not overlap

    def test_disallow_list_format(self):
        assert [
            f"mcp__genesis-memory__{n}"
            for n in sorted(BENCH_MEMORY_WRITE_DISALLOWED)
        ] == BENCH_MEMORY_DISALLOW


def _task(**overrides) -> BenchTask:
    kwargs = dict(
        id="t1", category="recall", prompt="Q?", expected="A.", timeout_s=900,
    )
    kwargs.update(overrides)
    return BenchTask(**kwargs)


class TestBareArm:
    def test_recipe(self, tmp_path):
        cfg = tmp_path / "bare-claude-config"
        cfg.mkdir()
        inv = build_bare_arm_invocation(
            _task(), tmp_path, CCModel.SONNET, EffortLevel.MEDIUM, cfg, "run1",
        )
        assert inv.safe_mode is True
        assert inv.bare is False  # --bare refuses OAuth; never use it
        assert inv.strict_mcp_config is True
        assert inv.mcp_config.endswith("no_mcp.json")
        assert inv.env_overrides == {"CLAUDE_CONFIG_DIR": str(cfg)}
        assert inv.system_prompt is None
        assert inv.disallowed_tools is None  # built-ins fully enabled
        assert inv.timeout_s == 900
        assert inv.skip_permissions is True
        assert inv.prompt.endswith(TASK_ENVELOPE)
        # Neutral per-task cwd, outside any repo, exists.
        assert inv.working_dir == str(tmp_path / "work" / "t1" / "bare")
        assert os.path.isdir(inv.working_dir)

    def test_config_dir_contains_only_credentials_symlink(self, tmp_path, monkeypatch):
        real_creds = tmp_path / "home" / ".claude" / ".credentials.json"
        real_creds.parent.mkdir(parents=True)
        real_creds.write_text("{}")
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path / "home")

        cfg = prepare_bare_config_dir(tmp_path / "run")
        entries = list(cfg.iterdir())
        assert [e.name for e in entries] == [".credentials.json"]
        assert entries[0].is_symlink()
        assert entries[0].resolve() == real_creds.resolve()
        # Idempotent (re-runs don't fail on the existing symlink).
        assert prepare_bare_config_dir(tmp_path / "run") == cfg

    def test_missing_credentials_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path / "nohome")
        with pytest.raises(RuntimeError, match="cannot authenticate"):
            prepare_bare_config_dir(tmp_path / "run")


class TestGenesisArm:
    def test_recipe(self, tmp_path):
        mcp_cfg = tmp_path / "bench_mcp.json"
        inv = build_genesis_arm_invocation(
            _task(), tmp_path, CCModel.SONNET, EffortLevel.MEDIUM, mcp_cfg, "run1",
        )
        assert inv.safe_mode is False  # user CLAUDE.md = production-faithful
        assert inv.strict_mcp_config is True
        assert inv.mcp_config == str(mcp_cfg)
        assert inv.disallowed_tools == BENCH_MEMORY_DISALLOW
        assert inv.system_prompt and "READ-ONLY" in inv.system_prompt
        assert inv.prompt.endswith(TASK_ENVELOPE)
        assert inv.working_dir == str(tmp_path / "work" / "t1" / "genesis")

    def test_fairness_parity(self, tmp_path):
        """Same model, effort, prompt, timeout, and sandbox across arms."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        task = _task()
        bare = build_bare_arm_invocation(
            task, tmp_path, CCModel.OPUS, EffortLevel.HIGH, cfg, "r",
        )
        genesis = build_genesis_arm_invocation(
            task, tmp_path, CCModel.OPUS, EffortLevel.HIGH, tmp_path / "m.json", "r",
        )
        assert bare.model == genesis.model
        assert bare.effort == genesis.effort
        assert bare.prompt == genesis.prompt
        assert bare.timeout_s == genesis.timeout_s
        assert bare.claude_code_tmpdir == genesis.claude_code_tmpdir


def test_scrub_nested_cc_env(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("CLAUDE_EFFORT", "high")
    monkeypatch.setenv("UNRELATED_VAR", "keep")
    removed = scrub_nested_cc_env()
    assert set(removed) >= {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT"}
    assert "CLAUDECODE" not in os.environ
    assert os.environ["UNRELATED_VAR"] == "keep"
