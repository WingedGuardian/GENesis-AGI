"""Locks for the post-update health gate in scripts/update.sh (PR #1715 round 1).

update.sh cannot be executed in CI, so these follow the house pattern from
test_update_robustness.py: EXTRACT the real block from the shipped script and
run that text under bash. Extracting rather than retyping matters here — a
retyped copy would test the copy, and every one of these findings is about what
the shipped text does with a hostile value.

Findings locked (Codex P2, PR #1715 round 1):
  * the window is parsed as BOUNDED BASE-10 — `0600` is 600 seconds, not 384;
    `0180` does not abort the script as an invalid octal literal; and an
    oversized digit string cannot wrap the deadline into the past.
  * the deadline is measured on a MONOTONIC clock, so a clock step mid-deploy
    can neither expire a healthy slow boot early nor stretch the window.
  * a MOMENT WHERE THE UNIT STATE CANNOT BE READ is not treated as death. The
    default branch ends the wait; an unreadable state must keep it going.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

_WINDOW_START = "    HEALTH_GUARDIAN_COVER="
_WINDOW_END = "    HEALTH_DEADLINE="
_CASE_START = '        case "$HEALTH_UNIT_STATE" in'
_CASE_END = "        esac"


def _extract(text: str, start: str, end: str) -> str:
    """Lines from the one starting with *start* through the one equal to *end*."""
    lines = text.splitlines()
    begin = next((i for i, ln in enumerate(lines) if ln.startswith(start)), None)
    assert begin is not None, f"anchor not found in update.sh: {start!r}"
    stop = next(
        (
            i
            for i in range(begin, len(lines))
            if lines[i].rstrip() == end.rstrip() or lines[i].startswith(end)
        ),
        None,
    )
    assert stop is not None, f"end anchor not found after {start!r}: {end!r}"
    block = "\n".join(lines[begin : stop + 1])
    assert block.strip(), "extracted an empty block — the test would prove nothing"
    return block


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


@pytest.fixture(scope="module")
def window_block(text: str) -> str:
    return _extract(text, _WINDOW_START, _WINDOW_END)


@pytest.fixture(scope="module")
def state_case(text: str) -> str:
    return _extract(text, _CASE_START, _CASE_END)


def _resolve_window(block: str, value: str | None) -> str:
    """Run the real parsing block with an override; return the RAW string.

    Returning a string — and comparing strings — is the point. `int()` here was
    a real defect: it performs exactly the normalisation the shell is supposed
    to perform, so deleting the `10#` conversion entirely and letting the
    literal "0600" flow through untouched still yielded 600 and PASSED.

    Runs under the same `set -Eeuo pipefail` the script sets, for the same kind
    of reason: without those options the invalid-octal ABORT that one of these
    findings is about cannot happen, so the test would be exercising a shell the
    deploy never runs under.
    """
    setter = (
        "unset GENESIS_DEPLOY_HEALTH_WINDOW_SECS"
        if value is None
        else f"export GENESIS_DEPLOY_HEALTH_WINDOW_SECS={value!r}"
    )
    # The cap DERIVES from these, which the real script sets far earlier.
    preamble = "set -Eeuo pipefail\nGUARDIAN_PAUSE_TTL=1800\nGUARDIAN_PAUSE_RENEW_MAX=4\n"
    script = f'{preamble}{setter}\n{block}\nprintf "%s" "$HEALTH_WINDOW_SECS"\n'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, (
        f"the block aborted (rc={out.returncode}) on {value!r}: {out.stderr[-300:]}"
    )
    assert out.stdout.strip(), f"block produced no value (stderr: {out.stderr[:200]})"
    # The cap prints an operator NOTE on stdout, like every other deploy
    # progress line, so take the printed VALUE — the final line — not the lot.
    return out.stdout.strip().splitlines()[-1].strip()


@pytest.mark.parametrize(
    "override,expected,why",
    [
        (None, "900", "unset uses the documented default"),
        ("0600", "600", "a leading zero is decimal, not octal 384"),
        ("0180", "180", "0180 is not a fatal invalid-octal literal"),
        ("08", "180", "08/09 stay invalid octal even after the zero-strip"),
        ("abc", "900", "non-numeric falls back rather than breaking the gate"),
        ("", "900", "empty falls back"),
        ("1800", "1800", "an ordinary value survives untouched"),
        ("10", "180", "below the floor clamps up to the old gate"),
        ("000000", "180", "all-zeros clamps up, never a zero-length window"),
        # PADDING is not magnitude. Bounding the length before stripping zeros
        # turned a padded three minutes into the ceiling — the operator asked
        # for the floor and got the cap. `000000` cannot expose this: it is the
        # one padded value whose stripped form is also clamped.
        ("0000180", "180", "a zero-padded value is judged on its magnitude"),
        ("0002700", "2700", "padding does not inflate a mid-range value"),
        # The cap is DERIVED from the guardian pause cover (RENEW_MAX*TTL/2 +
        # TTL = 5400), halved to leave room for the phases that run before this
        # wait starts. Hard-coding 2700 here would let the two drift apart
        # silently, so the expectation is written as the derivation.
        ("999999", "2700", "above the cap clamps to the guardian-derived ceiling"),
        # 2**64 wraps to 0 and 2**63 wraps NEGATIVE, and the floor would then
        # hand someone who asked for a huge window the OLD tight gate. A
        # merely-large value does not distinguish it — it wraps positive and the
        # cap catches it anyway.
        ("18446744073709551616", "2700", "2**64 must not wrap to zero"),
        ("9223372036854775808", "2700", "2**63 must not wrap negative"),
    ],
)
def test_health_window_is_bounded_base_ten(
    window_block: str, override: str | None, expected: str, why: str
) -> None:
    assert _resolve_window(window_block, override) == expected, why


def test_the_cap_is_derived_from_the_guardian_pause_cover(window_block: str) -> None:
    """The cap must MOVE when the guardian cover moves, not be written twice.

    A health window that outlives the guardian pause leaves the host Guardian
    watching a container whose health API is deliberately silent — the outage
    the pause exists to suppress. Doubling the renew count must therefore
    double the ceiling; if the cap were a literal, this would not move.
    """
    setter = "export GENESIS_DEPLOY_HEALTH_WINDOW_SECS=999999"
    doubled = "set -Eeuo pipefail\nGUARDIAN_PAUSE_TTL=1800\nGUARDIAN_PAUSE_RENEW_MAX=8\n"
    out = subprocess.run(
        ["bash", "-c", f'{doubled}{setter}\n{window_block}\nprintf "%s" "$HEALTH_WINDOW_SECS"'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # cover = 8*900 + 1800 = 9000; cap = 4500
    got = out.stdout.strip().splitlines()[-1].strip()
    assert got == "4500", (
        f"cap did not track the guardian cover (got {got!r}) — "
        "a hard-coded ceiling can drift out of step with the pause it depends on"
    )


def test_deadline_uses_a_clock_that_cannot_be_stepped(text: str) -> None:
    """`date +%s` is the adjustable wall clock; /proc/uptime cannot be stepped.

    A backward step stretches the wait; a forward one expires it early and rolls
    back a healthy slow boot — the exact failure this gate exists to prevent.

    RUNS the helper rather than grepping for "/proc/uptime". An earlier version
    of this test asserted that string was present in the file, and a mutation
    that replaced the read with `if false` still PASSED — because the string
    survived in the comment above it. A text-existence check cannot tell a
    live read from a mention of one.
    """
    helper = _extract(text, "    _health_now() {", "    }")
    picker = _extract(text, "    if read -r _ < /proc/uptime", "    fi")
    out = subprocess.run(
        ["bash", "-c", f'{picker}\n{helper}\nprintf "%s" "$(_health_now)"'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.stdout.strip(), f"_health_now produced nothing ({out.stderr[:200]})"
    value = int(out.stdout.strip())
    # A wall-clock epoch is ~1.7e9 and climbing; a Linux uptime is orders of
    # magnitude smaller. Falling back to `date +%s` is what this separates.
    assert value < 1_000_000_000, (
        f"_health_now returned {value}, which is a wall-clock epoch — the "
        "deadline would be steppable by NTP or an administrator"
    )
    # …and it must still move forwards, or a "monotonic" source that always
    # returns 0 would satisfy the assertion above and freeze the gate.
    later = subprocess.run(
        ["bash", "-c", f'{picker}\n{helper}\nsleep 1.1\nprintf "%s" "$(_health_now)"'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert int(later.stdout.strip()) > value, "_health_now did not advance"


def test_a_failed_clock_read_holds_the_wait_instead_of_ending_it(text: str) -> None:
    """The failure branch must not cross clock domains.

    /proc/uptime reads in the thousands; an epoch reads in the billions. If a
    transient read failure mid-loop fell back to `date +%s`, that epoch would be
    compared against an uptime-based deadline, the loop would exit on its next
    turn, and a healthy slow boot would be rolled back. Printing 0 cannot end
    the wait; the attempt cap bounds the hold.

    SUBSTITUTION, stated plainly: the extracted helper is real except that the
    path is pointed at a file that does not exist, which is the only way to
    reach the failure branch without privileges. One token differs; the branch
    logic under test is the shipped text.
    """
    helper = _extract(text, "    _health_now() {", "    }")
    assert "/proc/uptime" in helper, "helper no longer reads the expected source"
    broken = helper.replace("/proc/uptime", "/proc/uptime-does-not-exist")
    out = subprocess.run(
        ["bash", "-c", f'_HEALTH_CLOCK=uptime\n{broken}\nprintf "%s" "$(_health_now)"'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    got = out.stdout.strip()
    assert got == "0", (
        f"a failed clock read returned {got!r}. Anything else — an epoch above "
        "all — ends the wait early and rolls back a working deploy"
    )


def test_exactly_one_wait_loop_reads_the_same_clock_as_the_deadline(text: str) -> None:
    """The deadline and the loop condition must be in ONE clock domain.

    Asserting the correct `while` line is PRESENT is not enough, and that was a
    real hole: leaving the right line in place and adding a `date +%s` loop
    beside it satisfied a presence check while the deadline (an uptime, ~4.4e6)
    was compared against a wall clock (~1.8e9). The loop then runs ZERO
    iterations and every deploy rolls back — a green suite over total failure.

    So count the loops in the gate region instead: there must be exactly one,
    and it must read the same helper the deadline was built from.
    """
    region = _extract(text, "    HEALTH_START=", '    if [ "$HEALTH_OK" = "true" ]; then')
    loops = [ln for ln in region.splitlines() if ln.lstrip().startswith("while ")]
    assert len(loops) == 1, (
        f"expected exactly one wait loop in the health gate, found {len(loops)}: {loops}"
    )
    assert "_health_now" in loops[0], (
        f"the wait loop does not read the monotonic helper: {loops[0]!r}"
    )
    assert "date +%s" not in loops[0], f"the wait loop reads the steppable wall clock: {loops[0]!r}"
    assert "HEALTH_DEADLINE=$(( HEALTH_START + HEALTH_WINDOW_SECS ))" in region, (
        "the deadline must be built from the same reading the loop compares against"
    )
    assert "HEALTH_START=$(_health_now)" in text, (
        "the deadline must be computed from the monotonic helper"
    )


def _wait_continues(case_block: str, state: str) -> bool:
    """Does the real case statement keep waiting for this unit state?"""
    script = (
        "attempt=1\n"
        f"HEALTH_UNIT_STATE={state!r}\n"
        "for _ in 1; do\n"
        f"{case_block}\n"
        '  echo "__CONTINUED__"\n'
        "done\n"
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return "__CONTINUED__" in out.stdout


@pytest.mark.parametrize("state", ["active", "activating", "reloading"])
def test_a_living_unit_keeps_its_window(state_case: str, state: str) -> None:
    assert _wait_continues(state_case, state), f"unit {state!r} must keep waiting"


def test_an_unreadable_unit_state_is_not_proof_of_death(state_case: str) -> None:
    """The finding: `|| true` yields an empty state when the QUERY fails.

    `systemctl --help` documents is-active as "Check whether units are active" —
    it makes no claim about what an execution failure means, so a systemd or
    user D-Bus hiccup is not affirmative evidence the server is dead. Falling
    into the default branch would roll back a still-booting server on a
    transient blip.
    """
    assert _wait_continues(state_case, ""), (
        "an unreadable unit state ended the wait — a transient query failure "
        "must retry, not conclude death"
    )


@pytest.mark.parametrize("state", ["failed", "inactive", "deactivating"])
def test_a_terminal_unit_state_still_ends_the_wait_early(state_case: str, state: str) -> None:
    """The control. Without it this file would pass on a case statement that
    never breaks at all, which would make the gate wait out the full window on
    a server that is definitively dead."""
    assert not _wait_continues(state_case, state), (
        f"unit {state!r} must stop the wait rather than padding the clock"
    )
