"""The pin-receipt gate inside git_push_guard.

`origin` is the public repo, so merging a Claude Code pin bump IS the release.
Two gates are mandatory before that — the changelog read and the local-first
soak — and this is where they are ENFORCED. Not CI: the PR body stays mutable
after any check run completes, so only a merge-time read describes the body that
actually merges.

The fail direction is split on purpose and these tests pin BOTH halves:

  * a fact about the PR's CONTENT BLOCKS — the pin moved forward without receipts;
    the file is ABSENT at either side; or it is present but UNDECODABLE at the head.
    ("cannot be read" is deliberately NOT the phrasing: it overlaps the plumbing
    bullet below on exactly the state that matters, and that overlap is what let
    UNDECODABLE ship on the permissive side);
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


def test_an_unreadable_pin_read_is_plumbing_and_must_not_block(monkeypatch):
    """MEASURED regression: this once walled off 50 merge-gate cases at once.

    A read that produces no usable answer — a stub, a truncated body, a transport
    failure — is PLUMBING, and plumbing must not block: a gate that refuses every
    merge whenever a read comes back unusable is worse than the one thing it guards.
    """
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", _mod._PIN_SEAM_UNREADABLE)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False, "an unusable read must not wall off the merge"
    assert "NOT verified" in msg


@pytest.mark.parametrize("emptied", ["", "   \n"])
def test_a_pin_file_TRUNCATED_to_nothing_is_content_and_BLOCKS(monkeypatch, emptied):
    """The counterpart, and the distinction the JSON contents form buys.

    An empty file is not an empty READ. It exists, so it is not absent; and an empty
    pin is an UNPARSEABLE pin, which the checker's own policy blocks. Deciding that
    here would duplicate the policy in the wiring — which is exactly how an earlier
    revision came to block a DELETED pin file while waving through one truncated to
    nothing. Same condition, two enforcements, one of them a fail-open.
    """
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", emptied)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is True, "an emptied pin file must block, like a deleted one"
    # WHICH rule blocked matters, and asserting only the boolean cannot tell.
    # THREE paths produce True here, including the wiring-side ABSENT branch this
    # test exists to forbid — classifying an empty file as absence would duplicate
    # the policy in the wiring, which is how the deleted-vs-emptied split appeared.
    # It must be the checker's unparseable-pin rule that fires.
    assert "could not determine CC_VERSION" in msg, (
        f"blocked, but not via the checker's unparseable-pin rule. Message: {msg}"
    )
    assert "ABSENT" not in msg, "an EMPTY pin was misclassified as an ABSENT one"


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


# ── a pin that EXISTS but whose bytes cannot be obtained is CONTENT ───────
# The split above is between a fact about the PR and a failure of our own
# plumbing. Two conditions were on the wrong side of it: the API answering that
# the blob is there but not inline (>1MB, `"encoding": "none"`), and a blob whose
# base64 will not decode. Both are statements about what the PR contains, and both
# were routed to the plumbing bucket, so a forward pin move carrying either one
# reported NOT-verified instead of blocking. Same shape as the deleted-vs-emptied
# pin bug this gate was built to close: one condition, two enforcements, one of
# them a fail-open.


def _api_reply(monkeypatch, payload_json: str, *, returncode: int = 0, stderr: str = ""):
    """Answer the HEAD pin read from the live path, leaving the base on its seam."""

    class _R:
        pass

    def _fake_run(cmd, *a, **kw):
        r = _R()
        r.returncode = returncode
        r.stdout = payload_json
        r.stderr = stderr
        return r

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)
    monkeypatch.delenv("_TEST_GH_HEAD_PIN_FILE", raising=False)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "a forward pin bump carrying no receipts")


def test_a_pin_blob_too_large_to_inline_is_content_and_BLOCKS(monkeypatch):
    """GitHub returns `"encoding": "none"` with no content for a blob over 1MB.

    The file IS in the tree, so this is not absence — but it is equally a fact
    about the PR, not about our plumbing. Reporting NOT-verified here lets a PR
    that bloats the pin file past the inline limit move the pin forward without
    ever presenting its receipts.
    """
    _api_reply(
        monkeypatch, '{"name":"cc_version.sh","type":"file","encoding":"none","size":1200000}'
    )

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is True, "a present-but-unreadable pin blob must block, not report NOT-verified"
    assert "NOT verified" not in msg


def test_a_pin_blob_whose_base64_will_not_decode_is_content_and_BLOCKS(monkeypatch):
    """The sibling condition. The API claims base64 and the payload is not.

    The payload matters and is not arbitrary. ``b64decode`` DISCARDS non-alphabet
    characters by default (``validate=False``), so what reaches the decoder is only
    the surviving alphabet characters, and it raises only when that survivor count
    leaves an impossible length. MEASURED in CPython 3.12: ``"abc"`` raises
    ``Incorrect padding`` and reaches this branch, while ``"!!!!"`` survives to the
    empty string, decodes to ``b""``, and never reaches it — the block then comes
    from the checker's unparseable-pin rule instead, so such a test would pass
    without the fix.

    ``validate=False`` is correct and must not be "hardened": GitHub wraps its base64
    at 60 characters with newlines, which strict validation would reject outright.
    """
    _api_reply(
        monkeypatch,
        '{"name":"cc_version.sh","type":"file","encoding":"base64","content":"abc"}',
    )

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is True, "an undecodable pin blob must block"
    assert "NOT verified" not in msg


def test_a_transport_failure_is_still_plumbing_and_must_NOT_block(monkeypatch):
    """The invariant the fix must not break — and the reason a blanket
    'unreadable blocks' rule is the wrong fix. A non-JSON body from a stub or a
    truncated response says nothing about the PR, and blocking on it once walled
    off 50 merge-gate cases at once.

    Honest label: this test and its bad-ref sibling PASS against the pre-change
    source, so neither demonstrates that anything was fixed. They are pinned
    invariants, not evidence. The tests that actually discriminate are the two
    `_api_reply` content cases above, which were measured failing on the old code.
    """
    _api_reply(monkeypatch, "not json at all")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False, "a plumbing failure must not wall off the merge"
    assert "NOT verified" in msg


def test_a_bad_ref_is_still_plumbing_and_must_NOT_block(monkeypatch):
    """The other plumbing case gh names explicitly, kept on its own side."""
    _api_reply(
        monkeypatch,
        "",
        returncode=1,
        stderr="gh: No commit found for the ref deadbeef (HTTP 404)",
    )

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False
    assert "NOT verified" in msg


# ── the wedge: NO base-side condition may block ───────────────────────────
# A fault on the BASE branch is inherited by every open PR and repairable by none
# of them through this gate, which has no override sigil. Before 2026-08-28 five
# base-side conditions blocked, four of them blocking PRs that do not touch the pin
# at all, and ALL of them blocking a fully compliant pin bump. Measured then; pinned
# here so it cannot come back.
#
# This is not a fail-open. An unparseable pin on the base makes
# check_cc_node_lockstep exit 1, which reddens the blocking CI workflow, which the
# merge gate refuses on its own — and CI has an override this gate does not. The one
# PR the old rule blocked that CI does not is the PR that FIXES the pin.

_BASE_DEGRADATIONS = {
    "empty": "",
    "whitespace-only": "   \n",
    "no CC_VERSION assignment": "NODE_MAJOR=22\n",
    "assigned twice": PIN_BASE + 'CC_VERSION="${CC_VERSION:-9.9.9}"\n',
    "file absent": "__absent__",
    "blob undecodable": "__undecodable__",
}
_HEAD_INTENTS = {
    # (head pin, body) — the pin is untouched, or moved forward WITH both receipts.
    "pin untouched": (PIN_BASE, ""),
    "forward bump with receipts": (PIN_FORWARD, RECEIPTS),
}


@pytest.mark.parametrize("degradation", sorted(_BASE_DEGRADATIONS))
@pytest.mark.parametrize("intent", sorted(_HEAD_INTENTS))
def test_no_base_side_fault_may_block_a_merge(monkeypatch, degradation, intent):
    """The full product of (base fault × what the PR is doing). Every cell passes.

    Parametrised over the PRODUCT deliberately: the original bug was found only
    because the "pin untouched" column was checked, and a matrix with that axis
    pinned would have shown four of the six as harmless.
    """
    head_pin, body = _HEAD_INTENTS[intent]
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", _BASE_DEGRADATIONS[degradation])
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", head_pin)
    monkeypatch.setenv("_TEST_GH_PR_BODY", body)

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False, (
        f"base fault {degradation!r} blocked a PR ({intent}). A base-side fault must "
        f"never block: no PR can repair it here and CI already covers it. Message: {msg}"
    )
    # It must also be VISIBLE. The merge arm prints a message only when it is marked
    # as a note, so an unmarked fail-open merges in silence — which is how "the
    # residue is narrow and named" became false at the only surface a human reads.
    # A genuine pass ("pin unchanged") has nothing to warn about and is exempt.
    if "unchanged" not in msg:
        assert msg.startswith("NOTE:"), (
            f"base fault {degradation!r} did not block, but the message is not marked as "
            f"a note, so the merge arm will not print it: {msg}"
        )
        assert "NOT verified" in msg, f"a fail-open must say it did not verify: {msg}"


@pytest.mark.parametrize("degradation", sorted(_BASE_DEGRADATIONS))
def test_a_PR_that_leaves_the_pin_file_alone_is_never_blocked(monkeypatch, degradation):
    """The cell that fixing only the base side leaves open.

    A PR that does not touch cc_version.sh inherits whatever is on the base AT ITS OWN
    HEAD. So a broken base makes the HEAD pin unreadable too, and the head-side rule
    blocks the PR — the wedge simply moves from one side to the other. Identical bytes
    at both refs cannot move the pin, so this must pass whatever state those bytes are
    in. MEASURED before the fix: every one of these blocked.
    """
    same = _BASE_DEGRADATIONS[degradation]
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", same)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", same)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "an unrelated PR that never touches the pin")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False, (
        f"a PR that leaves the pin file untouched was blocked because the base is "
        f"{degradation!r}. Message: {msg}"
    )


def test_a_forward_bump_still_blocks_when_the_base_is_FINE(monkeypatch):
    """The control for the matrix above. Without this, making every base cell pass
    would be satisfied by a gate that blocks nothing at all."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", PIN_FORWARD)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "no receipts anywhere in this body")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is True, f"a receipt-free forward bump must still block: {msg}"


def test_a_non_canonical_version_is_INCOMPARABLE_and_still_blocks(monkeypatch):
    """The one condition that blocks despite involving the base, and why it is not
    a base-side rule.

    `2.1.0246` and `2.1.246` compare EQUAL as integers, so the move cannot be judged.
    It is reached only when the pin MOVES, and CI cannot see it on a PR — the merge
    tree carries the good HEAD pin, so lockstep passes while main is red. Routing it
    to the non-blocking base path would let a real forward bump merge unchecked.
    """
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv(
        "_TEST_GH_BASE_PIN_FILE", 'CC_VERSION="${CC_VERSION:-2.1.0246}"\nNODE_MAJOR=22\n'
    )
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", PIN_FORWARD)
    monkeypatch.setenv("_TEST_GH_PR_BODY", RECEIPTS)

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is True, f"an incomparable pin pair must block: {msg}"
    assert "canonical semver" in msg, msg


def test_the_live_404_path_classifies_a_deleted_pin_as_ABSENT(monkeypatch):
    """The live classifier, not the seam.

    The other deleted-pin test injects `__absent__` and short-circuits before
    `_pin_file_at_ref` ever parses a response, so mutations to the real 404 handling
    survive it. This drives the actual gh failure path.
    """

    class _R:
        returncode = 1
        stdout = ""
        stderr = "gh: Not Found (HTTP 404)"

    monkeypatch.setattr(_mod.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", PIN_BASE)
    monkeypatch.delenv("_TEST_GH_HEAD_PIN_FILE", raising=False)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is True, f"a 404 at the head is a DELETED pin and must block: {msg}"
    assert "ABSENT at the PR head" in msg, msg


def test_a_missing_pin_file_on_the_BASE_does_NOT_block(monkeypatch):
    """Inverted 2026-08-28. It used to block, and that was the wedge.

    No base pin means no comparison — but the conclusion drawn from that was wrong.
    A base with no pin is a fault in the base branch: every open PR inherits it, none
    can repair it through this gate, and the gate has no override. It also already
    reddens CI. So the old rule blocked every merge in the repository to prevent one
    unverified bump, and the merge it blocked *hardest* was the one that would have
    fixed the pin.
    """
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_BASE_PIN_FILE", _mod._PIN_SEAM_ABSENT)
    monkeypatch.setenv("_TEST_GH_HEAD_PIN_FILE", PIN_FORWARD)
    monkeypatch.setenv("_TEST_GH_PR_BODY", "")

    blocked, msg = _mod._check_pin_receipts("999", repo="owner/repo")

    assert blocked is False
    assert "ABSENT on the base branch" in msg
    assert "NOT verified" in msg, "the fail-open must say it did not verify"


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
