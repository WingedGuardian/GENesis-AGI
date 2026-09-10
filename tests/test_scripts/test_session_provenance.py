"""Tests for scripts/session_provenance.py — tracing work back to a session.

The load-bearing claim under test is narrow and easy to get wrong: session
provenance survives a squash merge in the commit BODY, and git's own trailer API
cannot see it there. Everything else in the module is built on that, so the
acceptance test below replays the real shape (a concatenated squash message) and
asserts BOTH halves — that the trailer API misses it, and that the parser does
not. Asserting only the second half would still pass if the premise were false.

Real git repos in tmp_path (the house pattern); the module shells out to git, so
there is no mock seam.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "session_provenance.py"
_spec = importlib.util.spec_from_file_location("session_provenance", _SCRIPT)
sp = importlib.util.module_from_spec(_spec)
sys.modules["session_provenance"] = sp
_spec.loader.exec_module(sp)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    """A repo whose main history mimics this one: squashed, concatenated bodies."""
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "config", "user.email", "s@s")
    _git(r, "config", "user.name", "s")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-q", "-m", "seed")

    monkeypatch.setattr(sp, "REPO_ROOT", r)
    # Never read the operator's real stores from a test.
    monkeypatch.setattr(sp, "DB_PATH", tmp_path / "absent.db")
    monkeypatch.setattr(sp, "PROJECTS_DIR", tmp_path / "absent-projects")
    return r


def _commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    msg = repo / ".msg"
    msg.write_text(message)
    _git(repo, "commit", "-q", "-F", str(msg))
    msg.unlink()
    return _git(repo, "rev-parse", "HEAD").strip()


# ─── the acceptance bar: replay the squash shape that blinds the trailer API ──


# The REAL shape, taken from a merged commit on this repo (e192c2beb) rather than
# invented. Two details make it what it is, and only the second is load-bearing:
# GitHub concatenates each branch commit's message, AND it appends its own
# `---------` separator plus a Co-authored-by block at the very end. That trailing
# block is what git's parser reads as "the trailers", so the stamped lines are
# never reached — even for the LAST commit in the squash.
SQUASHED_BODY = """fix(memory): size the recall read pool (#1874)

* fix(memory): size the recall read pool from the host

Some body text explaining the change.

Install: e05d97c0
Genesis-Session: fdfd2de8

* fix(memory): address review

More body text.

Install: e05d97c0
Genesis-Session: 745814ce

---------

Co-authored-by: Someone <someone@users.noreply.github.com>
"""


def test_trailer_api_is_blind_to_a_squashed_body(repo):
    """The PREMISE. If this ever fails, the module's reason to exist has changed.

    git parses trailers only from the final paragraph. GitHub concatenates the
    branch's commit messages and then appends its own separator and
    Co-authored-by block, so that block becomes the final paragraph and the
    stamped lines are never reached. MEASURED on this repo 2026-09-10: of the
    last 200 commits on main, 196 contain the text and 0 have it in the last
    paragraph.
    """
    _commit(repo, "a.txt", SQUASHED_BODY)

    api = _git(repo, "log", "-1", "--format=%(trailers:key=Genesis-Session,valueonly)")
    assert api.strip() == "", (
        "git's trailer API unexpectedly parsed a mid-body trailer — the squash "
        "assumption this module is built on no longer holds"
    )

    # The text is unambiguously there, which is what makes the API result a blind
    # spot rather than a genuine absence.
    body = _git(repo, "log", "-1", "--format=%B")
    assert body.count("Genesis-Session:") == 2


def test_parser_recovers_what_the_api_missed(repo):
    """The FIX, on the same commit that defeats the API."""
    _commit(repo, "a.txt", SQUASHED_BODY)
    found = sp.scan("main", limit=1)
    assert len(found) == 1
    assert found[0]["sessions"] == ["745814ce", "fdfd2de8"]
    assert found[0]["installs"] == ["e05d97c0"]
    assert found[0]["pr"] == 1874


# ─── attribution must not be fooled by prose ─────────────────────────────────


def test_prose_mentioning_a_trailer_is_not_attribution(repo):
    """A commit DISCUSSING a trailer is not a commit STAMPED with one.

    This module's own source contains the literal string in prose; a loose
    substring search would attribute commits to whatever ids they talk about.
    """
    _commit(
        repo,
        "a.txt",
        "docs: explain provenance\n\n"
        "We stamp `Genesis-Session: aaaaaaaa` on each commit, and the inline\n"
        "mention Genesis-Session: bbbbbbbb here is prose, not a trailer.\n\n"
        "Genesis-Session: cccccccc\n",
    )
    found = sp.scan("main", limit=1)
    assert found[0]["sessions"] == ["cccccccc"], (
        "only the line-anchored stamp counts; backticked and mid-sentence "
        "mentions must not attribute the commit"
    )


def test_malformed_ids_are_rejected(repo):
    """The hook constrains ids to exactly 8 lowercase hex; anything else is noise."""
    _commit(
        repo,
        "a.txt",
        "chore: junk\n\n"
        "Genesis-Session: NOTHEX01\n"
        "Genesis-Session: abc\n"
        "Genesis-Session: 0123456789abcdef\n"
        "Genesis-Session: 0a1b2c3d\n",
    )
    assert sp.scan("main", limit=1)[0]["sessions"] == ["0a1b2c3d"]


def test_unstamped_commit_reports_none_not_error(repo):
    """A pre-hook or external commit is unattributed — a real answer, not a crash."""
    _commit(repo, "a.txt", "chore: no trailers here\n\nJust a body.\n")
    assert sp.scan("main", limit=1)[0]["sessions"] == []


# ─── branch lane: where an OPEN PR's answer actually lives ───────────────────


def test_branch_scan_sees_only_commits_not_in_main(repo, capsys):
    """An open PR is answered from its branch, and only its own commits count."""
    _commit(repo, "base.txt", "feat: base\n\nGenesis-Session: 11111111\n")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f1.txt", "feat: one\n\nGenesis-Session: 22222222\n")
    _commit(repo, "f2.txt", "feat: two\n\nGenesis-Session: 33333333\n")

    rc = sp.cmd_branch("feature")
    assert rc == 0
    out = capsys.readouterr().out
    assert "22222222" in out and "33333333" in out
    assert "11111111" not in out, "a commit already in main is not the branch's work"


def test_unknown_branch_is_an_error_not_a_silent_empty(repo, capsys):
    assert sp.cmd_branch("no-such-branch") == 1


# ─── enrichment is optional, and absence is a real answer ────────────────────


def test_enrich_reports_absence_without_raising(repo):
    """A session with no DB row and no transcript must still resolve to a result.

    MEASURED motive: session 59b971ca authored PR #1702 and exists in neither
    store, so absence is the common case this must handle, not an edge case.
    """
    info = sp.enrich("deadbeef")
    assert info["id"] == "deadbeef"
    assert info["db"] is None
    assert info["transcript"] is None
    assert "only record" in sp._describe(info)


def test_describe_flags_an_ambiguous_prefix(repo):
    """Two sessions sharing an 8-hex prefix must not be silently collapsed to one."""
    assert "ambiguous" in sp._describe({"id": "abcd1234", "db": {"ambiguous": 2}})


# ─── dispatch and failure semantics ──────────────────────────────────────────


@pytest.mark.parametrize("flag", ["--commit", "--branch", "--session"])
def test_an_empty_argument_is_refused_not_silently_rerouted(repo, monkeypatch, capsys, flag):
    """S3: truthiness dispatch ran a DIFFERENT command and exited 0.

    `--branch "$UNSET_VAR"` binds an empty string, fails the truthiness test, and
    used to fall through to the coverage report — so a caller asking "which
    session authored this?" got a full-history summary and a success code. The
    fall-through target being a different command is what makes this a
    correctness bug rather than a cosmetic one.
    """
    monkeypatch.setattr(sys, "argv", ["session_provenance.py", flag, ""])
    with pytest.raises(SystemExit) as exc:
        sp.main()
    assert exc.value.code != 0
    assert "commits scanned" not in capsys.readouterr().out


def test_a_bad_session_id_is_refused_before_it_reaches_a_glob(repo, monkeypatch):
    """N7: the id reaches a filesystem glob and a SQL GLOB; '*' is not an id."""
    monkeypatch.setattr(sys, "argv", ["session_provenance.py", "--session", "*"])
    with pytest.raises(SystemExit) as exc:
        sp.main()
    assert exc.value.code != 0


def test_a_git_failure_does_not_report_a_merged_pr_as_open(repo, monkeypatch, capsys):
    """S4: fail-OPEN — a timeout rendered as a confident state claim.

    `_git` returned "" on failure, `scan` returned [], `hits` was empty, and
    control fell to the branch that concludes "not on main, therefore still
    open". A transport failure must never become an answer.
    """
    monkeypatch.setattr(sp, "_git", lambda *a, **k: None if k.get("check") else "")
    rc = sp.cmd_pr(1874)
    err = capsys.readouterr().err
    assert rc == 1, "a failed history read must not return success"
    assert "OPEN" not in capsys.readouterr().out
    assert "could not read" in err


def test_scan_distinguishes_git_failure_from_an_empty_range(repo):
    """None means git broke; [] means the range is genuinely empty."""
    _commit(repo, "a.txt", "feat: one\n\nGenesis-Session: 11111111\n")
    assert sp.scan("main", limit=1) != []          # real content
    assert sp.scan("main..main") == []             # empty range, git succeeded
    assert sp.scan("definitely-not-a-ref") is None  # git failed
