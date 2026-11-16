"""Tests for the in-flight working-state session-start block (open_loops)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from genesis.db.crud import task_states
from genesis.memory import open_loops
from genesis.memory.open_loops import build_inflight_block


async def _insert_task(db, task_id, description, phase="observing"):
    # task_states has an intake-token trigger — go through the crud path, not
    # a raw INSERT (see CLAUDE.md: never insert directly into task_states).
    token = await task_states.create_intake_token(db)
    await task_states.create(
        db,
        task_id=task_id,
        description=description,
        current_phase=phase,
        intake_token=token,
    )


@pytest.fixture
def empty_dirs(tmp_path):
    """A non-git repo_root + empty plans_dir → worktree/plan sections empty."""
    repo = tmp_path / "repo"
    plans = tmp_path / "plans"
    repo.mkdir()
    plans.mkdir()
    return repo, plans


async def test_all_empty_returns_empty_string(empty_db, empty_dirs):
    repo, plans = empty_dirs
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert block == ""


async def test_block_has_directive_header_and_no_divider(empty_db, empty_dirs):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "task0001", "Fix the widget")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert block.startswith("### In-flight state (for your recollection, not a report)")
    assert "never open a session by summarizing it" in block
    # No markdown horizontal-rule divider — caller folds under Essential Knowledge.
    assert "\n---" not in block
    assert not block.startswith("---")


async def test_active_tasks_render(empty_db, empty_dirs):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "abcdef12", "Refactor the dispatcher", phase="planning")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "Active autonomy tasks" in block
    assert "abcdef12" in block
    assert "planning" in block
    assert "Refactor the dispatcher" in block


async def test_task_id_printed_whole(empty_db, empty_dirs):
    # The id is pasted back into task_detail / task_control, which match it
    # exactly — a prefix is a not-found, so the block must print it whole.
    repo, plans = empty_dirs
    await _insert_task(empty_db, "t-0123456789ab", "Long-id task")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "`t-0123456789ab`" in block


async def test_terminal_tasks_excluded(empty_db, empty_dirs):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "done0001", "Completed thing", phase="completed")
    await _insert_task(empty_db, "live0001", "Live thing", phase="observing")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "live0001" in block
    assert "done0001" not in block


async def test_task_cap_and_overflow(empty_db, empty_dirs):
    repo, plans = empty_dirs
    for i in range(7):
        await _insert_task(empty_db, f"task{i:04d}", f"Task number {i}")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert block.count("- `") == 5  # 5 task bullets; overflow marker has no backtick
    assert "(+2 more)" in block


async def test_long_description_truncated(empty_db, empty_dirs):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "long0001", "x" * 200)
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "..." in block
    for line in block.splitlines():
        if line.startswith("- `"):  # a task bullet
            assert len(line) < 140  # no runaway 200-char task line


async def test_recent_plans_render(empty_db, empty_dirs):
    repo, plans = empty_dirs
    (plans / "my-plan.md").write_text("# plan")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "Recent plans" in block
    assert "my-plan.md" in block


async def test_plan_cap(empty_db, empty_dirs):
    repo, plans = empty_dirs
    for i in range(5):
        (plans / f"plan-{i}.md").write_text("x")
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    plan_section = block.split("**Recent plans:**")[1]
    assert plan_section.count("- ") == 3


async def test_worktrees_render_via_monkeypatch(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    monkeypatch.setattr(open_loops, "_list_worktrees", lambda root: [
        {"path": str(repo / "wt-a"), "head": "1234567890abcdef", "branch": "feat/foo"},
        {"path": str(repo / "wt-b"), "head": "abcdef1234567890"},  # detached (no branch)
    ])
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "Live worktrees" in block
    assert "feat/foo @ 12345678" in block
    assert "wt-b @ abcdef12" in block  # detached → path basename


async def test_worktree_cap_and_overflow(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    many = [
        {"path": str(repo / f"wt{i}"), "head": f"{i:040d}", "branch": f"b{i}"}
        for i in range(12)
    ]
    monkeypatch.setattr(open_loops, "_list_worktrees", lambda root: many)
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "(+4 more)" in block  # 12 - 8 cap


async def test_worktrees_ordered_by_git_activity(empty_db, empty_dirs, tmp_path, monkeypatch):
    import os

    repo, plans = empty_dirs

    def make_wt(name, index_mtime, branch):
        # A fake linked worktree: .git file points at a private gitdir whose
        # index mtime is the activity signal we sort on.
        wt = tmp_path / name
        wt.mkdir()
        gitdir = tmp_path / "gd" / name
        gitdir.mkdir(parents=True)
        (gitdir / "index").write_text("x")
        os.utime(gitdir / "index", (index_mtime, index_mtime))
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")
        return {"path": str(wt), "head": "0" * 40, "branch": branch}

    old = make_wt("oldwt", 1_000.0, "feat/old")
    new = make_wt("newwt", 9_000_000_000.0, "feat/new")
    # Return in old-first order so a working sort must reorder them.
    monkeypatch.setattr(open_loops, "_list_worktrees", lambda root: [old, new])
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert block.index("feat/new") < block.index("feat/old")  # newer git activity first


async def test_one_section_raises_others_still_render(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    await _insert_task(empty_db, "task0001", "survivor task")

    def boom(root):
        raise RuntimeError("worktree listing blew up")

    monkeypatch.setattr(open_loops, "_worktree_lines", boom)
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert "survivor task" in block  # tasks survive the worktree failure
    assert "Live worktrees" not in block


async def test_truncation_hard_cap(empty_db, empty_dirs, monkeypatch):
    repo, plans = empty_dirs
    monkeypatch.setattr(
        open_loops, "_plan_lines", lambda d: ["- " + "y" * 100 for _ in range(60)]
    )
    block = await build_inflight_block(empty_db, repo_root=repo, plans_dir=plans)
    assert len(block) <= open_loops._MAX_CHARS + 20
    assert "…(truncated)" in block


_ANCHOR_REASON = "archived by the reaper -> wt-gone-20260101; recover with --recover"


def _git(cwd, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def test_list_worktrees_skips_anchors_and_missing_dirs(tmp_path):
    """Real git: only registrations with a directory that are not reaper
    archive anchors count as live. A worktree locked for another reason is live."""
    import shutil

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    for name in ("live", "held", "anchor", "stale"):
        _git(repo, "worktree", "add", "-q", "-b", f"feat/{name}", str(tmp_path / name))
    _git(repo, "worktree", "lock", "--reason", "closing queue: in use", str(tmp_path / "held"))
    _git(repo, "worktree", "lock", "--reason", _ANCHOR_REASON, str(tmp_path / "anchor"))
    shutil.rmtree(tmp_path / "anchor")  # the reaper archives the directory away
    shutil.rmtree(tmp_path / "stale")   # unlocked, directory gone: prunable

    branches = sorted(w.get("branch") for w in open_loops._list_worktrees(repo))
    assert branches == ["feat/held", "feat/live"]


def test_overflow_counts_only_live_worktrees(empty_dirs, tmp_path, monkeypatch):
    """The "+N more" count ignores directory-less registrations and anchors,
    including an anchor whose directory happens to exist."""
    import types

    repo, _plans = empty_dirs
    records = [f"worktree {repo}\nHEAD {'0' * 40}\nbranch refs/heads/main\n"]
    for i in range(10):
        d = tmp_path / f"live{i}"
        d.mkdir()
        records.append(f"worktree {d}\nHEAD {'1' * 40}\nbranch refs/heads/b{i}\n")
    for i in range(20):
        records.append(
            f"worktree {tmp_path / f'gone{i}'}\nHEAD {'2' * 40}\n"
            f"branch refs/heads/g{i}\nlocked {_ANCHOR_REASON}\n"
        )
    for i in range(3):
        d = tmp_path / f"anchored{i}"
        d.mkdir()
        records.append(
            f"worktree {d}\nHEAD {'4' * 40}\nbranch refs/heads/a{i}\nlocked {_ANCHOR_REASON}\n"
        )
    for i in range(5):
        records.append(
            f"worktree {tmp_path / f'stale{i}'}\nHEAD {'3' * 40}\n"
            f"branch refs/heads/s{i}\nprunable gitdir file points to non-existent location\n"
        )
    out = "\n".join(records)
    monkeypatch.setattr(
        open_loops.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=out, stderr=""),
    )
    lines = open_loops._worktree_lines(repo)
    assert lines[-1] == "- …(+2 more)"  # 10 live - 8 cap; anchors/stale not counted
    assert not any(line.startswith(("- g", "- s", "- a")) for line in lines)


@pytest.mark.parametrize(
    ("locked", "has_dir", "live"),
    [
        (None, True, True),
        ("", True, True),  # bare `locked` line, no reason
        ("closing queue: in use", True, True),  # locked for another reason
        (_ANCHOR_REASON, True, False),  # anchor whose directory still exists
        ('"' + _ANCHOR_REASON + '"', True, False),  # porcelain C-quoted reason
        (None, False, False),  # directory gone
    ],
)
def test_is_live_classifies_each_registration_shape(tmp_path, locked, has_dir, live):
    path = tmp_path / "wt"
    if has_dir:
        path.mkdir()
    wt = {"path": str(path)}
    if locked is not None:
        wt["locked"] = locked
    assert open_loops._is_live(wt) is live


def test_anchor_prefix_matches_the_reapers_writer():
    """The reader restates the writer's prefix (scripts/ is not importable);
    if the reaper ever rewords it, anchors with a directory would count as live."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "scripts" / "worktree_lifecycle.py"
    ).read_text()
    values = [
        node.value.value
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "ARCHIVE_LOCK_PREFIX" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    assert values == [open_loops._REAPER_ANCHOR_PREFIX]
    assert _ANCHOR_REASON.startswith(open_loops._REAPER_ANCHOR_PREFIX)


def test_real_git_archive_reasons_agree_with_reaper_parser(tmp_path):
    """Read real porcelain; the writer's full parser is the archive oracle."""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "scripts/worktree_lifecycle.py"
    tree = ast.parse(source.read_text())
    nodes = [
        node for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                getattr(target, "id", None) in {"ARCHIVE_LOCK_PREFIX", "ARCHIVE_LOCK_SUFFIXES"}
                for target in node.targets
            )
        ) or (
            isinstance(node, ast.FunctionDef)
            and node.name in {"_archive_lock_reason", "_archive_entry_from_reason"}
        )
    ]
    oracle = {}
    # Only the trusted writer's constants and two pure functions enter the oracle.
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), oracle)  # noqa: S102
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    live = tmp_path / "live"
    _git(repo, "worktree", "add", "-q", "-b", "topic", str(live))
    reasons = [
        "archived by the reaper -> manual hold",
        _ANCHOR_REASON + " (manual)",
        "closing queue: in use",
    ]
    for entry in ("valid", "a;b", "tab\tentry", "quote\"entry", "", ".", "..", "../other"):
        for failed in (False, True):
            reasons.append(oracle["_archive_lock_reason"](entry, recovery_failed=failed))
    for reason in reasons:
        _git(repo, "worktree", "lock", "--reason", reason, str(live))
        try:
            expected_live = oracle["_archive_entry_from_reason"](reason) is None
            actual_live = any(w["path"] == str(live) for w in open_loops._list_worktrees(repo))
            assert actual_live is expected_live, repr(reason)
        finally:
            _git(repo, "worktree", "unlock", str(live))


# ── Live sessions: count + other sessions' missions ──────────────────────────


def _ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


async def _session(db, sid, *, pid=None, minutes_ago=1, channel=None,
                   cc_session_id=None, mission=None, origin=None,
                   session_type="foreground", status="active"):
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, channel, model, status, pid, "
        "started_at, last_activity_at, cc_session_id, origin_class) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, session_type, channel, "m", status, pid, _ago(minutes_ago + 60),
         _ago(minutes_ago), cc_session_id or sid, origin),
    )
    if mission is not None:
        await db.execute(
            "INSERT INTO session_charters (session_id, mission, created_at, "
            "mission_updated_at) VALUES (?,?,?,?)",
            (sid, mission, _ago(minutes_ago), _ago(minutes_ago)),
        )
    await db.commit()


@pytest.fixture
def live(monkeypatch):
    """Pids in ``alive`` are live claude processes; ``own`` is this hook's."""
    state = {"alive": set(), "own": None}
    monkeypatch.setattr(open_loops, "_pid_dead", lambda pid, row: pid not in state["alive"])
    monkeypatch.setattr(open_loops, "_own_claude_pid", lambda: state["own"])
    return state


async def _block(db, dirs, own_session_id=""):
    repo, plans = dirs
    return await build_inflight_block(
        db, repo_root=repo, plans_dir=plans, own_session_id=own_session_id
    )


async def test_one_pid_with_several_rows_is_one_session_showing_its_newest(
    empty_db, empty_dirs, live
):
    """A /clear or resume keeps the pid and leaves the old row active (#4015)."""
    live["alive"] = {100}
    await _session(empty_db, "old", pid=100, minutes_ago=90, mission="Old cleared work.")
    await _session(empty_db, "new", pid=100, minutes_ago=1, mission="Current work here.")
    block = await _block(empty_db, empty_dirs)
    assert "**Live sessions:** 1 CC" in block
    assert "Current work here." in block
    assert "Old cleared work" not in block


async def test_dead_pid_is_not_counted(empty_db, empty_dirs, live):
    live["alive"] = {100}
    await _session(empty_db, "a", pid=100, mission="Alive mission.")
    await _session(empty_db, "b", pid=200, mission="Dead mission.")
    block = await _block(empty_db, empty_dirs)
    assert "**Live sessions:** 1 CC" in block
    assert "Dead mission" not in block


async def test_channel_row_counts_only_while_recent(empty_db, empty_dirs, live):
    await _session(empty_db, "t1", channel="telegram", cc_session_id="x1", minutes_ago=30)
    await _session(empty_db, "t2", channel="telegram", cc_session_id="x2", minutes_ago=300)
    block = await _block(empty_db, empty_dirs)
    assert "**Live sessions:** 0 CC + 1 telegram" in block


async def test_pidless_terminal_row_counts_as_cc_while_recent(empty_db, empty_dirs, live):
    """Adopted terminal rows carry no pid and no channel."""
    await _session(empty_db, "adopted", minutes_ago=10, mission="Adopted work.")
    await _session(empty_db, "stale", minutes_ago=600, mission="Stale work.")
    block = await _block(empty_db, empty_dirs)
    assert "**Live sessions:** 1 CC" in block
    assert "Adopted work." in block
    assert "Stale work" not in block


async def test_own_session_excluded_by_pid_even_before_its_row_exists(
    empty_db, empty_dirs, live
):
    """SessionStart hooks run in parallel: after a /clear this session's row may
    not exist yet, and the newest row on its pid is the conversation cleared."""
    live["alive"] = {100, 300}
    live["own"] = 100
    await _session(empty_db, "cleared", pid=100, mission="Just-cleared conversation.")
    await _session(empty_db, "peer", pid=300, mission="Peer mission.")
    block = await _block(empty_db, empty_dirs, own_session_id="brand-new-id")
    assert "**Live sessions:** 2 CC (incl. this session)" in block
    assert "Just-cleared" not in block
    assert "Peer mission." in block


async def test_own_session_excluded_by_id(empty_db, empty_dirs, live):
    await _session(empty_db, "me", minutes_ago=1, mission="My own mission.")
    block = await _block(empty_db, empty_dirs, own_session_id="me")
    assert "(incl. this session)" in block
    assert "My own mission" not in block


async def test_external_origin_mission_never_shown(empty_db, empty_dirs, live):
    live["alive"] = {100}
    await _session(empty_db, "v", pid=100, mission="EXTERNAL_MISSION_TEXT",
                   origin="external_untrusted")
    block = await _block(empty_db, empty_dirs)
    assert "**Live sessions:** 1 CC" in block
    assert "EXTERNAL_MISSION_TEXT" not in block


async def test_mission_clipped_at_a_word_boundary_on_one_line(empty_db, empty_dirs, live):
    live["alive"] = {100}
    words = "Build the health sweep queue one worktree per PR\nstarting with lock contention "
    await _session(empty_db, "a", pid=100, mission=words * 6)
    block = await _block(empty_db, empty_dirs)
    [line] = [ln for ln in block.splitlines() if ln.startswith("- Build")]
    mission = re.sub(r" \([^)]*\)$", "", line[2:])
    assert mission.endswith("…")
    assert len(mission) <= 140
    assert set(mission[:-1].split()) <= set(words.split())


async def test_session_without_mission_counted_but_no_line(empty_db, empty_dirs, live):
    live["alive"] = {100}
    await _session(empty_db, "a", pid=100)
    block = await _block(empty_db, empty_dirs)
    assert "**Live sessions:** 1 CC" in block
    assert "self-declared missions" not in block


async def test_background_and_completed_rows_ignored(empty_db, empty_dirs, live):
    live["alive"] = {100, 200}
    await _session(empty_db, "bg", pid=100, session_type="background_task", mission="Bg.")
    await _session(empty_db, "done", pid=200, status="completed", mission="Done.")
    assert await _block(empty_db, empty_dirs) == ""


async def test_sessions_section_failure_leaves_others(empty_db, empty_dirs, live, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(open_loops, "_session_lines", boom)
    await _insert_task(empty_db, "task0001", "Fix the widget")
    block = await _block(empty_db, empty_dirs)
    assert "Fix the widget" in block
    assert "Live sessions" not in block


def test_pid_dead_is_the_reapers_predicate(monkeypatch):
    """One predicate for the reaper and this block, both through
    proc_io.claude_pid_dead; this block never imports the reaper or anything
    under genesis.observability (about 300 ms at every session start)."""
    from genesis.cc import foreground_reaper
    from genesis.util import proc_io

    assert foreground_reaper.claude_pid_dead is proc_io.claude_pid_dead
    seen = []
    monkeypatch.setattr(proc_io, "claude_pid_dead", lambda pid, row: seen.append(pid) or True)
    assert open_loops._pid_dead(4242, {}) is True
    assert seen == [4242]
    src = Path(open_loops.__file__).read_text()
    assert "foreground_reaper" not in src
    assert "genesis.observability" not in src


async def test_voice_row_is_a_channel_not_cc(empty_db, empty_dirs, live):
    """Voice rows carry id == cc_session_id like terminal rows, with no pid."""
    await empty_db.execute(
        "INSERT INTO cc_sessions (id, session_type, channel, model, status, "
        "started_at, last_activity_at, cc_session_id, source_tag, origin_class) "
        "VALUES ('v1','foreground','voice_s2s','m','active',?,?,'v1','voice',"
        "'external_untrusted')",
        (_ago(70), _ago(10)),
    )
    await empty_db.commit()
    block = await _block(empty_db, empty_dirs)
    assert "**Live sessions:** 0 CC + 1 voice_s2s" in block


async def test_own_channel_session_counted_once(empty_db, empty_dirs, live):
    """A session hosted by a channel row is this session, not also a channel."""
    live["own"] = 100
    await _session(empty_db, "row", channel="telegram", cc_session_id="cc-1", minutes_ago=5)
    block = await _block(empty_db, empty_dirs, own_session_id="cc-1")
    assert "**Live sessions:** 1 CC (incl. this session)" in block
    assert "telegram" not in block


async def test_own_session_counted_before_its_row_exists(empty_db, empty_dirs, live):
    """Fresh start: this session's row is not registered yet, so nothing on
    its pid is visible; it is still one of the live sessions."""
    live["alive"] = {300}
    live["own"] = 100
    await _session(empty_db, "peer", pid=300, mission="Peer mission.")
    block = await _block(empty_db, empty_dirs, own_session_id="fresh")
    assert "**Live sessions:** 2 CC (incl. this session)" in block
    assert "Peer mission." in block


async def test_live_sessions_survive_truncation(empty_db, empty_dirs, live, monkeypatch):
    """The section comes first: an over-long block drops plans and worktrees."""
    live["alive"] = {300}
    await _session(empty_db, "peer", pid=300, mission="Peer mission.")
    monkeypatch.setattr(
        open_loops, "_plan_lines", lambda d: ["- " + "y" * 100 for _ in range(60)]
    )
    block = await _block(empty_db, empty_dirs)
    assert "…(truncated)" in block
    assert "**Live sessions:** 1 CC" in block
    assert "Peer mission." in block
