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
    msg = _ua._deploy_message(FULL_A, FULL_B, 15, 0, ["1573", "1560"])
    assert msg is not None
    assert "15 commits" in msg
    assert FULL_A[:8] in msg and FULL_B[:8] in msg
    assert "#1573" in msg and "#1560" in msg
    assert msg.startswith("[") and msg.endswith("]")


def test_message_singular_commit():
    assert "1 commit " in _ua._deploy_message(FULL_A, FULL_B, 1, 0, [])


def test_message_caps_the_pr_list():
    prs = [str(1500 + i) for i in range(9)]
    msg = _ua._deploy_message(FULL_A, FULL_B, 9, 0, prs)
    assert msg.count("#") == _ua._PR_CAP
    assert f"+{9 - _ua._PR_CAP} more" in msg


def test_message_without_prs_omits_the_clause():
    msg = _ua._deploy_message(FULL_A, FULL_B, 3, 0, [])
    assert "PRs" not in msg and "3 commits" in msg


def test_message_suppressed_when_neither_side_holds_a_unique_commit():
    """The ONLY honest silence left. Two different shas with no unique commit
    on either side is not a state git can produce, so this is a floor rather
    than a case — and it is what stops the three lead clauses below from being
    reachable with nothing to report."""
    assert _ua._deploy_message(FULL_A, FULL_B, 0, 0, []) is None


def test_message_says_MOVED_BACK_when_only_this_session_has_commits():
    """The predecessor read this as "diverged" and stayed silent — the exact
    state where silence costs most. The session is running code the checkout no
    longer contains (a reset or a rollback), so restarting LOSES that code
    rather than catching up to it, and it must not be described as a deploy."""
    msg = _ua._deploy_message(FULL_A, FULL_B, 0, 2, [])
    assert msg is not None
    assert "moved BACK" in msg
    assert "main moved" not in msg, "a rollback announced as a deploy"
    assert "Rollback:" in msg and "Deploy:" not in msg, "the LABEL is a claim too"


def test_message_says_DIVERGED_when_both_sides_have_commits():
    """A rebase or force-move of main. `spawn..head` is NON-empty here, so the
    predecessor's two-dot count reported it as an ordinary "main moved N
    commits" — a fast-forward it had not established (Codex P2, PR #1651)."""
    msg = _ua._deploy_message(FULL_A, FULL_B, 4, 2, ["1500"])
    assert msg is not None
    assert "DIVERGED" in msg
    assert "4 commits landed" in msg and "2 it has" in msg
    assert "main moved" not in msg
    assert "Diverged:" in msg and "Deploy:" not in msg


def test_message_suppressed_on_non_sha():
    # The line is LLM-visible prompt context: never interpolate a non-sha.
    assert _ua._deploy_message("not-a-sha", FULL_B, 3, 0, []) is None
    assert _ua._deploy_message(FULL_A, "../../etc/passwd", 3, 0, []) is None
    assert _ua._deploy_message("", FULL_B, 3, 0, []) is None


def test_message_carries_no_commit_subjects(repo: Path):
    # Privacy floor: shas, a count and PR numbers only.
    head = _commit(repo, "feat: a private looking subject line (#1234)")
    spawn = _git(repo, "rev-parse", "HEAD~1")
    count, only_ours, prs = _ua._deploy_span(repo, spawn, head)
    msg = _ua._deploy_message(spawn, head, count, only_ours, prs)
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
    count, only_ours, prs = _ua._deploy_span(repo, spawn, head)
    assert (count, only_ours) == (3, 0), "a fast-forward has no commits on our side"
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
    _count, _ours, prs = _ua._deploy_span(repo, spawn, head)
    assert prs == ["1350"], "unanchored, this also harvests the quoted #1200"


def test_span_ignores_a_commit_with_no_pr_suffix(repo: Path):
    spawn = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "wip: local commit with no PR")
    head = _commit(repo, "fix: real one (#1200)")
    count, _ours, prs = _ua._deploy_span(repo, spawn, head)
    assert count == 2 and prs == ["1200"]


def test_span_splits_the_sides_when_the_checkout_moved_back(repo: Path):
    """HEAD moved BACKWARDS (a reset/force-move). Nothing landed, and the ONE
    commit this session holds is the whole story — a single count cannot carry
    it, which is why the split exists."""
    spawn = _commit(repo, "feat: later (#1300)")
    _git(repo, "reset", "--hard", "HEAD~1")
    head = _git(repo, "rev-parse", "HEAD")
    assert spawn != head
    landed, only_ours, prs = _ua._deploy_span(repo, spawn, head)
    assert (landed, only_ours) == (0, 1)
    assert prs == [], "PRs are harvested from what LANDED, never from our side"


def test_span_reports_both_sides_of_a_real_divergence(repo: Path):
    """THE case the two-dot range got wrong. Both branches carry unique commits,
    so `spawn..head` is non-empty and the predecessor called it an ordinary
    deploy. MEASURED against real git rather than reasoned: the left/right split
    names which side each commit is on.
    """
    base = _git(repo, "rev-parse", "HEAD")
    spawn = _commit(repo, "feat: ours (#1401)")
    _git(repo, "reset", "--hard", base)
    _commit(repo, "feat: theirs one (#1402)")
    head = _commit(repo, "feat: theirs two (#1403)")
    landed, only_ours, prs = _ua._deploy_span(repo, spawn, head)
    assert (landed, only_ours) == (2, 1)
    assert prs == ["1403", "1402"], "only the landed side contributes PR numbers"


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


def test_emit_speaks_when_the_checkout_moved_back(monkeypatch, capsys, tmp_path, repo):
    """DELIBERATE REVERSAL of the predecessor, and the reason is the finding.

    This state used to be silent, because a `spawn..head` count of 0 was read as
    "diverged — nothing honest to say". It is not diverged: the checkout moved
    BACK, and this session is running a commit the checkout no longer contains.
    That is the state where silence is most expensive — the session cannot see
    that a rollback happened under it, and restarting would DROP the code it is
    running rather than update it.

    So the nudge speaks, and the lead clause says which way it went.
    """
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
    out = capsys.readouterr().out
    assert "moved BACK" in out
    assert "main moved" not in out, "a rollback must not read as a deploy"


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
    count, only_ours, prs = _ua._deploy_span(repo, spawn, head)
    assert (count, only_ours) == (1, 0), "U+2028 must not split one commit into two"
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

    The stamp is written on any CONCLUSIVE observation of head — whether or not
    we choose to speak about it — so a state we deliberately keep quiet does not
    re-pay two git subprocesses on every prompt for the rest of the session.

    Silence is now rare (the moved-back case above SPEAKS), so the suppression is
    injected rather than staged from git: what this pins is the WIRING — that
    stamping does not depend on there being a message — which is precisely what
    a future suppression rule would otherwise quietly break. Contrast
    `test_a_failed_span_read_does_not_silence_the_session_for_good`: a FAILED
    read reaches the stamp by a different path and must NOT stamp.
    """
    spawn, head = _behind(repo)
    monkeypatch.setattr(_ua, "_deploy_message", lambda *a, **k: None)
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


# ── the notice may not claim more than it knows (PR #1651) ──────────────────


def test_the_notice_does_not_claim_hook_CONFIG_is_live():
    """A hook's COMMAND is re-read per invocation, so changed hook CODE is live.
    Claude Code snapshots hook CONFIGURATION at session start, so a commit that
    adds or removes a hook, or changes its matcher or timeout, is NOT live in
    this session — and "hooks/policy are already live" told the session it need
    not act on exactly the change it must act on.

    Pinned as two halves, because the failure mode is dropping the second: the
    line must say which half IS live and which is not.
    """
    msg = _ua._deploy_message(FULL_A, FULL_B, 3, 0, [])
    assert msg is not None
    low = msg.lower()
    assert "hook code is live" in low
    assert "config" in low, "the half that is NOT live must be named"
    assert "hooks/policy are already live" not in low


def test_the_notice_names_the_action_that_restarts_the_server():
    """Restarting the session respawns its MCP children. genesis-server is an
    independent systemd user unit and needs its own command — naming one action
    ("a session restart") for two different things left the server on old code
    while the notice read as though it had been handled.
    """
    msg = _ua._deploy_message(FULL_A, FULL_B, 3, 0, [])
    assert msg is not None
    assert "systemctl --user restart genesis-server" in msg
    assert "MCP needs a session restart" in msg


def test_a_failed_span_read_does_not_silence_the_session_for_good(
    monkeypatch, capsys, tmp_path, repo
):
    """THE one that silences a session forever.

    ``_deploy_span`` returns None only when it could NOT LOOK — git timed out,
    exited nonzero, a ref would not resolve — and signals the conclusive
    "nothing to say" case as a count of 0 instead. Stamping on None recorded a
    failed read as a completed announcement; and because the stamp check runs
    BEFORE the git call, that session then short-circuited on every later
    prompt. One transient timeout, no deploy nudge for the rest of the session's
    life.

    Asserted as the RECOVERY, not just the absent stamp: a later prompt whose
    read succeeds must still speak. That is the property the defect destroyed,
    and an "unstamped" assertion alone would also pass for a nudge that had been
    switched off.

    The CONTROL against this becoming "never stamp" is
    ``test_emit_stamps_even_when_it_stays_silent`` above: a diverged history is
    a conclusive answer and DOES stamp, so the recurring two-subprocess cost is
    still paid only for genuinely unknown state.
    """
    spawn, head = _behind(repo)
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(spawn, SPAWN_AT),
        root=repo,
    )
    real_span = _ua._deploy_span
    monkeypatch.setattr(_ua, "_deploy_span", lambda *a, **k: None)

    _ua._emit_deploy_nudge(SID)
    assert capsys.readouterr().out == "", "a failed read has nothing honest to say"
    assert _ua._deploy_stamped(SID) is None, "a failed read was recorded as told"

    # The transient failure clears; the SAME head must still be announced.
    monkeypatch.setattr(_ua, "_deploy_span", real_span)
    _ua._emit_deploy_nudge(SID)
    out = capsys.readouterr().out
    assert "main moved 2 commits" in out, "one timeout silenced the session for good"
    assert _ua._deploy_stamped(SID) == head
