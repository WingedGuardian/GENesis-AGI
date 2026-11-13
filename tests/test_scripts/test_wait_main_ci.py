"""wait_main_ci: a merge's main-CI verdict is the newest run that CONTAINS the
merge and concluded with a signal; cancelled and signal-less runs never count."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "hooks"))

import wait_main_ci as w  # noqa: E402

MERGE = "a" * 40


def _run(rid, head, status="completed", conclusion="success"):
    return {"databaseId": rid, "headSha": head, "status": status, "conclusion": conclusion}


# Fake ancestry for decide(): heads "new*" and MERGE contain the merge; "old" does not.
def _contains(head):
    return head.startswith("new") or head == MERGE


# --- decide --------------------------------------------------------------------


@pytest.mark.parametrize("conclusion", sorted(w.NO_SIGNAL) + ["some_future_value"])
def test_a_signal_less_conclusion_is_never_a_verdict(conclusion):
    assert w.decide([_run(1, MERGE, conclusion=conclusion)], _contains) == ("wait", None)


@pytest.mark.parametrize("conclusion", ["success", *sorted(w.RED)])
def test_success_and_red_conclusions_are_verdicts(conclusion):
    run = _run(1, MERGE, conclusion=conclusion)
    assert w.decide([run], _contains) == ("verdict", run)


def test_an_older_concluded_run_decides_while_newer_ones_are_queued_or_cancelled():
    """main never cancels its in-progress run, a newer push replaces the pending
    one, so the newest containing run can stay pending while an older one has
    already concluded on the commit."""
    runs = [
        _run(4, "new3", status="pending", conclusion=""),
        _run(3, "new2", conclusion="cancelled"),
        _run(2, "new1", conclusion="failure"),
        _run(1, MERGE, conclusion="cancelled"),
    ]
    assert w.decide(runs, _contains) == ("verdict", runs[2])


def test_runs_that_predate_the_merge_never_decide():
    assert w.decide([_run(1, "old", conclusion="success")], _contains) == ("wait", None)


def test_conclusion_sets_match_the_merge_gate():
    import git_push_guard as gate

    assert w.NO_SIGNAL == gate._MAIN_CI_NO_SIGNAL
    assert w.RED == gate._MAIN_CI_RED


def test_run_list_reads_main_push_runs_of_ci():
    args = w._run_list_args("o/r")
    for pair in (
        ["--workflow", "ci.yml"],
        ["--branch", "main"],
        ["--event", "push"],
        ["--repo", "o/r"],
    ):
        i = args.index(pair[0])
        assert args[i + 1] == pair[1]


# --- real git: ancestry and resolution ---------------------------------------------


def _g(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone whose origin/main is c1 -> c2 -> c3, plus a commit c_off not on main."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _g(work, "config", "user.email", "t@example.org")
    _g(work, "config", "user.name", "t")
    shas = []
    for i in range(3):
        _g(work, "commit", "-q", "--allow-empty", "-m", f"c{i + 1}")
        shas.append(_g(work, "rev-parse", "HEAD"))
    _g(work, "remote", "add", "origin", str(origin))
    _g(work, "push", "-q", "origin", "main")
    _g(work, "checkout", "-q", "-b", "side", shas[0])
    _g(work, "commit", "-q", "--allow-empty", "-m", "off")
    off = _g(work, "rev-parse", "HEAD")
    monkeypatch.chdir(work)
    return shas, off


def test_contains_follows_real_ancestry(repo):
    (c1, c2, c3), _ = repo
    assert w._contains(c2)(c3) is True
    assert w._contains(c2)(c2) is True
    assert w._contains(c3)(c1) is False  # an older, pre-merge head never contains it


def test_contains_raises_on_a_head_git_does_not_know(repo):
    (c1, _, _), _ = repo
    with pytest.raises(w.Unknown):
        w._contains(c1)("f" * 40)


def test_resolve_accepts_a_merge_on_main(repo):
    (_, c2, _), _ = repo
    assert w._resolve(c2[:10]) == c2


@pytest.mark.parametrize("which", ["unknown", "off-main"])
def test_resolve_refuses_what_it_cannot_check(repo, which):
    _, off = repo
    with pytest.raises(w.Unknown):
        w._resolve("deadbeefdeadbeef" if which == "unknown" else off)


# --- main --------------------------------------------------------------------------


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(w, "_resolve", lambda sha: sha)
    monkeypatch.setattr(w, "_git", lambda *a: None)
    monkeypatch.setattr(w, "_contains", lambda sha: _contains)
    monkeypatch.setattr(w, "_failed_jobs", lambda rid, repo: ["test"])


@pytest.mark.parametrize(("conclusion", "code"), [("success", 0), ("failure", 1), ("timed_out", 1)])
def test_main_waits_out_cancelled_and_pending_runs_then_reports(
    offline, monkeypatch, conclusion, code
):
    polls = iter(
        [
            [_run(1, MERGE, status="in_progress", conclusion="")],
            [_run(1, MERGE, conclusion="cancelled")],
            [
                _run(2, "new1", status="queued", conclusion=""),
                _run(1, MERGE, conclusion="cancelled"),
            ],
            [_run(2, "new1", conclusion=conclusion), _run(1, MERGE, conclusion="cancelled")],
        ]
    )
    monkeypatch.setattr(w, "_runs", lambda repo: next(polls))
    slept = []
    assert w.main([MERGE, "--wait", "3600"], sleep=slept.append) == code
    assert len(slept) == 3


def test_main_gives_up_with_2_when_nothing_concludes_with_a_signal(offline, monkeypatch):
    monkeypatch.setattr(w, "_runs", lambda repo: [_run(1, MERGE, conclusion="cancelled")])
    assert w.main([MERGE, "--wait", "0"], sleep=lambda s: None) == 2


def test_an_unreadable_run_list_keeps_waiting_rather_than_deciding(offline, monkeypatch):
    def broken(repo):
        raise ValueError("bad json")

    monkeypatch.setattr(w, "_runs", broken)
    assert w.main([MERGE, "--wait", "0"], sleep=lambda s: None) == 2


def test_an_unknown_ancestry_during_a_poll_keeps_waiting(offline, monkeypatch):
    """A head the checkout does not know yet (a poll's fetch failed) must not
    escape as a crash, whose exit 1 would read as a red main."""

    def unknown(head):
        raise w.Unknown("bad object")

    monkeypatch.setattr(w, "_contains", lambda sha: unknown)
    monkeypatch.setattr(w, "_runs", lambda repo: [_run(1, "new1", conclusion="success")])
    assert w.main([MERGE, "--wait", "0"], sleep=lambda s: None) == 2


def test_the_verdict_line_says_when_it_comes_from_a_later_push(offline, monkeypatch, capsys):
    monkeypatch.setattr(w, "_runs", lambda repo: [_run(2, "new1", conclusion="failure")])
    assert w.main([MERGE, "--wait", "0"], sleep=lambda s: None) == 1
    assert "a later push's run" in capsys.readouterr().out


def test_main_fails_fast_with_3_on_a_commit_it_cannot_check(monkeypatch):
    def refuse(sha):
        raise w.Unknown("not on origin/main")

    monkeypatch.setattr(w, "_resolve", refuse)
    assert w.main([MERGE], sleep=lambda s: pytest.fail("must not wait")) == 3
