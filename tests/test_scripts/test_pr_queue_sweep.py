from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import pr_queue_sweep as sweep  # noqa: E402

from genesis.session_awareness import repo_pulse_gh  # noqa: E402

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
SHA = "a" * 40


def _pr(number, *, labels=(), draft=False, mergeable="MERGEABLE", head=None, updated=None):
    labels = [{"name": label} for label in labels]
    return {
        "number": number,
        "title": f"Queue | PR {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "isDraft": draft,
        "mergeable": mergeable,
        "updatedAt": updated or "2026-11-10T00:00:00Z",
        "createdAt": "2026-11-01T00:00:00Z",
        "headRefOid": head or f"{number:040x}",
        "baseRefName": "main",
        "labels": {
            "totalCount": len(labels),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": labels,
        },
        "commits": {"nodes": [{"commit": {"committedDate": "2026-11-09T00:00:00Z"}}]},
    }


def _listing(prs, repo_url="https://github.com/o/r"):
    payload = {"data": {"repository": {
        "url": repo_url,
        "defaultBranchRef": {"name": "main"},
        "pullRequests": {
            "totalCount": len(prs),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": prs,
        },
    }}}
    return json.dumps(payload)


def _comments(comments):
    return json.dumps({"data": {"repository": {"pullRequest": {
        "comments": {
            "totalCount": len(comments),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": comments,
        },
    }}}})


def _runner(
    prs, *, comments=None, mergeability=None, fail_listing=False,
    repo_name="o/r", repo_url="https://github.com/o/r", comment_error=(),
):
    calls = []
    comments = comments or {}
    mergeability = mergeability or {}

    async def run(argv):
        calls.append(argv)
        if argv[1] == "repo":
            return 0, repo_name + "\n", ""
        if argv[1] == "pr":
            number = int(argv[3])
            return 0, mergeability.get(number, "UNKNOWN") + "\n", ""
        if argv[1] == "api":
            query = next(arg for arg in argv if arg.startswith("query="))
            if "pullRequests(states:OPEN" in query:
                if fail_listing:
                    return 1, "", "API unavailable"
                return 0, _listing(prs, repo_url), ""
            if "comments(first:100" in query:
                number = int(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("number=")))
                if number in comment_error:
                    return 1, "", "comment API unavailable"
                return 0, _comments(comments.get(number, [])), ""
            raise AssertionError(f"unexpected GraphQL query: {query[:80]}")
        raise AssertionError(argv)

    return run, calls


def _install_fake_gate(tmp_path, monkeypatch, *, fail=(), blocked=()):
    script = tmp_path / "fake-gate.py"
    calls = tmp_path / "gate-calls"
    ok = [
        "mergeable      : MERGEABLE", "ci             : green",
        "base-branch    : ok (default)", "pin-receipts   : ok (current)",
        "e2e-plan       : advisory — evidence declared", "codex-at-head  : ok (at head)",
        "rounds         : 1/5", "scheduled-claude: ok (at head)",
        "review-body    : ok", "inline-findings: ok (P2s: 0, P3s: 0)",
        "main-reverts   : advisory — clean", "rework         : ok (current)",
    ]
    blocked_rows = ok.copy()
    blocked_rows[8] = "review-body    : BLOCK — unanswered"
    blocked_rows[9] = "inline-findings: BLOCK — score 1.0"
    def emit(rows, verdict):
        return [f"print({row!r})" for row in rows] + [f"print({verdict!r})"]
    lines = [
        "from pathlib import Path", "import sys",
        "n = int(sys.argv[sys.argv.index('--check-pr') + 1])",
        f"Path({str(calls)!r}).open('a').write(str(n) + '\\n')",
        f"if n in {set(fail)!r}:", " print('verdict        : malformed')", " sys.exit(2)",
        f"if n in {set(blocked)!r}:",
        *[f" {line}" for line in emit(blocked_rows, "verdict        : 2 gate(s) would block")],
        " print('  indented detail: not a gate row')", " sys.exit(1)",
        *emit(ok, "verdict        : MERGEABLE (all gates pass)"),
    ]
    script.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(sweep, "GUARD_PATH", script)
    return calls


def _load(root):
    return json.loads((root / "worklist.json").read_text(encoding="utf-8"))


def _canonical(monkeypatch):
    monkeypatch.setattr(sweep.guard, "_canonical_public_repo", lambda: "o/r")
    monkeypatch.setattr(sweep, "UNKNOWN_REREAD_DELAY", 0)


@pytest.mark.asyncio
async def test_gate_exit_one_with_blocking_verdict_preserves_only_top_level_rows(
    tmp_path, monkeypatch
):
    calls = _install_fake_gate(tmp_path, monkeypatch, blocked={7})
    result = await sweep._run_gate(7, "o/r")
    report, error = sweep._gate_report(SHA, "now", sweep._stamp(NOW), result)

    assert error is None
    assert report["verdict_kind"] == "BLOCKED"
    assert report["rows"]["inline-findings"] == "BLOCK — score 1.0"
    assert all("indented" not in row["name"] for row in report["raw_rows"])
    assert len(report["raw_rows"]) == 13
    assert sweep._finding_only({"gate": report})
    assert calls.read_text().strip() == "7"


@pytest.mark.asyncio
async def test_budget_carries_unread_prs_then_reuses_unchanged_reports(tmp_path, monkeypatch):
    _canonical(monkeypatch)
    calls = _install_fake_gate(tmp_path, monkeypatch)
    prs = [_pr(n) for n in range(1, 18)]
    runner, _ = _runner(prs)
    root = tmp_path / "state"

    assert await sweep._sweep(root, runner=runner, now=NOW) == 0
    first = _load(root)
    assert sum(pr["gate"] is not None for pr in first["prs"]) == 15
    pending = [pr for pr in first["prs"] if pr["gate"] is None]
    assert [pr["number"] for pr in pending] == [16, 17]
    assert all(pr["report_status"] == "unread" and pr["report_pending"] for pr in pending)
    assert len(calls.read_text().splitlines()) == 15

    assert await sweep._sweep(root, runner=runner, now=NOW + timedelta(hours=1)) == 0
    second = _load(root)
    assert len(calls.read_text().splitlines()) == 17
    assert all(pr["gate"] is not None for pr in second["prs"])
    assert [p["gate"]["read_at"] for p in second["prs"][:15]] == [
        p["gate"]["read_at"] for p in first["prs"][:15]
    ]


def test_report_order_is_never_read_then_oldest_successful_read():
    never = {"number": 1, "gate": None, "report_attempted_at": None}
    failed = {"number": 2, "gate": None, "report_attempted_at": sweep._stamp(NOW)}
    newer = {"number": 3, "gate": {"read_at": sweep._stamp(NOW - timedelta(hours=2))}}
    older = {"number": 4, "gate": {"read_at": sweep._stamp(NOW - timedelta(hours=25))}}

    assert [p["number"] for p in sorted([newer, failed, older, never], key=sweep._candidate_key)] == [
        1, 2, 4, 3,
    ]


@pytest.mark.asyncio
async def test_changed_head_invalidates_report_and_pr_refresh_preserves_queue(tmp_path, monkeypatch):
    _canonical(monkeypatch)
    calls = _install_fake_gate(tmp_path, monkeypatch)
    prs = [_pr(1), _pr(2)]
    runner, _ = _runner(prs)
    root = tmp_path / "state"
    assert await sweep._sweep(root, runner=runner, now=NOW) == 0
    old = _load(root)
    prs[1] = _pr(2, head="b" * 40)

    assert await sweep._sweep(
        root, runner=runner, forced_pr=2, now=NOW + timedelta(hours=1)
    ) == 0
    current = _load(root)
    assert [p["number"] for p in current["prs"]] == [1, 2]
    assert current["prs"][0]["gate"]["read_at"] == old["prs"][0]["gate"]["read_at"]
    assert current["prs"][1]["gate"]["reported_head_sha"] == "b" * 40
    assert calls.read_text().splitlines() == ["1", "2", "2"]


@pytest.mark.asyncio
async def test_failed_listing_does_not_publish_and_reports_partial_count(tmp_path, monkeypatch, capsys):
    _canonical(monkeypatch)
    _install_fake_gate(tmp_path, monkeypatch)
    good, _ = _runner([_pr(1)])
    root = tmp_path / "state"
    assert await sweep._sweep(root, runner=good, now=NOW) == 0
    before = [(root / "worklist.json").read_text(), (root / "worklist.md").read_text()]
    failed, _ = _runner([_pr(1)], fail_listing=True)

    assert await sweep._sweep(root, runner=failed, now=NOW + timedelta(hours=1)) == 1
    assert [(root / "worklist.json").read_text(), (root / "worklist.md").read_text()] == before
    assert "limit_hit=true read=0 open_total=0" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_scope_mismatch_skips_listing_and_state_creation(tmp_path, monkeypatch):
    _canonical(monkeypatch)
    runner, calls = _runner([], repo_name="fork/r", fail_listing=True)
    root = tmp_path / "out-of-scope"

    assert await sweep._sweep(root, runner=runner) == 0

    assert [argv[1] for argv in calls] == ["repo"]
    assert not root.exists()


def test_missing_canonical_repo_skips_before_resolving_state_root(monkeypatch):
    monkeypatch.setattr(sweep.guard, "_canonical_public_repo", lambda: None)
    monkeypatch.setattr(sweep, "genesis_home", lambda: (_ for _ in ()).throw(AssertionError()))

    assert sweep.main([]) == 0


@pytest.mark.asyncio
async def test_unknown_mergeability_is_reread_once_and_gate_errors_are_published(tmp_path, monkeypatch):
    _canonical(monkeypatch)
    _install_fake_gate(tmp_path, monkeypatch, fail={2})
    prs = [_pr(1, mergeable="UNKNOWN"), _pr(2)]
    runner, calls = _runner(prs, mergeability={1: "UNKNOWN"})
    root = tmp_path / "state"

    assert await sweep._sweep(root, runner=runner, now=NOW) == 0
    out = _load(root)
    assert sum(argv[1] == "pr" and argv[2] == "view" for argv in calls) == 1
    assert out["prs"][0]["mergeable"] == "UNKNOWN"
    assert out["prs"][0]["conflict_files"] == []
    assert out["prs"][0]["state"] == "blocked"
    assert out["prs"][0]["readiness_status"] == "unknown"
    assert out["prs"][0]["gate"]["verdict"] == "MERGEABLE (all gates pass)"
    assert out["prs"][1]["gate"] is None
    assert out["prs"][1]["report_status"] == "error"
    assert "Could not read" in (root / "worklist.md").read_text()

    _install_fake_gate(tmp_path, monkeypatch)
    assert await sweep._sweep(root, runner=runner, now=NOW + timedelta(hours=1)) == 0
    recovered = _load(root)
    by_number = {pr["number"]: pr for pr in recovered["prs"]}
    assert by_number[2]["report_status"] == "fresh"
    assert by_number[2]["report_error"] is None
    assert by_number[1]["state"] == "blocked"
    assert by_number[1]["readiness_status"] == "unknown"


@pytest.mark.asyncio
async def test_comments_and_conflict_fetch_errors_publish_without_ready_state(tmp_path, monkeypatch):
    _canonical(monkeypatch)
    _install_fake_gate(tmp_path, monkeypatch)
    prs = [_pr(1, labels=("parked",)), _pr(2, labels=("parked",), mergeable="CONFLICTING")]
    runner, _ = _runner(
        prs, comment_error={1}, repo_url="file:///missing-pr-queue-test-remote.git"
    )
    root = tmp_path / "state"

    assert await sweep._sweep(root, runner=runner, now=NOW) == 0
    out = _load(root)
    by_number = {pr["number"]: pr for pr in out["prs"]}
    assert by_number[1]["readiness_status"] == "unknown"
    assert "comment read failed" in by_number[1]["error"]
    assert by_number[2]["readiness_status"] == "unknown"
    assert "scratch fetch failed" in by_number[2]["error"]
    assert all(pr["state"] != "ready" for pr in out["prs"])
    markdown = (root / "worklist.md").read_text()
    conflict = next(line for line in markdown.splitlines() if "#2" in line)
    assert "parked comment missing" in conflict
    assert "gate read" in conflict
    groups = [markdown.index(f"## {title}") for title in (
        "Ready for the maintainer's yes", "Blocked only by unanswered findings", "Conflicting",
        "Drafts with no named state", "Untouched for more than 3 days", "Could not read",
        "Other states",
    )]
    assert groups == sorted(groups)


@pytest.mark.asyncio
async def test_stale_full_budget_oldest_first_carries_remainder_and_invalidates_updated_at(
    tmp_path, monkeypatch
):
    _canonical(monkeypatch)
    calls = _install_fake_gate(tmp_path, monkeypatch)
    prs = [_pr(n) for n in range(1, 18)]
    runner, _ = _runner(prs)
    root = tmp_path / "state"
    assert await sweep._sweep(root, runner=runner, now=NOW) == 0
    seeded = _load(root)
    seeded["generated_at"] = sweep._stamp(NOW - timedelta(hours=25))
    for row in seeded["prs"]:
        if row["number"] <= 15:
            age = 25 + (16 - row["number"])
            row["gate"]["read_at"] = sweep._stamp(NOW - timedelta(hours=age))
    sweep._publish(root, seeded, NOW - timedelta(hours=25))

    assert await sweep._sweep(root, runner=runner, now=NOW) == 0
    assert calls.read_text().splitlines()[15:] == ["16", "17", *map(str, range(1, 14))]
    second = _load(root)
    by_number = {pr["number"]: pr for pr in second["prs"]}
    assert by_number[14]["report_status"] == by_number[15]["report_status"] == "stale"

    assert await sweep._sweep(root, runner=runner, now=NOW + timedelta(hours=1)) == 0
    assert calls.read_text().splitlines()[-2:] == ["14", "15"]
    prs[4] = _pr(5, head=prs[4]["headRefOid"], updated="2026-11-11T00:00:00Z")
    assert await sweep._sweep(root, runner=runner, now=NOW + timedelta(hours=2)) == 0
    updated = {pr["number"]: pr for pr in _load(root)["prs"]}[5]
    assert updated["gate"]["reported_head_sha"] == updated["head_sha"]
    assert updated["gate"]["reported_updated_at"] == "2026-11-11T00:00:00Z"
    assert calls.read_text().splitlines()[-1] == "5"


@pytest.mark.asyncio
async def test_cache_for_other_repo_is_ignored(tmp_path, monkeypatch):
    _canonical(monkeypatch)
    calls = _install_fake_gate(tmp_path, monkeypatch)
    root = tmp_path / "state"
    seed = {"schema": 1, "generated_at": sweep._stamp(NOW), "repo": "fork/r",
            "open_total": 0, "read": 0, "limit_hit": False, "prs": [], "errors": []}
    sweep._publish(root, seed, NOW)
    runner, _ = _runner([_pr(1)])

    assert await sweep._sweep(root, runner=runner, now=NOW) == 0
    assert calls.read_text().splitlines() == ["1"]
    assert _load(root)["repo"] == "o/r"


@pytest.mark.asyncio
async def test_cache_and_publication_failures_keep_previous_pair_and_return_zero(
    tmp_path, monkeypatch, capsys
):
    _canonical(monkeypatch)
    calls = _install_fake_gate(tmp_path, monkeypatch)
    runner, _ = _runner([_pr(1)])
    root = tmp_path / "state"
    assert await sweep._sweep(root, runner=runner, now=NOW) == 0
    old_link = (root / "current").readlink()
    (root / "current" / "worklist.json").write_text("broken cache", encoding="utf-8")
    monkeypatch.setattr(sweep, "_switch_current", lambda *_: (_ for _ in ()).throw(OSError("switch failed")))

    assert await sweep._sweep(root, runner=runner, now=NOW + timedelta(hours=1)) == 0
    assert (root / "current").readlink() == old_link
    assert (root / "current" / "worklist.json").read_text() == "broken cache"
    assert calls.read_text().splitlines() == ["1", "1"]
    err = capsys.readouterr().err
    assert "ignoring unreadable cache" in err
    assert "publication failed; prior generation retained" in err


def test_generation_retention_keeps_current_previous_and_prunes_abandoned(tmp_path):
    root = tmp_path / "state"
    data = {"schema": 1, "generated_at": sweep._stamp(NOW), "repo": "o/r",
            "open_total": 0, "read": 0, "limit_hit": False, "prs": [], "errors": []}
    sweep._publish(root, data, NOW)
    first = sweep._current_generation(root)
    abandoned = root / "generations" / "abandoned"
    abandoned.mkdir()
    sweep._publish(root, data, NOW + timedelta(minutes=1))
    second = sweep._current_generation(root)
    assert set(path.name for path in (root / "generations").iterdir()) == {first, second}
    assert abandoned.exists() is False

    sweep._publish(root, data, NOW + timedelta(minutes=2))
    third = sweep._current_generation(root)
    assert set(path.name for path in (root / "generations").iterdir()) == {second, third}
    (root / "current" / "worklist.json").write_text("corrupt", encoding="utf-8")
    sweep._publish(root, data, NOW + timedelta(minutes=3))
    fourth = sweep._current_generation(root)
    assert set(path.name for path in (root / "generations").iterdir()) == {second, fourth}


@pytest.mark.asyncio
async def test_production_timestamps_are_recorded_on_completion(tmp_path, monkeypatch):
    _canonical(monkeypatch)
    gate_calls = _install_fake_gate(tmp_path, monkeypatch)
    run_gate = sweep._run_gate

    async def delayed_gate(number, repo):
        await __import__("asyncio").sleep(1.1)
        return await run_gate(number, repo)

    monkeypatch.setattr(sweep, "_run_gate", delayed_gate)
    runner, _ = _runner([_pr(1), _pr(2)])
    root = tmp_path / "state"
    assert await sweep._sweep(root, runner=runner) == 0
    out = _load(root)
    read_at = sweep._parse_time(out["prs"][0]["gate"]["read_at"])
    generated_at = sweep._parse_time(out["generated_at"])
    assert generated_at > read_at
    assert gate_calls.read_text().splitlines() == ["1", "2"]


@pytest.mark.asyncio
async def test_named_labels_comments_and_parked_warning(tmp_path, monkeypatch):
    _canonical(monkeypatch)
    _install_fake_gate(tmp_path, monkeypatch)
    prs = [
        _pr(1, labels=("needs-rework", "parked")),
        _pr(2, labels=("parked",)),
        _pr(3, labels=("needs-architecture-session",)),
    ]
    comments = {
        1: [
            {"authorAssociation": "OWNER", "body": "## Rework spec\nDo it", "createdAt": "2026-11-12T00:00:00Z"},
            {"authorAssociation": "MEMBER", "body": "## Parked\nWait", "createdAt": "2026-11-11T00:00:00Z"},
            {"authorAssociation": "NONE", "body": "## Rework spec\nIgnore", "createdAt": "2026-11-13T00:00:00Z"},
        ],
    }
    runner, calls = _runner(prs, comments=comments)
    root = tmp_path / "state"

    assert await sweep._sweep(root, runner=runner, now=NOW) == 0
    out = _load(root)
    by_number = {pr["number"]: pr for pr in out["prs"]}
    assert by_number[1]["state"] == "rework-specced"
    assert by_number[1]["parked_comment_age"] == "2d 12h"
    assert by_number[2]["state"] == "parked"
    assert "no maintainer ## Parked comment" in by_number[2]["warnings"][0]
    assert by_number[3]["state"] == "architecture"
    comment_calls = [
        argv for argv in calls
        if argv[1] == "api" and any("comments(first:100" in arg for arg in argv)
    ]
    assert len(comment_calls) == 2
    markdown = (root / "worklist.md").read_text()
    assert "parked comment age" in markdown and "no maintainer" in markdown


def test_show_prints_cached_age_without_reading_github(tmp_path, monkeypatch, capsys):
    root = tmp_path / "pr_queue"
    data = {
        "schema": 1, "generated_at": sweep._stamp(NOW), "repo": "o/r",
        "open_total": 0, "read": 0, "limit_hit": False, "prs": [], "errors": [],
    }
    sweep._publish(root, data, NOW)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    monkeypatch.setattr(
        sweep.repo_pulse_gh, "list_queue_prs",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("--show read GitHub")),
    )

    assert sweep.main(["--show"]) == 0
    shown = capsys.readouterr().out
    assert "Cached worklist age:" in shown
    assert data["generated_at"] in shown


def test_show_without_cache_does_not_create_state_or_read_github(tmp_path, monkeypatch, capsys):
    root = tmp_path / "pr_queue"
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    monkeypatch.setattr(
        sweep.repo_pulse_gh, "resolve_repo",
        lambda: (_ for _ in ()).throw(AssertionError("--show resolved GitHub repo")),
    )
    monkeypatch.setattr(
        sweep.repo_pulse_gh, "list_queue_prs",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("--show read GitHub")),
    )

    assert sweep.main(["--show"]) == 0
    assert "No cached PR queue worklist." in capsys.readouterr().out
    assert not root.exists()


def test_show_waits_for_writer_and_reads_one_complete_generation(tmp_path, monkeypatch, capsys):
    root = tmp_path / "pr_queue"

    def worklist(repo):
        return {
            "schema": 1, "generated_at": sweep._stamp(NOW), "repo": repo,
            "open_total": 0, "read": 0, "limit_hit": False, "prs": [], "errors": [],
        }

    sweep._publish(root, worklist("generation-one"), NOW)
    first = sweep._current_generation(root)
    sweep._publish(root, worklist("generation-two"), NOW + timedelta(seconds=1))
    second = sweep._current_generation(root)
    assert first and second and first != second
    sweep._switch_current(root, first)

    original_lock = sweep._queue_lock
    attempting = threading.Event()
    finished = threading.Event()
    result = {}

    @contextlib.contextmanager
    def observed_lock(lock_root, *, shared=False):
        attempting.set()
        with original_lock(lock_root, shared=shared):
            yield

    def show():
        try:
            result["status"] = sweep._show(root)
        except BaseException as exc:
            result["error"] = exc
        finally:
            finished.set()

    monkeypatch.setattr(sweep, "_queue_lock", observed_lock)
    with original_lock(root):
        thread = threading.Thread(target=show)
        thread.start()
        assert attempting.wait(2), "--show did not acquire the shared publication lock"
        assert not finished.wait(0.05), "--show did not wait for the writer lock"
        sweep._switch_current(root, second)
        shutil.rmtree(root / "generations" / first)
    assert finished.wait(2)
    thread.join()

    assert "error" not in result, result.get("error")
    assert result.get("status") == 0
    shown = capsys.readouterr().out
    assert "generation-two" in shown
    assert "generation-one" not in shown


@pytest.mark.asyncio
async def test_default_runner_uses_fake_gh_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    payload = _listing([])
    fake.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "if sys.argv[1:3] == ['repo', 'view']:\n print('o/r')\n"
        f"else:\n print({payload!r})\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    out = await repo_pulse_gh.list_queue_prs()

    assert out["repo"] == "o/r" and out["read"] == 0 and out["open_total"] == 0


def test_conflict_paths_use_real_bare_fetch_and_merge_tree_for_rename_unicode(tmp_path):
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(work)], check=True, capture_output=True)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(work), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("config", "user.email", "queue-test@example.test")
    git("config", "user.name", "Queue Test")
    (work / "conflict-é.txt").write_text("base\n")
    (work / "rename-旧.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("remote", "add", "origin", remote.as_uri())
    git("push", "origin", "main:main")
    git("checkout", "-b", "pr")
    git("mv", "rename-旧.txt", "renamed-名.txt")
    (work / "renamed-名.txt").write_text("pr rename\n")
    (work / "conflict-é.txt").write_text("pr\n")
    git("commit", "-am", "PR changes")
    head = git("rev-parse", "HEAD")
    git("push", "origin", "HEAD:refs/pull/17/head")
    git("checkout", "main")
    (work / "rename-旧.txt").write_text("base changed\n")
    (work / "conflict-é.txt").write_text("base changed\n")
    git("commit", "-am", "base changes")
    git("push", "origin", "main:main")

    paths = __import__("asyncio").run(sweep._conflict_paths(
        tmp_path / "state", remote.as_uri(), "main", {"number": 17, "head_sha": head}
    ))
    raw = subprocess.run(
        ["git", "-C", str(tmp_path / "state" / "scratch.git"), "merge-tree",
         "--write-tree", "--name-only", "-z", "refs/pr_queue/base", head],
        capture_output=True, check=False,
    )
    assert raw.returncode == 1
    assert set(paths) == {"conflict-é.txt", "rename-旧.txt"}, repr(raw.stdout)
    with pytest.raises(ValueError, match="head moved"):
        __import__("asyncio").run(sweep._conflict_paths(
            tmp_path / "state", remote.as_uri(), "main", {"number": 17, "head_sha": "0" * 40}
        ))


def test_kill_mid_publication_keeps_previous_generation_readable(tmp_path):
    root = tmp_path / "pr_queue"
    old = {
        "schema": 1, "generated_at": sweep._stamp(NOW), "repo": "o/r",
        "open_total": 0, "read": 0, "limit_hit": False, "prs": [], "errors": [],
    }
    sweep._publish(root, old, NOW)
    script_dir = ROOT / "scripts"
    code = """import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import pr_queue_sweep as q
replace = os.replace
def interrupt(src, dst):
    if Path(dst).name == 'current':
        os.kill(os.getpid(), signal.SIGKILL)
    return replace(src, dst)
os.replace = interrupt
q._publish(Path(sys.argv[2]), json.loads(sys.argv[3]), q.datetime.now(q.UTC))
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(script_dir), str(root), json.dumps({**old, "read": 1})],
        capture_output=True,
        timeout=30,
    )

    assert child.returncode == -9
    assert _load(root) == old
    assert "Read: 0 of 0 open PRs" in (root / "worklist.md").read_text()


def test_timer_is_hourly_persistent_and_uses_project_timeout():
    systemd = ROOT / "scripts" / "systemd"
    timer = (systemd / "genesis-pr-queue-sweep.timer.template").read_text()
    service = (systemd / "genesis-pr-queue-sweep.service.template").read_text()
    assert "OnCalendar=hourly" in timer and "Persistent=true" in timer
    assert "TimeoutStartSec=7200" in service
    assert "scripts/pr_queue_sweep.py" in service
