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

Co-authored-by: Someone <someone@example.invalid>
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


# ─── the two fail-open paths an external reviewer found ──────────────────────


def test_a_pr_outside_the_scan_window_is_not_reported_as_open(repo, monkeypatch, capsys):
    """The scan is BOUNDED, so "not found" can mean "older than the window".

    Concluding OPEN from our own bound is the same fail-open shape as concluding
    it from a git failure, one level subtler: here the scan SUCCEEDED and was
    merely too short. A merged PR reported as open is a confident wrong answer.
    """
    monkeypatch.setattr(sp, "_pr_lookup", lambda n: ("MERGED", "feat/x", "", ""))
    rc = sp.cmd_pr(999999)
    out, err = capsys.readouterr()
    assert rc == 1
    assert "OPEN" not in out
    assert "outside the" in err and "scan window" in err


def test_an_open_pr_outside_the_window_still_resolves_via_its_branch(repo, monkeypatch, capsys):
    """The fix must not break the case it was built for."""
    _commit(repo, "a.txt", "feat: base\n\nGenesis-Session: 11111111\n")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f.txt", "feat: work\n\nGenesis-Session: 22222222\n")
    monkeypatch.setattr(sp, "_pr_lookup", lambda n: ("OPEN", "feature", "", ""))
    rc = sp.cmd_pr(4242)
    out = capsys.readouterr().out
    assert rc == 0
    assert "22222222" in out


def test_an_unreadable_branch_makes_the_session_report_incomplete(repo, monkeypatch, capsys):
    """A failed branch scan is not an empty one.

    Swallowing it reports a SHORTER list of unlanded work than exists — the
    wrong direction for a tool whose whole job is finding work that got lost.
    """
    _commit(repo, "a.txt", "feat: base\n\nGenesis-Session: 11111111\n")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f.txt", "feat: work\n\nGenesis-Session: 11111111\n")

    real = sp.scan

    def _fail_branch_scans(rev_range, limit=None):
        return None if ".." in rev_range else real(rev_range, limit)

    monkeypatch.setattr(sp, "scan", _fail_branch_scans)
    rc = sp.cmd_session("11111111", 50)
    err = capsys.readouterr().err
    assert rc == 1, "an incomplete answer must not exit 0"
    assert "INCOMPLETE" in err


# ─── P2 findings from the PR review, each pinned ─────────────────────────────


def test_an_uppercase_session_id_is_still_attribution(repo):
    """The hook preserves the CASE of the source env var.

    An uppercase CLAUDE_CODE_SESSION_ID stamps `Genesis-Session: ABCDEF12`, which
    a lowercase-only pattern reported as UNSTAMPED — silent under-attribution on
    a tool whose entire job is attribution. Ids are normalised on capture so both
    spellings resolve to one identity.
    """
    _commit(repo, "a.txt", "feat: x\n\nGenesis-Session: ABCDEF12\n")
    assert sp.scan("main", limit=1)[0]["sessions"] == ["abcdef12"]


def test_a_separator_inside_a_commit_body_does_not_split_the_record(repo):
    """A commit message can legally contain any byte.

    With single control characters as framing, a body carrying one split a real
    commit into two records or truncated it at the body field — losing exactly
    the provenance lines this tool reads.
    """
    _commit(
        repo, "a.txt",
        "feat: awkward\n\nbody with \x1e and \x1f inside it\n\nGenesis-Session: 0a1b2c3d\n",
    )
    found = sp.scan("main", limit=1)
    assert len(found) == 1, "the commit must stay ONE record"
    assert found[0]["sessions"] == ["0a1b2c3d"], "and its provenance must survive"


def test_a_closed_unmerged_pr_with_a_gone_head_fails_closed(repo, monkeypatch, capsys):
    """A deleted head branch is an unreadable answer, not a successful empty one.

    GitHub returns an empty headRefName for a closed PR whose branch is gone;
    printing the state and exiting 0 reported INCOMPLETE provenance as success —
    while a named branch that cannot be read exits 1. Both must fail alike.
    """
    monkeypatch.setattr(sp, "_pr_lookup", lambda n: ("CLOSED", "", "", ""))
    rc = sp.cmd_pr(4242)
    out, err = capsys.readouterr()
    assert rc == 1, "missing provenance must not exit 0"
    assert "CLOSED without merging" in out
    assert "OPEN" not in out
    assert "gone" in err


def test_an_invalid_revision_is_a_clean_error_not_a_traceback(repo, capsys):
    """`check=True` returns None; calling .strip() on it raised AttributeError."""
    rc = sp.cmd_commit("no-such-rev-exists")
    assert rc == 1
    assert "no such commit" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--limit", "--coverage"])
@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_nonpositive_bound_is_refused(repo, monkeypatch, flag, bad):
    """It previously OMITTED the bound, making the scan silently unbounded while
    every message still described it as capped."""
    argv = ["session_provenance.py", flag, bad]
    if flag == "--limit":
        argv += ["--session", "0a1b2c3d"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        sp.main()
    assert exc.value.code != 0


def test_a_squash_merged_branch_is_not_reported_as_unlanded(repo, monkeypatch, capsys):
    """This repo squash-merges, so a merged branch's commits are not ancestors.

    `main..<branch>` still returns them, and reporting those as unlanded would
    manufacture lost work out of work that shipped — the opposite of the job.
    """
    _commit(repo, "base.txt", "feat: base\n\nGenesis-Session: 11111111\n")
    _git(repo, "checkout", "-q", "-b", "shipped")
    _commit(repo, "f.txt", "feat: work\n\nGenesis-Session: 22222222\n")
    _git(repo, "checkout", "-q", "main")
    # Squash the branch onto main: same content, different commit.
    _git(repo, "merge", "--squash", "shipped")
    _git(repo, "commit", "-q", "-m", "feat: work (#1) squashed")

    assert sp._is_patch_merged("shipped") is True, "precondition: cherry sees it upstream"
    sp.cmd_session("22222222", 50)
    out = capsys.readouterr().out
    assert "unlanded branches" not in out, "a shipped branch must not be called unlanded"


# ─── round-2 review findings, each pinned ────────────────────────────────────


def test_a_multi_commit_squash_is_not_reported_as_unlanded(repo, monkeypatch, capsys):
    """P1: git cherry compares each commit's patch INDEPENDENTLY.

    A squash commit carries the AGGREGATE patch, so a two-commit branch's
    individual patch-ids match nothing upstream and cherry reports both `+` —
    the shipped branch read as unlanded. The merged-PR join closes that gap by
    asking which merged PR actually used this head object.
    """
    _commit(repo, "base.txt", "feat: base\n\nGenesis-Session: 11111111\n")
    _git(repo, "checkout", "-q", "-b", "shipped")
    _commit(repo, "f1.txt", "feat: one\n\nGenesis-Session: 22222222\n")
    _commit(repo, "f2.txt", "feat: two\n\nGenesis-Session: 22222222\n")
    tip = _git(repo, "rev-parse", "shipped").strip()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--squash", "shipped")
    _git(repo, "commit", "-q", "-m", "feat: squashed (#2)")

    # cherry alone must see unique commits — that is the reproduction.
    cherry = _git(repo, "cherry", "main", "shipped")
    assert any(ln.startswith("+") for ln in cherry.splitlines()), \
        "precondition: the aggregate squash defeats per-commit patch-ids"

    import json as _json

    real_run = subprocess.run

    def _fake_gh(args, **kwargs):
        if args[:1] == ["gh"]:
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=_json.dumps([{"number": 2, "headRefOid": tip}]), stderr="",
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(sp.subprocess, "run", _fake_gh)
    assert sp._is_patch_merged("shipped") is True


def test_trailer_matching_requires_a_column_zero_stamp(repo):
    """P2: `\\s` crosses newlines, and an indented example is prose, not a stamp."""
    _commit(
        repo, "a.txt",
        "feat: x\n\n"
        "docs mention `Genesis-Session:\naaaaaaaa` across a line break, and\n"
        "    Genesis-Session: bbbbbbbb  <- an indented markdown example\n\n"
        "Genesis-Session: cccccccc\n",
    )
    assert sp.scan("main", limit=1)[0]["sessions"] == ["cccccccc"]


def test_enrich_checks_the_transcript_id_namespace_too(repo, tmp_path, monkeypatch):
    """P2: foreground sessions are stamped with a CC transcript id, stored in
    `cc_session_id` — the `id` column is Genesis's own UUID namespace."""
    import sqlite3

    db = tmp_path / "g.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE cc_sessions (id TEXT, cc_session_id TEXT, session_type TEXT, "
        "channel TEXT, model TEXT, status TEXT, started_at TEXT, topic TEXT)"
    )
    con.execute(
        "INSERT INTO cc_sessions VALUES ('genesis-uuid-1', 'aaaabbbb-rest', "
        "'terminal', 'cli', 'opus', 'done', '2026-01-01', 'the topic')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(sp, "DB_PATH", db)
    info = sp.enrich("aaaabbbb")
    assert info["db"] is not None, "a cc_session_id match must enrich"
    assert info["db"]["topic"] == "the topic"


def test_enrich_marks_a_broken_store_unknown_not_absent(repo, tmp_path, monkeypatch):
    """P2: a failed lookup is UNKNOWN, not a confident 'no DB row'."""
    db = tmp_path / "g.db"
    db.write_bytes(b"not a sqlite file")
    monkeypatch.setattr(sp, "DB_PATH", db)
    info = sp.enrich("deadbeef")
    desc = sp._describe(info)
    assert "INCOMPLETE" in desc, f"a failed read must not claim absence: {desc}"


def test_ambiguous_transcript_prefixes_are_not_pick_one(repo, tmp_path, monkeypatch):
    """P2: `next(glob(...))` picks an arbitrary filesystem-order match."""
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "deadbeef-aaaa.jsonl").write_text("{}")
    (proj / "deadbeef-bbbb.jsonl").write_text("{}")
    monkeypatch.setattr(sp, "PROJECTS_DIR", tmp_path / "projects")
    info = sp.enrich("deadbeef")
    assert info["transcript"] is None
    assert "ambiguous" in sp._describe(info)


def test_an_open_pr_resolves_by_its_head_object(repo, monkeypatch, capsys):
    """P2: a cross-repo PR's headRefName is unqualified — use headRefOid."""
    _commit(repo, "a.txt", "feat: base\n\nGenesis-Session: 11111111\n")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f.txt", "feat: work\n\nGenesis-Session: 22222222\n")
    oid = _git(repo, "rev-parse", "feature").strip()
    # A same-named local branch pointing somewhere else must not be attributed.
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "someone-elses-feature")
    _commit(repo, "x.txt", "feat: unrelated\n\nGenesis-Session: 99999999\n")
    _git(repo, "branch", "-M", "someone-elses-feature", "feature")  # reuse the name
    monkeypatch.setattr(
        sp, "_pr_lookup", lambda n: ("OPEN", "feature", oid, ""),
    )
    rc = sp.cmd_pr(4242)
    out = capsys.readouterr().out
    assert rc == 0
    assert "22222222" in out
    assert "99999999" not in out, "the same-named local branch is not the PR head"


def test_file_and_line_modes_exist(repo, capsys):
    """P2: the docstring advertises file and line attribution — provide it."""
    sha = _commit(repo, "a.txt", "feat: work\n\nGenesis-Session: 22222222\n")
    assert sp.cmd_file("a.txt", None, 50) == 0
    out = capsys.readouterr().out
    assert "22222222" in out
    assert sp.cmd_file("a.txt", 1, 50) == 0
    out = capsys.readouterr().out
    assert sha[:9] in out


def test_the_pr_limit_flag_is_effective(repo, monkeypatch, capsys):
    """P2: --limit must reach cmd_pr's scan; previously it was advice that
    could never work — the command used a fixed constant."""
    seen: list[int] = []
    real = sp.scan

    def _spy(rev_range, limit=None, path=None):
        seen.append(limit)
        return real(rev_range, limit, path)

    monkeypatch.setattr(sp, "scan", _spy)
    monkeypatch.setattr(sp, "_pr_lookup", lambda n: ("MERGED", "x", "", ""))
    sp.cmd_pr(999999, limit=7)
    assert seen == [7], f"--limit must be the scan bound, got {seen}"


def test_a_capped_ambiguity_count_says_at_least(repo):
    """LIMIT-ed rows can only prove "at least N", and said "N" exactly."""
    assert "at least" in sp._describe({"id": "x", "db": {"ambiguous": 3, "capped": True}})
    assert "at least" not in sp._describe({"id": "x", "db": {"ambiguous": 2, "capped": False}})


# ─── Devin Review round, each pinned ─────────────────────────────────────────


def test_every_terminal_pr_number_is_recorded(repo, monkeypatch, capsys):
    """A subject can end with several `(#N)` refs — one per stacked PR.

    Collapsing to the LAST number made the earlier ones undiscoverable offline:
    the merge commit was in the clone but `--pr <earlier>` could not use it.
    """
    _commit(repo, "a.txt", "feat: stacked (#2152) (#2153)\n\nGenesis-Session: 11111111\n")
    found = sp.scan("main", limit=1)
    assert found[0]["prs"] == [2152, 2153]
    # And a mid-subject ref is prose, not merge attribution.
    _commit(repo, "b.txt", "fix: revisit (#1) in passing\n\nGenesis-Session: 22222222\n")
    assert sp.scan("main", limit=1)[0]["prs"] == []

    def _boom(n):
        raise AssertionError("gh must not be needed — the answer is local")

    monkeypatch.setattr(sp, "_pr_lookup", _boom)
    rc = sp.cmd_pr(2152)
    out = capsys.readouterr().out
    assert rc == 0 and "merged as" in out


def test_a_remote_tracking_ref_queries_its_github_head_name(repo, monkeypatch):
    """`origin/foo` is the LOCAL name; GitHub knows the head as `foo`.

    Asking `--head origin/foo` found nothing, so a merged remote-tracking
    branch read as unlanded. The gh query gets the stripped name; git keeps
    the full ref.
    """
    _git(repo, "checkout", "-q", "-b", "feat/x")
    _commit(repo, "f.txt", "feat: work\n\nGenesis-Session: 22222222\n")
    tip = _git(repo, "rev-parse", "feat/x").strip()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "update-ref", "refs/remotes/origin/feat/x", tip)
    _git(repo, "branch", "-D", "feat/x")

    seen: list[str] = []
    real_run = subprocess.run

    def _fake_gh(args, **kwargs):
        if args[:1] == ["gh"]:
            seen.append(args[args.index("--head") + 1])
            import json as _json
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=_json.dumps([{"number": 7, "headRefOid": tip}]), stderr="",
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(sp.subprocess, "run", _fake_gh)
    assert sp._is_patch_merged("origin/feat/x") is True
    assert seen and all(h == "feat/x" for h in seen), (
        f"gh must be asked for 'feat/x', not the remote-tracking name: {seen}"
    )


def test_an_advanced_branch_only_counts_commits_past_the_merge(repo, monkeypatch, capsys):
    """A squash-merged branch that ADVANCES still carries its shipped originals.

    `main..<branch>` includes them, so a session confined to the merged prefix
    read as unlanded. The scan must be bounded at the latest merged PR head.
    """
    _commit(repo, "base.txt", "feat: base\n\nGenesis-Session: 11111111\n")
    _git(repo, "checkout", "-q", "-b", "advancing")
    _commit(repo, "f1.txt", "feat: one\n\nGenesis-Session: 22222222\n")
    merged_head = _git(repo, "rev-parse", "advancing").strip()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--squash", "advancing")
    _git(repo, "commit", "-q", "-m", "feat: squashed (#9)")
    # The same branch then picks up new, genuinely unlanded work.
    _git(repo, "checkout", "-q", "advancing")
    _commit(repo, "f2.txt", "feat: two\n\nGenesis-Session: 33333333\n")
    _git(repo, "checkout", "-q", "main")

    monkeypatch.setattr(
        sp, "_merged_pr_heads",
        lambda ref: ([merged_head], True) if ref == "advancing" else ([], True),
    )
    rc = sp.cmd_session("22222222", 50)
    out = capsys.readouterr().out
    assert rc == 0
    assert "unlanded branches" not in out, (
        "the merged prefix is not unlanded work for its session"
    )
    rc = sp.cmd_session("33333333", 50)
    out = capsys.readouterr().out
    assert rc == 0
    assert "advancing" in out, "the post-merge suffix IS unlanded work"


def test_file_provenance_follows_a_rename(repo, capsys):
    """Without --follow, `git log -- new.py` stops at the rename commit and the
    file's earlier provenance under its old name is truncated away."""
    _commit(repo, "old.txt", "feat: create\n\nGenesis-Session: 11111111\n")
    _git(repo, "mv", "old.txt", "new.txt")
    _git(repo, "commit", "-q", "-m", "feat: rename\n\nGenesis-Session: 22222222\n")

    rc = sp.cmd_file("new.txt", None, 50)
    out = capsys.readouterr().out
    assert rc == 0
    assert "22222222" in out
    assert "11111111" in out, "the creating session is still the file's provenance"


# ─── Devin Review round 2, each pinned ───────────────────────────────────────


def test_a_tip_is_merged_when_ANY_alias_names_a_merged_pr(repo, monkeypatch, capsys):
    """Dedupe by tip must not discard the NAME GitHub knows.

    `backup` and `origin/feature` share one tip; only `feature` names the merged
    PR. Keeping the first-seen alias and dropping the other made a shipped
    multi-commit squash read as unlanded.
    """
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f1.txt", "feat: one\n\nGenesis-Session: 22222222\n")
    _commit(repo, "f2.txt", "feat: two\n\nGenesis-Session: 22222222\n")
    tip = _git(repo, "rev-parse", "feature").strip()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "backup", tip)
    _git(repo, "update-ref", "refs/remotes/origin/feature", tip)
    _git(repo, "branch", "-D", "feature")

    monkeypatch.setattr(
        sp, "_merged_pr_heads",
        lambda ref: ([tip], True) if sp._gh_head_name(ref) == "feature" else ([], True),
    )
    rc = sp.cmd_session("22222222", 50)
    out = capsys.readouterr().out
    assert rc == 0
    assert "unlanded branches" not in out, (
        "the tip is merged because an ALIAS name proves it — discarding the "
        "alias resurrects shipped work"
    )


def test_a_full_merged_pr_page_marks_the_branch_incomplete(repo, monkeypatch, capsys):
    """A head list returned AT the cap may be missing this branch's tip —
    classifying from it resurrects shipped work."""
    _git(repo, "checkout", "-q", "-b", "reused")
    _commit(repo, "f.txt", "feat: work\n\nGenesis-Session: 22222222\n")
    _git(repo, "checkout", "-q", "main")

    monkeypatch.setattr(sp, "_merged_pr_heads", lambda ref: (["0" * 40], False))
    rc = sp.cmd_session("22222222", 50)
    err = capsys.readouterr().err
    assert rc == 1
    assert "INCOMPLETE" in err
