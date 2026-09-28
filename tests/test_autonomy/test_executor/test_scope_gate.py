"""Tests for the build-lane diff allowlist gate (scope_gate + engine graft)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor import worktree_mgr
from genesis.autonomy.executor.engine import CCSessionExecutor
from genesis.autonomy.executor.scope_gate import (
    ScopeGateResult,
    evaluate_raw_diff,
    evaluate_scope,
)
from genesis.db.crud import task_states
from genesis.db.crud.task_states import create_intake_token


def _raw(path: str, new_mode: str = "100644") -> str:
    """One `git diff --raw` line for *path* (added file by default)."""
    return f":000000 {new_mode} 0000000 1111111 A\t{path}"

# ---------------------------------------------------------------------------
# Pure-function matrix
# ---------------------------------------------------------------------------


class TestEvaluateScope:
    @pytest.mark.parametrize("path", [
        "src/genesis/modules/star_velocity/collector.py",
        "src/genesis/skills/aws-fde-delivery/SKILL.md",
        "src/genesis/mcp/knowledge_tools.py",
        "tests/test_modules/test_star_velocity.py",
        "docs/reference/star-velocity.md",
    ])
    def test_allowed_capability_trees(self, path):
        result = evaluate_scope([path])
        assert result.allowed, result

    @pytest.mark.parametrize("path", [
        # Core subsystems — denied by absence from the allowlist.
        "src/genesis/autonomy/executor/scope_gate.py",   # the gate itself
        "src/genesis/autonomy/types.py",
        "src/genesis/inbox/monitor.py",
        "src/genesis/ego/proposals.py",
        "src/genesis/runtime/init/tasks.py",
        "src/genesis/db/schema/_tables.py",
        "src/genesis/contribution/pr_opener.py",
        "scripts/update.sh",
        ".claude/hooks/genesis-hook",
        ".github/workflows/ci.yml",
        "config/model_routing.yaml",
        "pyproject.toml",
        "CLAUDE.md",
    ])
    def test_core_paths_denied_by_absence(self, path):
        result = evaluate_scope([path])
        assert not result.allowed
        assert result.blocked_paths

    @pytest.mark.parametrize("path", [
        # Deny overrides WIN inside allowed trees.
        "src/genesis/modules/foo/.env",
        "src/genesis/modules/foo/prod.env",
        "src/genesis/skills/foo/secrets.yaml",
        "src/genesis/modules/foo/genesis-foo.service",
        "src/genesis/modules/foo/genesis-foo.timer",
        "src/genesis/mcp/settings.local.json",
        "src/genesis/mcp/health/task_tools.py",          # its own submission door
        "src/genesis/mcp/health/approval_tools.py",
        "tests/test_autonomy/test_approval.py",          # approval surface, even tests
        "docs/journey/2026-07-07.md",
        "docs/reflections/on-building.md",
        # Admin/cognitive-control MCP surface — whole subtree denied.
        "src/genesis/mcp/health/settings.py",
        "src/genesis/mcp/health/session_control.py",
        "src/genesis/mcp/health/ego_tools.py",
        # Case variants — fnmatch is case-sensitive on POSIX; the gate
        # must not be (review finding, 2026-07-07).
        "src/genesis/modules/foo/SECRETS.ENV",
        "src/genesis/modules/foo/Prod.ENV",
        "src/genesis/modules/foo/Settings.JSON",
        "src/genesis/modules/foo/genesis.SERVICE",
        "SRC/GENESIS/MODULES/foo/secrets.yaml",          # denied either way
        # Dotenv family — *.env alone missed these (review finding).
        "src/genesis/modules/foo/.env.local",
        "src/genesis/modules/foo/.env.production",
        "src/genesis/modules/foo/config.env.example",
        "src/genesis/modules/foo/client_secret.json",
    ])
    def test_deny_overrides_inside_allowed_trees(self, path):
        result = evaluate_scope([path])
        assert not result.allowed, path

    def test_new_mcp_module_outside_health_is_allowed(self):
        # The health/ denial must not wall off the whole mcp/ tree.
        assert evaluate_scope(["src/genesis/mcp/star_velocity_tools.py"]).allowed

    @pytest.mark.parametrize("path", [
        "/etc/passwd",
        "../outside/file.py",
        "src/genesis/modules/../../genesis/autonomy/types.py",
    ])
    def test_malformed_paths_blocked_not_normalized(self, path):
        result = evaluate_scope([path])
        assert not result.allowed
        assert "malformed" in result.blocked_paths[0]

    def test_empty_diff_blocked(self):
        result = evaluate_scope([])
        assert not result.allowed
        assert "empty diff" in result.reason

    def test_whitespace_only_entries_are_empty(self):
        assert not evaluate_scope(["", "  ", "\n"]).allowed

    def test_one_bad_path_blocks_the_whole_diff(self):
        result = evaluate_scope([
            "src/genesis/skills/foo/SKILL.md",
            "src/genesis/autonomy/dispatcher.py",
        ])
        assert not result.allowed
        assert result.checked_paths == 2
        assert len(result.blocked_paths) == 1
        assert "dispatcher.py" in result.blocked_paths[0]

    def test_dot_slash_prefix_normalized(self):
        assert evaluate_scope(["./docs/reference/x.md"]).allowed

    def test_to_json_round_trips(self):
        result = evaluate_scope(["src/genesis/inbox/monitor.py"])
        data = json.loads(result.to_json())
        assert data["allowed"] is False
        assert data["checked_paths"] == 1
        assert data["blocked_paths"]


class TestEvaluateRawDiff:
    def test_allowed_regular_files(self):
        result = evaluate_raw_diff([
            _raw("src/genesis/skills/foo/SKILL.md"),
            _raw("tests/test_learning/test_foo.py", "100755"),
        ])
        assert result.allowed
        assert result.checked_paths == 2

    def test_symlink_blocked_even_in_allowed_tree(self):
        result = evaluate_raw_diff([
            _raw("src/genesis/modules/foo/link.py", "120000"),
        ])
        assert not result.allowed
        assert "symlink" in result.reason
        assert "link.py" in result.blocked_paths[0]

    def test_symlink_plus_out_of_scope_reports_both(self):
        result = evaluate_raw_diff([
            _raw("src/genesis/modules/foo/link.py", "120000"),
            _raw("src/genesis/autonomy/types.py"),
        ])
        assert not result.allowed
        joined = " ".join(result.blocked_paths)
        assert "symlink" in joined and "types.py" in joined

    def test_malformed_raw_line_blocks(self):
        # A line without the tab separator can't be trusted — it becomes an
        # unparseable "path" that the allowlist rejects.
        result = evaluate_raw_diff([":100644 100644 abc def M"])
        assert not result.allowed

    def test_non_colon_line_treated_as_path(self):
        # Unexpected shapes are judged as paths, never silently skipped.
        assert not evaluate_raw_diff(["some noise"]).allowed
        assert evaluate_raw_diff(["docs/reference/x.md"]).allowed

    def test_empty_raw_diff_blocked(self):
        assert not evaluate_raw_diff([]).allowed


# ---------------------------------------------------------------------------
# Engine graft — _deliver gating (fail-closed at every branch)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._out = (stdout, stderr)

    async def communicate(self):
        return self._out


def _subprocess_script(monkeypatch, procs: list[_FakeProc]) -> list[tuple]:
    """Patch create_subprocess_exec to replay *procs*; capture call args."""
    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return procs[len(calls) - 1]

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    return calls


def _engine(db) -> CCSessionExecutor:
    return CCSessionExecutor(
        db=db, invoker=AsyncMock(), decomposer=AsyncMock(), reviewer=AsyncMock(),
    )


async def _seed(db, task_id="t-sg1"):
    token = await create_intake_token(db)
    await task_states.create(
        db, task_id=task_id, description="build", intake_token=token,
    )


_CODE_STEPS = [{"idx": 0, "type": "code", "description": "x"}]


@pytest.mark.asyncio
class TestDeliverScopeGate:
    async def test_user_tasks_bypass_gate_entirely(self, db, tmp_path, monkeypatch):
        """Non-build-lane delivery is byte-identical: first git call is the push."""
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-sg1"] = tmp_path
        calls = _subprocess_script(monkeypatch, [_FakeProc(0)])

        await engine._deliver("t-sg1", "d", _CODE_STEPS, [])

        assert len(calls) == 1
        assert calls[0][:2] == ("git", "push")

    async def test_build_lane_allowed_diff_pushes(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-sg1"] = tmp_path
        engine._open_build_pr = AsyncMock()  # PR-open path covered in test_pr_open
        monkeypatch.setattr(
            engine, "_task_source", AsyncMock(return_value="build_lane"),
        )
        calls = _subprocess_script(monkeypatch, [
            _FakeProc(0, b""),                                  # status --porcelain
            _FakeProc(0, b"abc123\n"),                          # merge-base
            _FakeProc(0, _raw("src/genesis/skills/foo/SKILL.md").encode() + b"\n"),
            _FakeProc(0),                                        # push
        ])

        await engine._deliver("t-sg1", "d", _CODE_STEPS, [])

        assert calls[-1][:2] == ("git", "push")
        task = await task_states.get_by_id(db, "t-sg1")
        outputs = json.loads(task["outputs"])
        assert json.loads(outputs["scope_gate"])["allowed"] is True
        assert outputs["branch"] == "task/t-sg1"

    async def test_build_lane_blocked_diff_never_pushes(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-sg1"] = tmp_path
        monkeypatch.setattr(
            engine, "_task_source", AsyncMock(return_value="build_lane"),
        )
        calls = _subprocess_script(monkeypatch, [
            _FakeProc(0, b""),
            _FakeProc(0, b"abc123\n"),
            _FakeProc(0, _raw("src/genesis/autonomy/approval_gate.py").encode() + b"\n"),
        ])

        await engine._deliver("t-sg1", "d", _CODE_STEPS, [])

        assert all(c[:2] != ("git", "push") for c in calls)
        task = await task_states.get_by_id(db, "t-sg1")
        outputs = json.loads(task["outputs"])
        assert json.loads(outputs["scope_gate"])["allowed"] is False
        assert "scope_blocked" in outputs

    async def test_dirty_worktree_blocks(self, db, tmp_path, monkeypatch):
        """Uncommitted work would be invisible to the diff — fail, don't push."""
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-sg1"] = tmp_path
        monkeypatch.setattr(
            engine, "_task_source", AsyncMock(return_value="build_lane"),
        )
        calls = _subprocess_script(monkeypatch, [
            _FakeProc(0, b" M src/genesis/skills/foo/SKILL.md\n"),
        ])

        await engine._deliver("t-sg1", "d", _CODE_STEPS, [])

        assert all(c[:2] != ("git", "push") for c in calls)
        task = await task_states.get_by_id(db, "t-sg1")
        assert "dirty worktree" in json.loads(task["outputs"])["scope_blocked"]

    async def test_git_failure_fails_closed(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-sg1"] = tmp_path
        monkeypatch.setattr(
            engine, "_task_source", AsyncMock(return_value="build_lane"),
        )
        calls = _subprocess_script(monkeypatch, [
            _FakeProc(0, b""),
            _FakeProc(128, b"", b"fatal: no merge base"),
        ])

        await engine._deliver("t-sg1", "d", _CODE_STEPS, [])

        assert all(c[:2] != ("git", "push") for c in calls)
        task = await task_states.get_by_id(db, "t-sg1")
        assert "merge-base failed" in json.loads(task["outputs"])["scope_blocked"]

    async def test_merge_base_is_taken_against_the_recorded_cut_commit(
        self, db, tmp_path, monkeypatch,
    ):
        """The diff base is the commit the task was cut from, recorded at the
        cut: not local `main` (which can lag the remote), and not the ref
        `origin/main` (which the task's own worktree can move)."""
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-sg1"] = tmp_path
        engine._open_build_pr = AsyncMock()
        await engine._set_output("t-sg1", "base_branch", "main")
        await engine._set_output("t-sg1", "base_sha", "c0ffee" * 7)
        monkeypatch.setattr(
            engine, "_task_source", AsyncMock(return_value="build_lane"),
        )
        calls = _subprocess_script(monkeypatch, [
            _FakeProc(0, b""),
            _FakeProc(0, b"abc123\n"),
            _FakeProc(0, _raw("src/genesis/skills/foo/SKILL.md").encode() + b"\n"),
            _FakeProc(0),
        ])

        await engine._deliver("t-sg1", "d", _CODE_STEPS, [])

        assert calls[1] == ("git", "merge-base", "HEAD", "c0ffee" * 7)
        assert calls[-1][:2] == ("git", "push")

    async def test_without_a_recorded_base_the_gate_compares_to_local_main(
        self, db, tmp_path, monkeypatch,
    ):
        """No recorded base (origin/HEAD unresolved, or a task from before this):
        exactly the previous comparison, one merge-base call, no retry."""
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-sg1"] = tmp_path
        engine._open_build_pr = AsyncMock()
        monkeypatch.setattr(
            engine, "_task_source", AsyncMock(return_value="build_lane"),
        )
        calls = _subprocess_script(monkeypatch, [
            _FakeProc(0, b""),
            _FakeProc(0, b"abc123\n"),
            _FakeProc(0, _raw("src/genesis/skills/foo/SKILL.md").encode() + b"\n"),
            _FakeProc(0),
        ])

        await engine._deliver("t-sg1", "d", _CODE_STEPS, [])

        assert calls[1] == ("git", "merge-base", "HEAD", "main")
        assert calls[-1][:2] == ("git", "push")

    async def test_blocked_gate_notifies_user(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-sg1"] = tmp_path
        engine._notify = AsyncMock()
        monkeypatch.setattr(
            engine, "_task_source", AsyncMock(return_value="build_lane"),
        )
        _subprocess_script(monkeypatch, [
            _FakeProc(0, b""),
            _FakeProc(0, b"abc\n"),
            _FakeProc(0, _raw("scripts/update.sh").encode() + b"\n"),
        ])

        await engine._deliver("t-sg1", "d", _CODE_STEPS, [])

        engine._notify.assert_awaited_once()
        assert "scope gate" in engine._notify.await_args.args[1]


@pytest.mark.asyncio
class TestTaskSource:
    async def test_row_without_source_column_degrades_to_user(self, db):
        """Pre-0048 DBs have no source column — delivery must treat rows
        as 'user' (gate inactive), never crash."""
        await _seed(db, task_id="t-src1")
        engine = _engine(db)
        assert await engine._task_source("t-src1") == "user"

    async def test_missing_row_is_user(self, db):
        engine = _engine(db)
        assert await engine._task_source("t-nope") == "user"

    async def test_db_error_is_user(self, db):
        engine = _engine(db)
        engine._db = None  # forces an exception inside get_by_id
        assert await engine._task_source("t-x") == "user"


@pytest.mark.asyncio
async def test_scope_gate_exception_fails_closed(db, tmp_path, monkeypatch):
    """Any unexpected error inside the gate parks the build."""
    await _seed(db, task_id="t-sg9")
    engine = _engine(db)
    engine._worktree_paths["t-sg9"] = tmp_path
    monkeypatch.setattr(
        engine, "_task_source", AsyncMock(return_value="build_lane"),
    )

    async def boom(*args, **kwargs):
        raise OSError("git binary vanished")

    monkeypatch.setattr("asyncio.create_subprocess_exec", boom)

    await engine._deliver("t-sg9", "d", _CODE_STEPS, [])

    task = await task_states.get_by_id(db, "t-sg9")
    outputs = json.loads(task["outputs"])
    assert json.loads(outputs["scope_gate"])["allowed"] is False
    assert "scope gate error" in outputs["scope_blocked"]


def test_scope_gate_result_import_shape():
    """The engine imports ScopeGateResult lazily — keep the contract pinned."""
    r = ScopeGateResult(allowed=False, reason="x")
    assert r.blocked_paths == [] and r.checked_paths == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("recorded", "expected_base"), [("develop", "develop"), (None, "main")])
async def test_pr_targets_the_branch_the_gate_diffed_against(
    db, tmp_path, monkeypatch, recorded, expected_base,
):
    """The PR base and the scope gate's diff base come from the same record, so
    the gate evaluates exactly what the PR carries."""
    from genesis.autonomy.executor import pr_open

    await _seed(db)
    engine = _engine(db)
    if recorded:
        await engine._set_output("t-sg1", "base_branch", recorded)
        await engine._set_output("t-sg1", "base_sha", "c0ffee" * 7)
    captured: dict = {}

    async def fake_open(**kwargs):
        captured.update(kwargs)
        return pr_open.PrOpenResult(ok=True, pr_url="https://example.invalid/pr/1")

    monkeypatch.setattr(pr_open, "open_draft_pr", fake_open)
    await engine._open_build_pr("t-sg1", tmp_path, "task/t-sg1")
    assert captured["base"] == expected_base


# ── The engine passes the resolved base through, and records it ─────────


@pytest.mark.asyncio
class TestBaseWiring:
    """End-to-end tests above run with a subprocess mock that makes origin/HEAD
    unresolved, so they only ever exercise the fallback. These pin the resolved
    path: the base reaches create_worktree and is persisted, on creation and on
    recovery, and an unresolved re-resolution clears a stale record."""

    _BASE = worktree_mgr.BaseRef(name="main", sha="c0ffee" * 7)

    def _spy(self, monkeypatch, base):
        seen: list[dict] = []

        async def fake_create(task_id, repo_root, worktree_base, base=None):
            seen.append({"base": base})
            return worktree_base / f"task-{task_id[:8]}"

        monkeypatch.setattr(worktree_mgr, "resolve_base", AsyncMock(return_value=base))
        monkeypatch.setattr(worktree_mgr, "create_worktree", fake_create)
        return seen

    async def _outputs(self, db) -> dict:
        task = await task_states.get_by_id(db, "t-sg1")
        return json.loads(task["outputs"])

    async def test_create_passes_and_records_the_base(self, db, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        seen = self._spy(monkeypatch, self._BASE)
        await engine._create_worktree("t-sg1")
        assert seen == [{"base": self._BASE}]
        out = await self._outputs(db)
        assert (out["base_branch"], out["base_sha"]) == ("main", self._BASE.sha)
        engine._bases.clear()  # a restart: read back from the outputs
        assert await engine._base_for("t-sg1") == self._BASE

    async def test_recovery_recut_passes_and_records_the_base(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        seen = self._spy(monkeypatch, self._BASE)
        monkeypatch.setattr(worktree_mgr, "verify_worktree", AsyncMock(return_value=False))
        await engine._set_output("t-sg1", "worktree_path", str(tmp_path / "gone"))
        task = await task_states.get_by_id(db, "t-sg1")
        assert await engine._recover_worktree("t-sg1", task) is True
        assert seen == [{"base": self._BASE}]
        assert (await self._outputs(db))["base_sha"] == self._BASE.sha

    async def test_an_unresolved_recut_clears_a_stale_record(self, db, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        await engine._set_output("t-sg1", "base_branch", "main")
        await engine._set_output("t-sg1", "base_sha", "c0ffee" * 7)
        seen = self._spy(monkeypatch, None)
        await engine._create_worktree("t-sg1")
        assert seen == [{"base": None}]
        engine._bases.clear()
        assert await engine._base_for("t-sg1") is None

    async def test_a_name_without_a_commit_is_not_a_base(self, db):
        await _seed(db)
        engine = _engine(db)
        await engine._set_output("t-sg1", "base_branch", "main")
        assert await engine._base_for("t-sg1") is None
