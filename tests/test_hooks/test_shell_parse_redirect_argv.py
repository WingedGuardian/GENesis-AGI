"""Redirect-target argv/raw separation (follow-up 1c8d0fd2).

A redirect target that carries a command expansion — ``$(…)`` / backtick / ``$'…'`` /
a quoted span — is LEFT in the segment ``raw`` (so ``_substitutions`` keeps a nested
``rm`` visible to protected_paths_guard), but under the pre-fix parser it also leaked
into ``argv`` and spoofed ``git_subcommand`` → the push and commit gates keyed on the
exact subcommand and never fired (fail-OPEN). This locks:

  1. ``argv`` is sourced from a redirect-STRIPPED view → the right subcommand;
  2. ``raw`` (hence ``split_segments``) is byte-identical to before for ordinary
     commands (three gate files match ``Segment.raw`` against a fresh re-split for
     cwd/occurrence tracking — any UNINTENDED drift silently breaks them). The lone
     intended difference: a ``$(…)``/backtick target with an unquoted control operator
     (``;``/``&&``/``||``/``|``) is paren-balanced into one segment — strictly SAFER
     than HEAD (which mis-split it, missing the push/commit) — locked via EXPLOITS;
  3. the nested command stays visible where bash would actually run it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))
import shell_parse as sp  # noqa: E402

_GOLDEN = Path(__file__).resolve().parent / "_split_segments_golden.json"

# ── Byte-identical corpus: split_segments(cmd) MUST NOT change ───────────────
CORPUS: list[str] = [
    # plain targets (stripped from raw today)
    "git 2>/dev/null push",
    "git > out.log push",
    "git >>out.log push",
    "git 2>&1 push",
    "git &>log push",
    "git &>>log push",
    "git >|out push",
    "git <in push",
    "git <<<here push",
    "echo hi 1>o 2>e",
    # expansion targets (retained in raw)
    "git 2>'$(rm x)' push",
    'git 2>"`rm x`" push',
    "git 2>$'a b' push",
    "git 2>a$(echo x)b push",
    "git 2>$(rm x) push",
    "git 22>$(rm x) push",
    "git 3>$(rm x) push",
    "git push2>$(rm x)",
    'git 2>"$(rm x); rm y" push',
    "echo hi 2>$(rm -rf /tmp/x)a\\ b",
    # escaped / concatenated-quote targets (PR-1 word scanner)
    "git 2>err\\ log push",
    'git 2>pre"a b"post push',
    # process substitution (documented gap — target stays)
    "diff <(a) <(b)",
    "tee >(cat) < in",
    # exotic / degenerate
    "git 2>",
    "git 2>'unterminated push",
    'git 2>"$(rm x',
    "git 2>'$(rm x",
    "git 12>&34 push",
    "cat 3< in",
    # expansion-then-fd / param-expansion adjacency (the subtlest raw-drift risk)
    "git 2>$(a)3>b",
    "git 2>$(a)>b push",
    "git 1>$(a) 2>$(b) push",
    "git 2>${VAR} push",
    "git 2>$VAR push",
    'echo "$(rm x)"',
    'git commit -m "2>x"',
    # chained + quoting + nesting
    "ruff check . && pytest -q",
    "a | b ; c && d || e",
    "bash -c 'git 2>/dev/null push'",
    "grep '2>x' file",
    "echo 'a && b' ; echo done",
    'cd /wt && git 2>"$(rm x)" push',
    'git -C /wt merge a && git 2>"$(rm x)" merge b',
    'git 2>"$(rm x)" commit -m a && git commit -m b',
    'git 2>"$(rm x)" commit -n',
    # no redirect at all
    "git push origin main --force",
    "pytest tests/foo.py --basetemp /wt",
]


# ── (1)+(2) byte-identical raw contract ─────────────────────────────────────
def test_split_segments_byte_identical_to_golden():
    """split_segments over the corpus reproduces the frozen pre-change output."""
    golden = json.loads(_GOLDEN.read_text())
    for cmd, expected in golden.items():
        assert sp.split_segments(cmd) == expected, cmd


@pytest.mark.parametrize("cmd", CORPUS)
def test_analyze_depth0_raw_equals_split_segments(cmd):
    """The invariant the cwd/occurrence consumers rely on: a depth-0 Segment.raw is
    exactly an element of a fresh split_segments(cmd)."""
    assert [s.raw for s in sp.analyze(cmd) if s.depth == 0] == sp.split_segments(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "> /tmp/result && git push",
        ">> /tmp/log && git push",
        "cd /x && > /tmp/result && git commit -m x",
    ],
)
def test_a_write_only_redirect_does_not_break_the_split_segments_invariant(cmd):
    """A redirection with NO command writes but executes nothing.

    Bash really does create the target — VERIFIED with `bash -c '> wo.txt'` — so the
    write is recorded, but the segment has no command text. Emitting it from
    `analyze` put an empty string in front of the consumers that match these values
    against a fresh `split_segments`, silently breaking the invariant above; the
    CORPUS contains no such command, so the parametrised test stayed green while the
    contract was violated. The write reaches callers through `write_targets`.
    """
    assert [s.raw for s in sp.analyze(cmd) if s.depth == 0] == sp.split_segments(cmd)
    assert "" not in sp.split_segments(cmd), "an empty segment must never reach consumers"
    assert sp.write_targets(cmd), "the orphan write must still be reachable"


# ── (3) argv is redirect-stripped; nested command stays visible ─────────────
# (cmd, expected git_subcommand, commit_skips_hooks, nested exe bash would run or None)
EXPLOITS = [
    ("git 2>'$(rm x)' push", "push", False, None),  # single-quote: bash does NOT expand
    ('git 2>"`rm x`" push', "push", False, "rm"),  # dquote backtick: expands
    ("git 2>$'a b' push", "push", False, None),  # ANSI-C quote, no substitution
    ("git 2>a$(echo x)b push", "push", False, "echo"),
    ("git 2>$(rm x) push", "push", False, "rm"),
    ("git 22>$(rm x) push", "push", False, "rm"),  # multi-digit fd
    ("git 3>$(rm x) push", "push", False, "rm"),  # non-1/2 fd
    ('git 2>"$(rm x); rm y" push', "push", False, "rm"),  # $() closes at first )
    ("git 2>'$(rm x)' commit --no-verify", "commit", True, None),
    ('git 2>"`rm x`" commit -n', "commit", True, "rm"),
    # $(...) target with an UNQUOTED control operator: the pre-#1455 scanner mis-split
    # on the operator (`git  $(a` + `b) push`) so git_subcommand saw '$(a' and the push
    # gate MISSED it; the paren-balancer keeps it one segment → correct subcommand, and
    # a/b still surface via _substitutions. Strictly SAFER than HEAD — locked here so a
    # future scanner "simplification" cannot silently re-open this fail-open class.
    ("git >|$(a; b) push", "push", False, "a"),
    ("git 2>$(a && b) commit --no-verify", "commit", True, "a"),
    ("git 2>$(a | b) push", "push", False, "a"),
]


@pytest.mark.parametrize("cmd,sub,skips,nested_exe", EXPLOITS)
def test_redirect_target_does_not_spoof_subcommand(cmd, sub, skips, nested_exe):
    segs = sp.analyze(cmd)
    top = [s for s in segs if s.depth == 0]
    git_seg = next(s for s in top if s.exe == "git")
    assert sp.git_subcommand(git_seg.argv) == sub, git_seg.argv
    assert sp.commit_skips_hooks(git_seg.argv) is skips
    exes = {s.exe for s in segs}
    if nested_exe:
        assert nested_exe in exes, f"nested {nested_exe} must stay visible: {exes}"
    else:
        # a single-quoted / ANSI-C target is not expanded by bash → no rm/echo runs
        assert "rm" not in exes


def test_fd_digit_word_boundary_preserved():
    # a digit that ENDS a word is part of that word, then a redirect on it:
    # `git push2>$(rm x)` → subcommand 'push2' (NOT 'push', NOT a residue token).
    seg = next(s for s in sp.analyze("git push2>$(rm x)") if s.exe == "git")
    assert sp.git_subcommand(seg.argv) == "push2"
    assert "rm" in {s.exe for s in sp.analyze("git push2>$(rm x)")}


@pytest.mark.parametrize(
    "cmd",
    [
        'git 2>"$(rm x',  # unterminated dquote+paren
        "git 2>'$(rm x",  # unterminated squote
        "git 2>`rm x push",  # unterminated backtick
    ],
)
def test_unterminated_target_no_crash_no_spoof(cmd):
    segs = sp.analyze(cmd)  # must not raise
    git_seg = next((s for s in segs if s.exe == "git"), None)
    if git_seg is not None:
        sub = sp.git_subcommand(git_seg.argv)
        # a broken/unterminated target must not resolve to a real push/commit, and must
        # not leave an expansion residue masquerading as the subcommand.
        assert sub not in ("push", "commit")
        assert sub is None or ("$" not in sub and "`" not in sub)


# ── (§6) three-consumer raw-equality through redirect-carrying compounds ─────
@pytest.mark.parametrize(
    "cmd,pred",
    [
        ('cd /wt && git 2>"$(rm x)" push', lambda s: sp.git_subcommand(s.argv) == "push"),
        (
            'git 2>"$(rm x)" commit -m a && git commit -m b',
            lambda s: sp.git_subcommand(s.argv) == "commit",
        ),
        ('git 2>"$(rm x)" commit -n', lambda s: sp.git_subcommand(s.argv) == "commit"),
    ],
)
def test_consumer_raw_is_resplittable(cmd, pred):
    """Every matched Segment.raw is an element of a fresh split_segments(cmd) — the
    exact property _effective_cwd / occurrence-indexing / invalidate loops depend on."""
    fresh = sp.split_segments(cmd)
    for s in sp.analyze(cmd):
        if s.depth == 0 and s.exe == "git" and pred(s):
            assert s.raw in fresh


def test_dual_buffer_no_desync_under_fd_expansion_adjacency():
    """Stress the mirrored fd-digit deletion against expansion-target divergence (the
    argv_buf/raw_buf suffix-identical invariant): many spaced $()-redirects then push.
    argv must exclude every target (→ 'push'), all bodies stay visible, raw resplittable."""
    cmd = "git 1>$(a) 2>$(b) 3>$(c) 4>$(d) push"
    seg = next(s for s in sp.analyze(cmd) if s.exe == "git")
    assert sp.git_subcommand(seg.argv) == "push"
    assert {"a", "b", "c", "d"} <= {s.exe for s in sp.analyze(cmd)}
    assert [s.raw for s in sp.analyze(cmd) if s.depth == 0] == sp.split_segments(cmd)


# ── Codex P1 (2026-08-26): quote-aware $() target boundary ────────────────────
# The $()/backtick target balancer must respect quotes/escapes: a `)` inside a
# single/double-quoted span (or escaped) inside the $() body is DATA, not the
# close. The quote-blind balancer closed the $() early, then the outer word-scan
# treated the trailing quote as an opener and SWALLOWED a following `&& rm`/`&&
# git push`/`commit -n` into the redirect target — analyze() emitted no segment
# for the following command, blinding the destructive/push/commit guards. Each
# case must keep the following command a SEPARATE, visible depth-0 segment.
# (cmd, following exe, its git subcommand or None, commit_skips_hooks)
QUOTED_PAREN_FOLLOWING_CMD = [
    ("echo ok 2>$(printf ')') && rm /x/y", "rm", None, False),  # Codex single-quote form
    ('echo ok 2>$(echo ")") && rm /x/y', "rm", None, False),  # double-quote form
    ("echo ok 2>$(printf ')') && git push origin main --force", "git", "push", False),
    ("echo ok 2>$(printf ')') && git commit --no-verify", "git", "commit", True),
]


@pytest.mark.parametrize("cmd,exe,sub,skips", QUOTED_PAREN_FOLLOWING_CMD)
def test_quoted_paren_target_does_not_swallow_following_command(cmd, exe, sub, skips):
    segs = sp.analyze(cmd)
    hit = next((s for s in segs if s.depth == 0 and s.exe == exe), None)
    assert hit is not None, (
        f"{exe!r} segment swallowed by redirect target: {[(s.exe, s.argv) for s in segs]}"
    )
    if sub is not None:
        assert sp.git_subcommand(hit.argv) == sub
        assert sp.commit_skips_hooks(hit.argv) is skips


def test_rm_inside_quoted_paren_sub_still_surfaces():
    """The already-safe direction stays safe: an rm INSIDE a $() whose operand
    carries a quoted `)` still surfaces via _substitutions, and the trailing
    subcommand still reads correctly."""
    cmd = 'git 2>$(rm ")" /x) push'
    segs = sp.analyze(cmd)
    assert "rm" in {s.exe for s in segs}
    git_seg = next(s for s in segs if s.exe == "git")
    assert sp.git_subcommand(git_seg.argv) == "push"


def test_quoted_paren_target_golden_raw_still_resplittable():
    """raw invariant holds through the quoted-paren target: depth-0 Segment.raw ==
    a fresh split_segments (the cwd/occurrence consumers depend on it)."""
    for cmd, *_ in QUOTED_PAREN_FOLLOWING_CMD:
        assert [s.raw for s in sp.analyze(cmd) if s.depth == 0] == sp.split_segments(cmd), cmd
