"""The pin-receipt gate inside git_push_guard.

`origin` is the public repo, so merging a Claude Code pin bump IS the release.
Two gates are mandatory before that — the changelog read and the local-first
soak — and this is where they are ENFORCED. Not CI: the PR body stays mutable
after any check run completes, so only a merge-time read describes the body that
actually merges.

The fail direction is split on purpose and these tests pin BOTH halves:

  * a fact about the PR's CONTENT (pin moved forward without receipts; the pin
    cannot be read at either side) BLOCKS;
  * a failure of the gate's own PLUMBING (checker missing, gh unreadable, no
    head sha) does NOT block — walling off every merge over a check that only
    ever guards a pin bump is a worse failure than the one it prevents.

Network-free via the _TEST_GH_* env-injection seams.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_spec = importlib.util.spec_from_file_location(
    "git_push_guard", _WORKTREE / "scripts" / "hooks" / "git_push_guard.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

HEAD = "a" * 40
PIN_BASE = 'CC_VERSION="${CC_VERSION:-2.1.218}"\nNODE_MAJOR="${NODE_MAJOR:-22}"\n'
PIN_FORWARD = 'CC_VERSION="${CC_VERSION:-2.1.246}"\nNODE_MAJOR="${NODE_MAJOR:-22}"\n'
RECEIPTS = (
    "CC-Gate-Changelog: read (2.1.218, 2.1.246] in full from CHANGELOG.md, 2026-08-27\n"
    "CC-Gate-Soak: 2.1.246 soaked 2026-08-25..2026-08-27, sweep clean, signed off\n"
)


@pytest.fixture
def gate(monkeypatch):
    """_check_pin_receipts with head-pin and body injected, base pinned to 2.1.218.

    BOTH sides go through the same API-backed seam now. The base was previously
    stubbed at the `git show origin/main:` call, which is what made it possible for
    the base read to be bound to the wrong repository and to a hardcoded branch —
    so the fixture no longer patches subprocess at all, and any live call would be
    a visible failure rather than a silently-satisfied stub.
    """
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)

    def _fake_run(cmd, *a, **kw):
        raise AssertionError(f"unexpected subprocess call — a seam should have answered: {cmd}")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)

    def _call(head_pin: str, body: str):
        monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", head_pin)
        monkeypatch.setenv("_TEST_GH_PR_BODY", body)
        return _mod._check_pin_receipts("999", repo="owner/repo")

    return _call


# ── content facts: these BLOCK ────────────────────────────────────────────


def test_forward_bump_without_receipts_blocks(gate):
    blocked, msg = gate(PIN_FORWARD, "just a normal PR description")

    assert blocked is True
    assert "2.1.218" in msg and "2.1.246" in msg
    assert "CC-Gate-Changelog" in msg and "CC-Gate-Soak" in msg


def test_receipts_hidden_in_an_html_comment_still_block(gate):
    """They satisfy a text search while being invisible in the rendered PR, and
    the only enforcement this has is a human reading a claim someone made."""
    blocked, _ = gate(PIN_FORWARD, f"<!--\n{RECEIPTS}-->\n")

    assert blocked is True


def test_an_unreadable_pin_blocks(gate):
    """A pin assigned twice — the shape engineered to look unchanged — cannot be
    characterised, and publishing a release nobody can characterise is the thing
    to prevent."""
    blocked, msg = gate(PIN_FORWARD + 'CC_VERSION="2.1.999"\n', RECEIPTS)

    assert blocked is True
    assert "assigned more than once" in msg or "cannot" in msg.lower()


# ── content facts: these PASS ─────────────────────────────────────────────


def test_forward_bump_with_receipts_passes(gate):
    blocked, msg = gate(PIN_FORWARD, RECEIPTS)

    assert blocked is False
    assert "both gate receipts are present" in msg


def test_unchanged_pin_passes(gate):
    blocked, msg = gate(PIN_BASE, "")

    assert blocked is False
    assert "unchanged" in msg


def test_backward_pin_passes_without_receipts(gate):
    """The downgrade path is the project's incident-recovery route. Putting a
    gate between an operator and a rollback would be a regression dressed as
    rigor, so the exemption is automatic — no syntax to recall under pressure."""
    blocked, msg = gate('CC_VERSION="${CC_VERSION:-2.1.100}"\n', "")

    assert blocked is False
    assert "BACKWARD" in msg


# ── plumbing failures: these must NOT block ───────────────────────────────


def test_an_unreadable_head_does_not_wall_off_the_merge(monkeypatch):
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", "")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False
    assert "NOT verified" in msg


def test_a_missing_checker_module_does_not_wall_off_the_merge(monkeypatch):
    monkeypatch.setattr(_mod, "_load_pin_receipt_checker", lambda: None)

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False
    assert "NOT verified" in msg


def test_an_unreadable_body_does_not_wall_off_the_merge(monkeypatch):
    """Distinct from an EMPTY body, which is a real state that determines the
    answer by itself and must BLOCK on a forward bump."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", PIN_FORWARD)
    monkeypatch.delenv("_TEST_GH_PR_BODY", raising=False)
    monkeypatch.setattr(_mod, "_pr_body_text", lambda *a, **k: None)

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False
    assert "NOT verified" in msg


@pytest.mark.parametrize("blank", ["", "   \n"])
def test_a_BLANK_pin_read_is_plumbing_and_must_not_block(monkeypatch, blank):
    """MEASURED regression: this walled off 50 merge-gate cases at once.

    A zero exit with no body means the read did not produce the file — a wrong
    ref, a path absent there, a stubbed response. Feeding that to the checker
    made it an unparseable pin, which BLOCKS by design, so the gate blocked every
    merge whenever a read came back blank. A real cc_version.sh is never empty,
    so blank is plumbing and plumbing must not block.
    """
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", blank)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False, "a blank read must not wall off the merge"
    assert "NOT verified" in msg


def test_an_empty_body_is_a_content_fact_not_a_plumbing_failure(gate):
    """The counterpart to the test above, and the distinction the gate turns on:
    absence of both receipts is fully determined by an empty body."""
    blocked, _ = gate(PIN_FORWARD, "")

    assert blocked is True


# ── the report and the enforcement arm must not disagree ──────────────────


def test_the_gate_is_wired_into_the_report(monkeypatch):
    """Guards against the failure this whole change exists to avoid: a checker
    that is built but never called. `check_pr_report` runs the SAME functions
    the merge arm enforces, so an absent call site here means an absent gate."""
    source = (_WORKTREE / "scripts" / "hooks" / "git_push_guard.py").read_text()

    report_start = source.index("def check_pr_report(")
    assert "_check_pin_receipts(" in source[report_start:], "report arm does not run the gate"
    # And the merge arm, which is what actually blocks.
    assert source.count("_check_pin_receipts(") >= 3, (
        "expected the definition plus BOTH call sites (report + merge enforcement)"
    )


def test_no_override_sigil_waives_this_gate():
    """Deliberate: every sigil waives exactly ONE gate so a waiver cannot
    silently disarm an unrelated one, and the demand here takes seconds to
    satisfy honestly. If a gate really was not run, the action is to run it."""
    source = (_WORKTREE / "scripts" / "hooks" / "git_push_guard.py").read_text()
    start = source.index("should_block, receipts_msg = _check_pin_receipts(")
    window = source[start : start + 400]

    assert "override" not in window.split("return 2")[0], (
        "an override was wired into the pin-receipt gate"
    )


# ── ABSENCE is content, not plumbing ──────────────────────────────────────
# The distinction this section pins: a file the API says is NOT THERE (404) is a
# fact about the PR, and the checker's own policy is that an unreadable pin BLOCKS.
# An earlier revision returned the same None for a 404 and for a transport error,
# so a PR that DELETED the pin file took the plumbing path and merged unexamined —
# a fail-open on the one gate that is the sole enforcement for a public release.


def test_a_deleted_pin_file_at_head_BLOCKS(monkeypatch):
    """A PR that removes scripts/lib/cc_version.sh must not sail through.

    Without a pin at head there is no way to establish whether the PR moves it
    forward, which is precisely the condition the gate exists to refuse.
    """
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", _mod._PIN_SEAM_ABSENT)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "a PR that deletes the pin file")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is True, "a deleted pin file must BLOCK, not report NOT-verified"
    assert "ABSENT at the PR head" in msg


def test_a_missing_pin_file_on_the_BASE_blocks(monkeypatch):
    """No base pin means no comparison, so a forward move cannot be ruled out."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", _mod._PIN_SEAM_ABSENT)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", PIN_FORWARD)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is True
    assert "ABSENT on the base branch" in msg


def test_an_unreadable_base_ref_is_plumbing_and_must_not_block(monkeypatch):
    """The counterpart: not knowing WHICH branch to compare against is plumbing."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "")
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", PIN_FORWARD)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False
    assert "NOT verified" in msg


def test_the_base_is_read_at_the_PRs_own_ref_not_a_hardcoded_branch(monkeypatch):
    """Regression guard for a wrong-base comparison.

    The base used to be read with a local `git show origin/main:` — bound neither
    to the repository being merged into nor to the PR's actual base branch. From a
    checkout whose `origin` is a fork sitting on a HIGHER pin, a genuine forward
    bump reads as a downgrade and is exempted. Both sides now go through the same
    API path at the PR's own base ref; this asserts the ref is actually consulted.
    """
    seen = {}
    real = _mod._pin_file_at_ref

    def _spy(ref, repo, *, seam):
        seen[seam] = ref
        return real(ref, repo, seam=seam)

    monkeypatch.setattr(_mod, "_pin_file_at_ref", _spy)
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "release-2.x")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", PIN_BASE)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "")

    _mod._check_pin_receipts("999", repo="owner/repo")

    assert seen["_TEST_GH_BASE_PIN_FILE"] == "release-2.x", "base not read at the PR's base ref"
    assert seen["_TEST_GH_HEAD_PIN_FILE"] == HEAD, "head not read at the PR head sha"
