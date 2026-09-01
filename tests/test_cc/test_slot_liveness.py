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

from genesis.cc.slot_liveness import ALIVE, POISONED, UNKNOWN, liveness


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

    def test_malformed_stat_does_not_crash(self, proc):
        _mkproc(proc, 1100, "bash", 1)
        d = _mkproc(proc, 1101, "claude", 1100, ["claude"])
        (d / "stat").write_text("garbage without parens\n")
        assert liveness([1100], proc) in (ALIVE, POISONED)

    def test_ancestry_cycle_terminates(self, proc):
        # A malformed tree must not spin forever.
        _mkproc(proc, 1200, "bash", 1201)
        _mkproc(proc, 1201, "bash", 1200)
        _mkproc(proc, 1202, "claude", 1200, ["claude"])
        assert liveness([9999], proc) == POISONED

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
