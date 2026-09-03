"""Tests for the deploy nudge in genesis_urgent_alerts.

The nudge runs on EVERY prompt but must be SILENT unless main has actually
moved under this session — so the omission matrix (at head / already told /
diverged history / no spawn record / no session pid / git unavailable /
unwritable stamp) is the contract that keeps it free.

Detection is OBSERVED HEAD DRIFT. The predecessor keyed on the newest
`update_history` row, which measured 10.9% coverage of real HEAD movements on a
live install and was structurally silent for every session spawned after the
last `scripts/update.sh` run. `test_acceptance_*` below replays that exact live
false negative.

The git-touching helpers are tested against a REAL temporary repository rather
than a monkeypatched subprocess: the thing most likely to be wrong here is the
git invocation itself, and a faked one proves nothing about it.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"

_ua_spec = importlib.util.spec_from_file_location(
    "genesis_urgent_alerts", _SCRIPTS_DIR / "genesis_urgent_alerts.py"
)
_ua = importlib.util.module_from_spec(_ua_spec)
_ua_spec.loader.exec_module(_ua)

SID = "sid-deploy-1"
PID = 424242
PROC_START = "2026-08-15T00:00:00+00:00"
SPAWN_AT = "2026-08-15T00:00:05+00:00"
FULL_A = "176f5b3b8bfd1e11907fafc92967fe1e956330b1"
FULL_B = "87590955c0ffee11907fafc92967fe1e956330b1"


# ── a real git repo, so the git helpers are exercised for real ──────────────


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo` with a hermetic identity (no global config needed)."""
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=T",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _commit(repo: Path, subject: str) -> str:
    (repo / "f.txt").write_text(subject)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose main line carries squash-merge-style `(#N)` subjects."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _commit(r, "chore: base (#1000)")
    return r


# ── _deploy_message (pure: wording + every suppression rule) ────────────────


def test_message_names_count_shas_and_prs():
    msg = _ua._deploy_message(FULL_A, FULL_B, 15, ["1573", "1560"])
    assert msg is not None
    assert "15 commits" in msg
    assert FULL_A[:8] in msg and FULL_B[:8] in msg
    assert "#1573" in msg and "#1560" in msg
    assert msg.startswith("[") and msg.endswith("]")


def test_message_singular_commit():
    assert "1 commit " in _ua._deploy_message(FULL_A, FULL_B, 1, [])


def test_message_caps_the_pr_list():
    prs = [str(1500 + i) for i in range(9)]
    msg = _ua._deploy_message(FULL_A, FULL_B, 9, prs)
    assert msg.count("#") == _ua._PR_CAP
    assert f"+{9 - _ua._PR_CAP} more" in msg


def test_message_without_prs_omits_the_clause():
    msg = _ua._deploy_message(FULL_A, FULL_B, 3, [])
    assert "PRs" not in msg and "3 commits" in msg


def test_message_suppressed_on_zero_count():
    # A diverged history (HEAD not a descendant of spawn) is not a deploy.
    assert _ua._deploy_message(FULL_A, FULL_B, 0, []) is None


def test_message_suppressed_on_non_sha():
    # The line is LLM-visible prompt context: never interpolate a non-sha.
    assert _ua._deploy_message("not-a-sha", FULL_B, 3, []) is None
    assert _ua._deploy_message(FULL_A, "../../etc/passwd", 3, []) is None
    assert _ua._deploy_message("", FULL_B, 3, []) is None


def test_message_carries_no_commit_subjects(repo: Path):
    # Privacy floor: shas, a count and PR numbers only.
    head = _commit(repo, "feat: a private looking subject line (#1234)")
    spawn = _git(repo, "rev-parse", "HEAD~1")
    count, prs = _ua._deploy_span(repo, spawn, head)
    msg = _ua._deploy_message(spawn, head, count, prs)
    assert "private looking" not in msg
    assert "#1234" in msg


# ── _current_head (real git) ────────────────────────────────────────────────


def test_current_head_reads_the_real_head(repo: Path):
    assert _ua._current_head(repo) == _git(repo, "rev-parse", "HEAD")


def test_current_head_survives_a_packed_ref(repo: Path):
    """A ref may be loose, packed, or both.

    A REGRESSION PIN, not a proof: it cannot fail for any subprocess-based
    implementation. It exists so a future rewrite to a hand-rolled `.git/HEAD` +
    loose-ref read — which works until the first `git gc`, then fails silently —
    goes red here instead of in production.
    """
    expected = _git(repo, "rev-parse", "HEAD")
    _git(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "main").exists()
    assert _ua._current_head(repo) == expected


def test_current_head_none_outside_a_repo(tmp_path: Path):
    assert _ua._current_head(tmp_path / "nope") is None


# ── _deploy_span (real git) ─────────────────────────────────────────────────


def test_span_counts_commits_and_collects_prs(repo: Path):
    spawn = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "fix: one (#1101)")
    _commit(repo, "feat: two (#1102)")
    head = _commit(repo, "fix: three (#1103)")
    count, prs = _ua._deploy_span(repo, spawn, head)
    assert count == 3
    assert prs == ["1103", "1102", "1101"]  # newest first, as git log orders


def test_span_pr_regex_is_anchored_to_end_of_line(repo: Path):
    """Only the TRAILING `(#N)` — the squash-merge suffix — is the PR number.

    The input matters: `(supersedes #1446) (#1577)` (a real shape on main) does
    NOT exercise the `$` anchor, because `(supersedes ` has no `(#` for the
    pattern to match in the first place — a test using it passes unanchored and
    proves nothing. A quoted subject that ALREADY contains `(#N)` is what makes
    the anchor load-bearing, so that is what this pins.
    """
    spawn = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, 'revert: "feat: the thing (#1200)" (#1350)')
    _count, prs = _ua._deploy_span(repo, spawn, head)
    assert prs == ["1350"], "unanchored, this also harvests the quoted #1200"


def test_span_ignores_a_commit_with_no_pr_suffix(repo: Path):
    spawn = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "wip: local commit with no PR")
    head = _commit(repo, "fix: real one (#1200)")
    count, prs = _ua._deploy_span(repo, spawn, head)
    assert count == 2 and prs == ["1200"]


def test_span_zero_when_history_diverged(repo: Path):
    """HEAD moved BACKWARDS (a reset/force-move): the shas differ but nothing
    landed. Zero is the signal the caller suppresses on."""
    spawn = _commit(repo, "feat: later (#1300)")
    _git(repo, "reset", "--hard", "HEAD~1")
    head = _git(repo, "rev-parse", "HEAD")
    assert spawn != head
    count, prs = _ua._deploy_span(repo, spawn, head)
    assert count == 0 and prs == []


def test_span_none_on_unknown_revision(repo: Path):
    assert _ua._deploy_span(repo, "0" * 40, _git(repo, "rev-parse", "HEAD")) is None


def test_span_none_outside_a_repo(tmp_path: Path):
    assert _ua._deploy_span(tmp_path / "nope", FULL_A, FULL_B) is None


# ── the stamp (once per deploy, per session) ────────────────────────────────


def test_stamp_absent_reads_as_not_yet_told(monkeypatch, tmp_path):
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    assert _ua._deploy_stamped(SID) is None


def test_stamp_roundtrips(monkeypatch, tmp_path):
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    assert _ua._record_deploy_notice(SID, FULL_B) is True
    assert _ua._deploy_stamped(SID) == FULL_B


def test_stamp_unreadable_reads_as_not_yet_told(monkeypatch, tmp_path):
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    marker = tmp_path / "sessions" / SID / _ua._DEPLOY_STAMP
    marker.parent.mkdir(parents=True)
    marker.mkdir()  # a directory where a file is expected → OSError on read
    assert _ua._deploy_stamped(SID) is None


def test_record_returns_false_when_unwritable(monkeypatch, tmp_path):
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    blocker = tmp_path / "sessions"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory")  # mkdir under it raises
    assert _ua._record_deploy_notice(SID, FULL_B) is False


def test_stamp_rejects_a_traversal_session_id(monkeypatch, tmp_path):
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    assert _ua._record_deploy_notice("../../escape", FULL_B) is False
    assert _ua._deploy_stamped("../../escape") is None


# ── _emit_deploy_nudge (integration; spawn plane + /proc monkeypatched) ─────


def _wire(monkeypatch, tmp_path, *, pid, slots, ident, root):
    """Point every non-git IO seam at fakes/tmp. Git stays REAL against `root`."""
    import genesis.observability.cc_slots as cc_slots
    import genesis.observability.mcp_spawn_store as sp

    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    monkeypatch.setattr(_ua, "_claude_ancestor_pid", lambda: pid)
    monkeypatch.setattr(sp, "enumerate_spawn_slots", lambda: slots)
    monkeypatch.setattr(sp, "read_spawn_identity", lambda *a, **k: ident)
    monkeypatch.setattr(cc_slots, "read_proc_start_iso", lambda _p: PROC_START)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(root))


def _behind(repo: Path):
    """Return (spawn, head) with main advanced 2 commits past spawn."""
    spawn = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "fix: alpha (#1401)")
    head = _commit(repo, "feat: beta (#1402)")
    return spawn, head


def test_emit_speaks_once_then_stays_silent(monkeypatch, capsys, tmp_path, repo):
    spawn, head = _behind(repo)
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(spawn, SPAWN_AT),
        root=repo,
    )
    _ua._emit_deploy_nudge(SID)
    out = capsys.readouterr().out
    assert "main moved 2 commits" in out
    assert "#1402" in out and "#1401" in out
    assert head[:8] in out
    # Same HEAD on the next prompt → silent (stamped).
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == ""


def test_emit_speaks_again_when_head_moves_again(monkeypatch, capsys, tmp_path, repo):
    spawn, _head = _behind(repo)
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(spawn, SPAWN_AT),
        root=repo,
    )
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out != ""
    _commit(repo, "fix: a further deploy (#1403)")
    _ua._emit_deploy_nudge(SID)
    out = capsys.readouterr().out
    assert "main moved 3 commits" in out and "#1403" in out


def test_emit_silent_when_session_is_at_head(monkeypatch, capsys, tmp_path, repo):
    head = _git(repo, "rev-parse", "HEAD")
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(head, SPAWN_AT),
        root=repo,
    )
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == ""


def test_emit_silent_when_history_diverged(monkeypatch, capsys, tmp_path, repo):
    """Shas differ but HEAD is not a descendant — a reset, not a deploy."""
    spawn = _commit(repo, "feat: to be rolled back (#1500)")
    _git(repo, "reset", "--hard", "HEAD~1")
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(spawn, SPAWN_AT),
        root=repo,
    )
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == ""


def test_emit_silent_when_the_path_is_not_a_repo(monkeypatch, capsys, tmp_path):
    spawn = FULL_A
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(spawn, SPAWN_AT),
        root=tmp_path / "not-a-repo",
    )
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == ""


def test_emit_silent_without_a_claude_pid(monkeypatch, capsys, tmp_path, repo):
    spawn, _ = _behind(repo)
    _wire(
        monkeypatch,
        tmp_path,
        pid=None,
        slots=[("1", PID, SPAWN_AT)],
        ident=(spawn, SPAWN_AT),
        root=repo,
    )
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == ""


def test_emit_silent_when_pid_not_in_spawn_plane(monkeypatch, capsys, tmp_path, repo):
    spawn, _ = _behind(repo)
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", 999999, SPAWN_AT)],  # a DIFFERENT session's pid
        ident=(spawn, SPAWN_AT),
        root=repo,
    )
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == ""


def test_emit_silent_on_unvalidated_ident(monkeypatch, capsys, tmp_path, repo):
    _behind(repo)
    _wire(monkeypatch, tmp_path, pid=PID, slots=[("1", PID, SPAWN_AT)], ident=None, root=repo)
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == ""


def test_emit_suppressed_when_stamp_unwritable(monkeypatch, capsys, tmp_path, repo):
    """If the stamp cannot persist, suppress rather than repeat every prompt."""
    spawn, _ = _behind(repo)
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(spawn, SPAWN_AT),
        root=repo,
    )
    monkeypatch.setattr(_ua, "_record_deploy_notice", lambda *a, **k: False)
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == ""


# ── ACCEPTANCE: the live false negative this work exists to fix ─────────────


def test_acceptance_replays_the_measured_live_false_negative(monkeypatch, capsys, tmp_path, repo):
    """Slot 4, measured live 2026-09-02.

    The session's MCP spawned at 85f91765 and main then advanced 15 commits to
    dd90bb39, while the newest `update_history` row was 84c7259d completed
    2026-08-31T21:48Z — BEFORE the spawn. The old verdict required the recorded
    deploy to postdate the spawn, so it returned False and the session was never
    told it was 15 commits behind.

    Reconstructed with the real PR numbers from that range. The assertion is
    that the session is now TOLD, and told what landed.
    """
    spawn = _git(repo, "rev-parse", "HEAD")
    for pr in (
        "1565",
        "1575",
        "1604",
        "1581",
        "1602",
        "1587",
        "1589",
        "1603",
        "1608",
        "1588",
        "1563",
        "1577",
        "1560",
        "1573",
    ):
        _commit(repo, f"fix: landed while the session ran (#{pr})")
    head = _commit(repo, "feat: the fifteenth (#1585)")

    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("4", PID, SPAWN_AT)],
        ident=(spawn, SPAWN_AT),
        root=repo,
    )
    _ua._emit_deploy_nudge(SID)
    out = capsys.readouterr().out

    assert "main moved 15 commits" in out, out
    assert spawn[:8] in out and head[:8] in out
    assert "#1585" in out  # newest first, so the most recent merge is named
    assert "+10 more" in out  # 15 PRs, capped at 5
    assert "session restart" in out


# ── boundary validation: a ref reaches git ARGV, not just message text ──────


def test_current_head_rejects_a_non_sha(monkeypatch, repo: Path):
    """The guard on git's OWN stdout is load-bearing, not belt-and-braces.

    `head` becomes half of the `<spawn>..<head>` argv operand in _deploy_span,
    so it must be validated where it ENTERS, not only in _deploy_message where
    it is printed. Real git cannot produce this, so the subprocess is faked —
    the assertion is about OUR guard, not about the fake.
    """
    import subprocess as _sp

    class _Proc:
        returncode = 0
        stdout = "--output=/tmp/pwned\n"
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(_ua.subprocess, "run", lambda *a, **k: _Proc())
    assert _ua._current_head(repo) is None


def test_span_refuses_an_option_shaped_ref_and_writes_no_file(repo: Path, tmp_path: Path):
    """git reads a leading-dash operand as an OPTION.

    MEASURED with git 2.43.0: `git log --oneline "--output=<p>..HEAD"` creates
    and truncates `<p>..HEAD`, exits 0, and prints nothing — an arbitrary-write
    primitive that then reads as a silent no-op. _deploy_span must refuse before
    git is invoked at all.
    """
    target = tmp_path / "must_not_appear"
    head = _git(repo, "rev-parse", "HEAD")
    assert _ua._deploy_span(repo, f"--output={target}", head) is None
    assert _ua._deploy_span(repo, head, f"--output={target}") is None
    # git would write `<target>..<the other ref>` — the ref is interpolated into
    # the operand, so glob rather than guessing the exact suffix. Asserting one
    # hand-written filename here passed vacuously against a path git never used.
    assert not list(tmp_path.glob("must_not_appear*")), "git wrote through --output"


def test_emit_refuses_an_option_shaped_spawn_commit(monkeypatch, capsys, tmp_path, repo):
    """Same guard, reached through the real emit path from the file plane."""
    target = tmp_path / "must_not_appear_emit"
    _behind(repo)
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(f"--output={target}", SPAWN_AT),
        root=repo,
    )
    _ua._emit_deploy_nudge(SID)
    # Silence alone is NOT proof: with the guard removed the zero-count rule also
    # silences this (an --output run prints nothing). The FILE is the discriminator.
    assert capsys.readouterr().out == ""
    assert not list(tmp_path.glob("must_not_appear_emit*")), "git wrote through --output"


def test_span_counts_by_newline_only(repo: Path):
    """str.splitlines() also breaks on U+2028/U+2029/\\x0b/\\x85, which a commit
    subject may contain — that would inflate the count (the one
    author-influenceable value in an LLM-visible line) and lose that PR number.
    """
    spawn = _git(repo, "rev-parse", "HEAD")
    sep = chr(0x2028)  # LINE SEPARATOR: splitlines() breaks on it, split("\n") does not
    head = _commit(repo, f"fix: subject with a{sep}line separator inside (#1400)")
    count, prs = _ua._deploy_span(repo, spawn, head)
    assert count == 1, "U+2028 must not split one commit into two"
    assert prs == ["1400"]


# ── stamp robustness ───────────────────────────────────────────────────────


def test_stamp_non_utf8_reads_as_not_yet_told(monkeypatch, tmp_path):
    """A torn/garbled stamp must not silence the session permanently.

    read_text raises UnicodeDecodeError on non-UTF-8 bytes, and that is a
    ValueError, NOT an OSError — catching only OSError let it escape to the
    emit-level blanket handler, which returns BEFORE re-stamping.
    """
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    marker = tmp_path / "sessions" / SID / _ua._DEPLOY_STAMP
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"\xff\xfe\x00garbage")
    assert _ua._deploy_stamped(SID) is None


def test_stamp_write_is_atomic_leaving_no_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    assert _ua._record_deploy_notice(SID, FULL_B) is True
    sdir = tmp_path / "sessions" / SID
    assert (sdir / _ua._DEPLOY_STAMP).read_text() == FULL_B
    # no temp files left behind
    assert [p.name for p in sdir.iterdir()] == [_ua._DEPLOY_STAMP]


def test_emit_stamps_even_when_it_stays_silent(monkeypatch, capsys, tmp_path, repo):
    """A suppressed state must cost ONE rev-parse on later prompts, not two git
    subprocesses forever.

    Diverged history: the silence is intended, the recurring cost is not. The
    stamp is written on any conclusive observation of head, so the next prompt
    short-circuits at the stamp check.
    """
    spawn = _commit(repo, "feat: to be rolled back (#1500)")
    _git(repo, "reset", "--hard", "HEAD~1")
    head = _git(repo, "rev-parse", "HEAD")
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(spawn, SPAWN_AT),
        root=repo,
    )
    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == ""
    assert _ua._deploy_stamped(SID) == head, "silent, but stamped — cost must not recur"
