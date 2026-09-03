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

The crossing needs a command longer than anything real (at 43,480 chars, the longest
of 45,956 real commands, the unbounded parse stays well inside the budget), so it is
reachable by a CRAFTED command rather than by ordinary work — which for a security
guard is the threat model, not a mitigation.

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


#: The longest Bash command in this install's history, over 45,956 real commands.
#: The realistic worst case is this length, NOT the synthetic bomb. (Was 14,682 from
#: a corpus half this size — see test_the_cap_admits_every_real_command for why that
#: number is a trap worth remembering rather than just a stale figure.)
_REAL_MAX_LEN = 43_480


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
    """A 65KB bomb nested 128 deep is refused INSTANTLY — this times the refusal, not
    the parse.

    Deliberately asserts only that a BOUND fired, not WHICH one. The payload is over
    both, and which one reports first is a function of the cap: at 32,768 this was the
    length bound, and raising the cap moved it to depth, failing this test on an
    assertion that was never the point. An adversarial command does not have to
    be refused for the reason a test author happened to have in mind — the two
    dedicated tests below pin each bound separately, on payloads over exactly one.

    The budget it must leave room inside is 5s, not 10s: `bash_safety_hook.sh` is
    registered at 5s and delegates to THREE guards over the same command. A hook
    killed before its `exit 2` does not refuse — it permits."""
    started = time.monotonic()
    segments, blind = sp.analyze_checked(_nested(128, length=65_400))
    elapsed = time.monotonic() - started
    assert blind is not None and blind.bounds_induced
    assert segments == []
    assert elapsed < 1.0, f"took {elapsed:.2f}s against a 5s budget shared by three guards"


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

    MEASURED over 45,956 distinct real Bash commands from this install's history,
    counting the depth ``analyze`` actually recurses to: 87.5% reach depth 0, 11.7%
    depth 1, 0.79% depth 2, and 7 commands reach depth 3. Nothing reaches the bound of 5.

    Those are ``Segment.depth`` units, which is what the bound counts — NOT how deep
    a command looks, since ``bash -c "$(…)"`` descends twice per level. Comparing the
    bound against an eyeballed nesting level is how it would get set too tight.
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
    assert blind is sp._BLIND_OVER_LONG
    assert segments == [], (
        "an over-length command must yield NO segments. A prefix of a shell command "
        "is not a partial parse: truncating mid-string flips the quoting state for "
        "everything after the cut, and the guards match on the resolved exe and argv"
    )
    assert elapsed < 0.5, f"refusing an over-length command took {elapsed:.2f}s"


def test_the_worst_shape_at_the_cap_still_leaves_the_guard_its_clock():
    """The number the cap was chosen from: worst token shape, at the depth bound.

    Calibrated against the TIGHTEST path, not the loosest. `bash_safety_hook.sh` is
    registered at 5s and delegates to THREE guards sequentially over the same command
    — destructive_command_guard, protected_paths_guard, and git_discard_guard, the
    last of which calls `analyze_checked` three times — so one 5s clock can carry
    FIVE full parses plus `git stash create` subprocesses, not the 10s a single guard
    is registered at and not the "two parses" an earlier version of this docstring
    claimed. MEASURED end to end through the real hook, payload inside both bounds so
    nothing short-circuits and allowed by every guard so nothing exits early:
    1.06s at depth 0, 2.16s at depth 3, 3.19s at depth 5.

    This asserts the in-process parse only, so its threshold is deliberately looser
    than the end-to-end figures; it exists to catch a bound raised far enough to
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


def test_the_cap_admits_every_real_command():
    """A cap that fires on real work would be worse than the bug it fixes.

    (This was named "…by_a_wide_margin" while asserting only `longest_real < cap`,
    i.e. any margin down to one character. The name promised what the assertion did
    not check — so the name came down to what is actually enforced rather than the
    assertion being tightened, because a wide margin is NOT wanted here: the cost
    ceiling is the binding constraint and the headroom is deliberately 1.13x.)

    MEASURED over 45,956 distinct real Bash commands from this install's history: the
    longest is 43,480 chars, so the cap is 1.13x anything ever actually run here and
    fires on 0 of them.

    THE FIGURE IN THIS TEST WAS ONCE 14,682, AND THAT IS THE POINT OF THE COMMENT.
    It came from an earlier corpus of 20,514 commands and justified a 32,768 cap as
    "2.2x headroom, 0 fires". The full corpus is 2.2x larger and holds three commands
    ABOVE that cap — all `cat > … <<EOF` here-docs writing review prose, a shape this
    workflow itself generates and the earlier corpus had not yet accumulated. A cap is
    only as good as the corpus it was sized against, and a corpus keeps growing after
    the measurement. Re-derive this number before trusting it; do not raise the cap to
    chase a single outlier without re-running the cost curve.

    The headroom is deliberately modest, because the cost ceiling binds before the
    corpus does. The cap trades against the hook's 5s budget, and the two directions
    are NOT symmetric: over the cap fails CLOSED (a refusal or a prompt), over the
    timeout fails OPEN (the command runs unchecked). Margin is bought on the
    failing-open side and paid for on the failing-closed side.

    Do NOT quote a depth-0 cost figure here to argue the cap is cheap. An earlier
    version of this docstring cited "0.86s at 64 KiB", which is the DEPTH-0 number —
    the same figure `shell_parse` explicitly labels as the mistake that produced an
    over-budget cap. Cost is length x levels; see the grid in `MAX_COMMAND_CHARS`.
    """
    longest_real = 43_480
    assert longest_real < sp.MAX_COMMAND_CHARS, (
        f"cap {sp.MAX_COMMAND_CHARS} is BELOW the longest real command "
        f"({longest_real}) — real work would be refused"
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


# ── The shapes a whole green suite missed ────────────────────────────────────────
#
# Every fixture above tests a command with ONE operation. That is why 1,224 tests and
# 15 verified mutations shipped over three fail-opens: the consumers decide by
# SEARCHING the segment list and treating "not found" as "not present", and a
# single-operation fixture can never reach the not-found branch where the blind-spot
# net lives. Two shapes are needed, and neither existed here before.

_DECOYS = [
    ("push", "git push origin feature", "git push origin +main"),
    ("commit", "git " + "commit" + " -m ok", "git " + "commit" + " --no-verify -m x"),
    ("clean", "git status", "git clean -fd"),
    ("rm", "echo hello", "rm -rf $HOME/genesis"),
]


@pytest.mark.parametrize("label,decoy,hidden", _DECOYS)
def test_a_decoy_cannot_hide_a_second_operation_past_the_bound(label, decoy, hidden):
    """A visible benign operation must NOT make the hidden one disappear.

    THE regression shape. MEASURED against a version that returned the segments a
    bounded parse managed to reach: a visible commit followed by a nested
    ``--no-verify`` one went BLOCK -> ALLOW, and the push equivalent BLOCK -> ask,
    because the decoy filled the segment list so the not-found branch — where the
    blind-spot net lives — was never reached.

    The fix is that a bounded parse returns NOTHING, so this asserts the property
    that makes every consumer correct without per-consumer logic: past the bound the
    parse yields no segments at all, and a decoy cannot be mistaken for the whole
    command. The guard-level suites assert the verdicts; this pins the contract they
    all rest on.
    """
    cmd = f"{decoy} && " + 'bash -c "$(' * 9 + hidden + ')"' * 9
    segs, blind = sp.analyze_checked(cmd)
    assert blind is not None and blind.bounds_induced, (
        f"a {label} command nested past the bound must report a bounds blind spot"
    )
    assert segs == [], (
        f"the {label} decoy leaked {len(segs)} segment(s) from a truncated parse. A "
        "consumer that searches this list would find only the decoy and conclude the "
        "hidden operation is absent — which is the fail-open this bound created"
    )


def test_a_blind_spot_carries_exactly_one_decision():
    """The chokepoint must answer ONE question, and this pins that it still does.

    A per-axis severity field once existed alongside `bounds_induced`, letting the
    length bound "ask" where the depth bound refused. It was wired into guards with
    no ask verdict — where not refusing is permitting — and MEASURED a real
    unrecoverable discard going BLOCK -> ALLOW. Its entire benefit was hypothetical:
    the axis it distinguished fires on 0 of 45,956 real commands.

    A distinction no real input exercises, which six call sites must each choose
    correctly, and whose wrong choice is silent, is a defect generator. So the domain
    is checked for exactly the fields a consumer may branch on: adding another
    decision to a BlindSpot re-opens that generator, and should fail here first.

    Iterates `_ALL_BLIND_SPOTS` so a blind spot added later is covered without anyone
    remembering to extend this — the drift the deleted `kind` string used to cause.
    """
    assert sp._ALL_BLIND_SPOTS, "the domain must not be empty, or this asserts nothing"
    decision_fields = {f for f in sp.BlindSpot._fields if f not in ("cause", "hint")}
    assert decision_fields == {"bounds_induced"}, (
        f"BlindSpot grew a second decision field {decision_fields - {'bounds_induced'}}. "
        "Every consumer must then choose which one to branch on, and a wrong choice "
        "fails OPEN silently — the exact shape that shipped a fail-open before"
    )
    for spot in sp._ALL_BLIND_SPOTS:
        assert isinstance(spot.bounds_induced, bool)
        assert spot.cause and spot.hint, "a blind spot must be actionable, not just true"


@pytest.mark.parametrize("bounds_expected,depth", [(True, 9), (False, 0)])
def test_two_causes_at_once_report_the_bound_not_the_tokenizer(bounds_expected, depth):
    """Precedence, tested where it can actually be wrong: BOTH causes firing at once.

    A trailing apostrophe-comment is valid shell that shlex cannot tokenize, so a
    nested command with one appended is over-nested AND untokenizable simultaneously.
    The first version of this chokepoint reported ``untokenizable`` — the one cause
    every consumer deliberately ignores, since widening to it is a measured
    over-block — so three guards went BLOCK -> ALLOW on a one-word suffix.

    One-cause-at-a-time cells cannot see this: each cause alone was already correct.
    A precedence bug exists ONLY in the intersection, which is exactly the region a
    matrix of single causes leaves untested. The depth-0 case is the control — same
    suffix, no bound — proving the fixture is not simply always-bounds.
    """
    cmd = 'bash -c "$(' * depth + "git clean -fd" + ')"' * depth + " # don" + chr(39) + "t"
    _, blind = sp.analyze_checked(cmd)
    assert blind is not None
    assert sp.untokenizable(cmd), "fixture must genuinely defeat the tokenizer"
    assert blind.bounds_induced is bounds_expected, (
        "a command over a BOUND must report the bound even when it is also "
        "untokenizable — consumers act on bounds_induced, so reporting the "
        "pre-existing cause hands them the one answer they are documented to ignore"
    )
