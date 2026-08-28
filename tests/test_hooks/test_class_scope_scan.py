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
            ("git -C /some/path commit -m x", True),
            ("git add -A && git commit -m x", True),
            ("ls -la", False),
            ("git status", False),
            ("git log --oneline -5", False),
            ("echo 'git commit'", False),
        ],
    )
    def test_only_real_commits_are_targeted(self, command, expected):
        import class_scope_commit_advisory as cca

        assert cca._targets_a_commit(command) is expected
