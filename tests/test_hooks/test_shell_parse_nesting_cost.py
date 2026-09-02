"""``analyze``'s cost must be bounded, because guards run it against a hard timeout.

Every PreToolUse guard is registered with a wall-clock timeout in
``.claude/settings.json`` (10s for the destructive/protected-path guards). A hook
killed at that timeout never reaches its ``exit 2``, and Claude Code reads any
non-2 exit as NON-blocking — so a guard that runs out of time does not refuse the
command, it PERMITS it. Cost is therefore a security property of this parser, not
a performance concern.

``analyze`` recurses once per nesting level and re-scans the remaining text at each
level, so its cost is length x depth — and the padding SHAPE moves it another 3.4x
(comment-padded 3.21s vs quoted-padded 9.64s at the same depth and length). A depth
quoted without a length and a shape is therefore not a measurement.

MEASURED on ``origin/main`` before this fix, end to end against the real
``protected_paths_guard``, on a payload it genuinely blocks (a protected data
directory) padded with a quoted string to 65,400 chars, under the guard's registered
10s timeout:

    depth   0  exit 2 refused   0.46s
    depth  32  exit 2 refused   6.78s
    depth  48  exit 2 refused   9.96s
    depth 128  KILLED at 10s -> non-2 -> the tool call PROCEEDS

The payload must name a genuinely protected path or the test is vacuous: an
unprotected path returns exit 0 at every depth, and timing an allow proves nothing.

The crossing needs a command longer than anything real (at 14,682 chars, the longest
of 20,212 real commands, the unbounded parse peaks at 3.86s), so it is reachable by a
CRAFTED command rather than by ordinary work — which for a security guard is the
threat model, not a mitigation.

The fix bounds the WORK rather than a proxy for it, and — critically — SIGNALS when
it truncated. Silently capping the recursion would trade this fail-open for a worse
one: the guard would stop seeing a nested ``rm`` past the cap and allow it happily.
The signal mirrors ``untokenizable()``, which exists for exactly this reason.

The signal comes from the PARSE, never from a second opinion about the parse. An
earlier draft predicted the depth with a hand-written counter; two shapes defeated it
(quoted parens and ``$(( … ))`` arithmetic each depressed the count below the real
depth while ``analyze`` truncated anyway), which is a silent all-clear on a buried
``rm`` — strictly worse than the timeout it replaced. Those two shapes are pinned
below as regression tests.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))
import shell_parse as sp  # noqa: E402

#: Just inside the length cap, and DERIVED from it rather than written down. A fixture
#: that sits above the cap is refused for LENGTH before the depth bound is ever
#: consulted — which does not merely fail the depth tests, it makes the cost tests
#: VACUOUS: both ends of the ratio become an instant refusal and the comparison
#: passes without measuring anything. Deriving it means lowering the cap can never
#: silently hollow these tests out.
_UNDER_CAP = sp.MAX_COMMAND_CHARS - 128


def _nested(depth: int, length: int = _UNDER_CAP) -> str:
    """A command whose substitutions nest ``depth`` deep at roughly ``length`` chars."""
    return "$(" * depth + "x" * max(length - depth * 3, 1) + ")" * depth


#: The longest Bash command in this install's history, over 20,461 real commands.
#: The realistic worst case is this length, NOT the synthetic bomb.
_REAL_MAX_LEN = 14_682


def test_cost_does_not_grow_with_nesting_depth():
    """The invariant, stated without an arbitrary threshold.

    Absolute timings drift with machine load, so the durable property is the SHAPE:
    past the bound, adding nesting levels must buy the attacker nothing. Before the
    bound, depth 16 -> 4.57s and depth 48 -> 16.48s on this box; a ratio, not a
    constant, is what distinguishes bounded from unbounded.
    """

    def cost(depth: int) -> float:
        started = time.monotonic()
        sp.analyze(_nested(depth))
        return time.monotonic() - started

    shallow, deep = cost(16), cost(128)
    assert deep < shallow * 2.0, (
        f"depth 128 cost {deep:.2f}s vs depth 16 at {shallow:.2f}s — "
        "cost still scales with depth, so the recursion is not bounded"
    )


def test_a_realistic_worst_case_command_is_cheap():
    """The number that actually matters: the longest command ever seen here, nested
    far past the bound. MEASURED at 0.33s — against a 10s guard registration."""
    started = time.monotonic()
    sp.analyze(_nested(128, length=_REAL_MAX_LEN))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"took {elapsed:.2f}s at the real-world maximum length"


def test_an_adversarial_command_stays_inside_the_guard_budget():
    """A 65KB bomb is 4.5x longer than anything real, and now past the length cap —
    so this asserts the REFUSAL is instant rather than that the parse is quick.

    The budget it must leave room inside is 5s, not 10s: `bash_safety_hook.sh` is
    registered at 5s and runs two guards sequentially over the same command. A hook
    killed before its `exit 2` does not refuse — it permits."""
    started = time.monotonic()
    segments, blind = sp.analyze_checked(_nested(128, length=65_400))
    elapsed = time.monotonic() - started
    assert blind is not None and blind.kind == "over_long"
    assert segments == []
    assert elapsed < 1.0, f"took {elapsed:.2f}s against a 5s budget shared by two guards"


@pytest.mark.parametrize("depth", [16, 48, 128])
def test_a_truncated_parse_reports_itself(depth):
    """The half that makes the bound safe rather than a different fail-open.

    A parse that stopped early and says nothing is INDISTINGUISHABLE from a parse
    that found nothing — which is precisely the confusion ``untokenizable`` was
    added to end. A guard must be able to tell "no nested rm" from "I stopped
    looking", and choose to fail closed.
    """
    assert sp.over_nested(_nested(depth)) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /tmp/x",
        "echo $(date)",
        'git commit -m "$(date) $(whoami)"',
        "bash -c 'cd /x && git push'",
        "echo $(echo $(echo hi))",
        " && ".join(f"touch q{i}" for i in range(500)),
        # Sequential substitutions are BREADTH, and breadth costs one level, not one
        # per pair. The hand-written counter this replaced scored six backtick pairs
        # as depth 6 and reported an ordinary free command as over-nested; twenty
        # `$()` in a row it scored correctly, so the two disagreed for no reason a
        # reader could defend.
        " ".join("echo `date`" for _ in range(6)),
        " ".join("echo $(date)" for _ in range(20)),
        # Arithmetic is not a substitution and must not be counted as one.
        "echo $(( 1 + 2 )) $(( 3 + 4 ))",
        # A substitution inside single quotes never runs, so it is not depth.
        "echo '$(not a sub)' $(date)",
    ],
)
def test_ordinary_commands_are_not_reported_as_over_nested(cmd):
    """The control. A bound that fires on real commands would make every guard
    fail closed on ordinary work, which is a worse outcome than the bug.

    MEASURED over 20,212 distinct real Bash commands from this install's history,
    counting the depth ``analyze`` actually recurses to: 83.9% reach depth 0, 15.4%
    depth 1, 0.70% depth 2, and exactly one command reaches depth 3. Nothing reaches
    the bound of 8.
    """
    assert sp.over_nested(cmd) is False


@pytest.mark.parametrize(
    "label,cmd",
    [
        # A `)` inside a double-quoted string does not close a substitution in bash,
        # but a naive counter decrements on it — so eight of them reset the count to
        # zero and the next eight openers looked like depth 8, not 16.
        (
            "quoted parens depress the count",
            "$(" * 8 + '"))))))))" ' + "$(" * 8 + "rm -rf /tmp/x" + ")" * 16,
        ),
        # `$(( … ))` is arithmetic; skipping only its opener leaves two closers that
        # decrement the count by two apiece.
        (
            "arithmetic closers depress the count",
            "$(" * 8 + "$((0))" * 6 + "$(" * 8 + "rm -rf /tmp/x" + ")" * 16,
        ),
    ],
)
def test_the_shapes_that_defeated_a_predicted_depth(label, cmd):
    """The regression that justifies taking the answer from the parse itself.

    An earlier revision predicted ``analyze``'s depth with a separate hand-written
    counter. Both shapes below drove the real parse past the bound while the counter
    stayed under it — so the buried ``rm`` was invisible to ``analyze`` AND
    unreported, a confident wrong all-clear. That is strictly worse than the timeout
    it was replacing, which was at least loud.

    Two parsers means two answers and the gap between them is the vulnerability, so
    this asserts the COUPLING rather than either fact alone: whatever the parse could
    not reach, the signal must own up to.
    """
    segments, blind = sp.analyze_checked(cmd)
    saw_rm = "rm" in {s.exe for s in segments}
    assert blind is not None or saw_rm, (
        f"{label}: the rm is invisible to the parse AND unreported — "
        "exactly the silent all-clear this signal exists to prevent"
    )


def test_the_bound_is_exact_at_its_edge():
    """Off-by-one here is a fail-open on one side and a false block on the other."""
    at_bound = "$(" * sp.MAX_SUBSTITUTION_DEPTH + "rm -rf /tmp/x" + ")" * sp.MAX_SUBSTITUTION_DEPTH
    past = (
        "$(" * (sp.MAX_SUBSTITUTION_DEPTH + 1)
        + "rm -rf /tmp/x"
        + ")" * (sp.MAX_SUBSTITUTION_DEPTH + 1)
    )
    segs_at, blind_at = sp.analyze_checked(at_bound)
    assert blind_at is None, "a command AT the bound is fully parsed, not truncated"
    assert "rm" in {s.exe for s in segs_at}, "the rm at the bound must still be seen"
    assert sp.analyze_checked(past)[1] is not None, "one level past the bound must report"


def test_the_chokepoint_names_which_blind_spot_fired():
    """A guard's message has to send the reader to the right remedy.

    Both causes route to the same fail-closed branch, so a shared message would be
    easy — and would tell someone whose command is merely nested to go re-quote an
    apostrophe. The two must be distinguishable and each must carry a way out.
    """
    over = sp.analyze_checked("$(" * 40 + "rm -rf /tmp/x" + ")" * 40)[1]
    untok = sp.analyze_checked("echo $'don\\'t'")[1]
    assert over is not None and untok is not None
    assert over.cause != untok.cause, "the two blind spots must not share a cause phrase"
    assert over.hint != untok.hint, "each cause needs its own way out, not a shared one"
    assert all(part.cause and part.hint for part in (over, untok)), "neither half may be empty"


def test_length_alone_can_exhaust_a_guard_so_length_alone_is_bounded():
    """The second axis, and the one a depth bound cannot touch.

    Cost is superlinear in the length of a single TOKEN — MEASURED on this parser,
    one long token: 65K 0.30s, 250K 2.65s, 450K 9.14s (0.46 -> 2.03 s per 100K), while
    the same character counts split into many small tokens stay flat at 0.19 s/100K.
    So a 450K command with NO nesting at all costs 9.14s against a 10s registration,
    and every bit of that is spent in the FIRST pass, before any recursion. A depth
    bound does not touch it, and neither can any budget checked between levels.
    """
    over = "echo " + "x" * sp.MAX_COMMAND_CHARS
    started = time.monotonic()
    segments, blind = sp.analyze_checked(over)
    elapsed = time.monotonic() - started
    assert blind is not None and blind.kind == "over_long"
    assert segments == [], (
        "an over-length command must yield NO segments. A prefix of a shell command "
        "is not a partial parse: truncating mid-string flips the quoting state for "
        "everything after the cut, and the guards match on the resolved exe and argv"
    )
    assert elapsed < 0.5, f"refusing an over-length command took {elapsed:.2f}s"


def test_the_worst_shape_at_the_cap_still_leaves_the_guard_its_clock():
    """The number the cap was chosen from: worst token shape, at the depth bound.

    Calibrated against the TIGHTEST path, not the loosest. `bash_safety_hook.sh` is
    registered at 5s and runs TWO guards sequentially over the same command, so the
    real budget is 5s for two parses — not the 10s a single guard is registered at.
    MEASURED end to end through that path at three candidate caps: 16 KiB 0.57s,
    32 KiB 0.95s, 64 KiB 2.33s. The cap is 32 KiB for a 5.3x margin.

    This asserts the in-process parse only, so its threshold is deliberately looser
    than the 0.95s end-to-end figure; it exists to catch a cap raised far enough to
    matter, not to re-measure the shell path.
    """
    worst = (
        "$(" * sp.MAX_SUBSTITUTION_DEPTH
        + "echo "
        + "x" * (sp.MAX_COMMAND_CHARS - 45)
        + ")" * sp.MAX_SUBSTITUTION_DEPTH
    )
    assert len(worst) <= sp.MAX_COMMAND_CHARS, "the fixture must sit INSIDE the cap"
    started = time.monotonic()
    sp.analyze(worst)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, (
        f"the worst command the cap admits took {elapsed:.2f}s; the guards are "
        "registered at 10s and a hook killed at its timeout does not refuse"
    )


def test_the_cap_admits_every_real_command_by_a_wide_margin():
    """A cap that fires on real work would be worse than the bug it fixes.

    MEASURED: the longest of 20,461 distinct real Bash commands from this install's
    history is 14,682 chars, so the cap is 2.2x anything ever actually run here and
    fires on 0 of them.

    The headroom is 2x rather than 4x on purpose. The cap trades against the hook's
    5s budget, and the two directions are NOT symmetric: over the cap fails CLOSED (a
    refusal or a prompt), over the timeout fails OPEN (the command runs unchecked).
    Margin is therefore bought on the failing-open side and paid for on the
    failing-closed side — and at this size it is not actually paid at all.
    """
    longest_real = 14_682
    assert longest_real * 2 < sp.MAX_COMMAND_CHARS, (
        f"cap {sp.MAX_COMMAND_CHARS} leaves less than 4x headroom over the longest "
        f"real command ({longest_real})"
    )
    assert sp.analyze_checked("echo " + "x" * (longest_real - 5))[1] is None


@pytest.mark.parametrize(
    "shape,leaf",
    [
        ("ancestor", "rm -rf $HOME/genesis"),
        ("glob", "rm -rf $HOME/genesis/*"),
        ("exact", "rm -rf $HOME/genesis/data"),
    ],
)
def test_a_bound_must_not_route_a_destructive_rm_to_a_weaker_check(shape, leaf):
    """The acceptance bar that missed a fail-open, widened to the shapes that matter.

    `protected_paths_guard` falls back to a literal substring test when it cannot read
    a command. That test is STRICTLY WEAKER than the parse: the parse catches an
    ANCESTOR of a protected directory (`prot.startswith(expanded + "/")`) and a GLOB
    over its contents (`fnmatch`), while a substring test catches NEITHER — a
    protected path is not a substring of a command naming its parent.

    MEASURED at depth 9, before the guard was changed to refuse outright on a
    bounds-induced blind spot: `rm -rf $HOME/genesis` and `rm -rf $HOME/genesis/*`
    were ALLOWED while base refused them; only the exact path was still caught. The
    original acceptance test used the exact path — the one shape the weak check does
    catch — so it went green over a hole that swallowed the whole install.

    This asserts the PARSER half of the contract: whatever the guard decides, the
    parse must report that it went blind, so the guard is never silently choosing
    between a strong check and a weak one.
    """
    buried = "$(" * (sp.MAX_SUBSTITUTION_DEPTH + 1) + leaf + ")" * (sp.MAX_SUBSTITUTION_DEPTH + 1)
    segments, blind = sp.analyze_checked(buried)
    assert blind is not None, f"{shape}: a buried rm past the bound reported no blind spot"
    assert "rm" not in {s.exe for s in segments}, (
        f"{shape}: fixture no longer buries the rm past the bound, so this test would "
        "pass for the wrong reason"
    )


def test_an_ordinary_command_reports_no_blind_spot():
    """The negative control for the chokepoint: silence when there is nothing to say."""
    segments, blind = sp.analyze_checked('git commit -m "$(date)"')
    assert blind is None
    assert "git" in {s.exe for s in segments}


def test_a_nested_command_inside_the_bound_is_still_seen():
    """The bound must not blind the parser on shapes guards actually rely on.

    A nested ``rm`` at ordinary depth is exactly what ``protected_paths_guard``
    consults ``analyze`` to find; if the fix hid it, the fix would be the bug.
    """
    exes = {s.exe for s in sp.analyze('echo "$(rm -rf /tmp/x)"')}
    assert "rm" in exes


def test_deeply_nested_work_is_not_silently_dropped():
    """Truncation and visibility are the same decision, checked together.

    If the parse stops descending, `over_nested` must be True for the same input —
    otherwise a caller that trusts `analyze` alone gets a clean-looking result that
    omits executing commands.
    """
    cmd = "$(" * 200 + "rm -rf /tmp/x" + ")" * 200
    truncated = sp.over_nested(cmd)
    saw_rm = "rm" in {s.exe for s in sp.analyze(cmd)}
    assert truncated or saw_rm, "the rm was dropped AND the truncation went unreported"
