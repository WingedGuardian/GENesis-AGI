"""scripts/pr_opener.py: which session opened a PR, from the body, history or trailer."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "pr_opener.py"

_spec = importlib.util.spec_from_file_location("pr_opener", SCRIPT)
po = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(po)


def _commit(headline: str, body: str = "") -> dict:
    return {"messageHeadline": headline, "messageBody": body}


def _edit(at: str, diff: str, login: str = "someone") -> dict:
    return {"editedAt": at, "diff": diff, "editor": {"login": login}}


# --- opener: the order of sources -------------------------------------------------


def test_the_body_line_answers_first():
    body = "## Summary\nx\n\nOpened-by-session: 0a1b2c3d\n"
    commits = [_commit("feat: x", "text\n\nGenesis-Session: ffffffff")]
    assert po.opener(body, [], commits) == ("0a1b2c3d", "body")


def test_a_crlf_body_still_matches():
    assert po.opener("x\r\n\r\nOpened-by-session: 0a1b2c3d\r\n", [], []) == ("0a1b2c3d", "body")


def test_the_line_must_stand_alone_and_be_lowercase_hex():
    for body in (
        "see Opened-by-session: 0a1b2c3d",
        "Opened-by-session: 0A1B2C3D",
        "Opened-by-session: 0a1b2c3",
    ):
        assert po.opener(body, [], []) == (None, "none"), body


def test_a_rewritten_body_falls_back_to_the_oldest_version_with_the_line():
    # GitHub lists edits newest first; the stamp's version is the oldest one with
    # the line, and a later edit naming another id must not win.
    edits = [
        _edit("2026-11-18T03:40:00Z", "rewritten, no line"),
        _edit("2026-11-18T03:39:00Z", "x\n\nOpened-by-session: 22222222\n"),
        _edit("2026-11-18T03:37:40Z", "x\n\nOpened-by-session: 11111111\n<!-- devin -->"),
        _edit("2026-11-18T03:37:28Z", "original, no line", "WingedGuardian"),
    ]
    assert po.opener("rewritten, no line", edits, []) == ("11111111", "history")


def test_the_oldest_stamp_beats_a_different_id_in_the_current_body():
    # A later rewrite (a copied body, a rework) can carry another session's line;
    # the first stamp is still who opened the PR.
    edits = [
        _edit("t2", "copied\n\nOpened-by-session: 22222222\n"),
        _edit("t1", "x\n\nOpened-by-session: 11111111\n"),
        _edit("t0", "original"),
    ]
    body = "copied\n\nOpened-by-session: 22222222\n"
    assert po.opener(body, edits, []) == ("11111111", "history")
    # The same id in the body and the history reads as the body.
    assert po.opener("x\n\nOpened-by-session: 11111111\n", edits[1:], []) == ("11111111", "body")


def test_same_second_versions_keep_githubs_order_not_a_timestamp_sort():
    # MEASURED on a real PR: the original body and Devin's edit share a second.
    # GitHub's order (newest first) is the truth; a sort on the stamp is not.
    edits = [
        _edit("2026-11-18T03:37:32Z", "y\n\nOpened-by-session: 22222222\n"),
        _edit("2026-11-18T03:37:32Z", "x\n\nOpened-by-session: 11111111\n"),
    ]
    assert po.opener("", edits, []) == ("11111111", "history")


def test_an_example_line_in_a_code_fence_is_not_a_stamp():
    body = (
        "The hook appends a line like this:\n\n```\nOpened-by-session: 0a1b2c3d\n```\n\n"
        "~~~text\nOpened-by-session: 0e0e0e0e\n~~~\n\n"
        "> Opened-by-session: 0f0f0f0f\n\nOpened-by-session: 12345678\n"
    )
    assert po.opener(body, [], []) == ("12345678", "body")
    assert po.stamped("```\nOpened-by-session: 0a1b2c3d\n```\n") is None


def test_no_line_anywhere_falls_back_to_the_first_commit_trailer():
    commits = [
        _commit("feat: one", "body\n\nGenesis-Session: ABCDEF01"),
        _commit("fix: two", "body\n\nGenesis-Session: 12345678"),
    ]
    assert po.opener("no line", [_edit("t", "no line")], commits) == ("abcdef01", "commit")


def test_a_trailer_outside_the_last_paragraph_is_not_a_trailer():
    commits = [_commit("feat", "Genesis-Session: abcdef01\n\nlast paragraph text")]
    assert po.opener("", [], commits) == (None, "none")


def test_trailer_sessions_lists_each_session_once_in_order():
    msg = "head\n\nbody\n\nGenesis-Session: aaaaaaaa\nGenesis-Session: bbbbbbbb\nGenesis-Session: AAAAAAAA"
    assert po.trailer_sessions(msg) == ["aaaaaaaa", "bbbbbbbb"]


def test_the_trailer_rule_matches_the_merge_tool():
    # One rule for "a Genesis-Session trailer": gh_merge's, which builds main's
    # squash message.
    sys.path.insert(0, str(REPO / "scripts" / "hooks"))
    try:
        import gh_merge
    finally:
        sys.path.pop(0)
    assert po.TRAILER.pattern == gh_merge._SESSION_TRAILER.pattern


# --- sessions: prefix against both id columns ---------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "genesis.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE cc_sessions (id TEXT, session_type TEXT, status TEXT, started_at TEXT,"
        " last_activity_at TEXT, topic TEXT, cc_session_id TEXT);"
        "CREATE TABLE session_charters (session_id TEXT, mission TEXT);"
        "INSERT INTO cc_sessions VALUES ('2bc87284-aaaa', 'foreground', 'active', 't1', 't2',"
        " 'topic one', '2bc87284-aaaa');"
        "INSERT INTO cc_sessions VALUES ('99999999-row', 'foreground', 'completed', 't3', 't4',"
        " 'topic two', '0d0d0d0d-cc');"
        "INSERT INTO session_charters VALUES ('2bc87284-aaaa', 'the mission');"
    )
    con.commit()
    con.close()
    return path


def test_a_prefix_resolves_by_id_with_the_charter_mission(db: Path):
    rows = po.sessions("2bc87284", db)
    assert [(r["id"], r["mission"]) for r in rows] == [("2bc87284-aaaa", "the mission")]


def test_a_prefix_resolves_by_cc_session_id(db: Path):
    assert [r["id"] for r in po.sessions("0d0d0d0d", db)] == ["99999999-row"]


def test_no_row_or_a_bad_prefix_is_an_empty_answer_but_no_database_is_not(db: Path, tmp_path: Path):
    assert po.sessions("12121212", db) == []
    assert po.sessions("2bc8%", db) == []
    # A database that is not there was never read: that is not "no such session".
    with pytest.raises(po.DbUnreadable, match="no session database"):
        po.sessions("2bc87284", tmp_path / "absent.db")


def test_the_database_is_opened_read_only(db: Path):
    before = db.read_bytes()
    po.sessions("2bc87284", db)
    assert db.read_bytes() == before
    assert not Path(str(db) + "-wal").exists()


def test_a_live_wal_database_with_a_writer_open_is_read(db: Path):
    # The install's database runs in WAL mode while the server writes to it.
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO cc_sessions VALUES ('2bc87284-bbbb', 'foreground', 'active', 't9', 't9',"
        " 'uncommitted', NULL)"
    )
    try:
        rows = po.sessions("2bc87284", db)
    finally:
        writer.rollback()
        writer.close()
    assert [r["id"] for r in rows] == ["2bc87284-aaaa"]


def test_an_install_without_charters_still_resolves_the_session(tmp_path: Path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE cc_sessions (id TEXT, session_type TEXT, status TEXT, started_at TEXT,"
        " last_activity_at TEXT, topic TEXT, cc_session_id TEXT);"
        "INSERT INTO cc_sessions VALUES ('abcdef01-x', 'foreground', 'active', 't', 't', 'tp', NULL);"
    )
    con.commit()
    con.close()
    rows = po.sessions("abcdef01", path)
    assert [(r["id"], r["mission"], r["topic"]) for r in rows] == [("abcdef01-x", None, "tp")]


def test_an_unreadable_database_is_reported_not_raised(tmp_path: Path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    with pytest.raises(po.DbUnreadable):
        po.sessions("abcdef01", bad)


# --- the command line, against a fake gh ---------------------------------------------


FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_GH_DIR"]
name = "view.json" if sys.argv[1:3] == ["pr", "view"] else "edits.json"
if sys.argv[1:3] not in (["pr", "view"], ["api", "graphql"]):
    sys.exit(f"unexpected gh call {sys.argv}")
with open(os.path.join(d, "calls"), "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
sys.stdout.write(open(os.path.join(d, name)).read())
"""


def _run(
    tmp_path: Path, db: Path, view: dict, edits: list[dict], total: int | None = None, *args: str
) -> subprocess.CompletedProcess:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "gh").write_text(FAKE_GH)
    (fake / "gh").chmod(0o755)
    (tmp_path / "view.json").write_text(json.dumps(view))
    history = {"totalCount": len(edits) if total is None else total, "nodes": edits}
    (tmp_path / "edits.json").write_text(
        json.dumps({"data": {"repository": {"pullRequest": {"userContentEdits": history}}}})
    )
    env = {
        **os.environ,
        "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_DIR": str(tmp_path),
        "GENESIS_DB_PATH": str(db),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), "4642", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _view(body: str, commits: list[dict]) -> dict:
    return {
        "number": 4642,
        "title": "t",
        "url": "u",
        "state": "OPEN",
        "createdAt": "c",
        "headRefName": "no-such-branch-here",
        "body": body,
        "commits": commits,
    }


def test_the_command_names_the_opener_its_source_and_every_committing_session(tmp_path, db):
    view = _view(
        "x\n\nOpened-by-session: 2bc87284\n",
        [
            _commit("feat", "b\n\nGenesis-Session: 2bc87284"),
            _commit("fix", "b\n\nGenesis-Session: 0d0d0d0d"),
        ],
    )
    out = _run(tmp_path, db, view, [], None, "--json")
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert (r["opener"], r["source"]) == ("2bc87284", "body")
    assert [s["id"] for s in r["opener_sessions"]] == ["2bc87284-aaaa"]
    assert list(r["commit_sessions"]) == ["2bc87284", "0d0d0d0d"]
    assert r["worktree"] is None and r["history_truncated"] is False
    calls = (tmp_path / "calls").read_text()
    assert "--repo WingedGuardian/GENesis-AGI" in calls
    assert "userContentEdits(last:100)" in calls


def test_the_text_report_says_a_trailer_answer_may_not_be_the_opener(tmp_path, db):
    view = _view("no line", [_commit("feat", "b\n\nGenesis-Session: 0d0d0d0d")])
    out = _run(tmp_path, db, view, [], None)
    assert out.returncode == 0, out.stderr
    assert "opened by session 0d0d0d0d" in out.stdout
    assert "may not have opened it" in out.stdout
    assert "topic two" in out.stdout


def test_no_session_anywhere_says_so_and_a_long_history_says_it_was_cut(tmp_path, db):
    out = _run(tmp_path, db, _view("no line", [_commit("feat")]), [_edit("t", "no")], 250)
    assert out.returncode == 0, out.stderr
    assert "no session recorded" in out.stdout
    assert "only the oldest 100 body edits were read" in out.stdout


def test_a_gh_failure_exits_1_with_the_reason(tmp_path, db):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "gh").write_text("#!/bin/sh\necho 'no such PR' >&2\nexit 1\n")
    (tmp_path / "bin" / "gh").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "GENESIS_DB_PATH": str(db),
    }
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "1"], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 1
    assert "no such PR" in out.stderr


def test_the_worktree_holding_the_head_branch_is_named(tmp_path: Path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "i",
        ],
        check=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feat/x", str(wt)], check=True
    )
    assert po.worktree_for("feat/x", cwd=repo) == str(wt)
    assert po.worktree_for("feat/none", cwd=repo) is None


# --- report(): the pieces around the answer, in process -----------------------------


def _fake_gh(monkeypatch, view: dict, edits_response: dict) -> None:
    def gh(args):
        return view if args[:2] == ["pr", "view"] else edits_response

    monkeypatch.setattr(po, "_gh", gh)


def _edits(nodes: list[dict], total: int | None = None) -> dict:
    history = {"totalCount": len(nodes) if total is None else total, "nodes": nodes}
    return {"data": {"repository": {"pullRequest": {"userContentEdits": history}}}}


def test_a_graphql_error_response_is_a_named_failure(monkeypatch, db):
    _fake_gh(monkeypatch, _view("", []), {"data": None, "errors": [{"message": "nope"}]})
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    with pytest.raises(RuntimeError, match="edit-history query failed"):
        po.report(4642, "o/r")
    assert po.main(["4642", "--repo", "o/r"]) == 1


def test_a_fork_pr_never_borrows_a_local_worktree(monkeypatch, db):
    view = {**_view("", []), "isCrossRepository": True}
    _fake_gh(monkeypatch, view, _edits([]))
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    called = []
    monkeypatch.setattr(po, "worktree_for", lambda branch, cwd=None: called.append(branch) or "/x")
    assert po.report(4642, "o/r")["worktree"] is None
    assert called == []


def test_only_a_stamped_line_is_authoritative(monkeypatch, db):
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    monkeypatch.setattr(po, "worktree_for", lambda branch, cwd=None: None)
    _fake_gh(monkeypatch, _view("", [_commit("f", "b\n\nGenesis-Session: 0d0d0d0d")]), _edits([]))
    r = po.report(4642, "o/r")
    assert (r["opener"], r["source"], r["authoritative"]) == ("0d0d0d0d", "commit", False)
    _fake_gh(monkeypatch, _view("Opened-by-session: 2bc87284", []), _edits([]))
    assert po.report(4642, "o/r")["authoritative"] is True


def test_an_unreadable_database_keeps_the_pr_side_answer(monkeypatch, tmp_path, capsys):
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    monkeypatch.setenv("GENESIS_DB_PATH", str(bad))
    monkeypatch.setattr(po, "worktree_for", lambda branch, cwd=None: None)
    _fake_gh(monkeypatch, _view("Opened-by-session: 2bc87284", []), _edits([]))
    assert po.main(["4642"]) == 0
    out = capsys.readouterr().out
    assert "opened by session 2bc87284" in out
    assert "the session database could not be read" in out


def test_the_text_report_names_what_the_body_says_now(monkeypatch, db, capsys):
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    monkeypatch.setattr(po, "worktree_for", lambda branch, cwd=None: None)
    edits = [
        _edit("t1", "copied\n\nOpened-by-session: 0d0d0d0d"),
        _edit("t0", "Opened-by-session: 2bc87284"),
    ]
    _fake_gh(monkeypatch, _view("copied\n\nOpened-by-session: 0d0d0d0d", []), _edits(edits))
    assert po.main(["4642"]) == 0
    out = capsys.readouterr().out
    assert "opened by session 2bc87284, from the oldest version of the body" in out
    assert "the body now says 0d0d0d0d" in out


# --- round 1 (Devin at 7171eab37): fences, charter keys, other repositories ---------


def test_a_shorter_fence_inside_a_longer_one_does_not_close_it():
    # Devin's case: a four-backtick block quoting a three-backtick example.
    body = (
        "````markdown\n```\nOpened-by-session: aaaaaaaa\n```\n````\n\nOpened-by-session: bbbbbbbb\n"
    )
    assert po.stamped(body) == "bbbbbbbb"


def test_a_tilde_fence_is_not_closed_by_backticks_and_an_unclosed_fence_runs_to_the_end():
    body = "~~~\n```\nOpened-by-session: aaaaaaaa\n```\n~~~\nOpened-by-session: bbbbbbbb"
    assert po.stamped(body) == "bbbbbbbb"
    assert po.stamped("```\nOpened-by-session: aaaaaaaa\n") is None
    # A closing fence may not carry text; a longer one closes.
    body = "```\n``` x\nOpened-by-session: aaaaaaaa\n`````\nOpened-by-session: bbbbbbbb"
    assert po.stamped(body) == "bbbbbbbb"


def test_inline_backticks_on_a_line_do_not_open_a_fence():
    # A backtick fence's info string cannot contain a backtick (CommonMark).
    assert po.stamped("``` `x` ```\nOpened-by-session: aaaaaaaa") == "aaaaaaaa"


def test_a_charter_keyed_by_the_transcript_id_is_found(db: Path):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO session_charters VALUES ('0d0d0d0d-cc', 'keyed by cc id')")
    con.commit()
    con.close()
    rows = po.sessions("0d0d0d0d", db)
    assert [(r["id"], r["mission"]) for r in rows] == [("99999999-row", "keyed by cc id")]


def test_local_repos_reads_every_github_remote(tmp_path: Path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    remotes = (
        ("origin", "https://github.com/Acme/Widgets.git"),
        ("private", "git@github.com:acme/widgets-private.git"),
        ("other", "https://example.com/x/y.git"),
    )
    for name, url in remotes:
        subprocess.run(["git", "-C", str(repo), "remote", "add", name, url], check=True)
    assert po.local_repos(cwd=repo) == {"acme/widgets", "acme/widgets-private"}


CREATED = "2026-11-18T03:37:28Z"


def test_a_bot_writing_the_copied_line_back_inside_the_window_does_not_win():
    # Premise check: the hook corrects 99999999 -> 11111111, then a bot that read
    # the body first writes the copied line back, still inside the window.
    edits = [
        _edit("2026-11-18T03:37:55Z", "copied\n\nOpened-by-session: 99999999\n<!-- badge -->"),
        _edit("2026-11-18T03:37:48Z", "copied\n\nOpened-by-session: 11111111\n"),
        _edit(CREATED, "copied\n\nOpened-by-session: 99999999\n"),
    ]
    assert po.opener("", edits, [], CREATED)[0] == "11111111"
    # A body created with its own (right) line keeps it.
    same = [
        _edit("2026-11-18T03:37:40Z", "x\n\nOpened-by-session: 11111111\n<!-- b -->"),
        _edit(CREATED, "x\n\nOpened-by-session: 11111111\n"),
    ]
    assert po.opener("", same, [], CREATED)[0] == "11111111"


def test_a_copied_line_corrected_inside_the_creation_window_names_the_corrector():
    # A rework opens with another PR's body (its line 99999999); the stamp hook
    # replaces it 20 s later. That correction is the opener, not the copied id.
    edits = [
        _edit("2026-11-18T03:37:48Z", "copied\n\nOpened-by-session: 11111111\n"),
        _edit(CREATED, "copied\n\nOpened-by-session: 99999999\n"),
    ]
    assert po.opener("copied", edits, [], CREATED) == ("11111111", "history")
    # Without a creation time the oldest stamp still wins (unchanged rule).
    assert po.opener("copied", edits, []) == ("99999999", "history")


def test_a_rewrite_after_the_creation_window_cannot_change_the_answer():
    edits = [
        _edit("2026-11-18T05:00:00Z", "rework\n\nOpened-by-session: 22222222\n"),
        _edit("2026-11-18T03:37:40Z", "x\n\nOpened-by-session: 11111111\n"),
        _edit(CREATED, "original"),
    ]
    body = "rework\n\nOpened-by-session: 22222222\n"
    assert po.opener(body, edits, [], CREATED) == ("11111111", "history")


def test_a_bot_edit_in_the_window_that_keeps_the_line_keeps_the_answer():
    # Devin appends its badge to the stamped body inside the window: same id.
    edits = [
        _edit("2026-11-18T03:37:52Z", "x\n\nOpened-by-session: 11111111\n\n<!-- badge -->"),
        _edit("2026-11-18T03:37:45Z", "x\n\nOpened-by-session: 11111111\n"),
        _edit(CREATED, "x"),
    ]
    assert po.opener("x\n\nOpened-by-session: 11111111\n\n<!-- badge -->", edits, [], CREATED) == (
        "11111111",
        "body",
    )


def test_another_projects_pr_never_borrows_a_local_worktree(monkeypatch, db):
    _fake_gh(monkeypatch, _view("", []), _edits([]))
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    monkeypatch.setattr(po, "local_repos", lambda cwd=None: {"acme/widgets"})
    called = []
    monkeypatch.setattr(po, "worktree_for", lambda branch, cwd=None: called.append(branch) or "/x")
    assert po.report(4642, "other/project")["worktree"] is None
    assert called == []
    assert po.report(4642, "Acme/Widgets")["worktree"] == "/x"


# --- round 2: the admission fence, the correction line, a cut history --------------


def test_a_quarantined_database_is_never_opened(monkeypatch, tmp_path, db):
    # CI's opener inventory requires the fence; this checks it is consulted first.
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from genesis.db import integrity

    integrity.quarantine_database(db, source="test", detail="unit")

    def no_open(*a, **k):
        raise AssertionError("a fenced database was opened")

    monkeypatch.setattr(po.sqlite3, "connect", no_open)
    with pytest.raises(po.DbUnreadable, match="fenced"):
        po.sessions("2bc87284", db)


def test_a_correction_line_overrides_the_record_and_says_so(monkeypatch, db, capsys):
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    monkeypatch.setattr(po, "worktree_for", lambda branch, cwd=None: None)
    edits = [_edit("t1", "x\n\nOpened-by-session: 0d0d0d0d"), _edit("t0", "x")]
    body = "x\n\nOpened-by-session: 0d0d0d0d\nOpened-by-session-correction: 2bc87284\n"
    _fake_gh(monkeypatch, _view(body, []), _edits(edits))
    r = po.report(4642, "o/r")
    assert (r["opener"], r["source"], r["authoritative"]) == ("2bc87284", "corrected", True)
    assert r["recorded_opener"] == "0d0d0d0d"
    assert [s["id"] for s in r["opener_sessions"]] == ["2bc87284-aaaa"]
    assert po.main(["4642"]) == 0
    out = capsys.readouterr().out
    assert "from the body's Opened-by-session-correction line" in out
    assert "the recorded stamp named 0d0d0d0d" in out


def test_a_correction_inside_a_fence_or_in_history_only_does_not_count():
    assert po.correction("```\nOpened-by-session-correction: 2bc87284\n```") is None
    # A correction is read from the CURRENT body only: removing it undoes it.
    assert po.correction("x") is None
    # It is not a stamp either way, so the hook never treats it as one.
    assert po.stamped("Opened-by-session-correction: 2bc87284") is None


def test_a_body_fallback_after_a_cut_history_is_not_authoritative(monkeypatch, db):
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    monkeypatch.setattr(po, "worktree_for", lambda branch, cwd=None: None)
    body = "x\n\nOpened-by-session: 2bc87284\n"
    _fake_gh(monkeypatch, _view(body, []), _edits([_edit("t0", "no line")], total=250))
    r = po.report(4642, "o/r")
    assert (r["opener"], r["source"], r["authoritative"]) == ("2bc87284", "body", False)
    # A stamped version among those read keeps its authority, cut or not.
    _fake_gh(monkeypatch, _view(body, []), _edits([_edit("t0", body)], total=250))
    assert po.report(4642, "o/r")["authoritative"] is True
    # A never-edited body (nothing cut) keeps it too.
    _fake_gh(monkeypatch, _view(body, []), _edits([]))
    assert po.report(4642, "o/r")["authoritative"] is True


# --- round-2 class audit: never claim more than the read proves --------------------


def _quiet(monkeypatch, db):
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    monkeypatch.setattr(po, "worktree_for", lambda branch, cwd=None: None)
    monkeypatch.setattr(po, "local_repos", lambda cwd=None: {"o/r"})


def test_a_correction_copied_with_the_body_is_ignored(monkeypatch, db):
    # S1: a rework copies another PR's body, correction line included. The body
    # GitHub first recorded already carried it, so it is not this PR's correction.
    _quiet(monkeypatch, db)
    copied = "x\nOpened-by-session-correction: aaaaaaaa\n"
    stamped_now = copied + "\nOpened-by-session: 2bc87284\n"
    edits = [_edit("t1", stamped_now), _edit("t0", copied)]
    _fake_gh(monkeypatch, _view(stamped_now, []), _edits(edits))
    r = po.report(4642, "o/r")
    assert (r["opener"], r["source"]) == ("2bc87284", "body")
    # A never-edited body has no history: a correction there came with it.
    _fake_gh(monkeypatch, _view(copied, []), _edits([]))
    assert po.report(4642, "o/r")["source"] != "corrected"


def test_a_correction_is_authoritative_even_when_the_record_is_unproven(monkeypatch, db, capsys):
    # S4: the record is a body fallback after a cut history; the hand correction
    # stands on its own and the text says why the record is not proven.
    _quiet(monkeypatch, db)
    body = "x\nOpened-by-session: 0d0d0d0d\nOpened-by-session-correction: 2bc87284\n"
    _fake_gh(monkeypatch, _view(body, []), _edits([_edit("t0", "x")], total=250))
    r = po.report(4642, "o/r")
    assert (r["source"], r["authoritative"], r["recorded_unproven"]) == ("corrected", True, True)
    assert po.main(["4642", "--repo", "o/r"]) == 0
    assert "not proven: the history read was cut" in capsys.readouterr().out


def test_an_unread_database_never_prints_no_session_row(monkeypatch, tmp_path, capsys):
    # S2: a missing database is "not read", never "no cc_sessions row".
    monkeypatch.setenv("GENESIS_DB_PATH", str(tmp_path / "absent.db"))
    monkeypatch.setattr(po, "worktree_for", lambda branch, cwd=None: None)
    view = _view("Opened-by-session: 2bc87284", [_commit("f", "b\n\nGenesis-Session: 0d0d0d0d")])
    _fake_gh(monkeypatch, view, _edits([]))
    assert po.main(["4642"]) == 0
    out = capsys.readouterr().out
    assert "no cc_sessions row" not in out
    assert out.count("session database not read") == 2
    assert "no session database on this install" in out


def test_a_capped_commit_list_is_said(monkeypatch, db, capsys):
    # S3: gh returns at most 100 commits (MEASURED 123 -> 100 on one PR).
    _quiet(monkeypatch, db)
    commits = [_commit(f"c{i}") for i in range(100)]
    _fake_gh(monkeypatch, _view("", commits), _edits([]))
    assert po.report(4642, "o/r")["commits_capped"] is True
    assert po.main(["4642", "--repo", "o/r"]) == 0
    assert "gh returns at most 100 commits" in capsys.readouterr().out
    _fake_gh(monkeypatch, _view("", commits[:99]), _edits([]))
    assert po.report(4642, "o/r")["commits_capped"] is False


def test_the_worktree_line_says_why_none_was_looked_up(monkeypatch, db):
    # N2: "none on this install" only when it was looked up.
    _quiet(monkeypatch, db)
    _fake_gh(monkeypatch, {**_view("", []), "isCrossRepository": True}, _edits([]))
    assert po.report(4642, "o/r")["worktree_note"] == "not looked up: a fork's branch"
    _fake_gh(monkeypatch, _view("", []), _edits([]))
    assert po.report(4642, "elsewhere/x")["worktree_note"].startswith("not looked up")
    assert po.report(4642, "o/r")["worktree_note"] == "none on this install"


def test_a_charter_under_the_row_id_is_preferred_when_both_ids_carry_one(db: Path):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO session_charters VALUES ('99999999-row', 'row id mission')")
    con.execute("INSERT INTO session_charters VALUES ('0d0d0d0d-cc', 'cc id mission')")
    con.commit()
    con.close()
    assert [r["mission"] for r in po.sessions("0d0d0d0d", db)] == ["row id mission"]
