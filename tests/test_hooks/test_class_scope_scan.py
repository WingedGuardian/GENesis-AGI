"""Tests for the class-scope scanners.

The first two tests are the ACCEPTANCE BAR: they replay the two real review
defects that motivated these scanners. If either stops catching its defect, the
scanner has no reason to exist, so these are permanent rather than one-off
verification.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(_SCRIPTS))

from class_scope_scan import (  # noqa: E402
    find_orphaned_literals,
    find_unrevisited_uses,
    searchable_core,
    string_literals,
)


def _advisory_context(result) -> str:
    """The text a hook actually DELIVERS to the model, or "" if none.

    Asserting on stderr proved only that the hook wrote something. Claude Code
    discards stderr from a hook that exits 0, so a stderr-only advisory is
    written, logged, testable — and never seen. The model reads
    ``hookSpecificOutput.additionalContext`` on stdout and nothing else, so
    that is what these tests read too.
    """
    import json as _json

    out = (result.stdout or "").strip()
    if not out:
        return ""
    try:
        payload = _json.loads(out)
    except ValueError:
        return ""
    return payload.get("hookSpecificOutput", {}).get("additionalContext", "")


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path):
    """A tiny git repo — the scanners use `git grep`, so this must be real."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


class TestAcceptanceBar:
    """Replays of the actual defects these scanners were built for."""

    def test_orphaned_literal_names_the_untouched_sibling(self, repo):
        """Round-2 defect: one renderer's message changed, two siblings left.

        The real shape: an identical prompt string lived in three files and only
        one was updated, so two renderers kept asserting something that had
        become false.
        """
        import subprocess

        old = 'def render():\n    return "*No performance data yet.*\\n"\n'
        sibling = 'def other():\n    return "*No performance data yet.*\\n"\n'
        edited = _write(repo, "a.py", old)
        _write(repo, "b.py", sibling)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

        new = 'def render():\n    return "*No qualifying rows shown.*\\n"\n'
        findings = find_orphaned_literals(edited, old, new, repo)

        assert findings, "the orphaned sibling was not detected"
        assert findings[0]["literal"] == "*No performance data yet.*\n"
        assert [p.name for p in findings[0]["survivors"]] == ["b.py"]

    def test_provenance_names_the_unrevisited_uses(self):
        """Round-3 defect: the read was swapped, only some uses revisited.

        `entries` changed from the whole table to a filtered subset. The empty
        branch was updated; the branch computing an average over it was not, and
        went on rendering a subset figure as a whole-map fact.
        """
        old = (
            "def section():\n"
            "    entries = get_all(db)\n"
            "    if not entries:\n"
            '        return "empty"\n'
            "    avg = sum(entries) / len(entries)\n"
            "    return avg\n"
        )
        new = old.replace("get_all(db)", "get_prompt_rows(db)")

        findings = find_unrevisited_uses(old, new)

        assert findings, "the provenance change was not detected"
        f = findings[0]
        assert f["variable"] == "entries"
        assert f["was"] == "get_all"
        assert f["now"] == "get_prompt_rows"
        # The avg/len line still reads `entries` and was never touched.
        assert f["unrevisited"]


class TestOrphanedLiteralPrecision:
    def test_silent_when_the_sibling_is_updated_too(self, repo):
        import subprocess

        old = 'def a():\n    return "*No performance data yet.*\\n"\n'
        edited = _write(repo, "a.py", old)
        _write(repo, "b.py", 'def b():\n    return "*Something else entirely.*\\n"\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

        new = 'def a():\n    return "*No qualifying rows shown.*\\n"\n'
        assert find_orphaned_literals(edited, old, new, repo) == []

    def test_identifier_like_literals_do_not_fire(self, repo):
        """Measured: the only false positive over 151 real edits was an
        identifier (`run_in_background`), which is duplicated across a repo as a
        matter of course. Prose is what must change in lockstep."""
        import subprocess

        old = 'def a():\n    return "run_in_background_flag"\n'
        edited = _write(repo, "a.py", old)
        _write(repo, "b.py", 'X = "run_in_background_flag"\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

        new = 'def a():\n    return "other_flag_name_here"\n'
        assert find_orphaned_literals(edited, old, new, repo) == []

    def test_literals_containing_escapes_are_visible(self):
        """The regex trap: a first prototype used a pattern excluding
        backslashes and so could not see ANY prompt string, since they all end
        in a newline escape. It reported 'no findings' while looking at
        nothing."""
        lits = string_literals('X = "line one\\nline two"\n')
        assert "line one\nline two" in lits

    def test_searchable_core_skips_escaped_characters(self):
        # The value contains a newline; source text does not, so the grep
        # prefilter must use the longest escape-free run.
        assert searchable_core("alpha beta\ngamma") == "alpha beta"


class TestProvenancePrecision:
    def test_silent_when_every_use_is_revisited(self):
        old = (
            "def s():\n"
            "    rows = get_all(db)\n"
            "    return len(rows)\n"
        )
        new = (
            "def s():\n"
            "    rows = get_prompt_rows(db)\n"
            "    return len(rows) + 1\n"
        )
        assert find_unrevisited_uses(old, new) == []

    def test_silent_when_only_the_receiver_changed(self):
        """Measured: comparing unparsed expressions fired on rewrites that call
        the SAME function through a different receiver, where no use of the
        result needs reconsidering."""
        old = "def s():\n    v = payload.get('k', '').strip()\n    return v + v\n"
        new = "def s():\n    v = (payload.get('k') or '').strip()\n    return v + v\n"
        assert find_unrevisited_uses(old, new) == []

    def test_new_function_has_no_prior_provenance(self):
        old = "def a():\n    return 1\n"
        new = old + "\n\ndef b():\n    rows = get_all(db)\n    return len(rows)\n"
        assert find_unrevisited_uses(old, new) == []

    def test_syntax_error_is_not_fatal(self):
        assert find_unrevisited_uses("def (:", "def (:") == []
        assert string_literals("def (:") == set()


class TestBoundedCost:
    """The scan must not be able to overrun its caller's timeout.

    Cost is multiplicative — files x literals x repo size — so per-item caps do
    not bound it. Measured before the budget existed: a 20-file changeset at the
    literal cap spent ~6s of `git grep` fan-out, against a 10s hook ceiling.
    """

    def test_zero_budget_returns_immediately(self, repo):
        import subprocess
        import time

        old = 'def a():\n    return "*a distinctive prose sentence here*"\n'
        edited = _write(repo, "a.py", old)
        _write(repo, "b.py", 'X = "*a distinctive prose sentence here*"\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        new = 'def a():\n    return "*a different prose sentence entirely*"\n'

        t = time.perf_counter()
        out = find_orphaned_literals(edited, old, new, repo, budget_seconds=0.0)
        assert time.perf_counter() - t < 0.5
        assert out == [], "an exhausted budget must yield, not scan on"

    def test_normal_budget_still_finds_the_orphan(self, repo):
        """The budget must not be able to silently blind the scan."""
        import subprocess

        old = 'def a():\n    return "*a distinctive prose sentence here*"\n'
        edited = _write(repo, "a.py", old)
        _write(repo, "b.py", 'X = "*a distinctive prose sentence here*"\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        new = 'def a():\n    return "*a different prose sentence entirely*"\n'

        assert find_orphaned_literals(edited, old, new, repo, budget_seconds=1.5)


class TestGrepArgumentSafety:
    def test_literal_starting_with_a_dash_is_not_read_as_a_flag(self, repo):
        """Without `-e`, git parses such a core as an unknown option and the
        miss is silent — a false negative that looks like a clean result."""
        import subprocess

        lit = "--- section header for config ---"
        old = f'def a():\n    return "{lit}"\n'
        edited = _write(repo, "a.py", old)
        _write(repo, "b.py", f'X = "{lit}"\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        new = 'def a():\n    return "=== a totally different header ==="\n'

        findings = find_orphaned_literals(edited, old, new, repo)
        assert findings, "a leading-dash literal was silently skipped"
        assert [p.name for p in findings[0]["survivors"]] == ["b.py"]


class TestSentinelLocation:
    def test_sentinels_avoid_the_watchdog_policed_temp(self):
        """TMPDIR is Claude Code's working temp; the tmp-watchgod service kills
        sessions when it fills, and project convention forbids writing there."""
        import class_scope_advisory as adv

        assert "cc-tmp" not in str(adv._SENTINEL_DIR)
        assert str(adv._SENTINEL_DIR).startswith(str(Path.home() / "tmp"))


class TestCommitAdvisoryTargeting:
    @pytest.mark.parametrize(
        "command,expected",
        [
            ("git commit -m x", True),
            # A real directory: the resolver refuses a -C path that does not
            # exist, since such a commit cannot run anyway.
            ("git -C /tmp commit -m x", True),
            ("git add -A && git commit -m x", True),
            ("ls -la", False),
            ("git status", False),
            ("git log --oneline -5", False),
            ("echo 'git commit'", False),
        ],
    )
    def test_only_real_commits_are_targeted(self, command, expected):
        import class_scope_commit_advisory as cca

        assert (cca._commit_target_cwd(command) is not None) is expected


class TestEscapeHandlingIsAnAllowlist:
    """A blocklist of escaped characters is wrong by construction.

    Every escape it forgets (\\xNN, \\uXXXX, octal, \\a\\b\\f\\v) puts a character
    into the grep core that never appears in raw source, so the prefilter misses
    and the scan returns a confident empty result — the same silent-skip shape
    as the regex prototype this scanner replaced.
    """

    @pytest.mark.parametrize(
        "literal",
        [
            "\u2014 no qualifying rows yet today\n",   # \u escape
            "\xe9 accented prose sentence here\n",     # \x escape
            "bell \a and prose that follows it\n",     # \a
            "vertical \v tab inside prose text\n",     # \v
        ],
    )
    def test_core_contains_only_verbatim_characters(self, literal):
        core = searchable_core(literal)
        assert core, "no searchable core extracted"
        # Whatever survives must appear literally in a source rendering.
        assert core in repr(literal), (
            f"core {core!r} is not present verbatim in the source form"
        )

    def test_exotic_escape_orphan_is_still_found(self, repo):
        import subprocess

        lit = "\u2014 no qualifying rows yet today\n"
        old = f"def a():\n    return {lit!r}\n"
        edited = _write(repo, "a.py", old)
        _write(repo, "b.py", f"X = {lit!r}\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        new = 'def a():\n    return "*a completely different sentence*\\n"\n'

        findings = find_orphaned_literals(edited, old, new, repo)
        assert findings, "a literal with a unicode escape was silently skipped"
        assert [p.name for p in findings[0]["survivors"]] == ["b.py"]

    def test_embedded_nul_does_not_raise(self, repo):
        """ValueError from subprocess is neither SubprocessError nor OSError."""
        import subprocess

        old = 'X = "alpha beta gamma \x00 delta epsilon zeta"\n'
        edited = _write(repo, "a.py", old)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        new = 'X = "something else entirely for this one"\n'
        assert find_orphaned_literals(edited, old, new, repo) == []


class TestProvenanceScopeAndReassignment:
    def test_same_method_name_in_two_classes_does_not_collide(self):
        """Keying by bare name compares one class's method to the other's.

        The file that motivated this scanner has the same method name in two
        builder classes, so a last-one-wins map would have missed its defect.
        """
        old = (
            "class A:\n"
            "    def h(self, db):\n"
            "        rows = get_all(db)\n"
            "        return len(rows)\n"
            "class B:\n"
            "    def h(self, db):\n"
            "        rows = get_all(db)\n"
            "        return len(rows)\n"
        )
        new = old.replace(
            "        rows = get_all(db)\n        return len(rows)\n"
            "class B:", "        rows = get_prompt_rows(db)\n"
            "        return len(rows)\nclass B:", 1
        )
        findings = find_unrevisited_uses(old, new)
        assert findings, "the change in A.h was masked by B.h"
        assert findings[0]["function"] == "A.h"

    def test_later_reassignment_does_not_mask_the_change(self):
        """Last-assignment-wins silences the exact motivating case."""
        old = (
            "def s(db):\n"
            "    entries = get_all(db)\n"
            "    entries = normalize(entries)\n"
            "    return len(entries) / total(entries)\n"
        )
        new = old.replace("get_all(db)", "get_prompt_rows(db)")
        findings = find_unrevisited_uses(old, new)
        assert findings, "a reassignment after the read hid the provenance change"
        assert "get_all" in findings[0]["was"]
        assert "get_prompt_rows" in findings[0]["now"]

    def test_walrus_provenance_is_visible(self):
        old = "def s(db):\n    if (entries := get_all(db)):\n        return len(entries)\n"
        new = old.replace("get_all(db)", "get_prompt_rows(db)")
        assert find_unrevisited_uses(old, new)

    def test_annotated_assignment_provenance_is_visible(self):
        old = "def s(db):\n    rows: list = get_all(db)\n    return len(rows)\n"
        new = old.replace("get_all(db)", "get_prompt_rows(db)")
        assert find_unrevisited_uses(old, new)

    def test_dotted_callee_change_is_detected(self):
        """Pins Attribute handling in _callee_identity, which was unconstrained."""
        old = "def s(db):\n    rows = crud.get_all(db)\n    return len(rows)\n"
        new = old.replace("crud.get_all(db)", "crud.get_prompt_rows(db)")
        findings = find_unrevisited_uses(old, new)
        assert findings and findings[0]["was"] == "get_all"


class TestBudgetIsEnforcedInsideTheSurvivorLoop:
    def test_many_matching_files_still_respect_the_budget(self, repo):
        """A single common core can match hundreds of files.

        Checking the deadline only between literals lets one literal's survivor
        loop run unbounded, which is where the real overrun lives.
        """
        import subprocess
        import time

        core = "the quick brown fox jumps over lazy dogs"
        old = f'def a():\n    return "{core}"\n'
        edited = _write(repo, "a.py", old)
        # Survivors must be EXPENSIVE TO PARSE, not merely numerous: the inner
        # deadline guards the ast.parse loop, and over tiny files that loop is
        # free, so a weaker fixture cannot tell the check from its absence.
        bulk = "".join(
            f"def f{n}(a, b):\n    return (a + b) * {n} if a else b\n"
            for n in range(1200)
        )
        for i in range(60):
            _write(repo, f"pkg/mod{i}.py", f'X = "{core}"\n{bulk}')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        new = 'def a():\n    return "an entirely different sentence here"\n'

        t = time.perf_counter()
        find_orphaned_literals(edited, old, new, repo, budget_seconds=0.05)
        elapsed = time.perf_counter() - t
        assert elapsed < 1.5, f"budget overrun: {elapsed:.2f}s for a 0.05s budget"


class TestHooksEndToEnd:
    """Run both hooks as real subprocesses with real payloads.

    Nothing previously executed either ``main()``, so deleting the dedup, or the
    provenance call, or the whole advisory body, left the suite green. These run
    the actual entry points the way Claude Code runs them.
    """

    _HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"

    @pytest.fixture
    def diverged(self, tmp_path):
        """A repo where one file's message changed and its sibling's did not."""
        import subprocess

        for cmd in (["git", "init", "-q"],
                    ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
        msg = '"*No performance data yet for this domain.*\\n"'
        (tmp_path / "a.py").write_text(f"def a():\n    return {msg}\n")
        (tmp_path / "b.py").write_text(f"def b():\n    return {msg}\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True,
                       capture_output=True)
        (tmp_path / "a.py").write_text(
            'def a():\n    return "*No qualifying rows are shown here.*\\n"\n'
        )
        return tmp_path

    def _run(self, script, payload, cwd):
        import json
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, str(self._HOOKS / script)],
            input=json.dumps(payload), capture_output=True, text=True,
            timeout=60, cwd=str(cwd),
        )

    def test_edit_advisory_reports_and_never_blocks(self, diverged, monkeypatch):
        monkeypatch.setenv("HOME", str(diverged))  # isolate the sentinel dir
        r = self._run(
            "class_scope_advisory.py",
            {"tool_name": "Edit", "session_id": "t1",
             "tool_input": {"file_path": str(diverged / "a.py")}},
            diverged,
        )
        assert r.returncode == 0, "an advisory must never block an edit"
        ctx = _advisory_context(r)
        assert "b.py" in ctx

    def test_edit_advisory_dedups_within_a_session(self, diverged, monkeypatch):
        monkeypatch.setenv("HOME", str(diverged))
        payload = {"tool_name": "Edit", "session_id": "t2",
                   "tool_input": {"file_path": str(diverged / "a.py")}}
        first = self._run("class_scope_advisory.py", payload, diverged)
        second = self._run("class_scope_advisory.py", payload, diverged)
        assert "b.py" in _advisory_context(first)
        assert not _advisory_context(second), "repeated edits must not nag"

    def test_commit_advisory_reads_the_staged_blob(self, diverged):
        """Staged content, not the worktree: they differ after `git add`."""
        import subprocess

        subprocess.run(["git", "add", "-A"], cwd=diverged, check=True,
                       capture_output=True)
        # Now diverge the WORKTREE from the index; the advisory must ignore this.
        (diverged / "a.py").write_text('def a():\n    return "unstaged text"\n')

        r = self._run(
            "class_scope_commit_advisory.py",
            {"tool_name": "Bash", "session_id": "t3",
             "tool_input": {"command": f"git -C {diverged} commit -m x"}},
            diverged,
        )
        assert r.returncode == 0
        assert "b.py" in _advisory_context(r), (
            "the staged divergence was not reported"
        )

    @pytest.mark.parametrize("command", ["ls -la", "git status", "git tag commit"])
    def test_commit_advisory_silent_on_non_commits(self, diverged, command):
        r = self._run(
            "class_scope_commit_advisory.py",
            {"tool_name": "Bash", "session_id": "t4",
             "tool_input": {"command": command}},
            diverged,
        )
        assert r.returncode == 0
        assert not _advisory_context(r), f"{command!r} should not trigger the scan"

    def test_edit_advisory_reports_provenance_too(self, tmp_path, monkeypatch):
        """The advisory must surface BOTH scans, not just orphaned literals.

        Deleting the provenance call from the hook's main() previously left the
        whole suite green, because every end-to-end case exercised only the
        literal path.
        """
        import subprocess

        monkeypatch.setenv("HOME", str(tmp_path))
        for cmd in (["git", "init", "-q"],
                    ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
        src = (
            "def section(db):\n"
            "    entries = get_all(db)\n"
            "    if not entries:\n"
            "        return 'none'\n"
            "    return sum(entries) / len(entries)\n"
        )
        (tmp_path / "m.py").write_text(src)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True,
                       capture_output=True)
        (tmp_path / "m.py").write_text(src.replace("get_all(db)", "get_prompt_rows(db)"))

        r = self._run(
            "class_scope_advisory.py",
            {"tool_name": "Edit", "session_id": "prov",
             "tool_input": {"file_path": str(tmp_path / "m.py")}},
            tmp_path,
        )
        ctx = _advisory_context(r)
        assert r.returncode == 0
        assert "entries" in ctx and "get_prompt_rows" in ctx, (
            "the provenance scan was not reported by the hook"
        )


class TestLiteralMultiplicityAndConcatenation:
    """Two ways a real change becomes invisible while looking like a clean run."""

    def test_changing_one_of_two_copies_in_a_file_is_visible(self, repo):
        """Set difference sees nothing when the file keeps another copy."""
        import subprocess

        lit = "*No performance data yet for this domain.*"
        old = f'def a():\n    return "{lit}"\n\n\ndef a2():\n    return "{lit}"\n'
        edited = _write(repo, "a.py", old)
        _write(repo, "b.py", f'X = "{lit}"\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        # Change only the FIRST occurrence; the second keeps the old text alive.
        new = old.replace(f'"{lit}"', '"*No qualifying rows are shown.*"', 1)

        findings = find_orphaned_literals(edited, old, new, repo)
        assert findings, "a change was masked by a second copy in the same file"
        assert [p.name for p in findings[0]["survivors"]] == ["b.py"]

    def test_implicitly_concatenated_literal_is_greppable(self, repo):
        """The joined VALUE never appears contiguously in source.

        A core taken from the value is a pattern git grep can never match, so
        the sibling is skipped in silence.
        """
        import subprocess

        old = (
            "def a():\n"
            '    return ("a distinctive opening fragment "\n'
            '            "and its continuation here")\n'
        )
        edited = _write(repo, "a.py", old)
        _write(repo, "b.py",
               'X = ("a distinctive opening fragment "\n'
               '     "and its continuation here")\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        new = 'def a():\n    return "something entirely different now"\n'

        findings = find_orphaned_literals(edited, old, new, repo)
        assert findings, "an implicitly concatenated literal was silently skipped"
        assert [p.name for p in findings[0]["survivors"]] == ["b.py"]


class TestCommitTargetResolution:
    @pytest.mark.parametrize(
        "command,expected_suffix",
        [
            ("git commit -m x", ""),
            ("git -C /tmp/other commit -m x", "/tmp/other"),
            ("git -c user.name=z commit -m x", ""),
            ("git tag commit", None),
            ("git status", None),
            ("ls -la", None),
        ],
    )
    def test_target_follows_the_command_not_the_hook_cwd(
        self, command, expected_suffix, tmp_path, monkeypatch
    ):
        import os

        import class_scope_commit_advisory as cca

        monkeypatch.chdir(tmp_path)
        os.makedirs("/tmp/other", exist_ok=True)
        got = cca._commit_target_cwd(command)
        if expected_suffix is None:
            assert got is None, f"{command!r} should not be treated as a commit"
        elif expected_suffix:
            assert got == expected_suffix, (
                f"{command!r} must target its -C path, not the hook's cwd"
            )
        else:
            assert got == str(tmp_path)
