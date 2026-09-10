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
* Records are own-user-only, created without a umask window. One file per
  flush, so there is no shared file to serialise and no lock.
"""

import importlib.util
import json
import os
import stat
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
def log_dir(tmp_path, monkeypatch):
    p = tmp_path / "sub" / "merge_overrides"  # parent does NOT exist
    monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_DIR", str(p))
    return p


def _files(directory):
    """Every record file, oldest first — the order the pruner also uses.

    REGULAR files only, without following links: a reader of this store must skip
    anything else, exactly as ``trim_dir_by_size`` does. A plain glob would try to
    read a symlink or FIFO planted in the directory, which is the very thing the
    writer's ``O_EXCL|O_NOFOLLOW`` refuses to write through.
    """
    if not directory.exists():
        return []
    return sorted(f for f in directory.glob("*.jsonl") if f.is_file() and not f.is_symlink())


def _rows(directory):
    """Every row across every file in the store, in file order then line order."""
    return [
        json.loads(line)
        for f in _files(directory)
        for line in f.read_text().splitlines()
        if line.strip()
    ]


def _note_and_flush(outcome="allowed", **kw):
    _mod._PENDING_OVERRIDES.clear()
    _mod._note_override(kw.pop("sigil", "ci-override"), **kw)
    _mod._flush_overrides(outcome)


class TestWriter:
    def test_records_a_row(self, log_dir):
        _note_and_flush(waived="ci:red", pr="1525", repo="o/r", head=HEAD)
        (row,) = _rows(log_dir)
        assert row["sigil"] == "ci-override"
        assert row["pr"] == "1525"
        assert row["head"] == HEAD
        assert row["waived"] == "ci:red"
        assert row["outcome"] == "allowed"
        assert row["ts"]

    def test_creates_a_missing_parent_directory(self, log_dir):
        """A missing parent silently made the whole feature a no-op in the first cut."""
        assert not log_dir.parent.exists()
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert len(_rows(log_dir)) == 1

    def test_appends_rather_than_truncates(self, log_dir):
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        _note_and_flush(sigil="stale-review-override", waived="codex", pr="2", repo="o/r")
        assert [r["pr"] for r in _rows(log_dir)] == ["1", "2"]

    def test_one_row_per_sigil_on_a_multi_sigil_merge(self, log_dir):
        """A merge routinely carries several sigils; per-sigil rates must stay countable."""
        _mod._PENDING_OVERRIDES.clear()
        _mod._note_override("stale-review-override", waived="codex", pr="9", repo="o/r")
        _mod._note_override("scheduled-review-override", waived="sched", pr="9", repo="o/r")
        _mod._flush_overrides("allowed")
        assert sorted(r["sigil"] for r in _rows(log_dir)) == [
            "scheduled-review-override",
            "stale-review-override",
        ]

    def test_rejects_a_sigil_outside_the_closed_set(self, log_dir):
        """The sigil field is a closed set (shell_parse._KNOWN_SIGILS), never free text."""
        _note_and_flush(sigil="not-a-sigil", waived="ci")
        assert _rows(log_dir) == []

    def test_never_raises_on_an_unwritable_path(self, tmp_path, monkeypatch, capsys):
        """Best-effort: a logging failure must never break a merge — but it is LOUD."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_DIR", str(blocker / "sub"))
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert "[audit-log]" in capsys.readouterr().err

    def test_files_and_directory_are_own_user_only(self, log_dir):
        """The store sits in ``~/.genesis`` beside secrets, so both levels matter.

        Records carry local repo paths, branch names and PR numbers. The directory
        must not be listable by others and the files must not be readable by them.
        """
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
        for f in _files(log_dir):
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f

    def test_the_store_is_never_resolved_by_NAME_a_second_time(self, log_dir):
        """The property that survives the rewrite, and the reason for its shape.

        The design this replaces held a verified descriptor and then re-resolved the
        same path to inspect it — the generator behind most of its defects, because
        a name can be swapped between two resolutions. One file per flush removes the
        need entirely: the single open CREATES the file. Nothing here can be asserted
        behaviourally (both designs write identical bytes), so the property is
        asserted directly. Opening by DESCRIPTOR is fine and is the point.
        """
        import builtins

        real_open = builtins.open
        by_name: list[str] = []

        def only_by_fd(file, *a, **kw):
            if not isinstance(file, int):  # an int is a descriptor, not a name
                by_name.append(str(file))
            return real_open(file, *a, **kw)

        builtins.open = only_by_fd
        try:
            _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        finally:
            builtins.open = real_open
        reopened = [p for p in by_name if p.startswith(str(log_dir))]
        assert not reopened, f"the log was resolved by name a second time: {reopened}"
        assert len(_rows(log_dir)) == 1, "the row itself must still be written"

    def test_a_planted_name_cannot_redirect_or_hang_the_write(self, log_dir):
        """The whole hazard class, in one test, against the shape that removes it.

        The previous design opened ONE well-known name, so a symlink planted there
        redirected the write and a FIFO there blocked the caller in the KERNEL —
        before any timeout could bound it, which on a verdict path is a fail-open.

        Here the open is ``O_CREAT|O_EXCL|O_NOFOLLOW``, so a name that already
        exists — as a symlink, a FIFO, or anything else — cannot be opened at all:
        it yields ``EEXIST`` and the next candidate name is tried. Both plants are
        placed at the first two candidates the writer will try, so it must step
        over both, touch neither, and still record the row promptly.
        """
        victim = log_dir.parent / "victim.txt"
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_text("untouched")
        log_dir.mkdir(parents=True, exist_ok=True)

        import audit_jsonl

        stamp, pid = audit_jsonl._stamp(), os.getpid()
        monkey = pytest.MonkeyPatch()
        monkey.setattr(audit_jsonl, "_stamp", lambda: stamp)
        try:
            (log_dir / f"{stamp}Z-{pid}-0.jsonl").symlink_to(victim)
            os.mkfifo(log_dir / f"{stamp}Z-{pid}-1.jsonl")
            started = time.monotonic()
            _note_and_flush(waived="ci:red", pr="1", repo="o/r")
            elapsed = time.monotonic() - started
        finally:
            monkey.undo()

        assert elapsed < 2.0, "a planted FIFO blocked the caller"
        assert victim.read_text() == "untouched", "the write was redirected through a symlink"
        (row,) = _rows(log_dir)
        assert row["pr"] == "1", "the row must still be recorded, at a later candidate name"

    def test_the_file_is_CREATED_restrictive_not_chmodded_afterwards(self, log_dir):
        """Pins the umask WINDOW, which a mode check structurally cannot see.

        A file created 0644 and chmodded to 0600 ends up at the same observable
        mode as one created 0600 — they differ only for the instants between the
        two calls, which is exactly the exposure ``open()``+``chmod`` has and
        ``os.open`` does not. Asserting the end state therefore passes on the
        insecure version, so this asserts the CREATE mode itself.

        This writer never chmods at all: the mode is applied at create and there is
        no second call, so the window does not exist. That makes the property
        cheaper to hold and this test the thing that keeps it true.
        """
        import audit_jsonl

        seen = {}
        real_open = audit_jsonl.os.open

        def spy(path, flags, mode=0o777):
            # O_CREAT only — a non-creating open carries a mode the kernel never
            # applies, and recording it would overwrite the create record.
            if flags & os.O_CREAT:
                seen[str(path)] = mode
            return real_open(path, flags, mode)

        audit_jsonl.os.open = spy
        try:
            _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        finally:
            audit_jsonl.os.open = real_open
        # Keyed on the RECORD FILE's own INODE, not merely "some open happened":
        # recording only modes once let a sibling file satisfy this while the record
        # was insecure. The record is published by hard-linking the staging name, so
        # the record and its scrap ARE one inode — which is why the create mode
        # applies to the record even though the record's name was never opened. Map
        # by name so a sibling file still cannot stand in for it.
        created = _files(log_dir)
        assert created, "no record file was created"
        for f in created:
            staged = str(f)[: -len(".jsonl")] + audit_jsonl._STAGING_SUFFIX
            assert staged in seen, f"{f} was never created by an os.open: {sorted(seen)}"
            assert seen[staged] == 0o600, oct(seen[staged])
            # The end state too — the create mode is only the interesting half if the
            # publish did not relax it on the way through.
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, oct(f.stat().st_mode)

    def test_the_SECOND_writer_of_waived_cannot_bypass_the_shape_check(self, log_dir):
        """Validation belongs at the chokepoint, not at each call site.

        ``waived`` has two writers — ``_note_override`` and ``_amend_note`` — and the
        shape check was originally added to the first one only, so the second wrote
        an unbounded string straight to a durable row. That is the same generator
        that produced this feature's round-over-round defects: a rule enforced per
        call site is a rule the next call site does not have.
        """
        _mod._PENDING_OVERRIDES.clear()
        _mod._note_override("ci-override", waived="ci:red", pr="1", repo="o/r")
        _mod._amend_note("ci-override", waived="x" * 300 + " 'token-shaped'")
        _mod._flush_overrides("allowed")
        (row,) = _rows(log_dir)
        assert row["waived"] == "", "an unvalidated waived reached a durable record"

    def test_a_valid_amended_waived_still_survives(self, log_dir):
        """The positive control: the chokepoint must not reject legitimate values."""
        _mod._PENDING_OVERRIDES.clear()
        _mod._note_override("ci-override", waived="ci", pr="1", repo="o/r")
        _mod._amend_note("ci-override", waived="ci:red")
        _mod._flush_overrides("allowed")
        assert _rows(log_dir)[0]["waived"] == "ci:red"

    def test_pending_rows_do_not_leak_between_flushes(self, log_dir):
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        _mod._flush_overrides("allowed")  # nothing pending
        assert len(_rows(log_dir)) == 1


class TestOutcomeTagging:
    """A sigil that was typed and a sigil that let a merge through are different facts."""

    @pytest.mark.parametrize("outcome", ["allowed", "asked", "blocked", "error"])
    def test_outcome_is_recorded(self, log_dir, outcome):
        _note_and_flush(outcome, waived="ci:red", pr="1", repo="o/r")
        assert _rows(log_dir)[0]["outcome"] == outcome

    def test_an_outcome_outside_the_closed_set_is_refused(self, log_dir):
        """The tuple is enforcement, not documentation — same as ``sigil``."""
        _note_and_flush("probably-fine", waived="ci:red", pr="1", repo="o/r")
        assert _rows(log_dir) == []


class TestNoCredentialLeak:
    """The load-bearing test. A durable log must not persist command text."""

    def test_row_carries_no_command_or_comment_text(self, log_dir):
        secret = "ghp_EXAMPLETOKENVALUE1234567890"
        _note_and_flush(
            waived="ci:red",
            pr="1",
            repo="o/r",
            # A caller passing command text must not get it persisted, even by
            # accident — the writer builds the row from a fixed field set.
            **{"raw": f"gh pr merge 1 -H 'Authorization: {secret}'  # ci-override"},
        )
        text = "".join(f.read_text() for f in _files(log_dir))
        assert secret not in text
        assert "Authorization" not in text
        assert "gh pr merge" not in text

    def test_actor_cannot_be_forged_by_the_audited_command(self, log_dir, monkeypatch):
        """`actor` is the one field whose entire job is saying who did it.

        `getpass.getuser()` looks right and is wrong: it reads $LOGNAME/$USER
        FIRST and only then passwd. MEASURED with that version — `LOGNAME=...`
        set by the very command being audited was written into the row, so a
        merge could forge its own attribution.
        """
        monkeypatch.setenv("LOGNAME", "someone-else")
        monkeypatch.setenv("USER", "someone-else")
        _note_and_flush(waived="ci-status", pr="1", repo="o/r")
        assert _rows(log_dir)[0]["actor"] != "someone-else"

    def test_waived_is_a_closed_shape(self, log_dir):
        """MEASURED before the check: a 311-char `waived` carrying quotes and a
        token-shaped string was written to the row, while the docstring claimed
        every field was shape-checked. It names a gate class, so it is closed."""
        _note_and_flush(waived="X" * 300 + " ghp_secret", pr="1", repo="o/r")
        assert _rows(log_dir)[0]["waived"] == ""

    def test_a_real_gate_class_survives(self, log_dir):
        """Positive control: the shape must not reject the values production sends."""
        for w in ("ci-status", "ci:red", "codex-freshness+base-invariant"):
            _note_and_flush(waived=w, pr="1", repo="o/r")
        assert [r["waived"] for r in _rows(log_dir)] == [
            "ci-status",
            "ci:red",
            "codex-freshness+base-invariant",
        ]

    def test_field_set_is_closed(self, log_dir):
        """The expected names are LITERAL here, deliberately.

        Comparing the row against ``_OVERRIDE_LOG_FIELDS`` compares production to
        production: adding ``reason`` to the tuple and populating it — the
        realistic way command text would ever reach this log — keeps that version
        green. Widening the row must fail a test and be a conscious edit here.
        """
        _note_and_flush(waived="ci:red", pr="1", repo="o/r")
        assert set(_rows(log_dir)[0]) == {
            "ts",
            "sigil",
            "outcome",
            "waived",
            "pr",
            "repo",
            "head",
            "actor",
        }

    def test_head_and_repo_are_shape_checked(self, log_dir):
        """`head` is whatever followed --match-head-commit — arbitrary typed text."""
        _note_and_flush(
            waived="ci:red",
            pr="1",
            repo='own\nrep/x"}{',
            head="ghp_notAShaButATokenShapedString12345",
        )
        row = _rows(log_dir)[0]
        assert row["head"] == ""
        assert row["repo"] == ""

    def test_pr_is_bounded_and_ascii(self, log_dir):
        """`pr` was the one field with no bound, and `str.isdigit()` was the wrong
        check twice: it accepts non-ASCII digits and imposes no length. That made an
        arbitrarily long row — and therefore a log-erasing trim — reachable."""
        _note_and_flush(waived="ci-status", pr="9" * 3000, repo="o/r")
        assert _rows(log_dir)[0]["pr"] == "", "an unbounded pr must not be stored"
        _mod._PENDING_OVERRIDES.clear()
        _mod._note_override("ci-override", waived="ci-status", pr="١٢٣٤٥", repo="o/r")
        _mod._flush_overrides("allowed")
        assert _rows(log_dir)[1]["pr"] == "", "non-ASCII digits must not be stored"

    def test_a_real_pr_number_survives(self, log_dir):
        """Positive control: the bound must not reject the values production sends."""
        _note_and_flush(waived="ci-status", pr="1553", repo="o/r")
        assert _rows(log_dir)[0]["pr"] == "1553"

    def test_a_valid_head_and_repo_survive(self, log_dir):
        """Positive control: the shape check must not reject real values."""
        _note_and_flush(waived="ci:red", pr="1", repo="owner/repo-name.x", head=HEAD)
        row = _rows(log_dir)[0]
        assert row["head"] == HEAD
        assert row["repo"] == "owner/repo-name.x"


class TestWiredIntoTheMergePath:
    """E2E: the CALL SITES fire, not just the helper.

    A helper that works and is never called is indistinguishable from no helper at
    all — and that is exactly the state this change found the gate in. These drive
    the real ``main()`` with a real merge command.
    """

    def _drive(self, monkeypatch, command, *, ci="SUCCESS", mergeable="MERGEABLE", rounds=1):
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
        # ONE JSON OBJECT PER LINE, per `_codex_reviews`' documented seam contract —
        # which is what makes `rounds` the round COUNT the escalation gate sees, and
        # so the only way a test here can reach that gate's cap comparisons at all.
        monkeypatch.setenv(
            "_TEST_GH_CODEX_REVIEWS",
            "\n".join(
                json.dumps({"login": "chatgpt-codex-connector[bot]", "commit_id": HEAD})
                for _ in range(rounds)
            ),
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

    def test_a_broken_store_never_costs_the_verdict(self, monkeypatch, tmp_path):
        """The one property whose violation is catastrophic, asserted not argued.

        ``_flush_overrides`` runs in ``main()``'s ``finally``, so anything it raises
        escapes into ``run_guard`` — which fails CLOSED and converts an ALLOW into a
        BLOCK. The rewrite made that path structurally more fragile by moving the
        write into a store whose directory can fail in new ways, and the old suite's
        coverage of it was deleted with the lock it tested. Three failure modes, and
        the verdict must be identical in all of them.
        """
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        cmd = f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}  # ci-override"

        monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_DIR", str(tmp_path / "fine"))
        baseline = self._drive(monkeypatch, cmd, ci="FAILURE")

        monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_DIR", str(blocker / "sub"))
        assert self._drive(monkeypatch, cmd, ci="FAILURE") == baseline, (
            "an unwritable store changed the guard's verdict"
        )

        monkeypatch.setattr(_mod, "audit_jsonl", None)
        assert self._drive(monkeypatch, cmd, ci="FAILURE") == baseline, (
            "a missing writer module changed the guard's verdict"
        )

    def test_a_broken_store_preserves_a_BLOCK_too(self, monkeypatch, tmp_path):
        """The other direction: a block must stay a block, not decay to an allow."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_DIR", str(blocker / "sub"))
        rc = self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}  # ci-override",
            mergeable="CONFLICTING",
        )
        assert rc == 2, "a broken audit store turned a BLOCK into something else"

    def test_ci_override_on_a_real_merge_is_recorded(self, monkeypatch, log_dir):
        self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}  # ci-override",
            ci="FAILURE",
        )
        (row,) = _rows(log_dir)
        assert row["sigil"] == "ci-override"
        assert row["pr"] == "77"
        assert row["waived"].startswith("ci:")

    def test_the_row_carries_the_head_the_merge_is_bound_to(self, monkeypatch, log_dir):
        """Production passed no head at all in the first cut, so the store could not
        answer the first question anyone would ask it: which commit merged?"""
        self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}  # ci-override",
            ci="FAILURE",
        )
        assert _rows(log_dir)[0]["head"] == HEAD

    def test_review_override_on_a_real_merge_is_recorded(self, monkeypatch, log_dir):
        """The sigil the genesis-development skill documents as logged. It is read
        off merge_seg.override, so enumerating has_trailing_override's call sites —
        how the first cut chose its population — missed it entirely."""
        self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}  # review-override",
        )
        assert "review-override" in [r["sigil"] for r in _rows(log_dir)]

    def test_a_blocked_command_is_tagged_blocked(self, monkeypatch, log_dir):
        """The distortion that made the live file useless: rows fired on ATTEMPTS
        with no way to tell them from overrides that took effect. Here the sigil
        waives CI, but the merge is still blocked by the head-binding gate."""
        rc = self._drive(
            monkeypatch,
            f"gh pr merge 77 --squash --admin --match-head-commit {OTHER}  # ci-override",
            ci="FAILURE",
        )
        assert rc == 2
        assert [r["outcome"] for r in _rows(log_dir)] == ["blocked"]

    def test_a_sigil_is_recorded_even_when_an_EARLIER_gate_blocks(self, monkeypatch, log_dir):
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
        rows = _rows(log_dir)
        assert sorted(r["sigil"] for r in rows) == [
            "ci-override",
            "scheduled-review-override",
            "stale-review-override",
        ]
        assert {r["outcome"] for r in rows} == {"blocked"}
        # The CI gate never ran, so the row keeps the gate CLASS, not a verdict.
        ci = next(r for r in rows if r["sigil"] == "ci-override")
        assert ci["waived"] == "ci-status"

    def test_an_override_that_TAKES_EFFECT_is_recorded_as_allowed(self, monkeypatch, log_dir):
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
        assert [r["outcome"] for r in _rows(log_dir)] == ["allowed"]

    def test_command_scoped_acks_are_not_logged_at_all(self, monkeypatch, log_dir):
        """The command-scoped acks are OUT of this store's scope — the pin for a
        decision, not an oversight. Two DIFFERENT reasons, kept distinct.

        `merge-to-main-override` waives a gate that short-circuits BEFORE the
        branch is resolved, so a row asserts a waiver that was never consulted,
        with every identifying field empty. MEASURED on a repo checked out at a
        feature branch — where the gate would have allowed the merge anyway — the
        row still claimed a local-merge-into-main waiver.

        The round-cap acks (`escalation-ack`, `final-round-accept`) are a SCOPE
        decision, not an impossibility. Under the bare short-circuit this branch
        was written against, attribution was hopeless: `escalation-ack` on
        `git commit`, `git status` and `pytest` each claimed the round cap was
        waived; after narrowing, `git commit … # escalation-ack && gh pr comment 5
        …` recorded the COMMIT gate's ack against PR 5. #1680 retired that
        short-circuit — both sigils are now honoured inside
        `_check_codex_round_escalation`'s scan loop, where the PR NUMBER and the
        real round count are resolved (`repo` only when the command carries
        `--repo` or a PR URL) — so a row naming a waiver that actually happened is
        writable today. Whether this store should carry ack-class sigils is a
        separate decision; this test pins the CURRENT answer (it does not), so a
        future change to it is deliberate rather than accidental.

        Which means the cases below must REACH the honour points, not merely run
        the scan. With one seeded review `effective == 1`, neither cap comparison
        is taken and the sigil is never consulted — a `_note_override` added inside
        either branch would leave such a test green, which is the exact accident
        this docstring claims to prevent. So the tail of this test drives the round
        count up to each cap.
        """
        for cmd in (
            'git commit -m "wip"  # escalation-ack',
            "git status  # escalation-ack",
            'gh pr comment 5 --body "@codex review"  # escalation-ack',
            'git commit -m "x"  # escalation-ack && gh pr comment 5 --body "@codex review"',
            'gh pr comment 5 --body "@codex review"  # final-round-accept',
        ):
            self._drive(monkeypatch, cmd)
            assert _rows(log_dir) == [], f"{cmd!r} produced a row"

        # THE HONOUR POINTS. Everything above runs with `effective == 1`, below both
        # caps, so the sigil is never consulted — those cases pin "the scan writes
        # nothing", not "the waiver writes nothing". These two put the round count
        # ON each cap, which is where `acked` / `final_acked` actually decide
        # something, and are therefore the cases a future `_note_override` inside
        # either branch would have to survive.
        for cmd, rounds in (
            (
                'gh pr comment 5 --body "@codex review"  # escalation-ack',
                _mod.ESCALATION_ROUND_CAP,
            ),
            (
                'gh pr comment 5 --body "@codex review"  # final-round-accept',
                _mod.FINAL_ROUND_CAP,
            ),
        ):
            self._drive(monkeypatch, cmd, rounds=rounds)
            assert _rows(log_dir) == [], f"{cmd!r} logged at the HONOUR point"

    def test_no_sigil_writes_nothing(self, monkeypatch, log_dir):
        """Positive control's twin: an ordinary merge must not log."""
        self._drive(monkeypatch, f"gh pr merge 77 --squash --admin --match-head-commit {HEAD}")
        assert _rows(log_dir) == []

    def test_merge_command_text_never_reaches_the_log(self, monkeypatch, log_dir):
        """The credential guard, exercised through the real path rather than the helper."""
        secret = "ghp_EXAMPLETOKENVALUE1234567890"
        self._drive(
            monkeypatch,
            f"GH_TOKEN={secret} gh pr merge 77 --squash --admin "
            f"--match-head-commit {HEAD}  # ci-override",
            ci="FAILURE",
        )
        text = "".join(f.read_text() for f in _files(log_dir))
        assert secret not in text
        assert "GH_TOKEN" not in text

    def test_a_local_merge_ack_writes_nothing(self, monkeypatch, tmp_path, log_dir):
        """`merge-to-main-override` is out of scope — see the class-scoped test
        above. Pinned here too because this is the path that produced the fiction:
        the ack short-circuits before the branch is resolved, so on a feature
        branch (where the gate would have allowed the merge anyway) the row still
        claimed a waiver, with pr, repo and head all empty."""
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
        assert _rows(log_dir) == []


#: The default, spelled out ONCE. Asserting only the SUFFIX was measured vacuous
#: for the property these tests claim: a mutant returning
#: `os.path.abspath(".genesis/merge_overrides")` puts the store INSIDE the
#: repo worktree — exactly "committed" — and still ends with that suffix.
_EXPECTED_DEFAULT = os.path.expanduser("~/.genesis/merge_overrides")


def test_the_live_default_path_is_outside_any_repo(monkeypatch):
    """The log must survive worktree removal and never be committed."""
    monkeypatch.delenv("GENESIS_MERGE_OVERRIDE_DIR", raising=False)
    assert _mod._override_log_dir() == _EXPECTED_DEFAULT


def test_a_relative_env_override_is_refused(monkeypatch):
    """A relative path resolves against the hook's cwd — the repo — putting a
    durable audit file inside the working tree where it can be committed."""
    monkeypatch.setenv("GENESIS_MERGE_OVERRIDE_DIR", "merge_overrides")
    assert _mod._override_log_dir() == _EXPECTED_DEFAULT
