"""Tests for server-side draft PR opening (pr_open + engine delivery hook)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor import pr_open
from genesis.autonomy.executor.engine import CCSessionExecutor
from genesis.autonomy.executor.pr_open import (
    PrOpenResult,
    build_pr_body,
    build_pr_title,
    open_draft_pr,
)
from genesis.db.crud import task_states
from genesis.db.crud.task_states import create_intake_token

# ---------------------------------------------------------------------------
# pr_open unit tests (subprocess-mocked)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._out = (stdout, stderr)

    async def communicate(self):
        return self._out

    def kill(self):
        pass


def _script(monkeypatch, procs: list[_FakeProc]) -> list[tuple]:
    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return procs[len(calls) - 1]

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(pr_open.shutil, "which", lambda _: "/usr/bin/gh")
    return calls


_KW = dict(branch="task/t-x", title="t", body="b")


@pytest.mark.asyncio
class TestOpenDraftPr:
    async def test_gh_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pr_open.shutil, "which", lambda _: None)
        result = await open_draft_pr(worktree_path=tmp_path, **_KW)
        assert not result.ok
        assert "not available" in result.error

    async def test_auth_failure(self, tmp_path, monkeypatch):
        _script(monkeypatch, [_FakeProc(1, b"", b"not logged in")])
        result = await open_draft_pr(worktree_path=tmp_path, **_KW)
        assert not result.ok
        assert "not authenticated" in result.error

    async def test_repo_resolution_failure(self, tmp_path, monkeypatch):
        _script(monkeypatch, [
            _FakeProc(0),
            _FakeProc(1, b"", b"no remote"),
        ])
        result = await open_draft_pr(worktree_path=tmp_path, **_KW)
        assert not result.ok
        assert "target repo" in result.error

    async def test_dry_run_stops_before_create(self, tmp_path, monkeypatch):
        calls = _script(monkeypatch, [
            _FakeProc(0),
            _FakeProc(0, b"owner/repo\n"),
        ])
        result = await open_draft_pr(worktree_path=tmp_path, dry_run=True, **_KW)
        assert result.ok
        assert "--draft" in result.dry_run_cmd
        assert len(calls) == 2  # auth + repo view only, no pr create

    async def test_success_parses_url(self, tmp_path, monkeypatch):
        calls = _script(monkeypatch, [
            _FakeProc(0),
            _FakeProc(0, b"owner/repo\n"),
            _FakeProc(0, b"Creating draft pull request...\nhttps://github.com/owner/repo/pull/99\n"),
        ])
        result = await open_draft_pr(worktree_path=tmp_path, **_KW)
        assert result.ok
        assert result.pr_url.endswith("/pull/99")
        create = calls[2]
        assert create[:3] == ("gh", "pr", "create")
        assert "--draft" in create
        head_idx = create.index("--head")
        assert create[head_idx:head_idx + 2] == ("--head", "task/t-x")

    async def test_create_failure(self, tmp_path, monkeypatch):
        _script(monkeypatch, [
            _FakeProc(0),
            _FakeProc(0, b"owner/repo\n"),
            _FakeProc(1, b"", b"GraphQL: something exploded"),
        ])
        result = await open_draft_pr(worktree_path=tmp_path, **_KW)
        assert not result.ok
        assert "gh pr create failed" in result.error

    async def test_success_without_url_is_error(self, tmp_path, monkeypatch):
        _script(monkeypatch, [
            _FakeProc(0),
            _FakeProc(0, b"owner/repo\n"),
            _FakeProc(0, b"done but no url\n"),
        ])
        result = await open_draft_pr(worktree_path=tmp_path, **_KW)
        assert not result.ok
        assert "no URL" in result.error


class TestTitleBody:
    def test_title_truncates(self):
        assert build_pr_title("x" * 200).endswith("...")
        assert len(build_pr_title("x" * 200)) <= 85

    def test_title_collapses_whitespace(self):
        assert build_pr_title("a\n b\t c") == "[build-lane] a b c"

    def test_body_carries_provenance(self):
        body = build_pr_body(
            task_id="t-1", plan_path="/p.md", scope_gate_json='{"allowed": true}',
        )
        assert "t-1" in body and "/p.md" in body and "Draft by design" in body


# ---------------------------------------------------------------------------
# Engine delivery hook
# ---------------------------------------------------------------------------


def _engine(db) -> CCSessionExecutor:
    return CCSessionExecutor(
        db=db, invoker=AsyncMock(), decomposer=AsyncMock(), reviewer=AsyncMock(),
    )


async def _seed(db, task_id="t-pr1"):
    token = await create_intake_token(db)
    await task_states.create(
        db, task_id=task_id, description="Build the thing", intake_token=token,
    )


_CODE_STEPS = [{"idx": 0, "type": "code", "description": "x"}]


def _subprocess_script(monkeypatch, procs: list[_FakeProc]) -> list[tuple]:
    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return procs[len(calls) - 1]

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    return calls


@pytest.mark.asyncio
class TestDeliverOpensDraftPr:
    async def test_build_lane_push_triggers_pr_open(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-pr1"] = tmp_path
        engine._open_build_pr = AsyncMock()
        monkeypatch.setattr(
            engine, "_task_source", AsyncMock(return_value="build_lane"),
        )
        _subprocess_script(monkeypatch, [
            _FakeProc(0, b""),                                   # status
            _FakeProc(0, b"abc\n"),                              # merge-base
            _FakeProc(0, b":000000 100644 0000000 1111111 A\tsrc/genesis/skills/foo/SKILL.md\n"),
            _FakeProc(0),                                        # push
        ])

        await engine._deliver("t-pr1", "d", _CODE_STEPS, [])

        engine._open_build_pr.assert_awaited_once_with(
            "t-pr1", tmp_path, "task/t-pr1",
        )

    async def test_user_task_never_opens_pr(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-pr1"] = tmp_path
        engine._open_build_pr = AsyncMock()
        _subprocess_script(monkeypatch, [_FakeProc(0)])  # push only

        await engine._deliver("t-pr1", "d", _CODE_STEPS, [])

        engine._open_build_pr.assert_not_awaited()

    async def test_failed_push_never_opens_pr(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        engine._worktree_paths["t-pr1"] = tmp_path
        engine._open_build_pr = AsyncMock()
        monkeypatch.setattr(
            engine, "_task_source", AsyncMock(return_value="build_lane"),
        )
        _subprocess_script(monkeypatch, [
            _FakeProc(0, b""),
            _FakeProc(0, b"abc\n"),
            _FakeProc(0, b":000000 100644 0000000 1111111 A\tsrc/genesis/skills/foo/SKILL.md\n"),
            _FakeProc(1, b"", b"remote rejected"),               # push fails
        ])

        await engine._deliver("t-pr1", "d", _CODE_STEPS, [])

        engine._open_build_pr.assert_not_awaited()


@pytest.mark.asyncio
class TestOpenBuildPr:
    async def test_success_records_url_and_notifies(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        engine._notify = AsyncMock()
        monkeypatch.setattr(
            pr_open, "open_draft_pr",
            AsyncMock(return_value=PrOpenResult(ok=True, pr_url="https://x/pull/1")),
        )

        await engine._open_build_pr("t-pr1", Path(tmp_path), "task/t-pr1")

        task = await task_states.get_by_id(db, "t-pr1")
        outputs = json.loads(task["outputs"])
        assert outputs["pr_url"] == "https://x/pull/1"
        assert "draft PR" in engine._notify.await_args.args[1]

    async def test_failure_records_error_and_notifies_manual(
        self, db, tmp_path, monkeypatch,
    ):
        await _seed(db)
        engine = _engine(db)
        engine._notify = AsyncMock()
        monkeypatch.setattr(
            pr_open, "open_draft_pr",
            AsyncMock(return_value=PrOpenResult(ok=False, error="gh exploded")),
        )

        await engine._open_build_pr("t-pr1", Path(tmp_path), "task/t-pr1")

        task = await task_states.get_by_id(db, "t-pr1")
        outputs = json.loads(task["outputs"])
        assert outputs["pr_error"] == "gh exploded"
        assert "manually" in engine._notify.await_args.args[1]

    async def test_crash_is_nonfatal(self, db, tmp_path, monkeypatch):
        await _seed(db)
        engine = _engine(db)
        engine._notify = AsyncMock()
        monkeypatch.setattr(
            pr_open, "open_draft_pr", AsyncMock(side_effect=OSError("boom")),
        )

        await engine._open_build_pr("t-pr1", Path(tmp_path), "task/t-pr1")

        task = await task_states.get_by_id(db, "t-pr1")
        assert "pr_open crashed" in json.loads(task["outputs"])["pr_error"]
