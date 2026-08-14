"""End-to-end characterization corpus for ``git_push_guard.main()``'s pr-merge arm.

This is the *safety net* slice of the review-gate-core extraction. The extraction
will move the tangled fetch/decide/aggregate logic in ``main()`` behind a
``PrFacts`` snapshot + pure decision functions + one ``UNKNOWN -> BLOCK``
aggregator. That refactor must be **behaviour-preserving**, and this module is the
oracle that proves it: it drives the WHOLE merge arm end-to-end through ``main()``
and locks, per gate, the decision (block = exit 2 / allow = exit 0) *and* the
ordering between gates.

Why a NEW module rather than more helper tests: the decision *helpers*
(``_check_mergeable``, ``_check_codex_reviewed_head``, ``_check_pr_review_findings``
, ...) are already unit-locked in ``test_merge_review_gate.py`` /
``test_git_push_guard_codex_freshness.py``. What is NOT locked anywhere is
``main()``'s **aggregation** — the gate ORDERING (freshness before findings) and
the ``UNKNOWN -> BLOCK`` boundary at each gate — which is exactly what the
extraction restructures.

Network-free by construction. Two injection mechanisms are combined, mirroring the
real code's (fragmented) fetch surface — itself the motivation for the extraction:
  * the granular ``_TEST_GH_*`` env seams for the gates that have them
    (head sha / codex reviews / CI rollup / base ref / default branch);
  * an in-process ``subprocess.run`` router for the three gates that have NO seam
    and shell out to gh directly (mergeable, review-body comments, inline comments).
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

# Load the hook module directly from THIS worktree (mirrors the freshness suite),
# so the corpus locks the behaviour of the code under test in-tree.
_WORKTREE = Path(__file__).resolve().parent.parent.parent
_GUARD = _WORKTREE / "scripts" / "hooks" / "git_push_guard.py"
_spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
STALE = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
REPO = "owner/repo"


@pytest.fixture(autouse=True)
def _reset_merge_deadline():
    """``main()`` arms the module-global ``_merge_deadline`` once and never resets
    it to ``None``; without this a prior test's (now-expired) deadline leaks into
    the next and skews ``_gh_timeout``. The router ignores timeouts, but reset to
    keep each case hermetic."""
    _mod._merge_deadline = None
    yield
    _mod._merge_deadline = None


# ── injection helpers ────────────────────────────────────────────────


def _reviews_jsonl(*commit_ids: str, login: str = "chatgpt-codex-connector[bot]") -> str:
    """gh .../reviews --jq '{login, commit_id}' shape — one JSON object per line."""
    return "\n".join(json.dumps({"login": login, "commit_id": c}) for c in commit_ids)


def _ci(conclusion: str = "SUCCESS") -> str:
    """_TEST_GH_CI_ROLLUP shape — a JSON array of check-runs."""
    return json.dumps([{"name": "test", "conclusion": conclusion, "status": "COMPLETED"}])


_INLINE_P1_LINE = json.dumps(
    {
        "id": 1,
        "reply_to": None,
        "login": "chatgpt-codex-connector[bot]",
        "type": "Bot",
        "body": "![P1 Badge](x) something is broken",
    }
)
# A review-BODY comment carrying a blocking [P1] marker (issues/N/comments; JSON array).
_REVIEW_BODY_P1 = json.dumps(
    [{"login": "chatgpt-codex-connector[bot]", "type": "Bot", "body": "[P1] real defect"}]
)
# An inline P2 badge comment — warns but does NOT block.
_INLINE_P2_LINE = json.dumps(
    {
        "id": 2,
        "reply_to": None,
        "login": "chatgpt-codex-connector[bot]",
        "type": "Bot",
        "body": "![P2 Badge](x) a minor nit",
    }
)


def _compare_json(status: str = "ahead", files=None) -> str:
    """_TEST_GH_COMPARE shape — the compare-API payload the smart-delta gate reads."""
    return json.dumps({"status": status, "files": files or []})


def _proc(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


def _router(
    *,
    mergeable: str = "MERGEABLE",
    mergeable_rc: int = 0,
    inline_lines: str = "",
    review_array: str = "[]",
):
    """A ``subprocess.run`` stand-in for the three un-seamed gh calls on the merge
    path. Everything else on this path (git config/rev-parse, and any gh call we do
    not model) returns a clean no-op, which is correct because an explicit
    ``--repo`` avoids cwd/PR derivation and CI/base/freshness read their env seams."""

    def run(argv, **kwargs):  # noqa: ANN001 - subprocess.run signature
        parts = [str(a) for a in argv]
        joined = " ".join(parts)
        # gh pr view <N> --json mergeable --jq .mergeable
        if "mergeable" in joined:
            return _proc(mergeable_rc, mergeable)
        # gh api repos/.../pulls/<N>/comments  → INLINE findings (one json obj/line)
        if any("/pulls/" in a and a.endswith("/comments") for a in parts):
            return _proc(0, inline_lines)
        # gh api repos/.../issues/<N>/comments → REVIEW-BODY findings (json array)
        if any("/issues/" in a and a.endswith("/comments") for a in parts):
            return _proc(0, review_array)
        return _proc(0, "")

    return run


def _merge_cmd(
    *,
    pr: str = "100",
    repo: str | None = REPO,
    admin: bool = True,
    match: str | None = HEAD,
    body: str | None = None,
    trailer: str = "",
) -> str:
    parts = ["gh", "pr", "merge", pr]
    if repo is not None:
        parts += ["--repo", repo]
    parts.append("--squash")
    if admin:
        parts.append("--admin")
    if match is not None:
        parts += ["--match-head-commit", match]
    if body is not None:
        parts += ["--body", body]
    cmd = " ".join(parts)
    if trailer:
        cmd += " " + trailer
    return cmd


def _run(
    monkeypatch,
    command: str,
    *,
    head: str = HEAD,
    reviews: str | None = None,
    ci: str | None = None,
    base: str = "main",
    default: str = "main",
    compare: str | None = None,
    router=None,
) -> int:
    """Drive ``main()`` in-process: granular seams via env, un-seamed gh via the
    router, command via the real stdin payload contract."""
    monkeypatch.delenv("CLAUDE_TOOL_INPUT", raising=False)  # force the stdin path
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", head)
    monkeypatch.setenv(
        "_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(HEAD) if reviews is None else reviews
    )
    monkeypatch.setenv("_TEST_GH_CI_ROLLUP", _ci() if ci is None else ci)
    monkeypatch.setenv("_TEST_GH_BASE_REF", base)
    monkeypatch.setenv("_TEST_GH_DEFAULT_BRANCH", default)
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")  # no clean-comment fallback unless asked
    if compare is not None:
        monkeypatch.setenv("_TEST_GH_COMPARE", compare)  # smart-delta seam (opt-in)
    monkeypatch.setattr(_mod.subprocess, "run", router or _router())
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    return _mod.main()


# ── the corpus ───────────────────────────────────────────────────────
# Each case is (id, thunk, expected_rc, expected_stderr_substr). The thunk closes
# over `monkeypatch` (bound in the test) and returns main()'s exit code. Blocks are
# exit 2 with a distinguishing message; allows/no-op are exit 0.

# Cases are defined as callables taking (monkeypatch) so per-case seam/router knobs
# stay local and readable.
_CASES: list[tuple[str, object, int, str]] = [
    # ── happy path: every merge gate green → no block (exit 0) ──
    (
        "happy_path_all_gates_green_allows",
        lambda mp: _run(mp, _merge_cmd()),
        0,
        "",
    ),
    # ── mergeable gate — the fail-CLOSED allowlist (the flagship invariant) ──
    (
        "mergeable_UNKNOWN_blocks",
        lambda mp: _run(mp, _merge_cmd(), router=_router(mergeable="UNKNOWN")),
        2,
        "mergeable status",
    ),
    (
        "mergeable_query_failed_None_blocks",
        lambda mp: _run(mp, _merge_cmd(), router=_router(mergeable="", mergeable_rc=1)),
        2,
        "mergeable status",
    ),
    (
        "mergeable_CONFLICTING_blocks",
        lambda mp: _run(mp, _merge_cmd(), router=_router(mergeable="CONFLICTING")),
        2,
        "merge conflicts",
    ),
    (
        "mergeable_novel_state_blocks",
        lambda mp: _run(mp, _merge_cmd(), router=_router(mergeable="SOME_FUTURE_STATE")),
        2,
        "mergeable status",
    ),
    # ── CI gate — red blocks; red + conscious # ci-override continues ──
    (
        "ci_red_blocks",
        lambda mp: _run(mp, _merge_cmd(), ci=_ci("FAILURE")),
        2,
        "CI is RED",
    ),
    (
        "ci_red_with_ci_override_continues",
        lambda mp: _run(mp, _merge_cmd(trailer="# ci-override"), ci=_ci("FAILURE")),
        0,
        "consciously accepted",
    ),
    # ── base-branch invariant ──
    (
        "base_not_default_blocks",
        lambda mp: _run(mp, _merge_cmd(), base="stacked-feature", default="main"),
        2,
        "base branch is not the repo default",
    ),
    # ── Codex freshness ──
    (
        "no_codex_review_blocks",
        lambda mp: _run(mp, _merge_cmd(), reviews=""),
        2,
        "has not reviewed the current head",
    ),
    (
        "stale_codex_review_blocks",
        lambda mp: _run(mp, _merge_cmd(), reviews=_reviews_jsonl(STALE)),
        2,
        "has not reviewed the current head",
    ),
    # ── TOCTOU binding to the verified head ──
    (
        "fresh_review_but_no_match_head_blocks",
        lambda mp: _run(mp, _merge_cmd(match=None)),
        2,
        "bound to the Codex-verified head",
    ),
    (
        "shadow_body_flag_blocks",
        lambda mp: _run(mp, _merge_cmd(body="squash message")),
        2,
        "shadow the",
    ),
    (
        "match_head_mismatch_blocks",
        lambda mp: _run(mp, _merge_cmd(match=STALE)),
        2,
        "does not equal",
    ),
    # ── inline P1 findings block (freshness already clean) ──
    (
        "inline_P1_finding_blocks",
        lambda mp: _run(mp, _merge_cmd(), router=_router(inline_lines=_INLINE_P1_LINE)),
        2,
        "INLINE review findings",
    ),
    # ── ORDERING: freshness runs BEFORE the finding scans. With BOTH a missing
    #    review AND a would-be inline P1, the block message must be the FRESHNESS
    #    one — proving the extraction can't reorder these gates. ──
    (
        "freshness_precedes_findings_ordering",
        lambda mp: _run(
            mp,
            _merge_cmd(),
            reviews="",
            router=_router(inline_lines=_INLINE_P1_LINE),
        ),
        2,
        "has not reviewed the current head",
    ),
    # ── merge argv gates (pre-fetch) ──
    (
        "merge_without_admin_blocks",
        lambda mp: _run(mp, "gh pr merge 100 --repo owner/repo --squash"),
        2,
        "without --admin",
    ),
    (
        "merge_unresolvable_repo_blocks",
        lambda mp: _run(
            mp, "gh pr merge 100 --repo $X --squash --admin --match-head-commit " + HEAD
        ),
        2,
        "cannot determine which repository",
    ),
    (
        "compound_double_merge_blocks",
        lambda mp: _run(
            mp,
            "gh pr merge 1 --repo owner/repo --admin && gh pr merge 2 --repo owner/repo --admin",
        ),
        2,
        "multiple publish/merge operations",
    ),
    # ── review-body findings block (F2 — the 2nd scanner; ordered AFTER binding,
    #    BEFORE the inline scan — a reorder the extraction could make undetected) ──
    (
        "review_body_P1_finding_blocks",
        lambda mp: _run(mp, _merge_cmd(), router=_router(review_array=_REVIEW_BODY_P1)),
        2,
        "unresolved review findings",
    ),
    # ── override-sigil INDEPENDENCE (F3 — highest-risk gap: the extraction's
    #    unified force-plumbing must NOT collapse the two sigils into one) ──
    (
        # `# stale-review-override` waives freshness → a no-review PR still allows.
        "stale_override_waives_freshness_allows",
        lambda mp: _run(mp, _merge_cmd(match=None, trailer="# stale-review-override"), reviews=""),
        0,
        "",
    ),
    (
        # `# review-override` waives the finding scans → a review-body P1 allows
        # (freshness + binding still satisfied by a fresh review + match-head).
        "review_override_waives_findings_allows",
        lambda mp: _run(
            mp,
            _merge_cmd(trailer="# review-override"),
            router=_router(review_array=_REVIEW_BODY_P1),
        ),
        0,
        "",
    ),
    (
        # THE CROSS (most important): the FINDINGS sigil must NOT waive freshness —
        # a no-review PR still blocks on freshness. Locks the #1366-hardened boundary.
        "review_override_does_NOT_waive_freshness_blocks",
        lambda mp: _run(mp, _merge_cmd(trailer="# review-override"), reviews=""),
        2,
        "has not reviewed the current head",
    ),
    # ── smart-delta: a STALE review whose delta is review-trivial ALLOWS, still
    #    bound to head (F4 — a positive-evidence allow inside the fail-closed gate) ──
    (
        "smart_delta_trivial_stale_review_allows",
        lambda mp: _run(
            mp, _merge_cmd(), reviews=_reviews_jsonl(STALE), compare=_compare_json("identical")
        ),
        0,
        "review-trivial",
    ),
    # ── inline P2 warns but does NOT block (F5) ──
    (
        "inline_P2_finding_warns_but_allows",
        lambda mp: _run(mp, _merge_cmd(), router=_router(inline_lines=_INLINE_P2_LINE)),
        0,
        "[P2]",
    ),
]


@pytest.mark.parametrize(
    "case_id,thunk,expected_rc,expected_msg", _CASES, ids=[c[0] for c in _CASES]
)
def test_merge_gate_aggregation(case_id, thunk, expected_rc, expected_msg, monkeypatch, capsys):
    rc = thunk(monkeypatch)
    err = capsys.readouterr().err
    assert rc == expected_rc, f"{case_id}: expected exit {expected_rc}, got {rc}. stderr:\n{err}"
    if expected_msg:
        assert expected_msg in err, f"{case_id}: {expected_msg!r} not in stderr:\n{err}"
