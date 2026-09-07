"""The E2E-obligation merge gate (`_check_e2e_plan` in git_push_guard.py, §8.12).

The parser's own semantics are covered in tests/test_scripts/test_e2e_declaration.py.
What is tested HERE is the gate's contract, which is a different thing and fails in
different directions:

  * it blocks at MERGE time and nowhere else (a body is written and revised while a
    PR is open — gating a push would gate the wrong moment);
  * pre-cutoff PRs are exempt, so landing this does not retro-block the open queue;
  * every unreadable input BLOCKS rather than passing, and says which one;
  * a missing parser module degrades to a presence-only scan with a NOTE, rather
    than taking the whole gate down with it;
  * the report row mirrors the enforcement arm (they call the same function, so a
    bug in one is a bug in both — the property the whole report rests on).

Network-free throughout via the `_TEST_GH_*` env seams.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_GUARD = _SCRIPTS / "git_push_guard.py"

_PLAN = "E2E: restart the server and confirm /api/genesis/health answers 200\n"
_NONE = "E2E: none — docs only, no runtime surface to verify\n"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _post_cutoff(monkeypatch):
    """Default every case to a PR created AFTER the convention — the population the
    gate actually governs. Pre-cutoff exemption gets its own explicit tests."""
    monkeypatch.setenv("_TEST_GH_PR_CREATED_AT", "2099-01-01T00:00:00Z")


class TestVerdicts:
    def test_plan_passes(self, guard, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_BODY", _PLAN)
        blocked, msg = guard._check_e2e_plan("100")
        assert not blocked, msg
        assert "plan" in msg

    def test_none_with_reason_passes(self, guard, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_BODY", _NONE)
        blocked, msg = guard._check_e2e_plan("100")
        assert not blocked, msg
        assert "none" in msg

    def test_absent_declaration_blocks(self, guard, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_BODY", "A body that never decided.\n")
        blocked, msg = guard._check_e2e_plan("100")
        assert blocked
        assert "E2E: <one-line plan" in msg and "E2E: none —" in msg

    def test_bare_none_blocks_and_says_why(self, guard, monkeypatch):
        """The message must name THIS mistake, not just block.

        `"reason" in msg` was vacuous: GUIDANCE is appended to every block message
        and itself contains "<reason there is no runtime surface to verify>", so the
        assertion passed for the generic detail too (CodeRabbit Minor, 2026-09-06).
        Assert the specific detail, and assert the generic one is ABSENT."""
        monkeypatch.setenv("_TEST_GH_PR_BODY", "E2E: none\n")
        blocked, msg = guard._check_e2e_plan("100")
        assert blocked
        assert "needs a REASON" in msg, "must name the bare-`none` mistake specifically"
        assert "no real content" not in msg, "must not fall back to the generic detail"

    def test_placeholder_block_uses_the_generic_detail(self, guard, monkeypatch):
        """The control that makes the assertion above discriminating: a DIFFERENT
        invalid shape must produce a DIFFERENT detail."""
        monkeypatch.setenv(
            "_TEST_GH_PR_BODY", "E2E: <one-line plan for the post-merge verification>\n"
        )
        blocked, msg = guard._check_e2e_plan("100")
        assert blocked
        assert "no real content" in msg
        assert "needs a REASON" not in msg

    def test_block_message_names_the_validator_seam(self, guard, monkeypatch):
        """§8.13: `none` passes this gate but does NOT release the validator. An
        author reading the block must learn the whole contract, or `none` becomes
        folklore for "no E2E needed"."""
        monkeypatch.setenv("_TEST_GH_PR_BODY", "nothing\n")
        _, msg = guard._check_e2e_plan("100")
        assert "validator" in msg.lower()

    def test_declaration_is_echoed_on_pass(self, guard, monkeypatch, capsys):
        """Belt-and-braces from the spec: the obligation is printed at merge time,
        so it is in front of the person merging, not only in the body."""
        monkeypatch.setenv("_TEST_GH_PR_BODY", _PLAN)
        guard._check_e2e_plan("100")
        assert "E2E obligation declared" in capsys.readouterr().err


class TestFailDirections:
    def test_unreadable_body_blocks(self, guard, monkeypatch):
        """Diverges from the pin gate on purpose: that one fails OPEN on an
        unreadable body because it guards the rare pin-bump path; this one guards
        EVERY merge, so an unread body is an unanswered question."""
        monkeypatch.delenv("_TEST_GH_PR_BODY", raising=False)
        monkeypatch.setattr(guard, "_pr_body_text", lambda *a, **k: None)
        blocked, msg = guard._check_e2e_plan("100")
        assert blocked
        assert "unreadable" in msg.lower()

    def test_unreadable_created_at_blocks_naming_the_cause(self, guard, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_BODY", _PLAN)
        monkeypatch.delenv("_TEST_GH_PR_CREATED_AT", raising=False)
        monkeypatch.setattr(guard, "_pr_created_at", lambda *a, **k: None)
        blocked, msg = guard._check_e2e_plan("100")
        assert blocked
        assert "createdAt" in msg

    def test_parser_unavailable_degrades_to_presence_scan(self, guard, monkeypatch, capsys):
        """Losing the comment/fence stripping must not lose the gate. A bare E2E:
        line still passes, and the NOTE says what protection was unavailable."""
        monkeypatch.setenv("_TEST_GH_PR_BODY", _PLAN)
        monkeypatch.setattr(guard, "_load_e2e_declaration", lambda: None)
        blocked, msg = guard._check_e2e_plan("100")
        assert not blocked
        assert "degraded" in msg
        err = capsys.readouterr().err
        assert "WITHOUT comment/fence stripping" in err
        assert "hidden in an HTML comment" in err, "the NOTE must name what is now unprotected"

    def test_parser_unavailable_still_blocks_a_body_with_no_line(self, guard, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_BODY", "no declaration here\n")
        monkeypatch.setattr(guard, "_load_e2e_declaration", lambda: None)
        blocked, _ = guard._check_e2e_plan("100")
        assert blocked

    def test_degraded_fallback_refuses_the_untouched_pr_template(self, guard):
        """The degraded path cannot strip HTML comments, and the shipped template's
        guidance lives in one — so without this the template's own
        `E2E: none — <reason …>` line satisfied the gate and EVERY
        straight-from-template PR passed, while the comment beside the regex claimed
        it could not (Kimi P2, 2026-09-06, reproduced)."""
        template = (
            Path(__file__).resolve().parents[2] / ".github" / "PULL_REQUEST_TEMPLATE.md"
        ).read_text()
        assert guard._E2E_FALLBACK_RE.search(template) is None

    @pytest.mark.parametrize(
        "line",
        [
            "E2E: restart the server and curl the health endpoint",
            "- E2E: run the migration on a fresh DB",
            "- [x] E2E: smoke the installer",
            "**E2E**: replay the failing case",
        ],
    )
    def test_degraded_fallback_still_accepts_real_declarations(self, guard, line):
        """The control: tightening against the template must not blind the fallback
        to the shapes it exists to read."""
        assert guard._E2E_FALLBACK_RE.search(line) is not None

    def test_degraded_mode_still_honours_the_cutoff(self, guard, monkeypatch):
        """A PRE-CUTOFF PR must stay exempt even when the parser cannot load.

        It did not: the exemption was gated on the module importing, so a broken
        install turned into a false BLOCK on a gate with no override, against
        exactly the population the cutoff protects (CodeRabbit Minor, 2026-09-06)."""
        monkeypatch.setenv("_TEST_GH_PR_BODY", "an old body, no declaration\n")
        monkeypatch.setenv("_TEST_GH_PR_CREATED_AT", "2020-01-01T00:00:00Z")
        monkeypatch.setattr(guard, "_load_e2e_declaration", lambda: None)
        blocked, msg = guard._check_e2e_plan("100")
        assert not blocked, msg
        assert "n/a" in msg

    def test_the_degraded_cutoff_mirror_matches_the_parser(self, guard):
        """The lock on the duplicated constant: two copies of a policy date drift,
        and a drifted mirror silently changes who is exempt in degraded mode."""
        mod = guard._load_e2e_declaration()
        assert mod is not None, "the parser must be loadable in the repo under test"
        assert guard._E2E_CUTOFF_FALLBACK == mod.E2E_CUTOFF_ISO


class TestCutoff:
    def test_pre_cutoff_pr_is_exempt_even_with_no_declaration(self, guard, monkeypatch):
        """The transition rule: landing this must not retro-block the open queue."""
        monkeypatch.setenv("_TEST_GH_PR_BODY", "an old body, no declaration\n")
        monkeypatch.setenv("_TEST_GH_PR_CREATED_AT", "2020-01-01T00:00:00Z")
        blocked, msg = guard._check_e2e_plan("100")
        assert not blocked
        assert "n/a" in msg

    def test_post_cutoff_pr_is_bound(self, guard, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_BODY", "a new body, no declaration\n")
        monkeypatch.setenv("_TEST_GH_PR_CREATED_AT", "2099-01-01T00:00:00Z")
        assert guard._check_e2e_plan("100")[0] is True


class TestWiring:
    """Built != wired.

    A source-string grep is NOT a wiring test: MEASURED (architect, 2026-09-06)
    that replacing the enforcement branch with `if False:` — leaving the call
    itself in place — kept every test in this file green while the gate did
    nothing at all. The exit-code proof lives in
    tests/test_hooks/test_merge_gate_characterization.py (the `e2e_*` cases drive
    a real `gh pr merge` and assert the exit code); what remains here is only the
    property no exit code can show — that the REPORT and the ENFORCEMENT call the
    same function, so the two can never disagree."""

    def test_report_and_enforcement_share_one_function(self):
        text = _GUARD.read_text()
        assert "e2e-plan       :" in text, "the report must carry the row"
        assert text.count("_check_e2e_plan(") >= 3, "def + merge arm + report row"


class TestEndToEndThroughTheHook:
    """Drive the real hook binary, so the gate is proven to fire through the actual
    PreToolUse contract rather than only as a Python function."""

    def _run(self, command: str, body: str, created: str = "2099-01-01T00:00:00Z"):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
        env["_TEST_GH_PR_BODY"] = body
        env["_TEST_GH_PR_CREATED_AT"] = created
        payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
        )
        return subprocess.run(
            [sys.executable, str(_GUARD)],
            input=payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_a_push_is_not_blocked_by_a_missing_declaration(self):
        """The audience/verdict axiom in executable form: no declaration, but a
        push must sail through — this gate has nothing to say at push time."""
        r = self._run("git push", "no declaration at all\n")
        assert "no post-merge E2E decision" not in r.stderr
