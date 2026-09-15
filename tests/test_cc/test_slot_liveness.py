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
    _CLAUDE_NAMES,
    _INTERPRETERS,
    ALIVE,
    POISONED,
    UNKNOWN,
    liveness,
    main,
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

    @pytest.mark.parametrize("flag", ["-p", "--print"])
    def test_a_headless_task_under_the_pane_is_that_panes_work(self, proc, flag):
        """CHANGED semantics, and the module's own contract is why.

        Genesis spawns `claude -p` for triage and reflection, and those must not
        make some unrelated slot look alive — but that is a question about WHERE
        the process is, and it was being answered by discarding every headless
        process before any ancestry was known. A headless task DESCENDING FROM
        THE PANE is not a stray probe, it is that slot running something, and
        the door's next move is to destroy it.

        This module's stated fail direction is "deliberately biased toward
        reporting ALIVE ... every ambiguity resolves the cheap way", and its
        header explains that it does NOT reuse `cc_slots._is_interactive`
        precisely because that predicate's purpose ("never let an internal
        `claude -p` masquerade as a slot") is backwards here. The global filter
        had imported that backwards purpose anyway.

        `test_a_headless_probe_elsewhere_is_still_invisible` is the control that
        keeps the original intent.
        """
        _mkproc(proc, 800, "bash", 1)
        _mkproc(proc, 801, "claude", 800, ["claude", flag, "summarise this"])
        assert liveness([800], proc) == ALIVE

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["claude", "-p", "x"], POISONED),          # headless: never votes UNKNOWN
            (["claude", "--print", "x"], POISONED),
            (["claude", "--model", "opus-p"], UNKNOWN),  # "-p" in a VALUE is not the flag
            (["claude"], UNKNOWN),
        ],
    )
    def test_headless_is_what_decides_an_unresolvable_ancestry(
        self, proc, argv, expected
    ):
        """Where the headless flag is still OBSERVABLE, now that in-pane
        headless counts as alive.

        A claude whose ancestry cannot be resolved makes the whole probe
        UNKNOWN — the broken walk is the one that might have connected it to the
        pane. A HEADLESS process does not get that vote: it is ignored outside
        the pane, and one unresolvable background probe anywhere on the box
        would otherwise suppress every heal. So this is the case that still
        distinguishes the two, and it keeps the flag PARSING under test —
        including that `-p` inside a value is not the flag.
        """
        _mkproc(proc, 2700, "bash", 1)            # the pane, unrelated
        _mkproc(proc, 2701, "bash", 999999)       # parent that does not exist
        _mkproc(proc, 2702, "claude", 2701, argv)
        assert liveness([2700], proc) == expected

    def test_p_inside_an_argument_value_is_still_interactive(self, proc):
        # Exact-arg match: "-p" as part of a VALUE must not read as the flag.
        # In-pane this is ALIVE either way now, so the case that actually
        # DISCRIMINATES lives in the unresolvable-ancestry test below.
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

    def test_headless_under_the_pane_counts_when_matched_by_argv0(self, proc):
        # Same semantics change as TestDiscrimination above: a headless task
        # descending from the pane is that pane's work, however it was matched.
        _mkproc(proc, 1800, "bash", 1)
        _mkproc(proc, 1801, "node", 1800, ["/usr/local/bin/claude", "-p", "x"])
        assert liveness([1800], proc) == ALIVE


class TestInterpreterWrappedInstalls:
    """A `node .../cli.js` slot is a LIVE claude, and misreading it is the
    expensive direction.

    `comm` reads "node" and argv[0] is the interpreter, so a classifier that
    looks only at those two marks a running session claude-less — and this door
    then OFFERS TO DESTROY IT. The repo already carries the closed rule set for
    these shapes in `scripts/check_cc_running_versions.sh`; these lock the port.
    """

    def test_node_running_the_entry_script_is_alive(self, proc):
        _mkproc(proc, 2100, "bash", 1)
        _mkproc(proc, 2101, "node", 2100, ["node", "/opt/cc/cli.js"])
        assert liveness([2100], proc) == ALIVE

    def test_a_flag_before_the_entry_script_does_not_hide_it(self, proc):
        """The trap the bash twin documents: testing argv[1] reads
        `node --enable-source-maps /opt/cc/cli.js` as proof of NOT-claude,
        because the flag occupies the slot. Node CLIs carry such flags
        routinely."""
        _mkproc(proc, 2200, "bash", 1)
        _mkproc(
            proc, 2201, "node", 2200,
            ["node", "--enable-source-maps", "--no-warnings", "/opt/cc/cli.js"],
        )
        assert liveness([2200], proc) == ALIVE

    @pytest.mark.parametrize("interp", ["node", "nodejs", "bun", "deno"])
    def test_every_supported_interpreter_counts(self, proc, interp):
        _mkproc(proc, 2300, "bash", 1)
        _mkproc(proc, 2301, interp, 2300, [interp, "/opt/cc/cli.js"])
        assert liveness([2300], proc) == ALIVE

    def test_claude_code_basename_counts(self, proc):
        _mkproc(proc, 2400, "bash", 1)
        _mkproc(proc, 2401, "node", 2400, ["/usr/local/bin/claude-code"])
        assert liveness([2400], proc) == ALIVE

    def test_an_interpreter_running_something_else_is_not_claude(self, proc):
        """The control. Widening that swallowed every node process would make a
        genuinely poisoned slot un-healable, and would 'pass' the tests above."""
        _mkproc(proc, 2500, "bash", 1)
        _mkproc(proc, 2501, "node", 2500, ["node", "/srv/app/server.js"])
        assert liveness([2500], proc) == POISONED

    def test_headless_under_the_pane_counts_through_the_interpreter_shape(self, proc):
        _mkproc(proc, 2600, "bash", 1)
        _mkproc(proc, 2601, "node", 2600, ["node", "/opt/cc/cli.js", "-p", "x"])
        assert liveness([2600], proc) == ALIVE


class TestShapeRulesStayInStepWithTheirTwin:
    """Two hand-synced copies of one closed set is how the set drifts.

    `scripts/check_cc_running_versions.sh` is the older copy and the reason this
    gap was found at all. They cannot share code across the language boundary,
    so they share a TEST: the bash file's `case` patterns are parsed and the
    Python sets must cover every one. A name added to either side alone fails
    here instead of silently costing someone a live session.
    """

    _SH = Path(__file__).resolve().parents[2] / "scripts" / "check_cc_running_versions.sh"

    @staticmethod
    def _case_names(text: str, func: str) -> set[str]:
        body = text.split(f"{func}() {{", 1)[1].split("\n}", 1)[0]
        for line in body.split("\n"):
            line = line.strip()
            if line.endswith("return 0 ;;") and "|" in line or line.endswith(") return 0 ;;"):
                pattern = line.split(")", 1)[0].strip()
                return {n for n in pattern.split("|") if n}
        raise AssertionError(f"no case pattern found in {func}")

    def test_the_bash_twin_is_parseable(self):
        """If this file moves or is rewritten, the parity tests below would
        silently pass against an empty set. Fail loudly instead."""
        assert self._SH.exists(), self._SH
        text = self._SH.read_text()
        assert self._case_names(text, "is_cc_name"), "no claude names parsed"
        assert self._case_names(text, "is_interpreter_name"), "no interpreters parsed"

    def test_every_claude_name_the_twin_knows_is_recognized_here(self):
        names = self._case_names(self._SH.read_text(), "is_cc_name")
        missing = {n for n in names if n.encode() not in _CLAUDE_NAMES}
        assert not missing, (
            f"{missing} is claude to check_cc_running_versions.sh but not to this "
            "classifier — a live session running it would be offered for destruction"
        )

    def test_every_interpreter_the_twin_knows_is_recognized_here(self):
        names = self._case_names(self._SH.read_text(), "is_interpreter_name")
        missing = {n for n in names if n.encode() not in _INTERPRETERS}
        assert not missing, (
            f"{missing} can run the CC entry script per check_cc_running_versions.sh "
            "but is not recognized here"
        )


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


class TestCliEntryPoint:
    """`main()` is what the door actually calls, and it had no tests at all.

    The door parses line 1 as the verdict and treats anything unexpected as
    "no verdict"; a crash here must therefore never propagate, because the
    caller would be left without an answer at the exact moment it is deciding
    whether to destroy a pane.
    """

    def test_no_pids_is_unknown_never_poisoned(self, capsys):
        """The sparing direction, and the one that matters most.

        POISONED with no pids would authorise a rebuild on an empty read.
        """
        assert main([]) == 0
        assert capsys.readouterr().out.splitlines()[0] == UNKNOWN

    def test_non_numeric_arguments_are_ignored(self, capsys):
        """Argument handling changed when `--idle` was removed: a leading flag
        is no longer a mode, it is simply not a pid. It must be dropped, not
        parsed as one — `int("--idle")` would raise, and the except-clause
        would mask a real verdict as UNKNOWN.
        """
        assert main(["--idle", "not-a-pid", ""]) == 0
        assert capsys.readouterr().out.splitlines()[0] == UNKNOWN

    def test_a_verdict_is_printed_with_its_note(self, capsys):
        assert main(["1"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[0] in (ALIVE, POISONED, UNKNOWN)
        assert out[1], "every verdict must carry a human note on line 2"

    def test_an_internal_error_still_answers(self, capsys, monkeypatch):
        """The caller must never be left without a verdict."""
        monkeypatch.setattr(
            "genesis.cc.slot_liveness.liveness",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert main(["1"]) == 0
        assert capsys.readouterr().out.splitlines()[0] == UNKNOWN


class TestInterpreterOptionsWithOperands:
    """`node -r preload /opt/cc/cli.js` is a live Claude.

    Skipping flags without consuming their OPERANDS is not a parser: `-r` /
    `--require` take a value, so the first non-flag token is the preload module,
    not the entry script. Doing it correctly means knowing which of an
    interpreter's options take values, per interpreter and per version — which
    this module cannot know, and being wrong costs a live session.

    So the question was weakened until no grammar is needed: does the argv
    mention the entry script at all. The price is a false ALIVE for a command
    that merely names it, which costs a rebuild offer.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["node", "-r", "preload", "/opt/cc/cli.js"],
            ["node", "--require", "preload", "/opt/cc/cli.js"],
            ["node", "--require=preload", "/opt/cc/cli.js"],
            ["node", "--max-old-space-size", "4096", "/opt/cc/cli.js"],
            ["node", "-r", "a", "-r", "b", "/opt/cc/cli.js"],
        ],
    )
    def test_an_option_operand_does_not_hide_the_entry_script(self, proc, argv):
        _mkproc(proc, 3100, "bash", 1)
        _mkproc(proc, 3101, "node", 3100, argv)
        assert liveness([3100], proc) == ALIVE

    def test_an_interpreter_with_no_entry_script_is_still_not_claude(self, proc):
        """The control. Answering ALIVE for every node process would pass the
        cases above while making a genuinely poisoned slot un-healable."""
        _mkproc(proc, 3200, "bash", 1)
        _mkproc(proc, 3201, "node", 3200, ["node", "-r", "preload", "/srv/app.js"])
        assert liveness([3200], proc) == POISONED


class TestHeadlessInsideThePaneIsLiveWork:
    """`claude -p` running IN the slot is that slot's work.

    Headless processes were filtered out globally, before any ancestry was
    known, so a slot running a long headless task reported POISONED and the door
    offered to destroy it — with a message saying the pane ran no claude. The
    filter exists for background probes elsewhere on the host, and that is
    exactly the distinction it failed to make.
    """

    def test_a_headless_task_in_the_pane_is_alive(self, proc):
        _mkproc(proc, 3300, "bash", 1)
        _mkproc(proc, 3301, "claude", 3300, ["claude", "-p", "long task"])
        assert liveness([3300], proc) == ALIVE

    def test_a_headless_probe_elsewhere_is_still_invisible(self, proc):
        """The control, and the reason the filter existed. A background
        `claude -p` under some other parent must NOT keep a genuinely dead slot
        looking alive, or no slot would ever heal while Genesis is working."""
        _mkproc(proc, 3400, "bash", 1)  # the pane, nothing under it
        _mkproc(proc, 3401, "bash", 1)  # an unrelated parent
        _mkproc(proc, 3402, "claude", 3401, ["claude", "-p", "background probe"])
        assert liveness([3400], proc) == POISONED

    def test_an_interactive_claude_in_the_pane_still_wins(self, proc):
        _mkproc(proc, 3500, "bash", 1)
        _mkproc(proc, 3501, "claude", 3500, ["claude"])
        assert liveness([3500], proc) == ALIVE
