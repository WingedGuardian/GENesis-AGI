"""Shared fixtures for inline hook tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _pin_required_ci_workflows(monkeypatch):
    """Pin the merge gate's required-CI-workflow identity policy to the shipped
    default ("CI") for EVERY hook test. The required set is config-driven from the
    host's ``~/.genesis/config/genesis.yaml`` (see git_push_guard.
    _required_ci_workflows), so without this pin any test that seeds a green CI
    rollup would have its green-vs-incomplete verdict depend on the dev box's local
    config and go non-deterministic — a flaky-by-environment security-gate test is
    how a real regression gets waved through as "known flake". Config-path behavior
    has its own tests (TestRequiredCiWorkflowsConfig), which delete this seam;
    per-test overrides (e.g. "CodeQL") simply setenv later and win."""
    monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "CI")
    yield


@pytest.fixture(autouse=True)
def _hermetic_e2e_declaration(monkeypatch):
    """Keep the ADVISORY E2E read hermetic for EVERY hook test (§8.12).

    The reader shells out to ``gh pr view`` for the body and for ``createdAt``, so
    without this pin each test that drives the merge arm or its report would make a
    LIVE call: green on a dev box with gh authenticated and PR "1" answering, red in
    CI, and in both cases testing the network rather than the thing under test.

    Since 2026-09-06 the E2E reader is advisory, so ITS verdict cannot fail a merge
    and what this prevents for that gate is NOTE noise, not a false block.

    But do not read that as "nothing here can fail a merge" — an earlier version of
    this docstring said exactly that and it is FALSE. ``_TEST_GH_PR_BODY`` is a
    SHARED seam: ``_check_pin_receipts`` reads the same body via ``_pr_body_text``
    and DOES block. MEASURED 2026-09-06 — feeding it this fixture's body with a
    forward pin returns blocked=True ("CC pin moves FORWARD … but the PR body is
    missing 2 required gate receipt(s)"). So changing or deleting the default here
    silently changes what every pin-gate test in this directory is fed; edit it only
    with those tests in view.

    Still the hermetic default rather than a waiver: the E2E reader's own behaviour
    (every classification, both report directions, the cutoff, the degraded path) is
    exercised in tests/test_hooks/test_e2e_plan_gate.py, which overrides these per
    case; a later ``monkeypatch.setenv`` in any test wins over this one."""
    monkeypatch.setenv("_TEST_GH_PR_BODY", "E2E: none — hermetic default for hook tests\n")
    monkeypatch.setenv("_TEST_GH_PR_CREATED_AT", "2099-01-01T00:00:00Z")
    yield


@pytest.fixture(autouse=True)
def _hermetic_review_bodies(monkeypatch):
    """Give EVERY hook test an EMPTY review-body set by default.

    The inline finding scan reads a SECOND endpoint (``pulls/N/reviews``) for the
    outside-diff channel. Without this pin the fetch falls through to the shared
    paginated helper and is answered by whatever the test's ``subprocess.run``
    mock returns — which for the existing suites is the INLINE comments payload,
    a shape that happens to parse to zero findings. Those tests would then pass
    for an accidental reason and would start failing the day an unrelated fixture
    changed its payload.

    Empty is the honest default: a test that says nothing about outside-diff
    findings should see none. The channel's own behaviour — every severity, both
    fail directions, dedupe, and the dismissed-review rule — is exercised in
    tests/test_hooks/test_outside_diff_findings.py, which overrides this per case."""
    monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", "")
    yield


@pytest.fixture(autouse=True)
def _hermetic_base_advance(monkeypatch):
    """Hermetic defaults for the base-advance refinement of the freshness gate.

    When the raw ``reviewed...head`` compare reads SUBSTANTIAL, the gate now asks
    a second question — did the BRANCH change, or did its base advance under it?
    — which reads the base tip and the PR's own contribution. Without a seam both
    are LIVE ``gh`` calls: green on a dev box with gh authenticated, red in CI,
    and slow either way, so every existing freshness test would be testing the
    network.

    The contribution seam defaults to ``{}``, which resolves to None for any
    revision pair → the refinement declines to rescue → existing tests keep the
    exact verdicts they were written for. That is the fail-CLOSED direction, so
    the default cannot mask a regression by accidentally allowing something.
    Cases that exercise the refinement set both seams themselves and win."""
    monkeypatch.setenv("_TEST_GH_BASE_OID", "ba5e" * 10)
    monkeypatch.setenv("_TEST_GH_CONTRIBUTION", "{}")
    yield


class OffDiffLock:
    """Per-test record of whether a review finding was DISCOUNTED as off-diff.

    ``expected()`` is the opt-in for a test whose subject IS off-diff routing.
    """

    #: The one prefix that means "this finding was silently not scored".
    #: All three of ``_check_inline_review_findings``' discount lanes print it —
    #: CodeRabbit Critical/Major, Codex P1, Codex P2. Line numbers are
    #: deliberately omitted: they rot, and the lane LABELS are the durable
    #: anchor. ``TestOffDiffLockItself`` parametrizes over all three against the
    #: guard's real output, so a reworded label fails there rather than here.
    #:
    #: Deliberately NOT `[outside-diff `: that label belongs to the review-body
    #: channel on PR #1847, which is OPEN and unmerged as of 2026-09-08, and it
    #: prints for in-diff Majors too. (Checked on that branch: it adds no new
    #: `[off-diff ` lane, so this marker stays complete once it lands.) Also NOT
    #: the scoping-unavailable NOTE, whose path SCORES everything — the stricter
    #: direction, so it cannot manufacture a passing not-block assertion.
    MARKER = "[off-diff "

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._declared = False

    def expected(self) -> None:
        """Declare that off-diff routing is what this test is FOR."""
        self._declared = True

    def record(self, err: str) -> None:
        self._chunks.append(err)

    @property
    def declared(self) -> bool:
        return self._declared

    @property
    def routed_off_diff(self) -> bool:
        return self.MARKER in self.captured

    @property
    def captured(self) -> str:
        """Everything the guard wrote to stderr during this test."""
        return "".join(self._chunks)


@pytest.fixture(autouse=True)
def offdiff_lock(capsys, monkeypatch):
    """FAIL a hook test that silently discounted a finding as outside the diff.

    THE DEFECT THIS EXISTS FOR. The merge gate scores a review finding only when
    its path is in the PR's changed-file set; a finding on any other path is
    routed to the off-diff lane, surfaced as a NOTE and never scored
    (``_off_diff``, checked BEFORE the doc-path lever on both the P1 branch and
    the P2 branch). Tests pin that changed-file set with a FIXED allowlist
    (``_TEST_GH_PR_FILES``). So a test anchoring a finding on a path the
    allowlist forgot does not fail — it quietly routes off-diff, and a bare
    ``assert not block`` then passes VACUOUSLY, proving nothing about the
    exemption it is named for. Measured live on PR #1690.

    Until this fixture, the only defence was PROSE — a fixture docstring and a
    standing comment asking every not-block test to also assert its lane marker
    — and 3 tests already did not. A rule every call site must REMEMBER is a
    convention; this makes forgetting fail.

    MECHANISM, and why it is this one. The lane writes to stderr, and
    ``_check_inline_review_findings`` returns only ``(should_block, message)``,
    so the routing is invisible to the assertion. Reading ``capsys`` after
    ``yield`` does NOT work: ~20 of the exposed tests call ``readouterr()`` in
    the body, which DRAINS the buffer. Wrapping ``sys.stderr`` does not work
    either — MEASURED at 0 of 4 chunks recorded, because pytest reinstalls
    ``sys.stdout``/``sys.stderr`` per test PHASE, discarding a fixture-setup
    wrap before the call phase runs. Wrapping ``readouterr`` itself is what
    survives both: every drain is recorded, and a final drain at teardown
    catches what a test never read.

    THREE LIMITS, stated rather than papered over — and note the first is the
    only one that fails SILENTLY, which is why it is first:

    * **It fails OPEN.** A ``MARKER`` that no longer matches the guard's labels,
      or a pytest change that breaks the ``readouterr`` wrap, makes this fixture
      pass everything and say nothing — the exact shape it exists to stop, one
      layer up. That is the right trade (failing closed would break every hook
      test on any capture hiccup), but it means the lock's value rests entirely
      on ``TestOffDiffLockItself`` in test_merge_review_gate.py staying honest.
      Treat those tests as load-bearing machinery, not as coverage: they are all
      that stands between this fixture working and this fixture being inert.
    * It cannot see a guard run in a CHILD process — ``_run_guard``'s subprocess
      tests capture the child's stderr into a ``CompletedProcess``, which never
      passes through ``capsys``. No path-anchored finding lives there today, so
      the gap is currently empty; it is a gap all the same. Fails closed in the
      sense that matters: those tests are simply not covered, not wrongly passed.
    * Requesting ``capsys`` here makes it active for EVERY hook test, and pytest
      refuses ``capsys`` and ``capfd`` in one test — loudly, as a setup ERROR.
      Measured 0 uses of ``capfd``/``capsysbinary`` in all of ``tests/`` when
      this landed, so a future test needing ``capfd`` is what will collide, and
      the fix then is to narrow this fixture's scope, not to delete the opt-in.
    """
    lock = OffDiffLock()
    original = capsys.readouterr

    def _recording():
        result = original()
        lock.record(result.err)
        return result

    monkeypatch.setattr(capsys, "readouterr", _recording)
    yield lock
    # Whatever the test never drained is still the guard's output. Read it
    # through the ORIGINAL, which is valid whether or not the patch is undone.
    lock.record(original().err)
    if lock.routed_off_diff and not lock.declared:
        pytest.fail(
            "A review finding was routed to the OFF-DIFF lane, so it was never "
            "scored — any 'does not block' assertion in this test passed for "
            "that reason, not for the one the test is named for.\n"
            "  * If the finding is meant to be IN the PR's diff: add its path to "
            "the changed-file allowlist this test uses (_TEST_GH_PR_FILES).\n"
            "  * If off-diff routing IS this test's subject: call "
            "`offdiff_lock.expected()`.\n"
            f"stderr carrying the marker:\n{lock.captured.strip()}"
        )


def _load_settings() -> dict:
    """Load .claude/settings.json from the repo root."""
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / ".claude" / "settings.json"
        if candidate.exists():
            return json.loads(candidate.read_text())
    raise FileNotFoundError("Could not find .claude/settings.json in any parent directory")


def _find_hook_command(settings: dict, matcher: str) -> str:
    """Extract the command for a PreToolUse hook by matcher name.

    Handles both inline hooks (``bash -c '...'``) and external script paths.
    """
    pre_tool_hooks = settings.get("hooks", {}).get("PreToolUse", [])
    for entry in pre_tool_hooks:
        if entry.get("matcher") == matcher:
            hooks = entry.get("hooks", [])
            for hook in hooks:
                if hook.get("type") == "command":
                    cmd = hook["command"]
                    # Inline hooks start with "bash -c"; external scripts don't.
                    return cmd
    raise ValueError(f"No hook command found for matcher '{matcher}' in settings.json")


@pytest.fixture(scope="session")
def settings() -> dict:
    """Parsed .claude/settings.json."""
    return _load_settings()


@pytest.fixture(scope="session")
def bash_hook_command(settings: dict) -> str:
    """The inline bash -c command string for the Bash PreToolUse hook."""
    return _find_hook_command(settings, "Bash")


@pytest.fixture(scope="session")
def rm_rf_hook_command() -> str:
    """Command to run the destructive_command_guard.py script directly.

    The rm-rf guard is a separate Python script (not the inline bash hook).
    This fixture resolves the script path and returns a shell command that
    invokes it via the venv Python, matching how genesis-hook runs it.
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        script = ancestor / "scripts" / "hooks" / "destructive_command_guard.py"
        if script.exists():
            venv_python = ancestor / ".venv" / "bin" / "python"
            python = str(venv_python) if venv_python.exists() else "python3"
            return f"{python} {script}"
    raise FileNotFoundError("Could not find destructive_command_guard.py")


@pytest.fixture(scope="session")
def webfetch_hook_command(settings: dict) -> str:
    """The raw bash -c command string for the WebFetch PreToolUse hook."""
    return _find_hook_command(settings, "WebFetch")


def run_hook(
    hook_command: str, tool_input: dict, *, tool_name: str = "Bash"
) -> subprocess.CompletedProcess:
    """Run an inline hook command with the real CC payload on stdin.

    Mirrors how current Claude Code invokes PreToolUse hooks: the full payload
    (``{"tool_name": ..., "tool_input": {...}}``) is delivered as JSON on
    **stdin**, NOT via a ``CLAUDE_TOOL_INPUT`` env var (which CC no longer sets).
    The legacy env var is scrubbed so a stray value can't mask a regression.

    Args:
        hook_command: The full "bash -c '...'" command from settings.json.
        tool_input: The tool-input dict (nested under ``tool_input`` in the payload).
        tool_name: The tool name for the payload envelope (default "Bash").

    Returns:
        CompletedProcess with returncode, stdout, stderr.
    """
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input}
    )
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
    result = subprocess.run(
        hook_command,
        shell=True,
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result
