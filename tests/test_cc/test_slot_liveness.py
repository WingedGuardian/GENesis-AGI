"""Unit tests for genesis.cc.slot_liveness — the slot-door liveness probe.

Built on a synthetic /proc tree so the real launch shapes can be reproduced
exactly, including the one that makes ``#{pane_current_command}`` unusable: a
non-interactive ``bash -c`` pane shell with claude as a CHILD.

Both directions are locked. A false POISONED is the expensive error — it makes
the door type into a live session — so every ambiguous input must resolve ALIVE.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.cc.slot_liveness import (
    ALIVE,
    BUSY,
    IDLE,
    POISONED,
    UNKNOWN,
    idleness,
    liveness,
)


def _mkproc(root: Path, pid: int, comm: str, ppid: int, cmdline: list[str] | None = None):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "comm").write_text(comm + "\n")
    # Field 2 is a parenthesised comm; everything after is located from the
    # LAST ')'. Include a space in the padding to keep the parser honest.
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0 0 -1 0 0 0\n")
    (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in (cmdline or [comm])) + b"\x00")
    return d


@pytest.fixture
def proc(tmp_path):
    return tmp_path / "proc"


class TestRealLaunchShapes:
    def test_canonical_pane_shell_with_claude_child_is_alive(self, proc):
        # cc-slot's own shape: `bash -c "cd … && claude …; trailer"`. This is
        # the case pane_current_command reports as "bash" while claude runs.
        _mkproc(proc, 100, "bash", 1, ["bash", "-c", "cd /x && claude; trailer"])
        _mkproc(proc, 101, "claude", 100, ["claude", "--dangerously-skip-permissions"])
        assert liveness([100], proc) == ALIVE

    def test_pane_process_is_claude_itself_is_alive(self, proc):
        # Legacy `exec claude` shape: no intervening shell.
        _mkproc(proc, 200, "claude", 1, ["claude"])
        assert liveness([200], proc) == ALIVE

    def test_claude_two_hops_down_is_alive(self, proc):
        # Login shell -> hand-typed `bash` -> claude.
        _mkproc(proc, 300, "bash", 1)
        _mkproc(proc, 301, "bash", 300)
        _mkproc(proc, 302, "claude", 301, ["claude"])
        assert liveness([300], proc) == ALIVE

    def test_bare_login_shell_with_no_children_is_poisoned(self, proc):
        # The reported bug: an alive session sitting at a prompt.
        _mkproc(proc, 400, "bash", 1)
        assert liveness([400], proc) == POISONED

    def test_multi_window_any_pane_running_claude_is_alive(self, proc):
        # One window shelled out, another still running claude. Healing here
        # would double-launch, so ANY live pane makes the session alive.
        _mkproc(proc, 500, "bash", 1)
        _mkproc(proc, 501, "bash", 1)
        _mkproc(proc, 502, "claude", 501, ["claude"])
        assert liveness([500, 501], proc) == ALIVE


class TestDiscrimination:
    def test_claude_in_a_different_session_does_not_count(self, proc):
        # A claude under someone ELSE's pane must not mark this slot alive,
        # or a poisoned slot is never healed while any session exists.
        _mkproc(proc, 600, "bash", 1)          # our pane, empty
        _mkproc(proc, 700, "bash", 1)          # another slot's pane
        _mkproc(proc, 701, "claude", 700, ["claude"])
        assert liveness([600], proc) == POISONED

    def test_headless_claude_p_does_not_count_as_a_session(self, proc):
        # Genesis spawns `claude -p` for triage/reflection; those share
        # comm=="claude" but are not interactive sessions.
        _mkproc(proc, 800, "bash", 1)
        _mkproc(proc, 801, "claude", 800, ["claude", "-p", "summarise this"])
        assert liveness([800], proc) == POISONED

    def test_headless_long_form_print_flag(self, proc):
        _mkproc(proc, 850, "bash", 1)
        _mkproc(proc, 851, "claude", 850, ["claude", "--print", "x"])
        assert liveness([850], proc) == POISONED

    def test_p_inside_an_argument_value_is_still_interactive(self, proc):
        # Exact-arg match: "-p" as part of a VALUE must not read as the flag.
        _mkproc(proc, 860, "bash", 1)
        _mkproc(proc, 861, "claude", 860, ["claude", "--model", "opus-p"])
        assert liveness([860], proc) == ALIVE

    def test_a_process_merely_named_like_claude_does_not_count(self, proc):
        _mkproc(proc, 900, "bash", 1)
        _mkproc(proc, 901, "claude-wrapper", 900, ["claude-wrapper"])
        assert liveness([900], proc) == POISONED


class TestSparesOnAmbiguity:
    """Every unreadable / malformed input must resolve to the cheap error."""

    def test_no_pane_pids_is_unknown(self, proc):
        proc.mkdir(parents=True)
        assert liveness([], proc) == UNKNOWN

    def test_unreadable_proc_is_unknown(self, tmp_path):
        assert liveness([1], tmp_path / "does-not-exist") == UNKNOWN

    def test_unreadable_cmdline_counts_as_a_real_session(self, proc):
        # Cannot PROVE it is headless -> must not call the slot poisoned.
        _mkproc(proc, 1000, "bash", 1)
        d = _mkproc(proc, 1001, "claude", 1000, ["claude"])
        (d / "cmdline").unlink()
        assert liveness([1000], proc) == ALIVE

    def test_malformed_stat_reads_unknown_not_poisoned(self, proc):
        # An unparseable ancestry is an ANSWER WITHHELD, not a death
        # certificate: the walk that broke might have been the one that
        # would have reached the pane.
        _mkproc(proc, 1100, "bash", 1)
        d = _mkproc(proc, 1101, "claude", 1100, ["claude"])
        (d / "stat").write_text("garbage without parens\n")
        assert liveness([1100], proc) == UNKNOWN

    def test_ancestry_cycle_terminates_and_spares(self, proc):
        # A malformed tree must not spin forever — and exhausting the hop
        # bound proves NOTHING about the pane, so it must not read POISONED.
        _mkproc(proc, 1200, "bash", 1201)
        _mkproc(proc, 1201, "bash", 1200)
        _mkproc(proc, 1202, "claude", 1200, ["claude"])
        assert liveness([9999], proc) == UNKNOWN

    def test_comm_containing_spaces_and_parens_parses(self, proc):
        # /proc/<pid>/stat's comm field is parenthesised and may contain both;
        # parsing from the left mis-reads ppid for these.
        _mkproc(proc, 1300, "bash", 1)
        d = proc / "1301"
        d.mkdir()
        (d / "comm").write_text("claude\n")
        (d / "stat").write_text("1301 (we ird ) name) S 1300 0 0 0 -1 0 0 0\n")
        (d / "cmdline").write_bytes(b"claude\x00")
        assert liveness([1300], proc) == ALIVE


class TestClaudeIdentification:
    """`comm` is measured-correct today but is one signal about a binary we do
    not control. argv[0] is accepted as an alternative so a future rename
    cannot make every live slot read as poisoned."""

    def test_identified_by_argv0_when_comm_differs(self, proc):
        _mkproc(proc, 1400, "bash", 1)
        _mkproc(proc, 1401, "node", 1400, ["/usr/local/bin/claude", "--verbose"])
        assert liveness([1400], proc) == ALIVE

    def test_identified_by_comm_when_argv0_differs(self, proc):
        _mkproc(proc, 1500, "bash", 1)
        _mkproc(proc, 1501, "claude", 1500, ["/opt/somewhere/launcher"])
        assert liveness([1500], proc) == ALIVE

    def test_claude_exe_basename_counts(self, proc):
        _mkproc(proc, 1600, "bash", 1)
        _mkproc(proc, 1601, "node", 1600, ["/usr/lib/node_modules/x/bin/claude.exe"])
        assert liveness([1600], proc) == ALIVE

    def test_neighbouring_tool_does_not_qualify(self, proc):
        # Exact basename only — a sibling tool must not mark a slot alive, or a
        # genuinely poisoned slot would never heal.
        _mkproc(proc, 1700, "bash", 1)
        _mkproc(proc, 1701, "node", 1700, ["/usr/local/bin/claude-monitor"])
        assert liveness([1700], proc) == POISONED

    def test_headless_still_excluded_when_matched_by_argv0(self, proc):
        _mkproc(proc, 1800, "bash", 1)
        _mkproc(proc, 1801, "node", 1800, ["/usr/local/bin/claude", "-p", "x"])
        assert liveness([1800], proc) == POISONED


class TestInconclusiveWalksSpare:
    """A walk that ends without REACHING a conclusion (hop bound hit, stat
    unreadable mid-chain) must never be read as POISONED — the broken walk is
    exactly the one that might have connected claude to the pane."""

    def test_chain_longer_than_the_hop_limit_is_unknown(self, proc):
        from genesis.cc.slot_liveness import _MAX_ANCESTRY_HOPS

        depth = _MAX_ANCESTRY_HOPS + 3
        _mkproc(proc, 2000, "bash", 1)  # the pane, far above the bound
        prev = 2000
        for i in range(depth):
            pid = 2001 + i
            _mkproc(proc, pid, "bash", prev)
            prev = pid
        _mkproc(proc, 2999, "claude", prev, ["claude"])
        assert liveness([2000], proc) == UNKNOWN

    def test_unreadable_ancestor_mid_walk_is_unknown(self, proc):
        _mkproc(proc, 2100, "bash", 1)      # the pane
        _mkproc(proc, 2101, "bash", 2100)   # intermediate, about to vanish
        d = _mkproc(proc, 2102, "claude", 2101, ["claude"])
        import shutil

        shutil.rmtree(proc / "2101")        # ancestry now unresolvable
        assert d.exists()
        assert liveness([2100], proc) == UNKNOWN

    def test_vanished_candidate_is_conclusive_not_inconclusive(self, proc):
        """A claude that EXITED between enumeration and its walk is
        conclusively not the slot's claude. Scoring it UNKNOWN would let
        routine box-wide claude churn (any interactive claude anywhere
        exiting mid-probe) suppress every heal."""
        from genesis.cc.slot_liveness import _walk_verdict

        _mkproc(proc, 2400, "bash", 1)
        # pid 2499 has NO /proc entry at walk time — the post-enumeration exit.
        assert _walk_verdict(proc, 2499, {2400}) == POISONED

    def test_present_but_unparseable_candidate_stays_unknown(self, proc):
        from genesis.cc.slot_liveness import _walk_verdict

        _mkproc(proc, 2500, "bash", 1)
        d = _mkproc(proc, 2501, "claude", 2500, ["claude"])
        (d / "stat").write_text("garbage without parens\n")
        assert _walk_verdict(proc, 2501, {2500}) == UNKNOWN

    def test_conclusive_walks_still_read_poisoned(self, proc):
        # Every walk reaching init cleanly IS a conclusion; sparing must not
        # swallow the real verdict or no slot ever heals.
        _mkproc(proc, 2200, "bash", 1)      # the pane, childless
        _mkproc(proc, 2300, "bash", 1)      # someone else's tree
        _mkproc(proc, 2301, "claude", 2300, ["claude"])
        assert liveness([2200], proc) == POISONED


class TestIdleness:
    """`idleness()`'s question is "is it SAFE TO TYPE here": the pane process
    must be sitting at a prompt with no running job. Only IDLE permits
    keystrokes; every ambiguity answers BUSY/UNKNOWN and costs a plain attach."""

    def test_shell_with_no_children_is_idle(self, proc):
        _mkproc(proc, 3000, "bash", 1)
        assert idleness(3000, proc) == IDLE

    def test_shell_with_a_foreground_child_is_busy(self, proc):
        # `bash script.sh`, rsync, vim — anything running is a child of the
        # pane shell, and C-c would kill it.
        _mkproc(proc, 3100, "bash", 1)
        _mkproc(proc, 3101, "rsync", 3100, ["rsync", "-a", "x", "y"])
        assert idleness(3100, proc) == BUSY

    def test_child_named_like_a_shell_is_still_busy(self, proc):
        # The pane_current_command whitelist reads `bash script.sh` as "bash";
        # child-presence is the signal that catches it.
        _mkproc(proc, 3200, "bash", 1)
        _mkproc(proc, 3201, "bash", 3200, ["bash", "script.sh"])
        assert idleness(3200, proc) == BUSY

    def test_missing_pane_process_is_unknown(self, proc):
        proc.mkdir(parents=True, exist_ok=True)
        assert idleness(4242, proc) == UNKNOWN

    def test_unenumerable_proc_is_unknown(self, tmp_path):
        assert idleness(1, tmp_path / "no-such-proc") == UNKNOWN

    def test_vanished_sibling_does_not_block_idle(self, proc):
        # Processes exit between the directory listing and the stat read all
        # the time. A vanished entry (dir present, stat gone — the shape a
        # mid-read exit leaves) cannot be a LIVE child of the pane, so it must
        # not withhold IDLE or busy boxes would never heal.
        _mkproc(proc, 3300, "bash", 1)
        (proc / "3301").mkdir()  # digit-named, but its stat never materialises
        assert idleness(3300, proc) == IDLE

    def test_unparseable_sibling_stat_is_unknown(self, proc):
        # A stat we can read but not parse might BE the pane's child; typing
        # over it is the expensive error, so withhold the IDLE verdict.
        _mkproc(proc, 3400, "bash", 1)
        d = _mkproc(proc, 3401, "mystery", 999)
        (d / "stat").write_text("garbage without parens\n")
        assert idleness(3400, proc) == UNKNOWN
