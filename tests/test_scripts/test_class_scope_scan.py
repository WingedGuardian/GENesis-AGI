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

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from class_scope_scan import (  # noqa: E402
    MAX_LITERALS_CHECKED,
    find_orphaned_literals,
    find_unrevisited_uses,
    searchable_core,
    source_cores,
    string_literals,
)


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
        old = "def s():\n    rows = get_all(db)\n    return len(rows)\n"
        new = "def s():\n    rows = get_prompt_rows(db)\n    return len(rows) + 1\n"
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
            "\u2014 no qualifying rows yet today\n",  # \u escape
            "\xe9 accented prose sentence here\n",  # \x escape
            "bell \a and prose that follows it\n",  # \a
            "vertical \v tab inside prose text\n",  # \v
        ],
    )
    def test_core_contains_only_verbatim_characters(self, literal):
        core = searchable_core(literal)
        assert core, "no searchable core extracted"
        # Whatever survives must appear literally in a source rendering.
        assert core in repr(literal), f"core {core!r} is not present verbatim in the source form"

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
            "        rows = get_all(db)\n        return len(rows)\nclass B:",
            "        rows = get_prompt_rows(db)\n        return len(rows)\nclass B:",
            1,
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
            f"def f{n}(a, b):\n    return (a + b) * {n} if a else b\n" for n in range(1200)
        )
        for i in range(60):
            _write(repo, f"pkg/mod{i}.py", f'X = "{core}"\n{bulk}')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        new = 'def a():\n    return "an entirely different sentence here"\n'

        t = time.perf_counter()
        find_orphaned_literals(edited, old, new, repo, budget_seconds=0.05)
        elapsed = time.perf_counter() - t
        assert elapsed < 1.5, f"budget overrun: {elapsed:.2f}s for a 0.05s budget"


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
        # BOTH files, and a.py is the point. This assertion used to read
        # `== ["b.py"]`, which encoded the very bug it sat next to: the scanner
        # skipped the edited file unconditionally, so the untouched copy in
        # a2() -- the whole reason this fixture puts the literal in a.py TWICE
        # -- was never reported. The test proved the count fix worked while
        # still missing the sibling one line below the change.
        assert sorted(p.name for p in findings[0]["survivors"]) == ["a.py", "b.py"]

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
        _write(
            repo,
            "b.py",
            'X = ("a distinctive opening fragment "\n     "and its continuation here")\n',
        )
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        new = 'def a():\n    return "something entirely different now"\n'

        findings = find_orphaned_literals(edited, old, new, repo)
        assert findings, "an implicitly concatenated literal was silently skipped"
        assert [p.name for p in findings[0]["survivors"]] == ["b.py"]


class TestSurvivorInTheEditedFile:
    """A sibling can be in the file that was just edited."""

    def test_second_copy_in_the_same_file_is_reported(self, repo):
        """Removal is occurrence-COUNT based, so 2 -> 1 leaves a live copy here.

        The scanner used to skip the edited file unconditionally, which meant
        the commonest shape of all -- two identical messages in one file, one of
        them updated -- reported clean.
        """
        old = 'a = "the very same long message here"\nb = "the very same long message here"\n'
        new = 'a = "a totally different long message"\nb = "the very same long message here"\n'
        edited = _write(repo, "m.py", new)
        found = find_orphaned_literals(edited, old, new, repo)
        survivors = {s for f in found for s in f["survivors"]}
        assert edited in survivors, (
            f"the surviving copy in the edited file itself was not reported: {found}"
        )


class TestCoreComesFromTheRightText:
    """The grep core must be this literal's own text, not its neighbour's."""

    def test_implicit_concatenation_with_a_trailing_comment(self):
        """Measured failure: the core was a COMMENT, so the scan grepped prose.

        CPython gives an implicitly concatenated string ONE Constant node whose
        source span covers the whole group, so `get_source_segment` can return a
        different fragment plus any inline comment.
        """
        # The comment text is verbatim from the real occurrence, and its LENGTH
        # is load-bearing: the old code took the longest escape-free run in the
        # node's whole source span, and this comment (47 chars) beats the SQL
        # fragment (38). Shorten it and the bug stops reproducing -- which is
        # what the first version of this test did, so it passed against the bug.
        source = (
            "q = (\n"
            '    "SELECT memory_id FROM memory_metadata "'
            "  # noqa: S608 - literal fragments; values bound\n"
            '    "WHERE memory_id IN ("\n'
            ")\n"
        )
        value = "SELECT memory_id FROM memory_metadata WHERE memory_id IN ("
        core = source_cores(source)[value]
        assert "noqa" not in core, f"core came from the comment: {core!r}"
        assert core in source, f"core is not present in the source at all: {core!r}"
        assert core.startswith("SELECT memory_id"), f"wrong fragment: {core!r}"


class TestAssignmentTargetsAreEnumerated:
    """Unpacking and chained assignment bind names too."""

    def test_tuple_unpacking(self):
        old = "def f(db):\n    rows, meta = get_all(db)\n    return rows, meta\n"
        new = "def f(db):\n    rows, meta = get_prompt_rows(db)\n    return rows, meta\n"
        found = find_unrevisited_uses(old, new)
        assert any(f["variable"] == "rows" for f in found), found

    def test_chained_assignment(self):
        old = "def f(db):\n    rows = alias = get_all(db)\n    return alias\n"
        new = "def f(db):\n    rows = alias = get_prompt_rows(db)\n    return alias\n"
        found = find_unrevisited_uses(old, new)
        assert any(f["variable"] == "alias" for f in found), found


class TestScopeBoundariesUseTwoDifferentPolicies:
    """Assignments stop at a nested scope; uses deliberately do not.

    Binding both to one policy trades a false positive for a false negative.
    """

    def test_inner_assignment_is_not_attributed_to_the_outer_function(self):
        """The outer scope must OWN a use of the name, or this proves nothing.

        A first version of this test had `inner` rebind `rows` with no outer
        use. The use-scan then correctly skipped the shadowed name, so NO
        finding was emitted under EITHER assignment policy and the test passed
        against the bug. Here `outer` has its own `rows` from an unchanged call
        and reads it, so descending into `inner` when collecting assignments
        merges the inner provenance into `outer` and produces a spurious
        finding -- which is the thing being guarded against.
        """
        old = (
            "def outer(db):\n"
            "    rows = fetch_local()\n"
            "    def inner():\n"
            "        rows = get_all(db)\n"
            "        return rows\n"
            "    return len(rows), inner\n"
        )
        new = (
            "def outer(db):\n"
            "    rows = fetch_local()\n"
            "    def inner():\n"
            "        rows = get_prompt_rows(db)\n"
            "        return rows\n"
            "    return len(rows), inner\n"
        )
        found = find_unrevisited_uses(old, new)
        assert not any(f["function"] == "outer" for f in found), (
            f"an inner assignment was attributed to the enclosing scope: {found}"
        )

    def test_a_closure_reading_the_variable_counts_as_a_use(self):
        old = (
            "def outer(db):\n"
            "    rows = get_all(db)\n"
            "    def inner():\n"
            "        return len(rows)\n"
            "    return inner\n"
        )
        new = (
            "def outer(db):\n"
            "    rows = get_prompt_rows(db)\n"
            "    def inner():\n"
            "        return len(rows)\n"
            "    return inner\n"
        )
        found = find_unrevisited_uses(old, new)
        assert any(f["variable"] == "rows" and f["unrevisited"] for f in found), (
            f"a closure read of the changed variable was not counted: {found}"
        )

    def test_a_nested_scope_that_rebinds_the_name_is_not_our_use(self):
        old = (
            "def outer(db):\n"
            "    rows = get_all(db)\n"
            "    def inner(rows):\n"
            "        return len(rows)\n"
            "    return inner\n"
        )
        new = (
            "def outer(db):\n"
            "    rows = get_prompt_rows(db)\n"
            "    def inner(rows):\n"
            "        return len(rows)\n"
            "    return inner\n"
        )
        found = find_unrevisited_uses(old, new)
        for f in found:
            assert 4 not in f["unrevisited"], (
                f"a shadowed parameter was counted as a use of the outer name: {f}"
            )


class TestScannerReportsWhatItDidNotCheck:
    """A quiet result and a clean result must not look the same.

    95% of this scanner's measured misses were literals it never opened, and
    nothing in its output said so.
    """

    def _two_literals(self):
        old = 'a = "one distinctly long message here"\nb = "another distinctly long message"\n'
        new = 'a = "one CHANGED long message here ok"\nb = "another CHANGED long message xx"\n'
        return old, new

    def test_literals_over_the_cap_are_reported(self, repo):
        old, new = self._two_literals()
        skipped = []
        find_orphaned_literals(
            _write(repo, "m.py", new), old, new, repo, max_literals=1, skipped=skipped
        )
        assert any("cap" in s["reason"] for s in skipped), skipped

    def test_budget_exhaustion_is_reported(self, repo):
        """A timeout makes the result quieter; unreported, that reads as clean."""
        old, new = self._two_literals()
        skipped = []
        find_orphaned_literals(
            _write(repo, "m.py", new), old, new, repo, budget_seconds=0.0, skipped=skipped
        )
        assert skipped, "an exhausted budget reported nothing at all"
        assert all(s["reason"] == "scan budget exhausted" for s in skipped), skipped


class TestCapIsPinned:
    """The cap is a MEASURED value, not an arbitrary one.

    At 6 (the old hook-timeout value) recall was 57%; at 50 it is 100% on the
    same corpus. A silent change back would cost most of the tool's value, so
    the number is pinned rather than left to drift.
    """

    def test_cap_is_the_measured_value(self):
        assert MAX_LITERALS_CHECKED == 50, (
            "the literal cap drives recall directly -- 6 scored 57%, 50 scored "
            "100% on 47 known divergences. Re-measure before changing it."
        )


class TestGitLayerDoesNotGuess:
    """The git layer was entirely untested; every mutant of it survived.

    A mutation run over this module removed `-M`, removed `-z`, ignored rename
    records, ignored git's return code, and falsified the summary line -- and
    the suite stayed green through all five. These tests exist so that the layer
    which decides WHAT gets scanned is not the one layer nobody checks.
    """

    @staticmethod
    def _fields(*parts):
        return "\0".join(parts) + "\0"

    def test_a_type_change_record_does_not_desync_the_rest(self, monkeypatch):
        """`T` is a REAL git status, and the old parser did not know it.

        It advanced ONE field on any status it did not recognise, landing on a
        PATH -- and a path beginning with M, A or R was then read as the next
        status, desyncing everything after it. So a single `T` record made
        genuinely modified files vanish with no error. The paths here start with
        A and M precisely to reproduce that: `zz.py` must still be found.
        """
        import class_scope_scan as m

        stream = self._fields("T", "Aaa.py", "M", "Mzz.py")
        monkeypatch.setattr(m, "_git", lambda a, c: stream)
        pairs = m._changed_python_files("HEAD", "/tmp", False)
        assert ("Mzz.py", "Mzz.py") in pairs, (
            f"a type-change record desynced the parser and lost a file: {pairs}"
        )

    def test_a_genuinely_unknown_status_raises_rather_than_guessing(self, monkeypatch):
        """Do not resync by guessing: say the stream is no longer understood."""
        import class_scope_scan as m

        monkeypatch.setattr(m, "_git", lambda a, c: self._fields("Z9", "weird.py"))
        with pytest.raises(m.GitError) as excinfo:
            m._changed_python_files("HEAD", "/tmp", False)
        assert "Z9" in str(excinfo.value)

    def test_a_merge_conflict_is_not_silently_empty(self, monkeypatch):
        """`U` (unmerged) is reachable any time the tree is mid-merge."""
        import class_scope_scan as m

        stream = self._fields("U", "conf.py", "M", "other.py")
        monkeypatch.setattr(m, "_git", lambda a, c: stream)
        # U is a known status carrying one path, so this parses -- the point is
        # that `other.py` is still found rather than lost to a desync.
        pairs = m._changed_python_files("HEAD", "/tmp", False)
        assert ("other.py", "other.py") in pairs, pairs

    def test_rename_with_edit_pairs_old_and_new(self, monkeypatch):
        """--name-only yields only the destination; the old blob is at the old path."""
        import class_scope_scan as m

        stream = self._fields("R096", "src/old_name.py", "src/new_name.py")
        monkeypatch.setattr(m, "_git", lambda a, c: stream)
        assert m._changed_python_files("HEAD", "/tmp", False) == [
            ("src/old_name.py", "src/new_name.py")
        ]

    def test_copy_status_carries_two_paths_like_rename(self, monkeypatch):
        import class_scope_scan as m

        stream = self._fields("C075", "src/from.py", "src/to.py")
        monkeypatch.setattr(m, "_git", lambda a, c: stream)
        assert m._changed_python_files("HEAD", "/tmp", False) == [("src/from.py", "src/to.py")]

    def test_a_path_containing_a_space_survives(self, monkeypatch):
        """Whitespace splitting turns one real file into two bogus ones."""
        import class_scope_scan as m

        stream = self._fields("M", "src/my file.py")
        monkeypatch.setattr(m, "_git", lambda a, c: stream)
        assert m._changed_python_files("HEAD", "/tmp", False) == [
            ("src/my file.py", "src/my file.py")
        ]

    def test_deleted_files_are_not_scanned(self, monkeypatch):
        import class_scope_scan as m

        stream = self._fields("D", "src/gone.py", "M", "src/here.py")
        monkeypatch.setattr(m, "_git", lambda a, c: stream)
        assert m._changed_python_files("HEAD", "/tmp", False) == [("src/here.py", "src/here.py")]

    def test_truncated_record_raises(self, monkeypatch):
        import class_scope_scan as m

        monkeypatch.setattr(m, "_git", lambda a, c: "R096\0only-one-path.py\0")
        with pytest.raises(m.GitError):
            m._changed_python_files("HEAD", "/tmp", False)


class TestGitFailureIsNotACleanResult:
    """A failed git command must never read as "nothing to do"."""

    def test_git_raises_on_non_zero_exit(self, repo):
        import class_scope_scan as m

        with pytest.raises(m.GitError):
            m._git(["rev-parse", "no-such-ref-at-all"], str(repo))

    def test_git_optional_returns_none_where_failure_is_information(self, repo):
        """`show <ref>:<path>` failing means the file was absent there."""
        import class_scope_scan as m

        assert m._git_optional(["show", "HEAD:nope.py"], str(repo)) is None

    def test_a_bad_base_ref_exits_non_zero(self, repo, capsys, monkeypatch):
        """It used to print "no modified Python files" and exit 0.

        The chdir is load-bearing, not tidiness: `main()` resolves the
        repository with `rev-parse --show-toplevel` against the PROCESS cwd, so
        without it this ran against the real repository under test. `rc != 0`
        was then also satisfiable by `rev-parse` failing for an unrelated
        reason, which is not the path this test is named for.
        """
        import class_scope_scan as m

        monkeypatch.chdir(repo)
        rc = m.main(["--base", "no-such-ref-at-all"])
        assert rc != 0, "an unreadable base ref reported success"
        assert "no modified Python files" not in capsys.readouterr().out


class TestEveryLiteralReachesExactlyOneVerdict:
    """The invariant the scan is built around, asserted directly.

    Every candidate literal must end in EXACTLY ONE of: a finding, a `skipped`
    entry naming why it was not checked, or `examined` (carried to a definite
    clean verdict). Never zero, never two.

    Written as a property over states rather than one case per state, because
    the defects this replaces were all the same shape — a NEW exit path added
    later that returned quietly and was therefore indistinguishable from
    "clean". A case-per-state suite cannot fail on a path nobody thought to add
    a case for; this one fails on any path that forgets to record a verdict.

    Zero is the dangerous direction (a silent exit reads downstream as "nothing
    to report"), and two is the quiet one (a literal counted twice inflates the
    "not checked" denominator, which is the number this tool asks to be trusted
    on).
    """

    @staticmethod
    def _candidates(old_source: str, new_source: str, limit: int) -> list[str]:
        """The candidate set, derived the way the scan derives it."""
        from class_scope_scan import _is_prose, literal_counts

        old_counts, new_counts = literal_counts(old_source), literal_counts(new_source)
        removed = {lit for lit, n in old_counts.items() if new_counts[lit] < n}
        return sorted((x for x in removed if _is_prose(x)), key=len, reverse=True)[
            :limit
        ]

    def _assert_invariant(self, repo, old, new, *, edited_name="a.py", **kwargs):
        edited = _write(repo, edited_name, new)
        skipped: list[dict] = []
        examined: list[str] = []
        findings = find_orphaned_literals(
            edited, old, new, repo, skipped=skipped, examined=examined, **kwargs
        )

        skipped_lits = [s["literal"] for s in skipped]
        assert len(skipped_lits) == len(set(skipped_lits)), (
            f"a literal was recorded as skipped more than once, inflating the "
            f"'not checked' count: {skipped_lits}"
        )

        overlap = set(examined) & set(skipped_lits)
        assert not overlap, (
            f"literal(s) both examined AND skipped — verdict ambiguous: {overlap}"
        )

        for f in findings:
            assert f["literal"] in examined, (
                f"{f['literal']!r} produced a finding but was never recorded as "
                "examined"
            )

        limit = kwargs.get("max_literals") or MAX_LITERALS_CHECKED
        considered = set(self._candidates(old, new, limit))
        verdicts = set(examined) | set(skipped_lits)
        assert considered <= verdicts, (
            f"literal(s) left the scan with NO verdict — silently unreported: "
            f"{considered - verdicts}"
        )
        return findings, skipped, examined

    def test_clean_edit(self, repo):
        old = 'X = "a removed sentence with words"\n'
        _f, _s, examined = self._assert_invariant(repo, old, "X = 1\n")
        assert "a removed sentence with words" in examined

    def test_edit_with_a_real_survivor(self, repo):
        import subprocess

        shared = "a shared sentence of prose"
        _write(repo, "b.py", f'Y = "{shared}"\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        old = f'X = "{shared}"\n'
        findings, _s, examined = self._assert_invariant(repo, old, "X = 1\n")
        assert findings, "the sibling should have been found"
        assert shared in examined

    def test_over_the_cap(self, repo):
        lits = [f"sentence number {i} of removed prose" for i in range(6)]
        old = "".join(f'V{i} = "{t}"\n' for i, t in enumerate(lits))
        _f, skipped, _e = self._assert_invariant(
            repo, old, "PASS = 1\n", max_literals=2
        )
        assert any("cap" in s["reason"] for s in skipped)

    def test_budget_exhausted(self, repo):
        old = "".join(
            f'V{i} = "sentence number {i} of removed prose"\n' for i in range(4)
        )
        _f, skipped, _e = self._assert_invariant(
            repo, old, "PASS = 1\n", budget_seconds=0.0
        )
        assert skipped, "a zero budget must report what it did not check"

    def test_grep_unavailable(self, tmp_path):
        """Not a git repo — `git grep` exits 128, which is NOT 'no matches'.

        This is the path that used to return [] and read as a clean scan. The
        directory is deliberately NOT a repo, so the failure is real rather than
        monkeypatched.
        """
        old = 'X = "a removed sentence with words"\n'
        _f, skipped, examined = self._assert_invariant(tmp_path, old, "X = 1\n")
        assert not examined, "a failed search must not be reported as checked"
        assert any("could not search" in s["reason"] for s in skipped), (
            f"git grep failed but was not recorded as unchecked: {skipped}"
        )

    def test_inner_truncation_is_recorded(self, repo, monkeypatch):
        """The per-FILE deadline break, which the outer sweep does not cover.

        `budget_seconds=0.0` trips the OUTER loop before any literal is
        processed, so it cannot reach this path — a first version of these tests
        assumed it did, and a mutation removing the inner record stayed green.
        The clock is therefore driven deterministically: time remains available
        long enough to run the grep and enter the per-path loop, then runs out
        inside it.

        Without the record, a PARTIAL survivor list is reported as though the
        search completed.
        """
        import subprocess

        import class_scope_scan as m

        shared = "a shared sentence of prose that survives"
        for name in ("b.py", "c.py", "d.py"):
            _write(repo, name, f'Y = "{shared}"\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

        calls = {"n": 0}

        def fake_remaining(_deadline):
            calls["n"] += 1
            # Generous while the candidate is selected and the grep runs;
            # exhausted once the per-path loop starts consuming results.
            return 10.0 if calls["n"] <= 2 else 0.0

        monkeypatch.setattr(m, "_remaining", fake_remaining)

        edited = _write(repo, "a.py", "X = 1\n")
        skipped: list[dict] = []
        examined: list[str] = []
        m.find_orphaned_literals(
            edited, f'X = "{shared}"\n', "X = 1\n", repo,
            skipped=skipped, examined=examined,
        )

        assert shared not in examined, (
            "a truncated survivor scan was reported as a completed check"
        )
        assert any("truncated" in s["reason"] for s in skipped), (
            f"the per-file truncation was not recorded: {skipped}"
        )


class TestPartialSearchStillReportsWhatIsProven:
    """An incomplete search must not discard evidence it already has.

    Two exits can leave the question half-answered: `git grep` failing, and the
    per-candidate deadline expiring. They are the SAME shape and must be
    dispositioned the same way -- report every survivor already PROVEN, and
    disclose that the rest of the search did not happen. Discarding a proven
    finding because a later step failed is the mirror image of reporting an
    unproven clean, which is the defect this whole scanner exists to name.
    """

    @staticmethod
    def _two_copies(repo):
        """One literal twice in one file; the edit updates only one copy.

        The surviving copy is established from `new_counts` alone -- no search
        is involved -- so it stays a definite finding however the search ends.
        """
        old = ('a = "the very same long message here"\n'
               'b = "the very same long message here"\n')
        new = ('a = "a totally different long message"\n'
               'b = "the very same long message here"\n')
        return old, new, _write(repo, "m.py", new)

    def test_grep_failure_keeps_the_proven_same_file_survivor(self, repo, monkeypatch):
        import class_scope_scan as m

        old, new, edited = self._two_copies(repo)

        def boom(*_a, **_k):
            raise m.GrepUnavailable("git grep exited 128")

        monkeypatch.setattr(m, "_grep_candidates", boom)
        found = m.find_orphaned_literals(edited, old, new, repo)
        assert found, "a proven same-file survivor was discarded by a failed search"
        assert edited in found[0]["survivors"]
        assert "search failed" in found[0]["search_incomplete"], (
            "the finding must disclose that the repository-wide half never ran"
        )

    def test_grep_failure_with_nothing_proven_is_still_a_skip(self, repo, monkeypatch):
        """The positive control: no survivor + no search really is unknown.

        Without this, a fix that reported EVERYTHING on a failed grep would
        pass the test above while destroying the guarantee it protects.
        """
        import class_scope_scan as m

        old = 'a = "the very same long message here"\n'
        new = 'a = "a totally different long message"\n'
        edited = _write(repo, "m.py", new)

        def boom(*_a, **_k):
            raise m.GrepUnavailable("git grep exited 128")

        monkeypatch.setattr(m, "_grep_candidates", boom)
        skipped: list[dict] = []
        found = m.find_orphaned_literals(edited, old, new, repo, skipped=skipped)
        assert found == [], "reported a finding it had no evidence for"
        assert skipped and "could not search" in skipped[0]["reason"]

    def test_budget_truncation_also_discloses_the_partial_search(self, repo, monkeypatch):
        """The sibling exit. It reported survivors with no hint of truncation."""
        import class_scope_scan as m

        old, new, edited = self._two_copies(repo)
        monkeypatch.setattr(m, "_grep_candidates",
                            lambda *a, **k: [repo / "other.py"])
        _write(repo, "other.py", 'z = "the very same long message here"\n')
        # Generous while the literal is selected, exhausted once the per-path
        # loop starts consuming results. A blanket -1 starves the OUTER
        # per-literal check instead and nothing reaches the survivor logic.
        calls = {"n": 0}

        def fake_remaining(_deadline):
            calls["n"] += 1
            # Call 1 is the OUTER per-literal budget check and must pass, or
            # nothing reaches the survivor logic at all. Call 2 is the first
            # per-path check, which is the one that must find the budget spent.
            return 10.0 if calls["n"] <= 1 else 0.0

        monkeypatch.setattr(m, "_remaining", fake_remaining)
        found = m.find_orphaned_literals(edited, old, new, repo)
        assert found, "the proven same-file survivor was dropped on truncation"
        assert "truncated" in found[0]["search_incomplete"], (
            "a truncated search reported its survivors as if the list were complete"
        )


class TestNumericBoundsAreValidated:
    """Both operator-supplied numbers, not just the one a reviewer named.

    `limit` is used as `prose[:limit]` / `prose[limit:]`. At -1 the scan checks
    EVERY literal except the last and then reports that last one as "over the
    -1-literal cap": the cap has stopped bounding anything while the output
    still claims one was applied. `--budget <= 0` puts the deadline in the past.
    """

    @pytest.mark.parametrize(
        "argv",
        [["--max-literals", "0"], ["--max-literals", "-1"],
         ["--budget", "0"], ["--budget", "-5"]],
    )
    def test_out_of_range_is_refused(self, argv, repo, monkeypatch):
        import class_scope_scan as m

        monkeypatch.chdir(repo)
        with pytest.raises(SystemExit) as exc:
            m.main(argv)
        assert exc.value.code != 0

    def test_valid_bounds_are_still_accepted(self, repo, monkeypatch, capsys):
        """Positive control: the guard must not refuse legitimate values.

        Needs a real commit -- the bare fixture has no HEAD, and `main` then
        exits 2 for an entirely different reason, which would let a
        refuse-everything guard pass this test.
        """
        import subprocess

        import class_scope_scan as m

        _write(repo, "seed.py", 'X = "a removed sentence with words here"\n')
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "seed"], cwd=repo, check=True,
        )
        # DIRTY THE TREE. With the working tree equal to HEAD the changed-file
        # set is empty and `main` returns 0 from an early exit without scanning
        # anything -- so the bounds were parsed and never used, and a guard
        # that refused everything AFTER the git calls would pass this too.
        _write(repo, "seed.py", "X = 1\n")
        monkeypatch.chdir(repo)
        assert m.main(["--max-literals", "1", "--budget", "0.5"]) == 0
        out = capsys.readouterr().out
        assert "no modified Python files" not in out, (
            "the scan body was never reached, so this proves nothing about the bounds"
        )
        assert "scanned" in out


class TestAnUnreadableCandidateIsNeverClean:
    """The THIRD way the search comes back partial.

    `git grep` positively identifies a file as containing the core, and the
    read then fails. The question about that candidate is unanswered in exactly
    the sense a failed grep or an expired deadline leaves it unanswered — but
    this arm used to `continue` and fall through to the examined-clean verdict,
    so the tool reported a confident clean on evidence it never gathered. That
    is the failure it exists to name in other people's code.

    Reachable without an exotic encoding: `--untracked` is passed to git grep,
    so a path it lists can be removed by a concurrent editor or build step
    before the read, raising FileNotFoundError on the same line.
    """

    def test_an_undecodable_sibling_is_not_reported_clean(self, repo):
        import class_scope_scan as m

        shared = "a shared sentence of prose here"
        old = f'A = "{shared}"\n'
        new = "A = 1\n"
        edited = _write(repo, "a.py", new)
        # A real file git grep WILL match, that read() cannot decode as UTF-8.
        (repo / "b.py").write_bytes(f'B = "{shared}"\n'.encode("latin-1")
                                    + b'\n# caf\xe9\n')

        skipped: list[dict] = []
        examined: list[str] = []
        findings = m.find_orphaned_literals(
            edited, old, new, repo, skipped=skipped, examined=examined,
        )
        assert shared not in examined, (
            "a literal whose only candidate could not be read was reported as "
            "examined-clean — a confident answer to a question never asked"
        )
        assert findings or skipped, "the literal reached no verdict at all"
        if skipped:
            assert "could not read" in skipped[0]["reason"]

    def test_a_readable_sibling_is_still_reported_normally(self, repo):
        """Positive control: the new arm must not make every scan 'incomplete'."""
        import class_scope_scan as m

        shared = "a shared sentence of prose here"
        old = f'A = "{shared}"\n'
        new = "A = 1\n"
        edited = _write(repo, "a.py", new)
        _write(repo, "b.py", f'B = "{shared}"\n')

        skipped: list[dict] = []
        findings = m.find_orphaned_literals(edited, old, new, repo, skipped=skipped)
        assert findings, "an ordinary survivor stopped being found"
        assert "search_incomplete" not in findings[0], (
            "a complete search was labelled incomplete"
        )
        assert skipped == []


class TestTheOperatorSeesTheCaveat:
    """`search_incomplete` is only worth adding if it reaches a human.

    The library tests assert the dict key; deleting the entire block that
    PRINTS it left the suite green, so the finding still rendered as
    'same text still in: …' with no hint that the repository-wide half of the
    search never ran — the exact reading error the key exists to prevent.
    """

    def test_the_incomplete_search_caveat_is_printed(self, repo, monkeypatch, capsys):
        import subprocess

        import class_scope_scan as m

        two = ('a = "the very same long message here"\n'
               'b = "the very same long message here"\n')
        _write(repo, "m.py", two)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "seed"], cwd=repo, check=True,
        )
        _write(repo, "m.py",
               'a = "a totally different long message"\n'
               'b = "the very same long message here"\n')

        def boom(*_a, **_k):
            raise m.GrepUnavailable("git grep exited 128")

        monkeypatch.setattr(m, "_grep_candidates", boom)
        monkeypatch.chdir(repo)
        m.main([])
        out = capsys.readouterr().out
        assert "same text still in" in out, "the finding itself vanished"
        assert "search incomplete" in out, (
            "the finding printed as if the search had completed"
        )

    def test_a_complete_search_prints_no_caveat(self, repo, monkeypatch, capsys):
        """Positive control: the caveat must not appear on every finding."""
        import subprocess

        import class_scope_scan as m

        two = ('a = "the very same long message here"\n'
               'b = "the very same long message here"\n')
        _write(repo, "m.py", two)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "seed"], cwd=repo, check=True,
        )
        _write(repo, "m.py",
               'a = "a totally different long message"\n'
               'b = "the very same long message here"\n')
        monkeypatch.chdir(repo)
        m.main([])
        out = capsys.readouterr().out
        assert "same text still in" in out
        assert "search incomplete" not in out


class TestLibraryEntryPointValidatesItsBounds:
    """`main` validates at parse time; the exported function is called directly."""

    def test_a_negative_cap_raises(self, repo):
        import class_scope_scan as m

        edited = _write(repo, "a.py", "A = 1\n")
        with pytest.raises(ValueError, match="must be >= 1 or None"):
            m.find_orphaned_literals(
                edited, 'A = "some removed prose here"\n', "A = 1\n", repo,
                max_literals=-1,
            )

    def test_none_and_positive_are_accepted(self, repo):
        """Positive control against a guard that refuses everything."""
        import class_scope_scan as m

        edited = _write(repo, "a.py", "A = 1\n")
        for cap in (None, 1, 50):
            m.find_orphaned_literals(
                edited, 'A = "some removed prose here"\n', "A = 1\n", repo,
                max_literals=cap,
            )


class TestOutputIsASCIIOnly:
    """No `print` in this tool may carry a non-ASCII character.

    Not style. This is a CLI whose stdout encoding is the environment's: under
    a non-UTF-8 `PYTHONIOENCODING` a non-ASCII `print` raises
    UnicodeEncodeError mid-run. When that happened it landed BETWEEN a finding
    and its caveat, killing every remaining file and the summary line — so a
    partial run printed as a complete one, which is the single thing this tool
    exists to prevent.

    Asserted as a CLASS over the whole file rather than as a case for the one
    character that did it, because the next one will be somewhere else and a
    case test would not see it. Also cheap and deterministic, where reproducing
    the real trigger needs a subprocess with a doctored environment.
    """

    def test_output_survives_an_ascii_stdout_with_non_ascii_data(self, repo):
        """The DATA half of the class, which the static check cannot see.

        The check below keeps non-ASCII out of this file's own print calls. It
        is only half the class: the output also INTERPOLATES scanned data --
        paths, git error text, and literal values through `!r`, which in Python
        3 does not escape printable non-ASCII. So ordinary accented prose in a
        SCANNED file puts non-ASCII on stdout no matter what this source says.

        Run as a real subprocess because the encoding is a property of the
        process's streams; pytest's capture replaces them, so an in-process
        test cannot reach this at all.
        """
        import os
        import subprocess
        import sys

        shared = 'a shared sentence of prose caf\u00e9 here'
        (repo / "m.py").write_text(f'A = "{shared}"\n', encoding="utf-8")
        (repo / "b.py").write_text(f'B = "{shared}"\n', encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "seed"], cwd=repo, check=True,
        )
        (repo / "m.py").write_text("A = 1\n", encoding="utf-8")

        scanner = Path(__file__).resolve()
        for parent in scanner.parents:
            cand = parent / "scripts" / "class_scope_scan.py"
            if cand.exists():
                scanner = cand
                break

        env = {**os.environ, "PYTHONIOENCODING": "ascii"}
        r = subprocess.run(
            [sys.executable, str(scanner), "--base", "HEAD"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=120,
        )
        assert "UnicodeEncodeError" not in r.stderr, (
            "a scanned non-ASCII literal aborted the run mid-output: "
            f"{r.stderr[-400:]}"
        )
        # And the run must COMPLETE -- the abort's real damage was the summary
        # line never printing, so a partial run read as a whole one.
        assert "changed file(s) scanned" in r.stdout, (
            f"the summary line never printed; rc={r.returncode} "
            f"stdout={r.stdout[-300:]!r}"
        )
        assert "b.py" in r.stdout, "the sibling finding itself was lost"

    def test_no_print_statement_contains_non_ascii(self):
        import ast

        src = Path(__file__).resolve()
        scan = None
        for parent in src.parents:
            cand = parent / "scripts" / "class_scope_scan.py"
            if cand.exists():
                scan = cand
                break
        assert scan is not None, "could not locate the scanner source"

        text = scan.read_text(encoding="utf-8")
        tree = ast.parse(text)
        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                continue
            segment = ast.get_source_segment(text, node) or ""
            bad = {c for c in segment if ord(c) > 127}
            # Also check what the string literals EVALUATE to: a `\u2014`
            # escape is pure ASCII in the source and still emits a non-ASCII
            # character at runtime, which is what actually raises.
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    bad |= {c for c in sub.value if ord(c) > 127}
            if bad:
                offenders.append((node.lineno, "".join(sorted(bad))))
        assert not offenders, (
            "non-ASCII in a print() — this raises under a non-UTF-8 "
            f"PYTHONIOENCODING and aborts the run mid-output: {offenders}"
        )
