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
``main()``'s **aggregation** — the gate ORDERING (freshness before findings), the
``UNKNOWN -> BLOCK`` boundary at each gate, and the INDEPENDENCE of the override
sigils (``# ci-override`` / ``# stale-review-override`` / ``# review-override`` must
each waive only their own gate) — which is exactly what the extraction restructures.

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


def _reviews_jsonl(
    *commit_ids: str,
    login: str = "chatgpt-codex-connector[bot]",
    state: str | None = None,
) -> str:
    """gh .../reviews --jq '{login, commit_id, state}' shape — one JSON object per line.

    ``state`` mirrors the production injection contract (``_codex_reviews``: "missing
    ``state`` = active"); omit it (the default) and no ``state`` key is emitted, keeping
    every pre-existing case byte-identical. Pass ``"DISMISSED"`` to exercise the freshness
    gate's dismissed-review exclusion (a dismissed review vouches for no commit)."""
    rows = []
    for c in commit_ids:
        obj = {"login": login, "commit_id": c}
        if state is not None:
            obj["state"] = state
        rows.append(json.dumps(obj))
    return "\n".join(rows)


def _scheduled_marker(
    head: str = HEAD,
    *,
    login: str = "owner",
    author_association: str = "OWNER",
    kinds: tuple[str, ...] = ("code-review", "leaks"),
) -> str:
    """_TEST_GH_SCHEDULED_COMMENTS shape — one OWNER-authored comment/review row whose
    body carries one scheduled-review marker per ``kind`` naming ``head``. One JSON object
    per line. The DEFAULT carries BOTH required kinds (code-review + leaks), so every
    pre-existing case satisfies the gate; scheduled-gate cases pass explicit ``kinds``.

    ``login``/``author_association`` model the trust check: a row is accepted only when
    ``login == <repo owner>`` OR ``author_association == "OWNER"``. The repo owner here is
    ``owner`` (``REPO = "owner/repo"``), so the default satisfies BOTH arms."""
    markers = "\n".join(f"<!-- genesis-scheduled-review: head={head} kind={k} -->" for k in kinds)
    body = "Scheduled review complete.\n" + markers
    return json.dumps({"login": login, "author_association": author_association, "body": body})


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
    calls: list | None = None,
):
    """A ``subprocess.run`` stand-in for the three un-seamed gh calls on the merge
    path (mergeable / inline / review-body). Everything else (git config/rev-parse,
    any gh call we do not model) returns a clean no-op — correct because an explicit
    ``--repo`` avoids cwd/PR derivation and CI/base/freshness read their env seams.

    ``calls`` (when given) records ``(endpoint_label, argv_str)`` per modelled call in
    FETCH ORDER. Assertions live in the TEST, not here: a wrong-repo/wrong-PR call must
    surface as a test failure, but asserting inside ``run`` would be SWALLOWED by the
    production fail-closed/fail-open ``try/except`` around each gh call (Codex #1399).
    So a test asserts on the recorded log — that every fetch targeted the gated PR/repo,
    or that findings are NOT fetched when an earlier gate blocks."""

    def run(argv, **kwargs):  # noqa: ANN001 - subprocess.run signature
        parts = [str(a) for a in argv]
        joined = " ".join(parts)

        def _rec(label: str):
            if calls is not None:
                # record argv PARTS (not the joined string) so tests can pin the PR as
                # an exact token / path segment, not a superstring (Codex #1399 NOTE).
                calls.append((label, tuple(parts)))

        # gh pr view <pr> --repo <repo> --json mergeable --jq .mergeable
        if "mergeable" in joined:
            _rec("mergeable")
            return _proc(mergeable_rc, mergeable)
        # gh api repos/<repo>/pulls/<pr>/comments → INLINE findings (one json obj/line)
        if any("/pulls/" in a and a.endswith("/comments") for a in parts):
            _rec("inline")
            return _proc(0, inline_lines)
        # gh api repos/<repo>/issues/<pr>/comments → REVIEW-BODY findings (json array)
        if any("/issues/" in a and a.endswith("/comments") for a in parts):
            _rec("review-body")
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
    scheduled: str | None = None,
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
    # Default: a valid OWNER-authored scheduled-review marker AT head, so every
    # pre-existing case stays behaviour-identical (mirrors the default codex review
    # above). Scheduled-gate cases pass an explicit ``scheduled=`` to override.
    monkeypatch.setenv(
        "_TEST_GH_SCHEDULED_COMMENTS", _scheduled_marker(head) if scheduled is None else scheduled
    )
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
    # ── Codex #1399 hardening: coverage the round-1/2 reviews missed ──
    # (Codex-P1) stale-override must NOT waive the FINDINGS scans — the symmetric half
    # of the review-override cross above: it waives freshness+base, but a review-body
    # P1 still blocks.
    (
        "stale_override_does_NOT_waive_findings_blocks",
        lambda mp: _run(
            mp,
            _merge_cmd(match=None, trailer="# stale-review-override"),
            reviews="",
            router=_router(review_array=_REVIEW_BODY_P1),
        ),
        2,
        "unresolved review findings",
    ),
    # (Codex-P2) CI-unknown is a deliberately fail-OPEN gate — empty/malformed CI reads
    # must ALLOW, so the unified UNKNOWN->BLOCK aggregation can't regress them. NOTE: CI
    # is not the ONLY fail-open path — the review-body and inline finding SCANS also fail
    # open on an unreadable read (git_push_guard.py _scan_unreadable, non-strict). That
    # scanner-unreadable path is a distinct axis carried to Slice 3 (PrFacts decision
    # level, follow-up 086bd8e3), not exercised here.
    (
        "ci_empty_unknown_fails_open_allows",
        lambda mp: _run(mp, _merge_cmd(), ci=""),
        0,
        "",
    ),
    (
        "ci_malformed_unknown_fails_open_allows",
        lambda mp: _run(mp, _merge_cmd(), ci="not-json"),
        0,
        "",
    ),
    # (Codex-P1) smart-delta NEGATIVE control: the trivial-stale allow must stay BOUND
    # to head — the same trivial delta with NO --match-head-commit blocks (a refactor
    # that dropped verified_head on this path would selectively disengage TOCTOU).
    (
        "smart_delta_trivial_unbound_blocks",
        lambda mp: _run(
            mp,
            _merge_cmd(match=None),
            reviews=_reviews_jsonl(STALE),
            compare=_compare_json("identical"),
        ),
        2,
        "bound to the Codex-verified head",
    ),
    # (follow-up 535648be) `# review-override` also waives the INLINE P1 scan directly
    # (not just transitively via the shared force_override).
    (
        "inline_override_waives_P1_allows",
        lambda mp: _run(
            mp,
            _merge_cmd(trailer="# review-override"),
            router=_router(inline_lines=_INLINE_P1_LINE),
        ),
        0,
        "",
    ),
    # (follow-up 535648be) numberless merge + explicit --repo → fail CLOSED (can't
    # resolve the PR; a cwd-branch number gated against a named repo = the wrong-PR class).
    (
        "pr_num_None_fail_closed_blocks",
        lambda mp: _run(mp, "gh pr merge --repo owner/repo --squash --admin"),
        2,
        "cannot resolve which PR",
    ),
    # ── Codex #1399 round-2 batch (2026-08-18): complete the sigil-independence
    #    matrix + the dismissed-review freshness state. Each locks CURRENT behaviour;
    #    the one-line source mutation that flips it is noted per case (verify-RED). ──
    #
    # (Codex-P1 3788128906) stale-override × INLINE P1: the INLINE half of
    # `stale_override_does_NOT_waive_findings_blocks` (which uses the review-BODY scan).
    # The two scanners are wired independently, so a refactor could route stale_override
    # into only ONE of them. `# stale-review-override` waives freshness+base (verified_head
    # → None, binding skipped), the review-body scan is clean, but the inline P1 still
    # BLOCKS. verify-RED: pass `force=stale_override` at the inline call site → allows.
    (
        "stale_override_does_NOT_waive_inline_findings_blocks",
        lambda mp: _run(
            mp,
            _merge_cmd(match=None, trailer="# stale-review-override"),
            reviews="",
            router=_router(inline_lines=_INLINE_P1_LINE),
        ),
        2,
        "INLINE review findings",
    ),
    # (Codex-P1 3788128908) `# ci-override` is scoped to the CI gate ALONE — it must not
    # act as a global force. The existing `ci_red_with_ci_override_continues` case has
    # every downstream gate green, so it can't tell "CI-scoped" from "global force". These
    # three bound the consequential downstream gates: with red CI + `# ci-override`, an
    # inline P1 / a review-body P1 / a missing review must EACH still block. (base-mismatch
    # and stale-review combos are document-accepted siblings → Slice 3.) verify-RED for the
    # two findings cases: `force_override = merge_seg.override or ci_override` → they allow.
    (
        "ci_override_does_NOT_waive_inline_findings_blocks",
        lambda mp: _run(
            mp,
            _merge_cmd(trailer="# ci-override"),
            ci=_ci("FAILURE"),
            router=_router(inline_lines=_INLINE_P1_LINE),
        ),
        2,
        "INLINE review findings",
    ),
    (
        "ci_override_does_NOT_waive_review_body_findings_blocks",
        lambda mp: _run(
            mp,
            _merge_cmd(trailer="# ci-override"),
            ci=_ci("FAILURE"),
            router=_router(review_array=_REVIEW_BODY_P1),
        ),
        2,
        "unresolved review findings",
    ),
    (
        # ci_override must not waive FRESHNESS either (freshness reads stale_override, not
        # ci_override). verify-RED: `force=ci_override or stale_override` on the freshness
        # check → this allows.
        "ci_override_does_NOT_waive_freshness_blocks",
        lambda mp: _run(
            mp,
            _merge_cmd(trailer="# ci-override"),
            ci=_ci("FAILURE"),
            reviews="",
        ),
        2,
        "has not reviewed the current head",
    ),
    # (Codex-P1 3788128914) a DISMISSED review AT the current head does NOT satisfy
    # freshness — a dismissed review vouches for no commit (_codex_review_commit_ids skips
    # DISMISSED, :1024). Distinct from the absent/stale cases: the sha MATCHES head, only
    # the state disqualifies it. verify-RED: drop the `!= "DISMISSED"` filter → allows.
    (
        "dismissed_review_at_head_blocks",
        lambda mp: _run(mp, _merge_cmd(), reviews=_reviews_jsonl(HEAD, state="DISMISSED")),
        2,
        "has not reviewed the current head",
    ),
    # ── Scheduled Claude review gate (owner-authored marker at HEAD) ──
    # An owner marker naming the CURRENT head → the gate is satisfied → merge allowed.
    (
        "scheduled_review_at_head_allows",
        lambda mp: _run(mp, _merge_cmd(), scheduled=_scheduled_marker(HEAD)),
        0,
        "",
    ),
    # A marker naming a STALE sha does NOT vouch for the current head → block.
    (
        "scheduled_review_stale_sha_blocks",
        lambda mp: _run(mp, _merge_cmd(), scheduled=_scheduled_marker(STALE)),
        2,
        "scheduled Claude review(s) missing",
    ),
    # No scheduled review at all (didn't run / rate-limited) → fail-closed block.
    (
        "scheduled_review_absent_blocks",
        lambda mp: _run(mp, _merge_cmd(), scheduled=""),
        2,
        "scheduled Claude review(s) missing",
    ),
    # A marker from a NON-owner author (login != owner, association != OWNER) is NOT
    # trusted → treated as absent → block.
    (
        "scheduled_review_non_owner_author_blocks",
        lambda mp: _run(
            mp,
            _merge_cmd(),
            scheduled=_scheduled_marker(HEAD, login="randouser", author_association="CONTRIBUTOR"),
        ),
        2,
        "scheduled Claude review(s) missing",
    ),
    # Owner-login match is case-INSENSITIVE (GitHub logins are canonically cased, but
    # fold both sides as defense-in-depth): a marker whose login differs only in case
    # from the repo owner, with a non-OWNER association, is still trusted → allows.
    (
        "scheduled_review_owner_login_case_insensitive_allows",
        lambda mp: _run(
            mp,
            _merge_cmd(),
            scheduled=_scheduled_marker(HEAD, login="OWNER", author_association="NONE"),
        ),
        0,
        "",
    ),
    # BOTH required kinds must be present at head: a marker for only code-review (leaks
    # routine did not run) blocks, naming the missing kind.
    (
        "scheduled_review_only_code_review_blocks_missing_leaks",
        lambda mp: _run(
            mp, _merge_cmd(), scheduled=_scheduled_marker(HEAD, kinds=("code-review",))
        ),
        2,
        "leaks",
    ),
    # ...and only leaks (code-review routine did not run) blocks too.
    (
        "scheduled_review_only_leaks_blocks_missing_code_review",
        lambda mp: _run(mp, _merge_cmd(), scheduled=_scheduled_marker(HEAD, kinds=("leaks",))),
        2,
        "code-review",
    ),
    # The # scheduled-review-override sigil consciously waives the gate → an absent
    # scheduled review still merges.
    (
        "scheduled_override_waives_absent_review_allows",
        lambda mp: _run(mp, _merge_cmd(trailer="# scheduled-review-override"), scheduled=""),
        0,
        "",
    ),
    # ── sigil INDEPENDENCE: the scheduled sigil and the stale sigil are separate ──
    # # stale-review-override waives Codex freshness+base, NOT the scheduled gate: an
    # absent scheduled review still blocks even with a fresh-review waiver.
    (
        "stale_override_does_NOT_waive_scheduled_blocks",
        lambda mp: _run(
            mp,
            _merge_cmd(match=None, trailer="# stale-review-override"),
            reviews="",
            scheduled="",
        ),
        2,
        "scheduled Claude review(s) missing",
    ),
    # # scheduled-review-override waives ONLY the scheduled gate, NOT Codex freshness:
    # a no-review PR still blocks on freshness (which runs first).
    (
        "scheduled_override_does_NOT_waive_freshness_blocks",
        lambda mp: _run(mp, _merge_cmd(trailer="# scheduled-review-override"), reviews=""),
        2,
        "has not reviewed the current head",
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


def test_findings_not_fetched_when_freshness_blocks(monkeypatch):
    """Fetch-order / TOCTOU (Codex #1399): when the freshness gate blocks, NEITHER
    finding endpoint is queried. Locks the ordering as a FETCH property (not merely a
    returned-error priority) — a refactor that fetched findings before confirming a
    current review would let a review published mid-fetch pass freshness with its own
    P1 comments unscanned. Proven via the router's call log."""
    calls: list = []
    rc = _run(
        monkeypatch,
        _merge_cmd(),
        reviews="",  # no current review → freshness blocks
        router=_router(inline_lines=_INLINE_P1_LINE, review_array=_REVIEW_BODY_P1, calls=calls),
    )
    assert rc == 2
    labels = [label for label, _argv in calls]
    assert "inline" not in labels and "review-body" not in labels, (
        f"findings were fetched despite a freshness block (TOCTOU risk): {labels}"
    )


def test_merge_fetches_target_the_gated_pr_and_repo(monkeypatch):
    """Repo/PR threading (Codex #1399): on a clean merge, all three un-seamed fetches
    (mergeable + both finding scanners) run AND every one targets the gated PR+repo.
    A refactor that dropped ``--repo`` from mergeability, or emitted a finding endpoint
    for the wrong PR/repo (the 2026-07-26 wrong-repo class), would still receive these
    fixtures — so the assertion is on the recorded call log, outside the production
    try/except that would otherwise swallow an in-router assert."""
    calls: list = []
    rc = _run(monkeypatch, _merge_cmd(), router=_router(calls=calls))
    assert rc == 0
    labels = [label for label, _parts in calls]
    assert set(labels) >= {
        "mergeable",
        "inline",
        "review-body",
    }, f"a merge fetch was skipped: {labels}"
    # Pin the PR as an exact argv token (mergeable) / path segment (the api scanners),
    # never a loose substring — so a mis-thread to PR 1000 or repo owner/repo-fork fails.
    pr = "100"
    for label, parts in calls:
        if label == "mergeable":
            ok = pr in parts and REPO in parts
        elif label == "inline":
            ok = f"repos/{REPO}/pulls/{pr}/comments" in parts
        else:  # review-body
            ok = f"repos/{REPO}/issues/{pr}/comments" in parts
        assert ok, f"{label} fetch off-target: {parts}"


def test_e3_stale_override_gates_positional_pr_not_flag_value(monkeypatch):
    """E3 (2026-08-19) — wrong-PR resolution. gh parses ``gh pr merge --subject 123 5``
    as subject="123" + PR **5** (the trailing positional), but the old
    ``_extract_pr_number`` returned **123** (the ``--subject`` VALUE) → every gate
    then checked the WRONG PR. It is contained on the normal path (the shadow-flag
    belt refuses ``--subject`` once the head binding engages), so the reachable
    main()-level observable is under ``# stale-review-override`` — which waives
    freshness and with it SKIPS the shadow belt + binding, letting the command flow
    into the finding scanners. An inline P1 seeded on PR 5 (the true gh target) must
    BLOCK, and the scan must target PR 5, never 123. Old resolver → 123 → clean scan
    → exit 0. Proven on the router's call log (assert in the TEST frame, Codex #1399)."""
    calls: list = []

    def router(argv, **kwargs):  # noqa: ANN001 - subprocess.run signature
        parts = [str(a) for a in argv]
        if "mergeable" in " ".join(parts):
            return _proc(0, "MERGEABLE")
        if any("/pulls/" in a and a.endswith("/comments") for a in parts):
            calls.append(("inline", tuple(parts)))
            # P1 lives only on the TRUE target (PR 5); the wrong PR (123) sees nothing.
            hit = f"repos/{REPO}/pulls/5/comments" in parts
            return _proc(0, _INLINE_P1_LINE if hit else "")
        if any("/issues/" in a and a.endswith("/comments") for a in parts):
            calls.append(("review-body", tuple(parts)))
            return _proc(0, "[]")
        return _proc(0, "")

    cmd = "gh pr merge --subject 123 5 --repo owner/repo --squash --admin  # stale-review-override"
    rc = _run(monkeypatch, cmd, reviews="", router=router)
    assert rc == 2, f"expected a block on PR 5's inline P1, got exit {rc}"
    inline = [parts for label, parts in calls if label == "inline"]
    assert inline, "inline findings were never fetched"
    assert all(f"repos/{REPO}/pulls/5/comments" in parts for parts in inline), inline
    assert not any(f"repos/{REPO}/pulls/123/comments" in parts for parts in inline), (
        f"findings scanned the WRONG PR (123, the --subject value): {inline}"
    )


def _report_env(monkeypatch, *, scheduled: str, head: str = HEAD):
    """Seed every OTHER gate green so ``check_pr_report`` isolates the scheduled line.
    The scheduled gate itself runs for real against the ``scheduled`` seam."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", head)
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(head))
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", scheduled)
    monkeypatch.setattr(_mod, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
    monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: ("green", []))
    monkeypatch.setattr(_mod, "_check_base_is_default", lambda n, repo=None: (False, ""))
    monkeypatch.setattr(
        _mod, "_check_pr_review_findings", lambda n, repo=None, strict=False: (False, "")
    )
    monkeypatch.setattr(
        _mod, "_check_inline_review_findings", lambda n, repo=None, strict=False: (False, "")
    )


def test_check_pr_report_includes_scheduled_line_ok(monkeypatch, capsys):
    """The canonical report prints a ``scheduled-claude`` line and PASSES when an
    owner marker names the current head (shares the enforcement function, strict mode)."""
    _report_env(monkeypatch, scheduled=_scheduled_marker(HEAD))
    rc = _mod.check_pr_report("100", repo=REPO)
    out = capsys.readouterr().out
    assert "scheduled-claude:" in out
    sched_line = next(ln for ln in out.splitlines() if ln.startswith("scheduled-claude"))
    assert "ok (at head)" in sched_line
    assert rc == 0


def test_check_pr_report_scheduled_absent_fails_verdict(monkeypatch, capsys):
    """An absent scheduled review is a FAILURE line and flips the report's verdict —
    the report must not diverge from the always-fail-closed enforcement gate."""
    _report_env(monkeypatch, scheduled="")
    rc = _mod.check_pr_report("100", repo=REPO)
    out = capsys.readouterr().out
    sched_line = next(ln for ln in out.splitlines() if ln.startswith("scheduled-claude"))
    assert "BLOCK" in sched_line
    assert rc == 1
