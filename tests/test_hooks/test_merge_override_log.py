"""The merge gate's override sigils are advertised as "(logged)" — make that true.

Before this module, the gate told the operator that appending `# ci-override` was
"(logged)" and nothing anywhere wrote a record. The claim was false on every
surface: no writer under scripts/ or src/genesis/, and nothing on disk but the
hook-surface evidence directory.

Two constraints are inherited rather than invented, both from
``git_discard_guard.py`` (which is now the same writer — ``audit_jsonl``):

* The row is METADATA ONLY. ``_record_snapshots``' docstring refuses to persist
  the command because "the Bash payload can carry credentials … and this log is
  durable". An override sigil IS a trailing comment on a Bash command, so the
  only free text available here is command-line text. It never reaches the log.
* The file is own-user-only, created without a umask window, appended under a
  sidecar lock.
"""

import ast
import importlib.util
import json
import stat
import subprocess
import time
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("git_push_guard_ovl", _HOOKS / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

HEAD = "a" * 40
OTHER = "c" * 40


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    p = tmp_path / "sub" / "merge_override_log.jsonl"  # parent does NOT exist
    monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_LOG", str(p))
    return p


def _rows(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _note_and_flush(outcome="allowed", **kw):
    _mod._PENDING_OVERRIDES.clear()
    _mod._note_override(kw.pop("sigil", "ci-override"), **kw)
    _mod._flush_overrides(outcome)


class TestWriter:
    def test_records_a_row(self, log_path):
        _note_and_flush(waived="ci:red", pr="1525", repo="o/r", head=HEAD)
        (row,) = _rows(log_path)
        assert row["sigil"] == "ci-override"
        assert row["pr"] == "1525"
        assert row["head"] == HEAD
        assert row["waived"] == "ci:red"
        assert row["outcome"] == "allowed"
        assert row["ts"]

    def test_creates_a_missing_parent_directory(self, log_path):
        """A missing parent silently made the whole feature a no-op in the first cut."""
        assert not log_path.parent.exists()
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert len(_rows(log_path)) == 1

    def test_appends_rather_than_truncates(self, log_path):
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        _note_and_flush(sigil="stale-review-override", waived="codex", pr="2", repo="o/r")
        assert [r["pr"] for r in _rows(log_path)] == ["1", "2"]

    def test_one_row_per_sigil_on_a_multi_sigil_merge(self, log_path):
        """A merge routinely carries several sigils; per-sigil rates must stay countable."""
        _mod._PENDING_OVERRIDES.clear()
        _mod._note_override("stale-review-override", waived="codex", pr="9", repo="o/r")
        _mod._note_override("scheduled-review-override", waived="sched", pr="9", repo="o/r")
        _mod._flush_overrides("allowed")
        assert sorted(r["sigil"] for r in _rows(log_path)) == [
            "scheduled-review-override",
            "stale-review-override",
        ]

    def test_rejects_a_sigil_outside_the_closed_set(self, log_path):
        """The sigil field is a closed set (shell_parse._KNOWN_SIGILS), never free text."""
        _note_and_flush(sigil="not-a-sigil", waived="ci")
        assert _rows(log_path) == []

    def test_never_raises_on_an_unwritable_path(self, tmp_path, monkeypatch, capsys):
        """Best-effort: a logging failure must never break a merge — but it is LOUD."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_LOG", str(blocker / "x.jsonl"))
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert "[audit-log]" in capsys.readouterr().err

    def test_file_and_directory_are_own_user_only(self, log_path):
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700

    def test_the_file_is_CREATED_restrictive_not_chmodded_afterwards(self, log_path):
        """Pins the umask WINDOW, which a mode check structurally cannot see.

        ``restrict()`` chmods to 0600 immediately after the open, so the final
        mode is 0600 whether or not ``os.open`` was given a restrictive mode —
        the two differ only for the instants between create and chmod, which is
        exactly the exposure ``open()``+``chmod`` has and ``os.open`` does not.
        Asserting the observable end state therefore passes on the insecure
        version (MEASURED: mutating the mode to 0o644 left every mode assertion
        green). So assert the CREATE mode itself.
        """
        import audit_jsonl

        seen = {}
        real_open = audit_jsonl.os.open

        def spy(path, flags, mode=0o777):
            seen[str(path)] = mode  # PATH too: append_row opens the sidecar AND
            return real_open(path, flags, mode)  # the log; only the log matters

        audit_jsonl.os.open = spy
        try:
            _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        finally:
            audit_jsonl.os.open = real_open
        # Recording only the mode let the .lock file satisfy the assertion while
        # the LOG was opened insecurely — measured vacuous before this keyed on path.
        assert str(log_path) in seen, f"the log itself was never os.open'd: {sorted(seen)}"
        assert seen[str(log_path)] == 0o600, oct(seen[str(log_path)])

    def test_pending_rows_do_not_leak_between_flushes(self, log_path):
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        _mod._flush_overrides("allowed")  # nothing pending
        assert len(_rows(log_path)) == 1


class TestOutcomeTagging:
    """A sigil that was typed and a sigil that let a merge through are different facts."""

    @pytest.mark.parametrize("outcome", ["allowed", "asked", "blocked", "error"])
    def test_outcome_is_recorded(self, log_path, outcome):
        _note_and_flush(outcome, waived="ci:red", pr="1", repo="o/r")
        assert _rows(log_path)[0]["outcome"] == outcome

    def test_an_outcome_outside_the_closed_set_is_refused(self, log_path):
        """The tuple is enforcement, not documentation — same as ``sigil``."""
        _note_and_flush("probably-fine", waived="ci:red", pr="1", repo="o/r")
        assert _rows(log_path) == []


class TestNoCredentialLeak:
    """The load-bearing test. A durable log must not persist command text."""

    def test_row_carries_no_command_or_comment_text(self, log_path):
        secret = "ghp_EXAMPLETOKENVALUE1234567890"
        _note_and_flush(
            waived="ci:red",
            pr="1",
            repo="o/r",
            # A caller passing command text must not get it persisted, even by
            # accident — the writer builds the row from a fixed field set.
            **{"raw": f"gh pr merge 1 -H 'Authorization: {secret}'  # ci-override"},
        )
        text = log_path.read_text()
        assert secret not in text
        assert "Authorization" not in text
        assert "gh pr merge" not in text

    def test_field_set_is_closed(self, log_path):
        """The expected names are LITERAL here, deliberately.

        Comparing the row against ``_OVERRIDE_LOG_FIELDS`` compares production to
        production: adding ``reason`` to the tuple and populating it — the
        realistic way command text would ever reach this log — keeps that version
        green. Widening the row must fail a test and be a conscious edit here.
        """
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert set(_rows(log_path)[0]) == {
            "ts",
            "sigil",
            "outcome",
            "waived",
            "pr",
            "repo",
            "head",
            "actor",
        }

    def test_head_and_repo_are_shape_checked(self, log_path):
        """`head` is whatever followed --match-head-commit — arbitrary typed text."""
        _note_and_flush(
            waived="ci:red",
            pr="1",
            repo='own\nrep/x"}{',
            head="ghp_notAShaButATokenShapedString12345",
        )
        row = _rows(log_path)[0]
        assert row["head"] == ""
        assert row["repo"] == ""

    def test_a_valid_head_and_repo_survive(self, log_path):
        """Positive control: the shape check must not reject real values."""
        _note_and_flush(waived="ci:red", pr="1", repo="owner/repo-name.x", head=HEAD)
        row = _rows(log_path)[0]
        assert row["head"] == HEAD
        assert row["repo"] == "owner/repo-name.x"


class TestRetention:
    """New store ships with its own prune — the New-Store Gate, same PR."""

    def test_prunes_rows_older_than_the_window(self, log_path):
        log_path.parent.mkdir(parents=True)
        log_path.write_text('{"ts": "2020-01-01T00:00:00+00:00", "pr": "old"}\n')
        _note_and_flush(waived="ci:red", pr="2", repo="o/r")
        assert [r["pr"] for r in _rows(log_path)] == ["2"]

    def test_keeps_rows_inside_the_window(self, log_path):
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        _note_and_flush(waived="ci:red", pr="2", repo="o/r")
        assert len(_rows(log_path)) == 2, "positive control: prune must not eat live rows"

    def test_a_naive_timestamp_neither_kills_the_write_nor_the_row(self, log_path, capsys):
        """THE ACCEPTANCE REPLAY for the blocker this rework exists to fix.

        In the first cut the aware/naive comparison sat OUTSIDE the per-row try, so
        ONE row with a timezone-naive ts raised, aborted the write, and did so on
        every subsequent write forever — while the gate kept printing "(logged)".
        The row is now appended BEFORE retention runs, and an unreadable ts is KEPT.
        """
        log_path.parent.mkdir(parents=True)
        log_path.write_text('{"ts": "2026-08-30T00:00:00", "pr": "111"}\n')  # no tz
        _note_and_flush(waived="ci:red", pr="222", repo="o/r")
        assert [r["pr"] for r in _rows(log_path)] == ["111", "222"]
        assert "[audit-log]" in capsys.readouterr().err

        _note_and_flush(waived="ci:red", pr="333", repo="o/r")
        assert "333" in [r["pr"] for r in _rows(log_path)], "writes must not stop"

    def test_the_row_survives_a_maintainer_that_explodes(self, log_path, capsys):
        """The structural fix behind the blocker: a maintainer that raises cannot
        cost the caller the row it is writing.

        MEASURED, and worth stating because the intuitive answer is wrong: removing
        the per-maintainer ``try`` makes this test RED, while merely reordering the
        append and the maintenance does NOT. Isolation is the guarantee; ordering is
        a secondary safeguard.
        """
        import audit_jsonl

        def _boom(_path):
            raise RuntimeError("retention is broken")

        _mod._PENDING_OVERRIDES.clear()
        _mod._note_override("ci-override", waived="ci:red", pr="1", repo="o/r")
        row = {k: "" for k in _mod._OVERRIDE_LOG_FIELDS}
        assert audit_jsonl.append_row(str(log_path), row, maintain=[_boom])
        assert len(_rows(log_path)) == 1
        assert "maintenance failed" in capsys.readouterr().err

    def test_unparseable_rows_are_kept_not_dropped(self, log_path):
        log_path.parent.mkdir(parents=True)
        log_path.write_text("not json at all\n")
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert "not json at all" in log_path.read_text()

    def test_size_backstop_bounds_a_file_of_unprunable_rows(self, log_path, monkeypatch):
        """Age retention KEEPS what it cannot read, so size is the real bound.

        The bound must be measured against the SEEDED size, not a round number: an
        earlier version asserted ``< 2000`` on a 1600-byte seed plus a 166-byte
        row, which is 1766 — i.e. it passed with the trim entirely inert, and was
        the only test of the property the module calls the real bound.
        """
        monkeypatch.setattr(_mod, "_OVERRIDE_LOG_MAX_BYTES", 200)
        log_path.parent.mkdir(parents=True)
        seeded = "garbage\n" * 200
        log_path.write_text(seeded)
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        after = log_path.stat().st_size
        assert after < len(seeded), f"trim did not shrink the file ({after} >= {len(seeded)})"
        # ...and it keeps the RECENT half: the new row must survive its own trim.
        assert '"pr": "1"' in log_path.read_text()


class TestWiredIntoTheMergePath:
    """E2E: the CALL SITES fire, not just the helper.

    A helper that works and is never called is indistinguishable from no helper at
    all — and that is exactly the state this change found the gate in. These drive
    the real ``main()`` with a real merge command.
    """

    def _drive(self, monkeypatch, command, *, ci="SUCCESS", mergeable="MERGEABLE"):
        monkeypatch.delenv("CLAUDE_TOOL_INPUT", raising=False)
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
        monkeypatch.setenv("_TEST_GH_DEFAULT_BRANCH", "main")
        # NETWORK-FREE BY CONSTRUCTION. Without these three seams the Codex
        # freshness and scheduled-review gates issue live `gh api` reads against
        # the real repo, so these tests were both flaky and MEASURING SOMETHING
        # ELSE: four of them blocked on a real PR's review state while their
        # docstrings claimed to exercise "a real merge". Mirrors the seam set in
        # test_merge_gate_characterization.py, which is network-free by design.
        monkeypatch.setenv(
            "_TEST_GH_CODEX_REVIEWS",
            json.dumps({"login": "chatgpt-codex-connector[bot]", "commit_id": HEAD}),
        )
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            json.dumps(
                {
                    "login": "owner",
                    "author_association": "OWNER",
                    "body": f"<!-- genesis-scheduled-review: head={HEAD} kind=code-review -->\n"
                    f"<!-- genesis-scheduled-review: head={HEAD} kind=leaks -->",
                }
            ),
        )
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([{"name": "t", "workflowName": "CI", "conclusion": ci}]),
        )
        monkeypatch.setattr(_mod, "_check_mergeable", lambda n, repo=None: mergeable)
        monkeypatch.setattr(
            _mod, "_check_pr_review_findings", lambda n, force=False, repo=None: (False, "")
        )
        monkeypatch.setattr(
            _mod, "_check_inline_review_findings", lambda n, force=False, repo=None: (False, "")
        )
        monkeypatch.setattr(
            _mod,
            "read_payload",
            lambda: {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
        )
        return _mod.main()

    def test_ci_override_on_a_real_merge_is_recorded(self, monkeypatch, log_path):
        self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}  # ci-override",
            ci="FAILURE",
        )
        (row,) = _rows(log_path)
        assert row["sigil"] == "ci-override"
        assert row["pr"] == "77"
        assert row["waived"].startswith("ci:")

    def test_the_row_carries_the_head_the_merge_is_bound_to(self, monkeypatch, log_path):
        """Production passed no head at all in the first cut, so the store could not
        answer the first question anyone would ask it: which commit merged?"""
        self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}  # ci-override",
            ci="FAILURE",
        )
        assert _rows(log_path)[0]["head"] == HEAD

    def test_review_override_on_a_real_merge_is_recorded(self, monkeypatch, log_path):
        """The sigil the genesis-development skill documents as logged. It is read
        off merge_seg.override, so enumerating has_trailing_override's call sites —
        how the first cut chose its population — missed it entirely."""
        self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}  # review-override",
        )
        assert "review-override" in [r["sigil"] for r in _rows(log_path)]

    def test_a_blocked_command_is_tagged_blocked(self, monkeypatch, log_path):
        """The distortion that made the live file useless: rows fired on ATTEMPTS
        with no way to tell them from overrides that took effect. Here the sigil
        waives CI, but the merge is still blocked by the head-binding gate."""
        rc = self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {OTHER}  # ci-override",
            ci="FAILURE",
        )
        assert rc == 2
        assert [r["outcome"] for r in _rows(log_path)] == ["blocked"]

    def test_a_sigil_is_recorded_even_when_an_EARLIER_gate_blocks(self, monkeypatch, log_path):
        """Noting at each gate's own site recorded NOTHING when a gate upstream of
        it blocked — dropping exactly the attempts this log exists to count.
        MEASURED then: a CONFLICTING mergeable with three sigils wrote zero rows.
        """
        rc = self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}"
            "  # ci-override stale-review-override scheduled-review-override",
            mergeable="CONFLICTING",
        )
        assert rc == 2
        rows = _rows(log_path)
        assert sorted(r["sigil"] for r in rows) == [
            "ci-override",
            "scheduled-review-override",
            "stale-review-override",
        ]
        assert {r["outcome"] for r in rows} == {"blocked"}
        # The CI gate never ran, so the row keeps the gate CLASS, not a verdict.
        ci = next(r for r in rows if r["sigil"] == "ci-override")
        assert ci["waived"] == "ci-status"

    def test_an_override_that_TAKES_EFFECT_is_recorded_as_allowed(self, monkeypatch, log_path):
        """The distinction the whole outcome field exists for, asserted E2E.

        Every other wired test asserted `blocked` or `asked`, or asserted no
        outcome at all — so mislabelling every EFFECTIVE override as "asked"
        (i.e. as still-undecided) left the whole suite green.
        """
        rc = self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}  # ci-override",
            ci="FAILURE",
        )
        assert rc == 0, "the sigil should have let this through"
        assert [r["outcome"] for r in _rows(log_path)] == ["allowed"]

    def test_escalation_ack_on_an_unrelated_command_records_nothing(self, monkeypatch, log_path):
        """`escalation-ack` is a SHARED sigil — it is also the commit gate's ack,
        printed by review_enforcement_commit.py as `git commit -m "…"  # escalation-ack`.

        MEASURED before this control existed: `git commit`, `git status` and even
        `pytest` each wrote a row claiming the codex-round-escalation cap had been
        waived, for a gate that was never consulted and with no PR to reconcile
        against. That would have made this sigil's dominant population fiction.
        """
        for cmd in (
            'git commit -m "wip"  # escalation-ack',
            "git status  # escalation-ack",
            "pytest tests/test_x.py -q  # escalation-ack",
        ):
            self._drive(monkeypatch, cmd)
            assert _rows(log_path) == [], f"{cmd!r} fabricated a row"

    def test_escalation_ack_is_recorded(self, monkeypatch, log_path):
        """The cap-BYPASS sigil — the escape from the anti-whack-a-mole gate, and
        the highest-signal one. It IS a has_trailing_override call site, and was
        still missed by enumerating that helper's call sites."""
        self._drive(monkeypatch, 'gh pr comment 5 --body "@codex review"  # escalation-ack')
        (row,) = _rows(log_path)
        assert row["sigil"] == "escalation-ack"
        assert row["pr"] == "5"
        assert row["waived"] == "codex-round-escalation-cap"

    def test_an_ask_decision_is_not_recorded_as_allowed(self, monkeypatch, log_path, capsys):
        """`_ask` returns 0 while emitting a prompt the human may still DENY, so
        rc==0 is three states. Recording it as allowed would have the log assert a
        merge that never happened."""
        monkeypatch.setattr(
            _mod,
            "read_payload",
            lambda: {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {
                    "command": "git merge feat  # merge-to-main-override && git push origin feat",
                    "cwd": "/home/ubuntu/tmp",
                },
            },
        )
        rc = _mod.main()
        assert rc == 0
        assert '"ask"' in capsys.readouterr().out
        assert [r["outcome"] for r in _rows(log_path)] == ["asked"]

    def test_no_sigil_writes_nothing(self, monkeypatch, log_path):
        """Positive control's twin: an ordinary merge must not log."""
        self._drive(monkeypatch, f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}")
        assert _rows(log_path) == []

    def test_merge_command_text_never_reaches_the_log(self, monkeypatch, log_path):
        """The credential guard, exercised through the real path rather than the helper."""
        secret = "ghp_EXAMPLETOKENVALUE1234567890"
        self._drive(
            monkeypatch,
            f"GH_TOKEN={secret} gh pr merge 77 --squash --admin "
            f"--match-head-commit {HEAD}  # ci-override",
            ci="FAILURE",
        )
        text = log_path.read_text() if log_path.exists() else ""
        assert secret not in text
        assert "GH_TOKEN" not in text

    def test_merge_to_main_override_is_recorded(self, monkeypatch, tmp_path, log_path):
        """The fifth merge-path sigil, and the second one the first cut missed."""
        monkeypatch.setattr(
            _mod,
            "read_payload",
            lambda: {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {
                    "command": "git merge feature  # merge-to-main-override",
                    "cwd": str(tmp_path),
                },
            },
        )
        _mod.main()
        assert [r["sigil"] for r in _rows(log_path)] == ["merge-to-main-override"]


class TestLoggingCannotCostTheVerdict:
    """A hook's block is delivered by RETURNING in time. Anything the audit path
    can do to delay that return is a security bug, not a latency one."""

    def test_a_held_lock_drops_the_row_not_the_verdict(self, log_path, capsys):
        """MEASURED before the bound existed: a second process holding the sidecar
        hung the merge gate with no verdict until it was killed at 20s. Past the
        harness wall-clock that is a SIGKILL, which is not exit 2, which lets the
        guarded command run — a fail-CLOSED gate turned fail-OPEN."""
        log_path.parent.mkdir(parents=True)
        lock = str(log_path) + ".lock"
        holder = subprocess.Popen(["flock", lock, "-c", "sleep 10"])
        try:
            time.sleep(0.4)  # let flock actually take it
            started = time.monotonic()
            _note_and_flush(waived="ci:red", pr="1", repo="o/r")
            elapsed = time.monotonic() - started
        finally:
            holder.terminate()
            holder.wait(timeout=5)
        # HARD number, not LOCK_TIMEOUT_S + slack: deriving the bound from the
        # constant under test detects the bound's REMOVAL but not its loosening —
        # raising LOCK_TIMEOUT_S to 8.0 (a 16x regression on a hook with a 60s
        # wall clock) kept that version green.
        assert elapsed < 2.0, (
            f"the audit write blocked for {elapsed:.1f}s — a hook that cannot "
            "return in time cannot deliver a block"
        )
        assert _mod.audit_jsonl.LOCK_TIMEOUT_S <= 1.0, "the bound itself has drifted"
        assert _rows(log_path) == [], "the row is what gets dropped, not the verdict"
        assert "lock busy" in capsys.readouterr().err

    def test_the_row_lands_when_the_lock_is_free(self, log_path):
        """Positive control: without it, the test above passes on a broken writer."""
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert len(_rows(log_path)) == 1


class TestSigilRunRegression:
    """``merge-to-main-override`` was passed to has_trailing_override from the day it
    shipped but never listed in _KNOWN_SIGILS, so the leading-run scan treated it as
    PROSE and silently ended the run — disabling every sigil written after it."""

    def test_an_unlisted_leading_sigil_no_longer_ends_the_run(self):
        from shell_parse import has_trailing_override as h

        assert h("git merge f  # merge-to-main-override audit-ack", "audit-ack")
        assert h("git merge f  # audit-ack merge-to-main-override", "audit-ack")
        assert h("git merge f  # merge-to-main-override", "merge-to-main-override")

    def test_prose_still_ends_the_run(self):
        from shell_parse import has_trailing_override as h

        assert not h("git merge f  # see merge-to-main-override docs", "merge-to-main-override")

    def test_every_sigil_any_guard_queries_is_declared(self):
        """``_KNOWN_SIGILS`` claims to be kept in sync with the sigils actually
        passed to has_trailing_override "across the guard hooks". Twice now it
        was not, and both times the consequence was silent: an undeclared token
        ends the leading run, disabling every sigil written after it.

        Derived with ``ast`` over every CONSUMER, not a regex over one file. Two
        guards pass their sigil as a module constant (``_OVERRIDE_SIGIL``,
        ``_OVERRIDE``), so a literal-only scan cannot see them — which is how
        ``full-suite-ok`` stayed undeclared while a regex test passed green.

        The population is `scripts/**.py`, NOT `scripts/hooks/*.py`: a
        hooks-only glob misses ``review_enforcement_commit.py``, which is where
        ``audit-ack`` and ``depth-ack`` are queried — 2 of the declared sigils,
        invisible to a scan that claimed to cover "every hook".
        """
        from shell_parse import _KNOWN_SIGILS

        queried: dict[str, str] = {}
        for path in sorted(_HOOKS.parent.rglob("*.py")):
            tree = ast.parse(path.read_text())
            consts = {}
            for n in ast.walk(tree):
                # Assign (`X = "s"`) AND AnnAssign (`X: str = "s"`) — a scan that
                # handled only the first missed an annotated constant silently.
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant):
                    targets = [t for t in n.targets if isinstance(t, ast.Name)]
                elif (
                    isinstance(n, ast.AnnAssign)
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.target, ast.Name)
                ):
                    targets = [n.target]
                else:
                    continue
                if isinstance(n.value.value, str):
                    for t in targets:
                        consts[t.id] = n.value.value
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name not in ("has_trailing_override", "_has_trailing_override"):
                    continue
                arg = node.args[1] if len(node.args) > 1 else None
                for kw in node.keywords:
                    if kw.arg == "sigil":
                        arg = kw.value
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    queried[arg.value] = path.name
                elif isinstance(arg, ast.Name) and arg.id in consts:
                    queried[consts[arg.id]] = path.name

        # Guard the guard, against the CONSUMERS not a magic number: a floor of 5
        # was satisfied by three files even when a fourth was silently dropped
        # from the population, so it could not detect a lost consumer.
        files = set(queried.values())
        assert files >= {
            "git_push_guard.py",
            "git_discard_guard.py",
            "full_suite_guard.py",
            "review_enforcement_commit.py",
        }, f"a known consumer produced no sigils — the walk went blind: {sorted(files)}"
        undeclared = {s: f for s, f in queried.items() if s not in _KNOWN_SIGILS}
        assert not undeclared, f"queried but not declared in _KNOWN_SIGILS: {undeclared}"


def test_the_live_default_path_is_outside_any_repo(monkeypatch):
    """The log must survive worktree removal and never be committed."""
    monkeypatch.delenv("GENESIS_MERGE_OVERRIDE_LOG", raising=False)
    assert _mod._override_log_path().endswith("/.genesis/merge_override_log.jsonl")


def test_a_relative_env_override_is_refused(monkeypatch):
    """A relative path resolves against the hook's cwd — the repo — putting a
    durable audit file inside the working tree where it can be committed."""
    monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_LOG", "merge_override_log.jsonl")
    assert _mod._override_log_path().endswith("/.genesis/merge_override_log.jsonl")
