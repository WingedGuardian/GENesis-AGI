#!/usr/bin/env python3
"""PreToolUse hook: block git push/merge to main without user approval.

Catches all variations of pushing to or merging into the main branch:
- git push (bare, when on main)
- git push origin main
- git push -u origin main
- git merge <branch> (when on main)
- gh pr merge (without --admin — requires explicit user approval flag)
- gh pr merge with unresolved review findings (ERROR/[P1]/HARD BLOCK)

Stdlib-only. Fail-open on parse errors (don't block legitimate work).

Threat model (declared — the standing triage rule for the merge gate)
---------------------------------------------------------------------
This is a PROCESS gate for a SINGLE-AUTHOR, single-remote, github.com repo whose
PRs target the default branch. It defends the SESSION'S OWN process failures, not
an adversary:

  * merging a head Codex never reviewed, or reviewed at an earlier commit
    (``_check_codex_reviewed_head`` + the ``--match-head-commit`` binding);
  * a race with the session's OWN later push between check and merge (same
    binding — GitHub enforces it server-side);
  * an ACCIDENTAL wrong-repo merge — a bare merge run from another checkout or
    after a ``cd`` (``_derive_repo_from_cwd``); or an unresolvable ``--repo``;
  * an ACCIDENTAL base retarget that silently invalidates the review, since the
    head never moves (``_check_base_is_default``);
  * an unreadable mergeability / review status read as "clean" (allowlist
    posture: block unless a DEFINITE good value — MERGEABLE, a current review).

It does NOT model a malicious operator crafting argv to defeat the gate (they own
``--admin`` and ``# review-override`` already), NOR a multi-actor repo where a
hostile collaborator force-pushes or retargets. Findings outside this model
(adversarial argv shaping, hostile-collaborator timelines, non-github hosts) are
OUT OF SCOPE BY DESIGN — reply as such rather than adding another patch. The
cluster-safe ``--match-head-commit`` parse + fail-closed shadow-flag belt already
exceed the model (defense-in-depth), which is fine; they are not an invitation to
chase every argv-shaping edge. Conscious escapes are split by boundary so one
waiver cannot silently disarm an unrelated gate: ``# review-override`` waives the
FINDING scans (review-body + inline P1s), ``# stale-review-override`` waives the
review-CONTEXT gates (Codex-at-head freshness + base-is-default),
``# scheduled-review-override`` waives the SCHEDULED-Claude-review-at-head gate, and
``# ci-override`` waives the CI gate — red/pending everywhere, plus (canonical repo
only) "absent" (empty rollup: CI never ran) and "incomplete" (partial rollup: a
REQUIRED workflow — ``merge_gate.required_ci_workflows`` in genesis.yaml, default
``CI`` — contributed no verdict; see ``_required_ci_workflows``). A session that
genuinely needs several appends several (one trailing comment may carry multiple
sigils).

Hook-surface merge teeth (2026-08-23): a PR whose diff touches the
ENFORCEMENT-HOOK surface (``_HOOK_SURFACE_PREFIXES``/``_HOOK_SURFACE_FILES`` —
the code these gates themselves run on) gets stricter freshness handling: its
stale-review delta is never "review-trivial", and ``# stale-review-override``
alone cannot merge it — recorded fallback-review evidence keyed to the exact
head sha is additionally required (``_hook_surface_override_check``; the block
message documents the user-authorized fallback procedure).

Scheduled Claude review markers
-------------------------------
A "scheduled Claude review" runs on the repo OWNER's GitHub account (NOT a bot) and must
post a comment (issue comment) or PR review whose body contains a marker
``<!-- genesis-scheduled-review: head=<full-40-hex-sha> kind=<name> -->`` naming the exact
head it reviewed AND which routine it was (``kind``). The merge gate
(``_check_scheduled_claude_reviewed_head``) blocks unless an owner-authored marker for
EVERY effective required kind (``_required_scheduled_review_kinds()`` — DEFAULT
code-review + leaks, with the leak scanner irreducible; an install may relax the optional
kinds to advisory via ``merge_gate.required_scheduled_reviews`` in genesis.yaml) names
the PR's CURRENT head — so if any required routine never ran, ran on a stale commit, or
was rate-limited, the merge is blocked (naming the missing kinds). An ADVISORY routine
(one relaxed out of the required set locally) still posts its review on the PR to be read
and addressed, but its absence does not block. SCOPE: this gate
enforces ONLY when the merge targets the configured PUBLIC repo — the declared
``github.user``/``github.public_repo`` in ``~/.genesis/config/genesis.yaml``
(``_scheduled_gate_applies`` / ``_canonical_public_repo``). A merge to any OTHER repo
(a private fork, the voice repo, backups) no-ops, since the required ``/schedule``
routines run only on the public repo. Deployment note: on the public repo the required
routines ARE configured (the deploy precondition); a clone that runs on its own public
repo without a producer uses `# scheduled-review-override` (or relaxes the optional kinds
via ``merge_gate.required_scheduled_reviews``) — the override valve is the escape by design, not an
opt-in flag. Fail-closed on scope uncertainty: if the canonical repo is undeterminable
the gate ENGAGES rather than silently disarming.
A DISMISSED or PENDING (draft) review no longer vouches (its marker is ignored), mirroring
the Codex path. The marker means "ran CLEAN", not merely "ran": a review whose body carries a
blocking finding ([P1]/HARD BLOCK/### ERROR, unless a clean marker overrides — the same rule
the finding scanners use) is rejected, so a scheduled reviewer that explicitly BLOCKED cannot
stamp a passing marker (owner-authored bodies are not seen by the bot-only finding scanners).
On the normal path this is ATOMIC: the Codex-freshness gate's ``--match-head-commit``
binding pins the merge to the very head the marker was verified at, so a race with a new
push cannot swap in an unreviewed head. Under ``# stale-review-override`` (which waives
that binding for ALL gates — a conscious "merge without current verification" choice) the
scheduled check is point-in-time, matching the reduced posture the operator already opted
into; it is not additionally head-bound there.
The marker is the trust anchor (single-author, non-adversarial threat model): this gate
defends against a review that did not run at HEAD, NOT against the owner's own review
automation being manipulated (e.g. prompt-injected by attacker-controlled PR content) into
posting a false marker — that guarantee lives in the scheduled-review producer, which must
not treat untrusted PR content as instructions when composing its comment body.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time

# Self-locate so `from hook_input import …` resolves both when CC runs this as a
# script (sys.path[0] is this dir) AND when it is imported as a module for tests
# (importlib does not add the file's dir to sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# scripts/ (parent dir) for review_state — the shared escalation-cap constant, so
# the Codex-round gate below and the commit gate's Rule 3 stop at the same N.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hook_input import field, read_payload, run_guard  # noqa: E402

# SOFT dependency (mirrors review_enforcement_commit.py's guard for the SAME
# import): an unimportable review_state must degrade ONLY the round-escalation
# advisory to its documented default — never crash this module at load time.
# A module-load exception exits 1 BEFORE run_guard's fail-closed wrapper can
# convert it to a block, and CC treats non-2 as non-blocking → EVERY fail-closed
# gate in this file (force-push, merge, sqlite) would silently vanish.
try:
    from review_state import ESCALATION_ROUND_CAP  # noqa: E402
except Exception:  # noqa: BLE001 — ANY failure (absent OR broken: SyntaxError,
    # read error, top-level runtime error in review_state) must degrade to the
    # default cap, NEVER propagate: a module-load exception exits 1 (non-blocking)
    # and silently disables every fail-closed gate in this file (round-6 P1).
    ESCALATION_ROUND_CAP = 3  # the genesis-development SKILL.md prose cap

from shell_parse import (  # noqa: E402
    analyze,
    commit_skips_hooks,
    gh_pr_subcommand,
    git_subcommand,
    has_trailing_override,
    split_segments,
    untokenizable,
)

# Mentions of a GATED operation, consulted ONLY on the un-parseable path where
# analyze() has gone blind. Deliberately BROAD — both the gated verbs and the
# destructive flags — because the outcome there is an approval PROMPT, not a
# block: an over-match costs one confirmation, while an under-match silently
# runs an unverified publish. (An earlier flag-only, hard-block version had to be
# surgically precise, and precision is exactly what an unreliable parse cannot
# deliver — every narrowing conjunct became a new way to starve the trigger.)
_GATED_MENTION = re.compile(
    r"(?:^|\s)(?:--force(?:-with-lease)?|--no-verify|--admin)(?:\s|=|$)|\b(?:push|merge)\b"
)

# `gh pr create` is the FOURTH gated operation (it can push or fork the branch —
# see _pr_create_would_publish), and it was missing from the mention set above.
# MEASURED: an ANSI-C-hidden `gh pr create` on an unpushed branch was ALLOWED
# while the plain form correctly asked — the same fail-open this net exists to
# close, for an op the first cut omitted.
#
# It is a CONJUNCTION rather than a `create` alternative in the regex because
# `create` alone is an ordinary English word. Measured over 11,488 real commands
# (328 un-tokenizable): a bare `\bcreate\b` alternative adds 6 new prompts, all
# benign here-doc Python; requiring `gh` as well adds ZERO while still catching
# the bypass. Two literal token tests combined in code — deliberately NOT a
# lookaround, which is positional: `(?=.*\bgh\b)\bcreate\b` reads FORWARD from
# `create`, and in `gh pr create` the `gh` is BEHIND it, so that pattern matches
# nothing and would have measured 0 false positives by never firing at all.
_GH_MENTION = re.compile(r"\bgh\b")
_CREATE_MENTION = re.compile(r"\bcreate\b")


def _mentions_gated_op(command: str) -> bool:
    """Whether the RAW text names any gated operation, on the blind path only."""
    if _GATED_MENTION.search(command):
        return True
    return bool(_GH_MENTION.search(command) and _CREATE_MENTION.search(command))


# Local push allowlist (offline re-push cache). SOFT dependency, guarded exactly
# like the review_state import above: a module-LOAD exception must degrade to
# None (→ the pure live-ls-remote republish path, i.e. today's behavior), NEVER
# propagate — a raising import exits 1, which CC treats as non-blocking, silently
# disabling EVERY fail-closed gate in this file. push_allowlist itself is
# fail-open by contract (its calls never raise); None here only covers an
# import-time breakage (absent module, SyntaxError). It can only ever RELAX a
# re-push of a branch already confirmed on the remote — never a first push.
try:
    import push_allowlist  # noqa: E402 — scripts/hooks is on sys.path[0]
except Exception:  # noqa: BLE001 — see the review_state guard's rationale (108-114)
    push_allowlist = None

# Sentinel: the effective cwd cannot be confidently resolved (a cd into a
# variable/command-substitution, a subshell, or a target nested at depth>0).
# Callers MUST fail closed on it — block the merge, do not soften a force push.
_CWD_UNKNOWN = object()

# git global options that consume the FOLLOWING token as their value — used to
# skip past `git -C <dir>` / `git -c KEY=VAL` when locating a push's positionals.
_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
)


def _current_branch(cwd: str | None = None) -> str | None:
    """Get current git branch name, optionally in a specific working dir.

    When ``cwd`` is truthy, run ``git -C <cwd> branch --show-current`` so the
    branch reflects the worktree the command actually targets — not the hook's
    own cwd (always the main tree, on ``main``). Fail-safe: None on any error.
    """
    try:
        args = ["git"]
        if cwd:
            args += ["-C", cwd]
        args += ["branch", "--show-current"]
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _seg_dash_C(argv) -> str | None:
    """The dir named by a ``git -C <dir>`` in a segment's argv, else None."""
    argv = argv or []
    for i, tok in enumerate(argv):
        if tok == "-C" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _cd_target(raw: str):
    """Classify a top-level command segment as a ``cd``.

    Returns the literal target dir when ``raw`` is a plain ``cd <literal-path>``
    segment; ``_CWD_UNKNOWN`` when it is a cd we cannot resolve (no arg, ``cd -``,
    a variable/command-substitution/glob target, or a subshell/group that would
    scope the cd); ``None`` when it is not a cd at all. Because ``split_segments``
    already split on shell operators, a genuine cd segment is exactly ``cd <path>``.
    """
    s = raw.strip()
    # A subshell/group ( … ) / { … } scopes its cd — we cannot track it.
    if s.startswith("(") or s.startswith("{"):
        return _CWD_UNKNOWN
    m = re.match(r"^cd(?:\s+(?P<p>.*))?$", s)
    if not m:
        return None
    p = (m.group("p") or "").strip()
    if not p or p == "-":
        return _CWD_UNKNOWN  # `cd` (home) / `cd -` (previous) — unresolvable here
    # Quoted target.
    if len(p) >= 2 and p[0] in "'\"" and p[-1] == p[0]:
        inner = p[1:-1]
        if p[0] == '"' and ("$" in inner or "`" in inner):
            return _CWD_UNKNOWN  # double-quote expansion
        return inner
    if " " in p or "\t" in p:
        return _CWD_UNKNOWN  # extra args / unexpected shape → don't guess
    if p.startswith("~"):
        p = os.path.expanduser(p)
    if any(ch in p for ch in "$`*?"):
        return _CWD_UNKNOWN  # expansion or glob → unresolvable
    return p


def _resolve_against(current, target: str):
    """Resolve a possibly-relative ``cd``/``-C`` target to an ABSOLUTE path.

    An absolute target is normalized and returned (it recovers even from a prior
    ``_CWD_UNKNOWN``). A relative target is joined onto ``current``; if
    ``current`` is unknown/None the result is ``_CWD_UNKNOWN`` (fail closed) —
    a relative path with no known base cannot be resolved, and running
    ``git -C <relative>`` from the hook's own cwd would silently target the wrong
    tree (P1-A).
    """
    if os.path.isabs(target):
        return os.path.normpath(target)
    if not isinstance(current, str) or not current:
        return _CWD_UNKNOWN
    return os.path.normpath(os.path.join(current, target))


def _effective_cwd(cmd: str, payload: dict, seg=None):
    """The ABSOLUTE directory where a SINGLE target segment actually runs.

    Returns ``str`` (resolved), ``None`` (no cwd info → run in the hook's own
    cwd), or ``_CWD_UNKNOWN`` (ambiguous → caller must fail closed). Resolution:
      1. If the target is nested (depth>0, inside bash -c/subshell/$()), UNKNOWN.
      2. The LAST top-level ``cd`` that runs BEFORE the target segment — bash
         applies cds sequentially, so the last one wins; relative cds/-C are
         resolved against the running cwd, and an unresolvable one ⇒ UNKNOWN.
      3. ``git -C <dir>`` on the target segment's argv overrides, resolved
         against the cwd in effect at the target.
    Used for single-target callers (push); the merge check uses the multi-target
    walk in ``_walk_merge_into_main`` so every merge in a compound is covered.
    """
    if seg is not None and getattr(seg, "depth", 0) > 0:
        return _CWD_UNKNOWN
    base = payload.get("cwd") if isinstance(payload, dict) else None
    cur = os.path.normpath(base) if isinstance(base, str) and base else None
    target_raw = getattr(seg, "raw", None)
    if target_raw is not None:
        for raw in split_segments(cmd):
            if raw == target_raw:
                break
            cd = _cd_target(raw)
            if cd is _CWD_UNKNOWN:
                cur = _CWD_UNKNOWN
            elif cd is not None:
                cur = _resolve_against(cur, cd)
    if seg is not None:
        dash_c = _seg_dash_C(getattr(seg, "argv", None))
        if dash_c is not None:
            return _resolve_against(cur, dash_c)
    return cur


def _walk_merge_into_main(cmd: str, payload: dict, merge_git_segs: list) -> bool:
    """True if ANY executed ``git merge`` would run on main/master (fail-closed).

    Walks the top-level segments in bash order tracking the ABSOLUTE cwd (last
    ``cd`` wins; relative cds/-C resolved against it), and checks EACH ``git
    merge`` as it would actually run — so a compound like ``git -C <feat-wt>
    merge a && git merge b`` cannot smuggle the second (bare) merge into main
    behind the first. A per-segment ``# merge-to-main-override`` acknowledges that
    segment. Fail closed: a merge nested at depth>0, reached under an unresolvable
    cwd, OR whose branch cannot be read (None) is treated as targeting main and
    blocked (unless overridden). A detached HEAD ("") is left allowed.
    """
    # depth>0 merges cannot be associated with a top-level cwd → fail closed.
    for s in merge_git_segs:
        if getattr(s, "depth", 0) > 0 and not has_trailing_override(
            s.raw, "merge-to-main-override"
        ):
            return True

    base = payload.get("cwd") if isinstance(payload, dict) else None
    cur = os.path.normpath(base) if isinstance(base, str) and base else None
    for raw in split_segments(cmd):
        top = [s for s in analyze(raw) if getattr(s, "depth", 0) == 0]
        merge_here = next(
            (s for s in top if s.exe == "git" and git_subcommand(s.argv) == "merge"),
            None,
        )
        if merge_here is not None and not has_trailing_override(raw, "merge-to-main-override"):
            dash_c = _seg_dash_C(merge_here.argv)
            mcwd = _resolve_against(cur, dash_c) if dash_c is not None else cur
            if mcwd is _CWD_UNKNOWN:
                return True
            branch = _current_branch(cwd=mcwd if isinstance(mcwd, str) else None)
            if branch is None or branch in ("main", "master"):
                return True  # None branch (error/unresolved) fails closed
        cd = _cd_target(raw)
        if cd is _CWD_UNKNOWN:
            cur = _CWD_UNKNOWN
        elif cd is not None:
            cur = _resolve_against(cur, cd)
    return False


def _get_push_remote_and_branch(seg, cwd: str | None = None) -> tuple[str | None, str | None]:
    """The (remote, destination-branch) a ``git push`` segment targets.

    Parsed from the quote-stripped ``seg.argv`` via ``_push_positionals`` — NOT a
    naive ``cmd.split()``, which mis-skips value flags (``-o <val>``) and wrongly
    treats no-value flags like ``-u`` / ``--set-upstream`` as consuming the next
    token (collapsing the branch to the wrong value — a security bug once the
    branch feeds an approval decision). Positional handling mirrors
    ``_push_named_remote``:
      • no positional   → bare ``git push``               → (``"upstream"``, current branch);
      • one positional  → ``git push <remote>``           → (remote, current branch);
      • two positionals → ``git push <remote> <refspec>`` → (remote, refspec DST).
    Used for the approval-dialog LABEL; the safety decision uses the stricter
    ``_push_targets_current_branch``. Returns (None, None) if not a push.
    """
    argv = getattr(seg, "argv", None) or []
    if git_subcommand(argv) != "push":
        return None, None
    pos = _push_positionals(argv)
    if not pos:
        # Bare 'git push' — pushes current branch to its upstream
        return "upstream", _current_branch(cwd=cwd)
    if len(pos) == 1:
        # 'git push origin' — pushes current branch to remote
        return pos[0], _current_branch(cwd=cwd)
    # 'git push origin main' or 'git push origin feature:main'
    remote, refspec = pos[0], pos[1]
    branch = refspec.split(":")[-1] if ":" in refspec else refspec
    return remote, branch


# A gh --repo/-R (or PR URL) is PRESENT but cannot be resolved to a plain
# github.com OWNER/REPO (a shell variable, an enterprise HOST/OWNER/REPO, a URL
# with extra path). Callers MUST fail closed on this — gating the cwd repo while
# gh merges elsewhere is exactly the wrong-repo bug _merge_target_repo prevents.
_REPO_UNRESOLVED = object()


def _normalize_repo(value: str) -> str | None:
    """A plain github.com OWNER/REPO from a gh --repo value, or None if it cannot
    be resolved to one.

    Accepts ``owner/repo``, ``github.com/owner/repo``, and a github.com URL.
    Returns None for a shell variable (``$X`` / backtick), an enterprise
    ``HOST/OWNER/REPO`` (would misnormalize to github.com and gate the wrong
    repo), or any other shape — the caller turns None-when-present into a
    fail-closed block. Genesis is github.com-only, so refusing the exotic forms
    is safe (a user can re-issue as OWNER/REPO)."""
    if not value or "$" in value or "`" in value:
        return None
    v = value.strip().split("://", 1)[-1]  # strip scheme if a URL
    parts = [p for p in v.split("/") if p]
    # Drop a leading github.com HOST COMPONENT (exact match of the first path
    # segment — NOT a substring test, which would mishandle `github.com.evil/…`
    # or `evilgithub.com/…`; CodeQL py/incomplete-url-substring-sanitization).
    if parts and parts[0] == "github.com":
        parts = parts[1:]
    if len(parts) != 2:  # not a plain OWNER/REPO (host-prefixed / malformed)
        return None
    return f"{parts[0]}/{parts[1]}"


def _merge_target_repo(argv: list[str], cmd: str):
    """The repo a `gh pr merge` explicitly targets. Returns one of:

    * ``str`` — a normalized github.com OWNER/REPO to gate against;
    * ``None`` — NO explicit ``--repo``/``-R``/URL → gate gh's cwd repo (default);
    * ``_REPO_UNRESOLVED`` — an explicit target we cannot resolve → caller fails
      CLOSED. This is the fix for the residual the first cut missed: a variable
      ``--repo "$X"`` normalized to None and was indistinguishable from "no
      --repo", so the gates silently ran against the cwd repo while gh merged
      ``$X`` — re-opening the 2026-07-26 wrong-repo class through the fix itself.

    Sources: ``--repo <v>`` / ``--repo=<v>`` / ``-R <v>`` / ``-R<v>`` anywhere in
    the segment argv (gh pflag accepts any position), else a full PR URL in cmd.
    """
    argv = argv or []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--repo", "-R") and i + 1 < len(argv):
            return _normalize_repo(argv[i + 1]) or _REPO_UNRESOLVED
        if tok.startswith("--repo="):
            return _normalize_repo(tok.split("=", 1)[1]) or _REPO_UNRESOLVED
        if tok.startswith("-R") and len(tok) > 2 and not tok.startswith("--"):
            return _normalize_repo(tok[2:]) or _REPO_UNRESOLVED  # glued -Rowner/repo
        i += 1
    url = re.search(r"(?:https?://)?([^/\s]+)/([^/\s]+)/([^/\s]+)/pull/\d+", cmd)
    if url:
        host, owner, repo = url.group(1), url.group(2), url.group(3)
        if "." in host and host != "github.com":
            return _REPO_UNRESOLVED  # enterprise host — can't gate via github.com
        return _normalize_repo(f"{owner}/{repo}") or _REPO_UNRESOLVED
    return None


# Merge-path SHARED-DEADLINE budget. `.claude/settings.json` kills this PreToolUse
# hook at ~60s. The gh-pr-merge gates run sequentially, so per-call timeouts alone
# cannot bound the AGGREGATE: on a degraded API where each call succeeds just under
# its own cap, the sum exceeds 60s and Claude SIGKILLs the hook MID-GATE — which
# fails toward "tool runs" and silently disengages the WHOLE gate stack (incl. the
# TOCTOU binding), the exact bypass the timeouts exist to prevent (Codex P1 #1373).
# main() computes ONE deadline before the gates and threads it through every
# merge-path gh helper via `_gh_timeout`, so the total finishes with headroom under
# 60s and each fail-closed gate reaches its own block/allow decision first.
_MERGE_GATE_BUDGET_S = 45.0
# Shared merge-path deadline (a monotonic() instant). main() sets it once before the
# gh-pr-merge gates; every merge-path gh call reads it via _gh_timeout so the AGGREGATE
# finishes with headroom under the hook's ~60s wall-clock. A module global is safe:
# this PreToolUse hook is a SINGLE-SHOT process (one command, no concurrency), and it
# stays None on every other path (push, the --check-pr report) → those use full caps.
_merge_deadline: float | None = None


def _gh_timeout(cap: float) -> float:
    """Per-call subprocess timeout under the shared merge-path deadline (``_merge_deadline``).

    ``cap`` when no merge deadline is set (every non-merge caller, and the tests, are
    unaffected). Under a deadline, the smaller of ``cap`` and the time remaining, floored
    at 1s so a nearly-expired budget makes the call fail FAST — its caller's existing
    error path then returns its fail-closed/open value — rather than overrun the
    wall-clock and get the whole hook SIGKILLed mid-gate. Never raises."""
    if _merge_deadline is None:
        return cap
    return max(1.0, min(cap, _merge_deadline - time.monotonic()))


def _derive_repo_from_cwd(cwd: str) -> str | None:
    """The OWNER/REPO gh resolves in directory ``cwd`` (``nameWithOwner``), or None.

    ``gh pr merge`` and ``gh repo view`` share gh's base-repo resolution (git
    remotes in the working dir + the ``gh-resolved`` config key), so this is the
    repo a bare (no ``--repo``) merge run FROM ``cwd`` will actually target.

    Root cause this addresses: the hook's gh queries run in the HOOK process's
    cwd, while gh executes the merge in the Bash tool's effective cwd. When those
    differ (a ``cd`` before the merge, or the session sitting in another checkout)
    a bare merge gated the wrong repo. Deriving the repo from the merge's own
    effective cwd re-aligns them. Tests inject via ``_TEST_GH_DERIVED_REPO``.
    """
    raw = os.environ.get("_TEST_GH_DERIVED_REPO")
    if raw is None:
        try:
            result = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(
                    6
                ),  # merge-path budget (see main): pre-gate resolution, fail-closed
                cwd=cwd,
            )
            raw = result.stdout if result.returncode == 0 else ""
        except Exception:
            return None
    got = (raw or "").strip()
    return _normalize_repo(got) if got else None


def _extract_pr_number(cmd: str) -> str | None:
    """PR number from a gh pr merge command (bare number, #N, or URL)."""
    match = re.search(r"\bgh pr merge\b(.*)$", cmd, re.DOTALL)
    if not match:
        return None
    # A newline ends the command too, but shlex collapses it to plain
    # whitespace (so a `\n echo 456` chain would leak its digits) — cut
    # the tail at the first newline before tokenizing.
    tail = match.group(1).split("\n", 1)[0]
    # Pre-space shell separators into standalone tokens (shlex keeps them
    # attached, e.g. '123;'). Quotes still protect a separator inside an
    # arg value — shlex parses the quoted region as one token afterwards.
    spaced = re.sub(r"(\|\||&&|[|;&])", r" \1 ", tail)
    try:
        # shlex keeps quoted args whole so digits inside a --subject
        # string are never mistaken for the PR number.
        tokens = shlex.split(spaced)
    except ValueError:
        tokens = spaced.split()
    seps = {";", "&", "|", "&&", "||"}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Stop at the end of THIS command — tokens after a separator
        # belong to a chained command, and their digits must not be read
        # as this merge's target (`gh pr merge 123; echo 456` merges 123
        # but this loop would otherwise return 456). 2026-07-10 review.
        if tok in seps:
            break
        # A value-taking flag consumes the NEXT token as its value, so an
        # UNQUOTED numeric value is never misread as the PR: gh parses
        # `gh pr merge --subject 123 5` as subject="123" + PR 5, and the old
        # loop returned 123 (the flag value) → the gates checked the WRONG PR
        # (E3, 2026-08-19). Covers long/short-single value flags
        # (--subject/-t/--body/--match-head-commit/--repo…) and short clusters
        # whose trailing value-letter has no glued remainder (`-db 123` → -b
        # eats 123). Mirrors _merge_match_head / _comment_target, which already
        # skip value flags — this was the one gh-arg parser here that didn't.
        # NEVER swallow a separator as a value (a dangling `--subject ; gh pr
        # merge 999` must not leak the chained command's 999): only consume the
        # next token when it isn't a separator; the break above then ends it.
        if tok in _GH_MERGE_VALUE_FLAGS or _short_cluster_consumes_next(tok):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            i += 2 if (nxt is not None and nxt not in seps) else 1
            continue
        # The PR is a POSITIONAL — a bare number / #N / /pull/N URL, which never
        # starts with '-'. Guarding the matchers on that closes the GLUED
        # value-flag sibling of E3 (adversarial review 2026-08-19): `--body=…`
        # and `-b…` are single '-'-prefixed tokens that DON'T consume a next
        # token, so they fall through here — and the URL matcher's `\S*` prefix
        # would otherwise read a `/pull/N` smuggled inside the flag's VALUE as
        # the PR while gh merges the trailing positional. (`--` end-of-options
        # also starts with '-' → skipped; the following bare positional still
        # resolves, matching gh.)
        if not tok.startswith("-"):
            if tok.isdigit():
                return tok
            if tok.startswith("#") and tok[1:].isdigit():
                return tok[1:]
            url = re.match(r"\S*/pull/(\d+)\b", tok)
            if url:
                return url.group(1)
        i += 1
    return None


def _repo_args(repo: str | None) -> list[str]:
    """gh CLI args selecting an explicit target repo (empty = cwd repo)."""
    return ["--repo", repo] if repo else []


def _resolve_pr_number(cmd: str, repo: str | None = None, cwd: str | None = None) -> str | None:
    """Command PR number, else the current branch's open PR.

    No-arg `gh pr merge` from a PR branch is valid gh usage, but it
    used to skip EVERY merge gate here (the gates only ran under
    `if pr_num:`) — the 2026-07-10 audit points at this as a mechanism
    behind findings-ignored merges. Resolution failure is the caller's
    signal to fail CLOSED for merge commands.

    Numberless resolution of the branch's PR:
      * ``cwd`` given (the merge's repo was DERIVED from that cwd, F3): run
        ``gh pr view`` IN that dir with NO ``--repo`` — gh resolves the repo and
        the branch's PR together, exactly as the bare merge will, so the number
        matches the derived repo (both come from the same dir). Passing ``--repo``
        here would instead error ("argument required when using the --repo flag").
      * explicit user ``repo`` and NO ``cwd`` → fail CLOSED (None). Resolving a
        cwd-branch PR *number* and gating it against a DIFFERENT user-named repo
        would re-create the wrong-PR bug this gate kills.
      * neither → gh's own cwd (the hook process's), the legacy behavior.
    """
    pr_num = _extract_pr_number(cmd)
    if pr_num:
        return pr_num
    if repo is not None and cwd is None:
        return None  # explicit --repo + numberless → fail CLOSED (see above)
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
            capture_output=True,
            text=True,
            timeout=_gh_timeout(
                6
            ),  # merge-path budget (see main): pre-gate resolution, fail-closed
            cwd=cwd,  # None ⇒ the hook's own cwd (legacy path)
        )
        resolved = result.stdout.strip()
        if result.returncode == 0 and resolved.isdigit():
            return resolved
    except Exception:
        pass
    return None


def _check_mergeable(pr_num: str, repo: str | None = None) -> str | None:
    """Query GitHub for PR mergeable status. Returns MERGEABLE/UNKNOWN/CONFLICTING,
    or None on a failed query. Callers fail CLOSED: the merge gate blocks unless
    the value is a definite MERGEABLE (None/"" — a failed read — no longer merges)."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                pr_num,
                *_repo_args(repo),
                "--json",
                "mergeable",
                "--jq",
                ".mergeable",
            ],
            capture_output=True,
            text=True,
            timeout=_gh_timeout(
                8
            ),  # merge-path budget (see main): fail-closed → a timeout BLOCKS w/ retry
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None  # unreadable → caller treats as non-MERGEABLE → blocks


# Check-run conclusions / statuses that mean the CI is NOT green. STALE is a real
# GitHub conclusion (a superseded/outdated run — NOT a pass) and is red; any OTHER
# unrecognized *terminal* conclusion also fails closed to red in the classify loop,
# so no unenumerated conclusion value can be silently mistaken for green.
_CI_RED_CONCLUSIONS = {
    "FAILURE",
    "CANCELLED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "STALE",
}
_CI_RED_STATES = {"FAILURE", "ERROR"}  # legacy StatusContext state
_CI_SKIP_CONCLUSIONS = {"SKIPPED", "NEUTRAL"}
_CI_GREEN = {"SUCCESS"}
# Legacy StatusContext states meaning "not finished" → block as pending. EXPECTED =
# a required context that has not reported a status yet (not started); it must not
# read green. (A CheckRun uses `status` for this; a StatusContext uses `state`.)
_CI_PENDING_STATES = {"PENDING", "EXPECTED"}
# A CANCELLED check-run carries NO pass/fail verdict — the run was aborted,
# almost always by a `concurrency: cancel-in-progress` supersession, which leaves
# the cancelled dup attached to the head commit. It is red BY DEFAULT (it is also
# in _CI_RED_CONCLUSIONS), and dropped ONLY when a check of the SAME identity
# (name + workflowName, see _ci_identity) concluded SUCCESS at-or-after it on this
# head (so a SUCCESS-then-cancel re-run on an unchanged head still blocks).
# Deliberately scoped to CANCELLED alone: FAILURE/TIMED_OUT/ACTION_REQUIRED/
# STARTUP_FAILURE carry real verdicts and always block, even with a success sibling.
_CI_CANCEL_CONCLUSIONS = {"CANCELLED"}
# The only CheckRun.status that means "finished". Everything else
# (QUEUED/IN_PROGRESS/PENDING/WAITING/REQUESTED/…) is treated as unfinished, so
# a new/renamed non-terminal state can never be silently mistaken for green.
_CI_TERMINAL_STATUSES = {"COMPLETED"}

# Display label for a rollup entry that has neither a CheckRun `name` nor a
# StatusContext `context` — used only for the human-facing problem-check list.
_CI_NAMELESS = "check"


def _check_name(c: dict) -> str:
    """Human-facing display label for one statusCheckRollup entry: a CheckRun
    ``name`` or a legacy StatusContext ``context``, falling back to _CI_NAMELESS.
    Used only to build the problem-check list — NOT the sibling-match key (that is
    _ci_identity, which is stricter)."""
    return c.get("name") or c.get("context") or _CI_NAMELESS


def _ci_identity(c: dict) -> tuple[str, str] | None:
    """Strict same-check identity for the concurrency-cancel sibling match:
    ``(name, workflowName)`` for a GitHub Actions CheckRun, or ``None`` when the
    entry cannot be identity-matched — a legacy StatusContext (no workflowName) or
    a CheckRun from a non-Actions app (empty workflowName). ``None`` means the
    entry is NEVER a sibling and NEVER droppable → it fails CLOSED (a cancel with
    no resolvable identity stays red).

    Keying on name ALONE would be unsafe: this gate forces `--admin`, which
    bypasses GitHub's server-side required-status-checks, so _pr_ci_status is the
    SOLE CI enforcement for every merge it allows. A bare-name match would let a
    same-named SUCCESS from a DIFFERENT workflow (an accidental collision, or a
    decoy job) mask a genuinely-cancelled required check → wrong-green. Requiring
    workflowName to match scopes the drop to a true same-job re-run — the only
    thing `cancel-in-progress` produces. Still pure set-membership: no
    time-ordering (that surface was the pulled #1420 finding-magnet)."""
    name = (c.get("name") or "").strip()
    wf = (c.get("workflowName") or "").strip()
    if name and wf:
        return (name, wf)
    return None


def _pr_ci_status(pr_num: str, repo: str | None = None) -> tuple[str, list[str]]:
    """Classify a PR's CI check-runs.

    Returns ``(state, problem_checks)`` where state is one of:
      * ``"green"``   — every non-skipped check concluded SUCCESS
      * ``"red"``     — at least one check failed/timed-out, or was cancelled
                        with NO same-identity SUCCESS completing at-or-after it.
                        A CANCELLED CheckRun that a same (name, workflowName)
                        SUCCESS completed at-or-after is a superseded
                        `concurrency: cancel-in-progress` duplicate and is dropped
                        (see _ci_identity + success_latest) — strict identity,
                        terminal completedAt comparison only, fail-closed.
      * ``"pending"`` — a check is still queued/running (and none are red)
      * ``"absent"``  — a READABLE but genuinely EMPTY rollup (``[]``): zero checks
                        exist, i.e. CI has NOT run. A DEFINITE fact, not a read
                        failure — so the merge arm fail-CLOSES on it ON THE CANONICAL
                        REPO (where CI always runs), waivable by ``# ci-override``.
                        Off the canonical repo (which may legitimately have no CI) the
                        caller lets it pass. This is the state that catches a
                        conflicting branch / dropped ``pull_request`` trigger, which
                        would otherwise merge un-CI'd.
      * ``"incomplete"`` — the rollup is NON-empty and nothing is red/pending, but a
                        REQUIRED workflow (rollup ``workflowName``; config-driven via
                        ``merge_gate.required_ci_workflows``, default ``CI`` — see
                        _required_ci_workflows) never contributed a verdict. Closes the
                        #1484-P2 partial-rollup residual: e.g. a lone green CodeQL with
                        the CI suite absent (a workflow-specific trigger drop) must not
                        read green. ``problem_checks`` carries the MISSING workflow
                        names. Enforced like ``"absent"``: canonical repo only,
                        waivable by ``# ci-override``.
      * ``"unknown"`` — could NOT determine: an API error, empty/no output, an
                        unparseable payload, or a non-empty payload with no CI-shaped
                        entries. Callers FAIL OPEN, because blocking a merge on our own
                        inability to read CI would be worse than the gap this closes.
                        (Contrast with ``"absent"``: unreadable ≠ definitely-zero.)

    Tests inject via the ``_TEST_GH_CI_ROLLUP`` env var (a JSON array like
    ``gh pr view --json statusCheckRollup``) so no network is needed.
    """
    raw = os.environ.get("_TEST_GH_CI_ROLLUP")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    pr_num,
                    *_repo_args(repo),
                    "--json",
                    "statusCheckRollup",
                    "--jq",
                    ".statusCheckRollup",
                ],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(8),  # merge-path budget (see main): fail-open → "unknown"
            )
            if result.returncode != 0:
                return "unknown", []
            raw = result.stdout.strip()
        except Exception:
            return "unknown", []
    if not raw:
        # No output to read (empty stdout / unset seam): we cannot tell zero-checks
        # from a silent read failure → fail-OPEN "unknown", never "absent".
        return "unknown", []
    try:
        checks = json.loads(raw)
    except Exception:
        return "unknown", []
    if not isinstance(checks, list):
        return "unknown", []
    if not checks:
        # A READABLE, genuinely-empty rollup: zero checks exist = CI has NOT run.
        # Distinct from "unknown" (a read we could not complete) — this is a definite
        # fact, so the canonical-repo merge arm fail-CLOSES on it (see main()): merging
        # an empty-check PR is an un-CI'd merge. gh emits `[]` here when pull_request CI
        # never fired (e.g. a conflicting branch suppresses the whole suite).
        return "absent", []

    # First pass: for each strict identity (name, workflowName), the latest
    # `completedAt` among its SUCCESS CheckRuns on this head. A CANCELLED entry is
    # a superseded concurrency-cancel duplicate ONLY if a SUCCESS of the same
    # identity completed AT OR AFTER it (see the drop branch). Only GitHub Actions
    # CheckRuns with a resolvable identity AND a completedAt contribute; legacy
    # StatusContexts, non-Actions checks, and timestampless successes never serve
    # as siblings. This is NOT the #1420 finding-magnet: that sorted the WHOLE set
    # (incl. QUEUED runs with null startedAt) to pick a global "latest"; here both
    # sides of the comparison are terminal COMPLETED runs that always carry a
    # completedAt, and every unresolvable case fails CLOSED (stays red).
    success_latest: dict[tuple[str, str], str] = {}
    for c in checks:
        if not isinstance(c, dict) or c.get("conclusion") not in _CI_GREEN:
            continue
        ident = _ci_identity(c)
        ts = (c.get("completedAt") or "").strip()
        if ident is None or not ts:
            continue
        if ts > success_latest.get(ident, ""):
            success_latest[ident] = ts

    red: list[str] = []
    pending: list[str] = []
    saw_recognized = False
    # Workflows that contributed a VERDICT on this head (casefolded ``workflowName``),
    # for the required-identity check below. SKIPPED/NEUTRAL entries deliberately do
    # NOT contribute — a fully-skipped required suite tested nothing. Legacy
    # StatusContexts (no workflowName) contribute "" and can never satisfy a named
    # required workflow (fail-closed on the Actions-only canonical repo).
    workflows_ran: set[str] = set()
    for c in checks:
        if not isinstance(c, dict):
            continue
        name = _check_name(c)
        conclusion = c.get("conclusion")  # CheckRun
        status = c.get("status")  # CheckRun: QUEUED/IN_PROGRESS/COMPLETED/PENDING/…
        state = c.get("state")  # StatusContext: SUCCESS/FAILURE/PENDING/ERROR
        wf_key = (c.get("workflowName") or "").strip().casefold()
        if conclusion in _CI_SKIP_CONCLUSIONS:
            saw_recognized = True
            continue
        if conclusion in _CI_CANCEL_CONCLUSIONS:
            ident = _ci_identity(c)
            cts = (c.get("completedAt") or "").strip()
            if ident is not None and cts and success_latest.get(ident, "") >= cts:
                # Superseded concurrency-cancel duplicate: a SUCCESS of this EXACT
                # identity (name + workflowName) completed AT OR AFTER this cancel,
                # so the cancelled entry is a `cancel-in-progress` leftover with no
                # verdict of its own — drop it. Wrong-green-impossible:
                # FAILURE/TIMED_OUT are not in _CI_CANCEL_CONCLUSIONS (still red);
                # an in-flight re-run is a non-terminal entry that still counts
                # pending below; and a cancel with no identity, no completedAt, or
                # NO same-identity success at-or-after it (e.g. SUCCESS-then-cancel
                # on an unchanged head) falls through and stays red.
                saw_recognized = True
                # A superseded duplicate implies a same-identity SUCCESS exists on
                # this head, so the workflow demonstrably ran.
                workflows_ran.add(wf_key)
                continue
        if conclusion in _CI_RED_CONCLUSIONS or state in _CI_RED_STATES:
            saw_recognized = True
            red.append(name)
        elif conclusion in _CI_GREEN or state in _CI_GREEN:
            # The ONLY branch (besides the superseded-cancel drop above, which implies
            # a green sibling) that feeds workflows_ran: the required-identity check
            # runs only when nothing is red/pending (those return first, and already
            # block), so only PASSING verdicts can vouch that a required workflow ran.
            # A COMPLETED run with a null conclusion (the benign ignore below) carries
            # no verdict and deliberately does NOT vouch — fail-closed. Vouching is
            # additionally gated on a CheckRun-shaped pass (``conclusion``): a
            # StatusContext-shaped green (``state``) can't carry a real Actions
            # workflowName, so an entry gluing state=SUCCESS to a workflowName key
            # must not satisfy the required identity by construction (not merely by
            # gh's current output shape).
            saw_recognized = True
            if conclusion in _CI_GREEN:
                workflows_ran.add(wf_key)
        elif status in _CI_TERMINAL_STATUSES:
            saw_recognized = True
            if conclusion is not None:
                # A COMPLETED run with a conclusion we DON'T recognize (a value
                # GitHub adds later) is NOT a pass — fail CLOSED to red, never
                # silently ignore. This mirrors the not-terminal ⇒ pending
                # inversion below so no unenumerated *terminal* conclusion can read
                # green. (conclusion is None ⇒ genuinely no verdict data: keep the
                # benign ignore — distinct from a named value we simply don't map.)
                red.append(name)
        elif status is not None or state in _CI_PENDING_STATES:
            # ANY non-terminal CheckRun status (QUEUED/IN_PROGRESS/PENDING/
            # WAITING/REQUESTED/…) or an unfinished StatusContext state
            # (PENDING/EXPECTED) is unfinished → block. Enumerating "known pending"
            # states would silently miss new ones (P1 review finding), so we
            # invert: not-terminal ⇒ pending.
            saw_recognized = True
            pending.append(name)
        # else: no status/state/conclusion at all — unrecognized shape, ignore

    if red:
        return "red", sorted(set(red))
    if pending:
        return "pending", sorted(set(pending))
    if not saw_recognized:
        return "unknown", []  # payload had no CI-shaped entries
    # Required-workflow identity (closes the #1484-P2 partial-rollup residual): every
    # present check passed, but "green" must ALSO assert that each REQUIRED workflow
    # (default "CI"; config lever merge_gate.required_ci_workflows for an install whose
    # suite is named differently) actually contributed a passing verdict. Otherwise a
    # workflow-specific trigger drop — e.g. a lone green CodeQL, the CI suite absent —
    # reads green and merges an untested PR. (The "absent" branch above only catches a
    # FULLY-empty rollup.) Canonical-scoping and the # ci-override valve are applied by
    # the CALLERS, exactly as for "absent", so a non-canonical repo is never blocked
    # by this identity policy.
    required = _required_ci_workflows()
    missing = sorted(w for w in required if w.strip().casefold() not in workflows_ran)
    if missing:
        return "incomplete", missing
    return "green", []


# The CI-status gate is waived only by a genuine ``# ci-override`` trailing
# comment on the merge segment itself — detected with the same quote-aware,
# segment-bound parser as ``# review-override`` (shell_parse.has_trailing_override)
# so it cannot be spoofed from inside a quoted --body or a different chained
# command. Distinct from --admin and # review-override; waives ONLY this gate.


# ── Review findings detection ──────────────────────────────────────────

# Patterns that indicate blocking review findings.
# Matches structural review ERRORs, gstack [P1] markers, and PII hard blocks.
_BLOCKING_PATTERNS = [
    re.compile(r"^#{2,3}\s*(?:🔴\s*)?ERROR\b", re.MULTILINE),
    re.compile(r"\[P1\](?!\d)"),
    re.compile(r"HARD\s+BLOCK", re.IGNORECASE),
]

# Patterns that indicate the review was clean (no real findings).
# If a comment matches both blocking AND clean, clean wins — it means
# the reviewer mentioned the category but found nothing.
_CLEAN_PATTERNS = [
    re.compile(r"(?:PII|Secrets|Wording)\s*(?:scan)?:\s*\**CLEAN\**", re.IGNORECASE),
    re.compile(r"Pre-Landing Review:\s*No issues found", re.IGNORECASE),
    re.compile(r"^Pre-Landing Review:\s*No issues found", re.IGNORECASE | re.MULTILINE),
    re.compile(r"VERDICT:\s*PASS", re.IGNORECASE),
]

# Bot usernames that post automated reviews
_REVIEW_BOTS = {"chatgpt-codex-connector[bot]", "github-actions[bot]"}

# ── Inline review comments (pulls/N/comments — a DIFFERENT endpoint) ──
# Codex posts its actual P1/P2 findings ONLY as inline review comments;
# its review body is boilerplate. This endpoint was never scanned, so
# the gate was blind to them (audited 2026-07-10: 173 findings across
# 118 merged PRs passed unseen, 64 of them P1).
_INLINE_P1_RE = re.compile(r"!\[P1 Badge\]")
_INLINE_P2_RE = re.compile(r"!\[P2 Badge\]")
_INLINE_REVIEW_BOTS = {
    "chatgpt-codex-connector[bot]",
    "github-advanced-security[bot]",
}
# A reply "engages" (silences) an inline P1 finding ONLY when authored by someone with
# repository authority — otherwise any GitHub account (a throwaway, or the PR author on
# their own PR) could post a one-word reply and clear a real P1 (PR #1434 security
# review, LOW-a). GitHub's author_association on a PR review comment; these three denote
# push/triage authority. A reply from NONE / FIRST_TIME_CONTRIBUTOR / CONTRIBUTOR does
# NOT count as acknowledgement.
_MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
# Badge/markup prefix stripped when rendering a finding's title line.
_INLINE_MARKUP_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|</?sub>|[*]{1,2}")

# ── Documentation-path allowlist for review findings (ledger 54eb3752) ───────
# A P1 inline finding on a DOCUMENTATION file is not a code defect and must not
# block a merge (a CHANGELOG typo, a README wording nit). This is a FAIL-CLOSED
# ALLOWLIST — a path is exempted only when it is provably prose; everything else
# blocks. Prose is: (1) a known doc-named file (CHANGELOG/README/LICENSE/NOTICE/…)
# with a doc/text/empty extension, at any depth; (2) any UNAMBIGUOUS documentation
# extension (.md/.rst/.markdown/.adoc) UNDER a top-level ``docs/``; or (3) any
# ``*.rst`` anywhere. A random top-level ``NOTES.md``, ANY source/config/executable
# file even under ``docs/`` (``docs/conf.py``, ``docs/build.rs``, ``docs/config.yaml``,
# ``docs/Makefile``), a missing/empty path, or a path bearing a control character is
# NON-doc and STILL BLOCKS — the gate never opens for a code path merely mislabeled.
# ``.txt`` is deliberately NOT a blanket doc extension: it is ambiguous (a build/dep
# manifest — ``docs/requirements.txt``, ``docs/CMakeLists.txt`` — carries it too, and
# this repo classifies such files as config), so ``.txt`` is prose ONLY on a known
# doc-named stem (``LICENSE.txt``, ``README.txt``). Only the inline endpoint carries
# a per-file path; the review-BODY gate is PR-level and stays unfiltered. (A denylist
# of code extensions was rejected in review: it can't enumerate every source/config
# type — an allowlist fails closed on the unknown.)
_DOC_EXTS = {"md", "markdown", "rst", "adoc"}
# Extensions permitted on a KNOWN doc-named stem only (rule 1). ``.txt``/empty are
# safe here because the STEM already pins the file as prose (LICENSE, README).
_DOC_STEM_EXTS = _DOC_EXTS | {"txt", ""}
_DOC_STEMS = {
    "changelog",
    "readme",
    "license",
    "notice",
    "copying",
    "authors",
    "contributing",
}


def _is_doc_path(path: str) -> bool:
    """Whether a review finding's file *path* is documentation whose findings do
    NOT block a merge. FAIL-CLOSED allowlist (see the section comment): anything
    not provably prose — a source/config/manifest file under ``docs/`` (incl. a
    ``.txt`` build manifest), a random top-level ``*.md``, a missing path, or a
    control-char-bearing path — blocks."""
    # Reject the COMPLETE control-character range (Unicode Cc): C0 (<0x20), DEL
    # (0x7F), and C1 (0x80-0x9F, incl. NEL 0x85 which ``gh --jq`` emits literally).
    if not path or any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in path):
        return False  # empty or control-char-bearing → block (fail-closed)
    base = path.rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    ext = ext.lower() if dot else ""
    stem_l = (stem if dot else base).lower()
    # (1) A known doc-named file with a doc/text/empty extension, at any depth.
    if stem_l in _DOC_STEMS and ext in _DOC_STEM_EXTS:
        return True
    # (2) A pure documentation format (reStructuredText) anywhere.
    if ext == "rst":
        return True
    # (3) An unambiguous documentation extension under a top-level docs/ directory.
    return ext in _DOC_EXTS and path.startswith("docs/")


# ── Direct-sqlite-write detection ────────────────────────────────────────────
# Robust bare-keyword match (the historical approach). A SINGLE statement keyword
# is inherently immune to shell quoting/escapes AND SQL comments BETWEEN tokens,
# because it never depends on two tokens being adjacent in the raw command. The
# ONLY refinement over the historical pattern is a negative lookahead excluding a
# keyword immediately followed by `(` — i.e. the `replace(...)` scalar FUNCTION
# (or any keyword-as-function) — so a read-only SELECT using replace() no longer
# false-positives. This is strictly narrower than the old pattern for reads and
# IDENTICAL for writes (no real write statement is `KEYWORD(`), so it cannot open
# a bypass. Detection stays on the WHOLE command so a heredoc/`bash -c`/wrapper
# cannot fragment and hide a write.
#
# Two earlier approaches were REJECTED for weakening the guard, and this returns
# to the robust original: (a) exe-scoping + a read-only exemption exempted
# read-only tokens appearing in write SQL *data* and missed heredocs/wrappers;
# (b) statement-position two-token matching was bypassable by a SQL comment or a
# shell escape/quote inserted between the two tokens (`DELETE /*c*/ FROM`,
# `DELETE\ FROM`, `DELETE' 'FROM`). A single-keyword match has neither failure.
#
# Accepted limitation (NOT a regression — the historical guard did the same): a
# command that merely MENTIONS a bare keyword alongside "sqlite3" without a write
# (a `grep 'sqlite3 … DELETE' file`, or a read whose STRING VALUE is a keyword
# like `WHERE s='DELETE'`) still matches. Excluding those needs real SQL/shell
# parsing — tracked as a follow-up, not worth reintroducing bypass risk for.
_DML_KEYWORD_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|REPLACE)\b(?!\s*\()",
    re.IGNORECASE,
)


def _is_sqlite_write(cmd: str) -> bool:
    """Whether *cmd* issues a direct sqlite3 DML/DDL write.

    Broad on purpose (any mention of ``sqlite3`` in the whole command, matching
    the historical behavior) so a heredoc/`bash -c`/wrapper cannot fragment and
    hide the write. A DML keyword must be present in non-function position (the
    negative lookahead drops the ``replace()`` scalar function).
    """
    return "sqlite3" in cmd and bool(_DML_KEYWORD_RE.search(cmd))


def _inline_title(body: str) -> str:
    """First readable line of an inline finding body."""
    # split("\n") not splitlines(): a NEL (U+0085) inside the body must not shift
    # which line is shown as the title (consistent with the JSONL parser).
    first = _INLINE_MARKUP_RE.sub("", body).strip().split("\n")
    return (first[0].strip() if first else "")[:120]


def _scan_unreadable(what: str) -> tuple[bool, str]:
    """Return value for a finding scan that could NOT be read — a gh error,
    timeout, or malformed JSON — as distinct from a scan that ran and found
    nothing (an empty result / Codex quota, which stays clean either way).

    ALWAYS fails CLOSED (blocks): an unreadable or incomplete scan is treated as
    "not clean, retry", NEVER as a silent pass. This closes the CRITICAL fail-open
    (PR #1434 security review): a comment-flood, a budget-starved scan, or GitHub
    secondary rate-limiting on a burst of sequential gh calls could terminate the
    scan early (page cap / drained merge deadline / API error). The old fail-OPEN
    merge path then returned "clean" and the merge sailed through with an UNSEEN
    newest finding. The freshness gate (``_check_codex_reviewed_head``) reads a
    DIFFERENT surface (the Reviews API) and does not catch it. Fail-closed makes the
    completeness of the read irrelevant to safety — an unreadable scan blocks, and
    ``# review-override`` remains the conscious escape hatch for a transient gh error.
    """
    return True, f"could not read {what} — review status UNREADABLE (retry), not clean."


# Page size for the comment-fetch loop. MUST be the pagination signal's basis: a
# page returning fewer than this many rows is the last page. It must exceed any
# test's fixed comment count so a mocked `return_value` (same rows every call)
# terminates after one page instead of looping forever.
_COMMENTS_PER_PAGE = 100
# Backstop against an unbounded loop (a real PR never approaches this; a mocked
# return_value of exactly _COMMENTS_PER_PAGE rows would otherwise never terminate).
_MAX_COMMENT_PAGES = 100


def _fetch_comments_paged(
    endpoint: str,
    pr_num: str,
    repo: str | None,
    jq: str,
) -> tuple[list[dict] | None, bool]:
    """Fetch ALL pages of a PR comments endpoint as parsed JSON objects.

    Pages through ``gh api repos/<repo>/<endpoint> -X GET -f per_page=100 -f page=N
    --jq <jq>`` (query params via ``-f``; the endpoint token keeps its ``…/comments``
    suffix so the char-router's ``endswith("/comments")`` still matches). ``gh --jq``
    emits one compact JSON object per line — JSONL — across the page. Pages are fetched
    in the endpoint's default ASCENDING (oldest-first) order — both callers accumulate
    ALL pages, so order does not affect the verdict, and ascending means a comment
    appended during the scan lands on the last page (reached) rather than shifting a
    never-revisited first page under descending paging.

    Returns ``(objects, complete)``:
      - ``(None, False)``  — the FIRST page could not be read (gh error/exception):
        the caller has NO data → treat as UNREADABLE.
      - ``(accumulated, False)`` — a LATER page failed: the caller keeps what it saw
        (so a blocking finding on an earlier page still stands) but knows the read is
        INCOMPLETE and cannot confirm the ABSENCE of a newer finding.
      - ``(accumulated, True)`` — every page read (a page with < per_page rows, or an
        empty page, is the last).

    NEL-safe: splits on ``"\\n"`` only — NOT ``str.splitlines()``, which also breaks on
    U+0085 (NEL) that ``gh --jq`` emits literally inside a JSON string. One NEL-bearing
    comment would otherwise fragment the JSONL, fail ``json.loads``, and be SILENTLY
    dropped — a real finding missing from an otherwise-complete read, which fail-closed
    cannot catch (the read looks complete). Non-dict lines are dropped (Codex P2 — a
    bare string/number line never masquerades as a comment).

    Each page uses ``_gh_timeout(8)`` AND the loop checks the shared merge deadline
    BETWEEN pages, so it self-bounds under the wall-clock in BOTH failure modes: a
    slow/hung gh call fails fast at the floored per-call timeout, AND a flood of pages
    that each SUCCEED fast cannot keep spawning calls past the budget. Either way the
    partial read is returned as incomplete → the caller fails CLOSED.
    """
    acc: list[dict] = []
    page = 1
    while page <= _MAX_COMMENT_PAGES:
        # Respect the shared merge deadline BETWEEN pages, not only via each call's
        # timeout (HIGH, PR #1434 security review). A comment-flood — thousands of
        # pre-seeded comments, and reads are NOT write-rate-limited — makes every page
        # SUCCEED fast; the per-call timeout alone never stops the loop, so it would keep
        # spawning gh calls past the 45s budget and blow the hook's ~60s SIGKILL ceiling.
        # A hook killed mid-gate "fails toward tool runs" (see main's budget note) — an
        # EXTERNAL fail-open the in-function fail-closed logic can't see. Out of budget →
        # return what we have as INCOMPLETE (page 1 → None) → the caller blocks.
        if _merge_deadline is not None and time.monotonic() >= _merge_deadline:
            return (None if page == 1 else acc), False
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo or ':owner/:repo'}/{endpoint}",
                    "-X",
                    "GET",
                    "-f",
                    f"per_page={_COMMENTS_PER_PAGE}",
                    "-f",
                    f"page={page}",
                    "--jq",
                    jq,
                ],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(8),  # merge-path budget (see main): self-bounding
            )
        except Exception:
            return (None if page == 1 else acc), False  # page-1 fail = no data
        if result.returncode != 0:
            return (None if page == 1 else acc), False
        # NEL-safe split; count RAW non-empty lines for the pagination signal (a
        # dropped malformed line must not prematurely signal "last page").
        lines = [ln for ln in result.stdout.split("\n") if ln.strip()]
        parsed_ok = 0
        for ln in lines:
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            parsed_ok += 1
            if isinstance(obj, dict):
                acc.append(obj)
        # A NON-EMPTY page whose every line failed to parse as JSON is a MALFORMED
        # response (not a genuine "zero comments" page) — treat as unreadable rather than
        # letting a garbage body masquerade as a clean short page that ends the scan (LOW,
        # PR #1434 defense-in-depth: preserves the pre-diff whole-body JSONDecodeError guard
        # for the per-line model). returncode==0 with an unparseable body is unusual (gh
        # --jq normally exits non-zero), so this is belt-and-suspenders.
        if lines and parsed_ok == 0:
            return (None if page == 1 else acc), False
        if len(lines) < _COMMENTS_PER_PAGE:
            return acc, True  # short/empty page → last page
        page += 1
    # Hit the page backstop — treat as incomplete (cannot confirm we saw everything).
    return acc, False


def _check_inline_review_findings(
    pr_num: str,
    *,
    force: bool = False,
    repo: str | None = None,
) -> tuple[bool, str]:
    """Scan INLINE review comments for P1/P2 badge findings.

    Returns (should_block, message). P1 findings block unless their thread has a
    MAINTAINER reply (engagement = a reviewer read it) or the merge carries
    '# review-override'. P2 findings never block but are printed to stderr one per
    line — the session must consciously accept them. An UNREADABLE or INCOMPLETE scan
    (gh error/timeout/malformed/clipped budget) ALWAYS blocks (fail-closed — see
    ``_scan_unreadable``); '# review-override' is the conscious escape hatch.
    """
    if force:
        return False, ""  # override NOTE already printed by the body gate
    # Paginate via the shared helper (findings beyond the first REST page must still
    # gate); it accumulates ALL pages as parsed dicts, NEL-safe. ``raw is None`` = the
    # first page was unreadable; ``complete`` False = a later page failed. Fetch in the
    # endpoint's default ASCENDING (oldest-first) order: a P1 appended DURING the scan
    # lands on the last page, which sequential ascending pagination reaches — descending
    # (newest-first) page-number paging would never revisit page 1 to see it on a >100-
    # comment PR. ``assoc`` = author_association, for the maintainer-reply engagement check.
    raw, complete = _fetch_comments_paged(
        f"pulls/{pr_num}/comments",
        pr_num,
        repo,
        ".[] | {id: .id, reply_to: .in_reply_to_id, login: .user.login, "
        "type: .user.type, assoc: .author_association, path: .path, body: .body}",
    )
    if raw is None:
        return _scan_unreadable("inline review comments")

    # Build replied_to over ALL accumulated pages BEFORE classifying, so a P1 on an
    # early page acknowledged by a reply on a later page is treated as engaged (never
    # per-page — that would false-block a genuinely-acked finding). Count ONLY replies
    # from a MAINTAINER (author_association in _MAINTAINER_ASSOCIATIONS): a non-authority
    # reply must not silence a real P1 (LOW-a).
    replied_to = {
        c.get("reply_to")
        for c in raw
        if c.get("reply_to") and c.get("assoc") in _MAINTAINER_ASSOCIATIONS
    }
    p1: list[str] = []
    p2: list[str] = []
    doc_skipped: list[str] = []  # P1s on doc paths — surfaced, never blocking
    for c in raw:
        login, utype = c.get("login") or "", c.get("type") or ""
        body = c.get("body") or ""
        if utype != "Bot" and login not in _INLINE_REVIEW_BOTS:
            continue
        if c.get("reply_to"):
            continue  # replies aren't findings
        if _INLINE_P1_RE.search(body):
            if c.get("id") in replied_to:
                continue  # thread engaged — treated as acknowledged
            if _is_doc_path(c.get("path") or ""):
                # A P1 on a documentation file (ledger 54eb3752) is surfaced but
                # does NOT block; a code file (incl. code under docs/) still does.
                doc_skipped.append(_inline_title(body))
                continue
            p1.append(_inline_title(body))
        elif _INLINE_P2_RE.search(body):
            p2.append(_inline_title(body))

    if doc_skipped:
        print(
            f"NOTE: PR #{pr_num} — {len(doc_skipped)} inline [P1] finding(s) on "
            f"documentation paths (CHANGELOG/README/LICENSE/NOTICE/docs/**/*.rst) "
            f"NOT blocking:",
            file=sys.stderr,
        )
        for title in doc_skipped[:5]:
            print(f"  [doc P1] {title}", file=sys.stderr)
    if p2:
        print(
            f"WARNING: PR #{pr_num} has {len(p2)} inline [P2] review "
            f"finding(s) (not blocking — address or consciously accept):",
            file=sys.stderr,
        )
        for title in p2[:8]:
            print(f"  [P2] {title}", file=sys.stderr)
    if p1:
        listing = "\n".join(f"  [P1] {t}" for t in p1[:5])
        return True, (
            f"{len(p1)} inline [P1] finding(s) with no reply:\n{listing}\n"
            f"Fix and reply in-thread, or append '# review-override' "
            f"to the merge command to acknowledge and proceed."
        )
    # No unresolved P1 among what we read. If the read is INCOMPLETE (a later page
    # failed), a P1 could exist on an unread page — fail per _scan_unreadable rather
    # than report a clean scan.
    if not complete:
        return _scan_unreadable("inline review comments (incomplete read)")
    return False, ""


def _check_pr_review_findings(
    pr_num: str, *, force: bool = False, repo: str | None = None
) -> tuple[bool, str]:
    """Check PR comments for unresolved automated review findings.

    Returns (should_block, message).

    An UNREADABLE or INCOMPLETE scan (gh error/timeout/malformed JSON, or a clipped
    budget) ALWAYS blocks (fail-closed — see ``_scan_unreadable``): the hook must never
    silently pass a merge past a scan it could not complete, since the newest finding
    may sit on the page it failed to read. An empty result that was read COMPLETELY (no
    comments / Codex quota) is clean; the freshness gate separately requires a CURRENT
    review to EXIST. '# review-override' is the conscious escape hatch for a transient
    gh error.
    """
    if force:
        print(
            f"NOTE: Review gate override for PR #{pr_num}. Findings acknowledged by session.",
            file=sys.stderr,
        )
        return False, ""

    # Paginate via the shared helper (a review-body finding beyond the first REST
    # page must still gate). ``comments is None`` = the first page was unreadable;
    # ``complete`` False = a later page failed.
    comments, complete = _fetch_comments_paged(
        f"issues/{pr_num}/comments",
        pr_num,
        repo,
        # Only login + body are read below (the verdict-bearing walk keys on the
        # recognized-bot login, not user.type — unlike the inline scanner which still
        # projects type for its Bot-vs-User check).
        ".[] | {login: .user.login, body: .body}",
    )
    if comments is None:
        return _scan_unreadable("review-body comments")  # first page unreadable

    # Walk comments in reverse (most recent first). Only a VERDICT-BEARING comment
    # from a recognized review bot sets the state: one matching a BLOCKING marker
    # (→ block) or a CLEAN marker (→ clean). Any other comment — a CI/status bot
    # notice (e.g. github-actions), bot chit-chat, or an unrecognized author — is
    # SKIPPED, never treated as the "newest clean review".
    #
    # This closes the fail-open (Codex P1) where the old terminal clause
    # ``if is_clean or not blocking_matches: return clean`` let ANY marker-less
    # comment from a Bot-typed account (Dependabot, or github-actions — which is IN
    # _REVIEW_BOTS) end the walk clean, silently clearing an earlier ERROR. Requiring
    # a recognized verdict marker closes BOTH the unrecognized-bot and the
    # recognized-but-non-verdict (status-comment) masking paths.
    for c in reversed(comments):
        login = c.get("login") or ""
        body = c.get("body") or ""  # GitHub returns null body for deleted comments
        # Only recognized review bots set the verdict.
        if login not in _REVIEW_BOTS:
            continue
        # Codex quota-exhausted messages are not a review.
        if "reached your Codex usage limits" in body and not any(
            p.search(body) for p in _BLOCKING_PATTERNS
        ):
            continue

        is_clean = any(p.search(body) for p in _CLEAN_PATTERNS)
        blocking_matches = [p.pattern for p in _BLOCKING_PATTERNS if p.search(body)]

        if blocking_matches and not is_clean:
            # A seen finding stands even on an incomplete read (fail-closed on an
            # observed blocking result — Codex P1: a later-page failure must not
            # erase an already-observed finding).
            return True, (
                f"Automated review has unresolved findings.\n"
                f"Matched patterns: {', '.join(blocking_matches[:3])}\n"
                f"Fix the findings, or append '# review-override' to "
                f"the merge command to acknowledge and proceed."
            )
        if is_clean:
            # An explicit clean verdict ends the walk — but trust it only on a
            # COMPLETE read. On an incomplete read the pages we have are the
            # EARLIEST, so a newer finding could sit on an unread page.
            if complete:
                return False, ""
            return _scan_unreadable("review-body comments (incomplete read)")
        # Neither blocking nor clean → not a verdict comment → keep walking.

    # No verdict-bearing finding among the comments we read. On an INCOMPLETE read a
    # newer finding could exist on an unread page — fail per _scan_unreadable rather
    # than report clean. An empty/complete result is clean: the freshness gate
    # (_check_codex_reviewed_head) separately requires a CURRENT review to EXIST.
    if not complete:
        return _scan_unreadable("review-body comments (incomplete read)")
    return False, ""


# ── Codex review FRESHNESS (a CURRENT review must exist, not just no findings) ──
# The finding scanners above block on UNRESOLVED findings, but a merge can still
# proceed with NO review at all, or a review of a STALE commit (Codex reviewed A,
# code B pushed after) — the scans come back empty and the merge sails through
# unseen. This gate requires Codex's most recent review to cover the PR's current
# head, compared on the FULL 40-char oid GitHub records for the review
# (the review object's ``commit_id``) — NOT a short prefix from the body, which a
# stale prefix (or a ground SHA sharing it) could satisfy. Waived by
# '# review-override' (the conscious "merge without a current Codex review" case).
_CODEX_REVIEW_BOT = "chatgpt-codex-connector[bot]"


def _pr_head_sha(pr_num: str, repo: str | None = None) -> str | None:
    """The PR's current head commit oid (headRefOid), or None on any error.

    Tests inject via the ``_TEST_GH_HEAD_SHA`` env var so no network is needed.
    """
    raw = os.environ.get("_TEST_GH_HEAD_SHA")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    pr_num,
                    *_repo_args(repo),
                    "--json",
                    "headRefOid",
                    "--jq",
                    ".headRefOid",
                ],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(
                    6
                ),  # merge-path budget (see main): fail-closed → unreadable head BLOCKS
            )
            raw = result.stdout if result.returncode == 0 else ""
        except Exception:
            return None
    sha = (raw or "").strip()
    return sha or None


def _pr_base_sha(pr_num: str, repo: str | None = None) -> str | None:
    """The LIVE tip oid of the PR's base branch, or None on any error.

    Deliberately NOT ``pulls/N → .base.sha``: that field is a SNAPSHOT taken at
    PR creation/retarget and does not advance with the base branch (measured
    2026-08-23 against live PRs — three pre-merge PRs still reported the old
    tip while a post-merge control reported the new one). The evidence binding
    exists precisely for the base-advanced case, so it must read the branch
    ref's current tip.

    Resolution is via GraphQL ``repository.ref(qualifiedName:"refs/heads/<branch>")``
    with the branch passed as a raw-string VARIABLE — NOT the REST ``commits/{ref}``
    endpoint (Codex #10). Interpolating a branch name into a URL path is ambiguous
    and fragile in two ways the REST path could not fully close: (1) an UNqualified
    ``commits/<name>`` resolves a same-named TAG or a branch literally named
    ``heads/x`` to the WRONG ref (a wrong-tip evidence binding that stays valid
    after the real base moves); (2) a name that is structural in a URL (``#``
    fragment, ``?`` query, ``%`` escape) truncates the request. A fully-qualified
    ``refs/heads/<branch>`` passed as a GraphQL variable is unambiguous (heads vs
    tags) AND carries no URL-path interpolation at all, closing both classes at once.
    A branch ref's ``target`` is always a Commit, so ``.target.oid`` is its live tip.

    Consumed by the hook-surface override evidence identity (see
    _hook_surface_override_check). Tests inject via ``_TEST_GH_BASE_OID``.
    """
    raw = os.environ.get("_TEST_GH_BASE_OID")
    if raw is None:
        ref = _pr_base_ref(pr_num, repo=repo)
        # Reject only an ASCII control char or a plain ASCII space — Git's own ref
        # rules (git check-ref-format) forbid exactly those, and they'd be garbage in
        # a base branch name. Do NOT reject every Python str.isspace() code point
        # (Codex P2, round 5): non-ASCII whitespace (e.g. U+00A0) is a LEGAL Git branch
        # character, and rejecting it would falsely block the authorized
        # fallback-evidence path for a legitimately-named branch.
        if not ref or any(ord(c) < 0x20 or ord(c) == 0x7F or c == " " for c in ref):
            return None
        # GraphQL needs an explicit owner/name (no REST ``:owner/:repo`` placeholder).
        # Resolve the slug from the passed repo, else the cwd's base repo; fail-closed
        # if it can't be resolved or split. A non-str ``repo`` (an unresolved-repo
        # sentinel) fails CLOSED here — never fall through to a cwd guess, and never
        # reach _normalize_repo's ``str``-typed body with a non-str (would TypeError
        # OUTSIDE the subprocess try). Belt-and-suspenders: callers already fail-closed
        # on the sentinel before this runs.
        if repo is not None and not isinstance(repo, str):
            return None
        slug = _normalize_repo(repo) if repo else _derive_repo_from_cwd(os.getcwd())
        if not slug or "/" not in slug:
            return None
        owner, name = slug.split("/", 1)
        query = (
            "query($owner:String!,$name:String!,$ref:String!){"
            "repository(owner:$owner,name:$name){ref(qualifiedName:$ref){target{oid}}}}"
        )
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    # -f (raw string), NOT -F: -F type-coerces and reads @file, which
                    # would mangle a numeric-looking or @-leading branch name. -f keeps
                    # every value a literal GraphQL String.
                    "-f",
                    f"query={query}",
                    "-f",
                    f"owner={owner}",
                    "-f",
                    f"name={name}",
                    "-f",
                    f"ref=refs/heads/{ref}",
                    "--jq",
                    ".data.repository.ref.target.oid",
                ],
                capture_output=True,
                text=True,
                # Merge-path budget (see main()): runs ONLY on the rare
                # stale-review-override path; fail direction is closed there.
                timeout=_gh_timeout(6),
            )
            # A missing branch → ``ref: null`` → jq emits the literal "null"; a GraphQL
            # error → data null → same. Both, and any non-zero exit, fail closed below.
            raw = result.stdout if result.returncode == 0 else ""
        except Exception:
            return None
    sha = (raw or "").strip()
    if not sha or sha == "null":
        return None
    return sha


def _codex_reviews(pr_num: str, repo: str | None = None) -> list[dict] | None:
    """EVERY Codex review record ``{commit_id, state}`` on the PR, oldest-first,
    or None on any API/parse error (distinct from ``[]`` = query succeeded, no
    Codex review). INCLUDES ``DISMISSED`` reviews — consumers filter per their
    need: freshness (``_codex_review_commit_ids``) skips dismissed (a dismissed
    review vouches for NO commit); the escalation counter COUNTS them (a
    dismissed round already RAN and consumed the review budget — #1385 round-5:
    3 dismissed rounds must still trip the 3-round cap). Uses GitHub's
    authoritative per-review ``commit_id`` (immune to prefix grinding); the
    ``/pulls/N/reviews`` endpoint returns reviews oldest-first. Tests inject via
    ``_TEST_GH_CODEX_REVIEWS`` (one JSON object per line: ``{login,
    commit_id[, state]}``; missing ``state`` = active). Fail-safe: None on error.
    """
    raw = os.environ.get("_TEST_GH_CODEX_REVIEWS")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo or ':owner/:repo'}/pulls/{pr_num}/reviews",
                    "--paginate",
                    "--jq",
                    ".[] | {login: .user.login, commit_id: .commit_id, state: .state}",
                ],
                capture_output=True,
                text=True,
                # See the merge-path timeout budget note in main(): every gh call on
                # this path must finish (or fail open/closed on its own) well inside
                # the hook's 60s wall-clock, or a SIGKILL disengages ALL gates at once.
                timeout=_gh_timeout(8),
            )
            if result.returncode != 0:
                return None
            raw = result.stdout
        except Exception:
            return None
    reviews: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if (obj.get("login") or "") != _CODEX_REVIEW_BOT:
            continue
        reviews.append(
            {
                "commit_id": (obj.get("commit_id") or "").strip().lower(),
                "state": (obj.get("state") or "").upper(),
            }
        )
    return reviews


def _codex_review_commit_ids(pr_num: str, repo: str | None = None) -> list[str] | None:
    """NON-DISMISSED Codex review commit oids, oldest-first, or None on error.

    Freshness identity — a dismissed review vouches for no commit, so the #1366
    reviewed-head gate must not treat its sha as reviewed. For ROUND COUNTING
    (dismissed rounds still ran) the escalation gate uses ``_codex_reviews``
    directly. Consumer: ``_latest_codex_reviewed_sha`` (last entry).
    """
    reviews = _codex_reviews(pr_num, repo=repo)
    if reviews is None:
        return None
    return [r["commit_id"] for r in reviews if r["state"] != "DISMISSED" and r["commit_id"]]


def _latest_codex_reviewed_sha(pr_num: str, repo: str | None = None) -> str | None:
    """The FULL commit oid of Codex's MOST RECENT review, or None if Codex has
    posted no review (or on any API/parse error). See
    ``_codex_review_commit_ids`` for the query/identity model."""
    ids = _codex_review_commit_ids(pr_num, repo=repo)
    return ids[-1] if ids else None


def _comment_target(argv: list[str]) -> tuple[str | None, str | None]:
    """(pr_number, repo) from a ``gh pr comment`` segment's argv.

    Number from a bare number, #N, or PR URL; a URL target ALSO carries its
    OWNER/REPO (counting against the hook cwd's repo for a cross-repo URL could
    produce a wrong count and a FALSE block — Codex round-1 finding). An
    explicit ``--repo``/``-R`` flag wins over the URL-derived repo. A branch
    target (non-numeric, non-URL positional) yields (None, …) → fail-open.
    """
    pr_num: str | None = None
    url_repo: str | None = None
    try:
        idx = argv.index("comment")
    except ValueError:
        return None, None
    # Value-taking flags whose SEPARATED value must not be read as the positional
    # target — a PR URL inside a `--body` text would otherwise redirect the gate
    # to an unrelated repo (#1385 round-5). Glued forms (`--body=…`, `-b…`,
    # `-R…`, `--repo=…`) are single `-`-prefixed tokens already skipped below.
    _VALUE_FLAGS = {"-b", "--body", "-F", "--body-file", "-R", "--repo"}
    skip_next = False
    for tok in argv[idx + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if tok in _VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        if pr_num is None and tok.isdigit():
            pr_num = tok
        elif pr_num is None and tok.startswith("#") and tok[1:].isdigit():
            pr_num = tok[1:]
        elif pr_num is None:
            url = re.match(r"(?:[a-z]+://[^/\s]+/)?([^/\s]+/[^/\s]+)/pull/(\d+)\b", tok)
            if url:
                url_repo, pr_num = url.group(1), url.group(2)
    return pr_num, _comment_repo(argv) or url_repo


def _comment_repo(argv: list[str]) -> str | None:
    """Explicit ``--repo``/``-R`` value on a gh pr comment segment, if any.

    Handles separated (``--repo o/r`` / ``-R o/r``), ``--repo=o/r``, and glued
    ``-Ro/r`` forms. A host-qualified ``HOST/OWNER/REPO`` is reduced to its
    OWNER/REPO tail (the REST path shape this gate queries).
    """
    val: str | None = None
    for i, tok in enumerate(argv):
        if tok in ("--repo", "-R") and i + 1 < len(argv):
            val = argv[i + 1]
        elif tok.startswith("--repo="):
            val = tok.split("=", 1)[1]
        elif tok.startswith("-R") and len(tok) > 2 and not tok.startswith("-R="):
            val = tok[2:]
        elif tok.startswith("-R=") and len(tok) > 3:
            val = tok[3:]
    if val and val.count("/") >= 2:
        val = "/".join(val.split("/")[-2:])
    return val


def _escalation_advisory(pr_num: str, rounds: int, repo: str | None = None) -> str:
    repo_arg = f" --repo {repo}" if repo else ""
    return (
        f"BLOCKED: this would be Codex round {rounds + 1} on PR #{pr_num} — "
        f"{rounds} rounds already ran (cap {ESCALATION_ROUND_CAP}). Repeated "
        "rounds each finding NEW defects is the whack-a-mole signature: the "
        "fixes themselves are becoming the bug source. STEP BACK before "
        "requesting another round:\n"
        "  1. TRIAGE every open finding FIRST — classify each as {live bug | "
        "latent trap | hardening | observation}. Only live bugs and "
        "cheaper-now-than-later traps may change already-reviewed code; "
        "everything else gets a documented acceptance or routes to the PR that "
        "owns that area. Findings are inputs to judgment, not a to-do list.\n"
        "  2. Fix MECHANISMS, not instances — ask 'what made this bug "
        "possible?' and remove that; patching the named instance leaves the "
        "class alive for the next round to find.\n"
        "  3. State-machine/queue/lifecycle code: enumerate EVERY status value "
        "and trace your change under each one. Your tests encode your own "
        "model of the states — they cannot catch the states you didn't "
        "consider.\n"
        "  4. Consider REVERTING a prior round's fix instead of patching it "
        "again — less code is often the real fix.\n"
        "  5. ESCALATE to the user with a minimize-change recommendation — "
        "past the cap, standing approval is consumed; each extra round needs "
        "a fresh, conscious decision.\n"
        "After doing the above (triage table produced, user consulted), "
        "re-run with a trailing shell comment (outside any quotes):\n"
        f'  gh pr comment {pr_num}{repo_arg} --body "@codex review"  # escalation-ack'
    )


def _check_codex_round_escalation(segs) -> tuple[bool, str]:
    """Block a ``gh pr comment … @codex review`` once the PR already carries
    ``ESCALATION_ROUND_CAP`` Codex reviews, until a trailing ``# escalation-ack``.

    Companion to the commit gate's Rule 3 (review_enforcement_commit.py): that
    counter tracks LOCAL review→fix rounds and stays asleep when every local
    review is clean while the loop churns through CODEX rounds on the PR — the
    exact blind spot of the 2026-08-12 MW-3 #1372 whack-a-mole (5 Codex rounds,
    local counter at 0). This gate counts the PR's actual Codex reviews from the
    GitHub API (stateless, authoritative) at the one moment the groove happens:
    requesting the next round.

    FAIL-OPEN state table (advisory logic must never break workflow — the
    opposite posture from the fail-closed merge gates, on purpose):
      segment isn't `gh pr comment`               → untouched
      comment without an '@codex review' body     → untouched (body-file/stdin
        bodies are unresolvable here — documented coverage limit, fail-open;
        blocking unresolvable bodies would false-block non-trigger comments)
      '# escalation-ack' trailing ANY segment     → allow the whole command (a
        nested `bash -c '…' # escalation-ack` carries the ack on the OUTER
        segment; the ack is a conscious human-directed act, so one ack licenses
        the command it trails)
      numberless/branch target (no PR number)     → that segment allows;
        SCANNING CONTINUES (an allowed segment must not shield a later one)
      gh/API/parse error (ids is None)            → that segment allows, scan on
      rounds < ESCALATION_ROUND_CAP               → that segment allows, scan on
      any segment at rounds >= cap, no ack        → BLOCK with the step-back
        order (URL targets carry their OWN repo into the count — counting the
        hook cwd's repo for a cross-repo URL could produce a FALSE block)
    Any unexpected exception → allow (caught here, NOT left to run_guard's
    fail-closed exit-2, which would turn an advisory bug into a hard block).
    """
    try:
        if any(has_trailing_override(s.raw, "escalation-ack") for s in segs):
            return False, ""
        # Bound this scan's gh (_codex_reviews) calls by the SHARED hook deadline,
        # so a slow API + a compound command can't push the aggregate past the
        # ~60s hook wall-clock and get the WHOLE hook SIGKILLed — which fails open
        # on every gate (round-6 P1). Idempotent: a later merge gate reuses this
        # same deadline (it arms only when None), never resets it.
        global _merge_deadline
        if _merge_deadline is None:
            _merge_deadline = time.monotonic() + _MERGE_GATE_BUDGET_S
        # Earlier trigger segments in THIS command count toward the total: each
        # segment sees the same pre-execution API count, so `request && request`
        # at cap-1 would otherwise dispatch round N+1 unacknowledged (round-2
        # finding). Keyed per (repo, pr) so distinct PRs don't cross-count.
        in_cmd: dict[str, int] = {}
        for seg in segs:
            if gh_pr_subcommand(seg.argv) != "comment":
                continue
            if not any("@codex review" in tok.lower() for tok in seg.argv):
                continue
            pr_num, repo = _comment_target(seg.argv)
            if not pr_num:
                continue
            # Count ALL Codex review rounds — including DISMISSED, which still
            # ran and consumed the budget (#1385 round-5). Freshness uses the
            # dismissed-filtered ``_codex_review_commit_ids``; the cap does not.
            reviews = _codex_reviews(pr_num, repo=repo)
            if reviews is None:
                continue
            key = f"{repo or ''}|{pr_num}"
            effective = len(reviews) + in_cmd.get(key, 0)
            if effective >= ESCALATION_ROUND_CAP:
                return True, _escalation_advisory(pr_num, effective, repo)
            in_cmd[key] = in_cmd.get(key, 0) + 1
    except Exception:
        return False, ""
    return False, ""


# A CLEAN Codex re-review is posted as an ISSUE COMMENT (not a review object): the body
# opens "Codex Review: Didn't find any major issues." (the flavour sentence after —
# "Swish!", "You're on a roll.", "Keep them coming!", … — VARIES, so anchor ONLY on the
# stable prefix) and carries a "**Reviewed commit:** `<sha>`" line with a 10-char
# ABBREVIATED sha. Both must be present for the comment to vouch for a commit.
_CODEX_CLEAN_COMMENT_RE = re.compile(
    r"Codex Review:\s*Didn'?t find any major issues", re.IGNORECASE
)
_CODEX_REVIEWED_COMMIT_RE = re.compile(
    r"Reviewed commit:\**\s*`?([0-9a-fA-F]{7,40})`?", re.IGNORECASE
)


def _latest_codex_clean_comment_sha(pr_num: str, repo: str | None = None) -> str | None:
    """The ABBREVIATED commit sha from Codex's most recent CLEAN issue-comment, or None.

    Codex posts a clean RE-review as an ISSUE COMMENT, not a review object, so
    ``_latest_codex_reviewed_sha`` (which reads the reviews API) never sees it — a clean
    re-review would then false-block the merge (bit PR #1386 twice). This is the fallback
    the freshness gate consults when the review-object path would otherwise block: it
    reads ``issues/N/comments``, and for a comment authored by the Codex bot (login AND
    ``user.type == "Bot"``) requires BOTH the clean marker AND a parseable
    ``Reviewed commit: <sha>`` line — a marker alone never vouches (fail-closed to None).

    Returns a PREFIX (>=7 hex, lowercased). The caller confirms it against the
    AUTHORITATIVE head via ``head.startswith(...)``: there is NO prefix-grinding surface
    because the head is a fixed value read from GitHub and the comment author is verified
    as the Codex bot (a human cannot post as ``chatgpt-codex-connector[bot]``). The
    reviews-API path keeps its full-oid identity; only this comment fallback is a prefix,
    and only against a known head. Comments come oldest-first, so the last match (most
    recent clean comment) wins. Tests inject via ``_TEST_GH_CODEX_COMMENTS`` (one JSON
    object per line: ``{login, type, body}``). Fail-safe: None on any API/parse error.
    """
    raw = os.environ.get("_TEST_GH_CODEX_COMMENTS")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo or ':owner/:repo'}/issues/{pr_num}/comments",
                    "--paginate",
                    "--jq",
                    ".[] | {login: .user.login, type: .user.type, body: .body}",
                ],
                capture_output=True,
                text=True,
                # See the merge-path timeout budget note in main(): fail-safe → None.
                timeout=_gh_timeout(8),
            )
            if result.returncode != 0:
                return None
            raw = result.stdout
        except Exception:
            return None
    latest: str | None = None
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if (obj.get("login") or "") != _CODEX_REVIEW_BOT:
            continue
        # Require the GitHub-enforced Bot author type too — belt-and-suspenders against
        # a spoofed login string in an injected/malformed payload.
        if (obj.get("type") or "") != "Bot":
            continue
        body = obj.get("body") or ""
        if not _CODEX_CLEAN_COMMENT_RE.search(body):
            continue
        m = _CODEX_REVIEWED_COMMIT_RE.search(body)
        if not m:
            continue  # clean marker but no parseable sha → does not vouch (fail-closed)
        latest = m.group(1).strip().lower()
    return latest


# ── Hook-surface merge teeth (2026-08-23, user decision) ─────────────────────
# The ENFORCEMENT-HOOK surface is the code the merge/push/commit gates themselves
# run on: an unreviewed change here disarms every other gate, so it gets stricter
# review teeth than ordinary code. Two rules, both scoped to these paths:
#   1. A stale-review delta touching this surface is NEVER "review-trivial"
#      (_classify_post_review_delta): a "small single-file touch-up" to a guard
#      is exactly the change that must not skip re-review.
#   2. `# stale-review-override` alone cannot merge a hook-surface PR without a
#      current GitHub Codex review — it additionally requires recorded
#      fallback-review evidence keyed to the EXACT head sha
#      (_hook_surface_override_check). The GitHub Codex review is the required
#      evidence class (user decision, 2026-08-23); the fallback procedure
#      (user-authorized local codex / Claude Code adversarial review) is the
#      documented exception path, not a self-serve bypass.
# WHY this exists (the origin story — keep it, it is the anti-rationalization):
# on PR #1432 a hand-rolled findings query returned empty and was reported as
# "review-clean" while 13 real Codex findings (10 P1) sat on the PR; only this
# file's merge gate caught it. The lesson: the human-facing claim and the gate
# MUST run the same code path, and the gate's own code must never merge
# unreviewed. Tests: tests/test_hooks/test_git_push_guard_hook_surface.py.
# config/behavioral_rules/ rides as a prefix: behavioral_linter.py loads every
# YAML under it (decision config = enforcement surface, see the note in
# _HOOK_SURFACE_FILES).
_HOOK_SURFACE_PREFIXES = ("scripts/hooks/", ".claude/hooks/", "config/behavioral_rules/")
# EVERY hook wired in .claude/settings.json is fence surface — not only the
# blocking gates: any script auto-executing inside sessions is enforcement-
# adjacent (architect SHOULD-FIX 2026-08-23: the named-list-as-sample trap this
# PR itself documents — review_enforcement_commit.py et al. lived outside the
# original 4-file fence). Over-fencing costs only stricter review; a guardrail
# test (test_git_push_guard_hook_surface.py) parses settings.json and FAILS CI
# if a wired hook ever falls outside this fence, so the set is self-maintaining.
_HOOK_SURFACE_FILES = frozenset(
    {
        "scripts/bash_safety_hook.sh",  # the global Bash chokepoint
        "scripts/review_scope.py",  # substantiality classifier (feeds THIS gate)
        "scripts/review_state.py",  # escalation counter + review markers
        ".claude/settings.json",  # hook wiring (inline blob + matchers)
        # scripts/-root hooks wired via .claude/hooks/genesis-hook (which
        # resolves bare names as scripts/<name>); scripts/hooks/* wirings are
        # covered by the prefix above.
        "scripts/behavioral_linter.py",
        "scripts/check_stale_pending.py",
        "scripts/content_safety_hook.py",
        "scripts/contribution_offer_hook.py",
        "scripts/edit_failure_sensor.py",
        "scripts/file_context_hook.py",
        "scripts/file_modification_audit_hook.py",
        "scripts/genesis_precompact.py",
        "scripts/genesis_session_context.py",
        "scripts/genesis_session_end.py",
        "scripts/genesis_stop_hook.py",
        "scripts/genesis_urgent_alerts.py",
        "scripts/plan_bookmark_hook.py",
        "scripts/pretool_check.py",
        "scripts/proactive_memory_hook.py",
        "scripts/procedure_advisor.py",
        "scripts/review_enforcement_commit.py",
        "scripts/review_enforcement_prompt.py",
        "scripts/review_invalidate_on_commit.py",
        "scripts/surface_open_prs.py",
        "scripts/surface_pr_updates.py",
        # Hook-owned DECISION CONFIGURATION (Codex P2, round 1): these files
        # determine what the wired hooks enforce, and the ordinary
        # substantiality classifier treats YAML as docs/config (review-trivial)
        # — so a rewrite that removes blocking patterns could merge on a stale
        # review. Config that drives enforcement is enforcement surface.
        "config/protected_paths.yaml",  # pretool_check.py
        "config/repo_topology.yaml",  # repo_routing_guard.py
    }
)


def _is_hook_surface_path(path: str) -> bool:
    """True iff ``path`` (repo-relative, as GitHub reports it) is enforcement-hook
    surface. Prefix matches are real path segments (``scripts/hooks/x``), never
    substrings (``scripts/hooks_readme.md`` does not match)."""
    return path in _HOOK_SURFACE_FILES or any(path.startswith(p) for p in _HOOK_SURFACE_PREFIXES)


# A real fallback review (reviewer + findings + dispositions) is substantive; this
# floor rejects a rubber-stamp / stray/boilerplate file at the evidence path (Codex
# P2 round 2). Note the filename already binds repo+PR+base+head, so the content
# check's marginal value is specifically catching a stale body copied to a correctly
# named file. Anti-autopilot floor, NOT tamper-proof — the teeth are the human merge
# + cloud reviewer (same threat model as review_state's evidence check).
_MIN_OVERRIDE_EVIDENCE_CHARS = 200
# Bound the read on the merge-gate hot path (the floor is 200 chars; any real review
# fits easily) so an oversized file in the evidence dir can't be slurped into memory.
_MAX_OVERRIDE_EVIDENCE_READ = 65536


def _override_evidence_dir() -> str:
    """Directory holding fallback-review evidence files
    (``<repo>__<pr>__<base-tip-12>__<head-sha>.txt``).

    ``GENESIS_OVERRIDE_REVIEW_EVIDENCE_DIR`` overrides (config knob + test seam);
    default lives outside the repo so evidence survives worktree removal and is
    never committed."""
    return os.environ.get("GENESIS_OVERRIDE_REVIEW_EVIDENCE_DIR") or os.path.expanduser(
        "~/.genesis/override_review_evidence"
    )


def _pr_changed_files(pr_num: str, repo: str | None = None) -> list[str] | None:
    """Every filename the PR touches (including rename SOURCES via
    ``previous_filename`` — a guard renamed OUT of scripts/hooks/ is a hook
    change), or None on any API/parse error. GitHub caps ``pulls/N/files`` at
    3000 entries; at the cap a hook file may sit beyond it → None (the caller
    fails closed). Tests inject via ``_TEST_GH_PR_FILES`` (one JSON object per
    line: ``{filename, previous_filename}``; the literal ``__error__`` simulates
    an API error)."""
    raw = os.environ.get("_TEST_GH_PR_FILES")
    if raw == "__error__":
        return None
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo or ':owner/:repo'}/pulls/{pr_num}/files",
                    "--paginate",
                    "--jq",
                    ".[] | {filename: .filename, previous_filename: .previous_filename}",
                ],
                capture_output=True,
                text=True,
                # Merge-path timeout budget (see main()): this runs ONLY on the
                # rare `# stale-review-override` path, so one paginated read
                # stays inside the hook's wall-clock.
                timeout=_gh_timeout(8),
            )
            if result.returncode != 0:
                return None
            raw = result.stdout
        except Exception:
            return None
    files: list[str] = []
    rows = 0
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            return None  # malformed page → cannot vouch for the full file set
        if not isinstance(obj, dict):
            return None
        rows += 1
        # Strict record shape (Codex P2, round 1): a null/empty/non-string
        # filename means the record did NOT parse into a usable path — treating
        # it as parsed could hide a hook file behind a degenerate row. Require
        # a nonempty string filename; previous_filename may be None (no rename)
        # or a nonempty string. Anything else → None → caller fails closed.
        fname = obj.get("filename")
        if not isinstance(fname, str) or not fname:
            return None
        files.append(fname)
        prev = obj.get("previous_filename")
        if prev is not None:
            if not isinstance(prev, str) or not prev:
                return None
            files.append(prev)
    if rows >= 3000:
        # The cap applies to API ROWS, not the expanded path list (renames
        # contribute two paths per row — Codex P2, round 1): at the documented
        # 3000-entry endpoint cap a hook file may be hidden beyond it.
        return None
    return files


def _hook_surface_override_check(pr_num: str, repo: str | None = None) -> tuple[bool, str]:
    """Gate the ``# stale-review-override`` escape on hook-surface PRs.

    Returns ``(should_block, message)``. Fail direction is CLOSED throughout:
    an unreadable diff or head sha blocks — this path exists precisely because
    the normal review evidence is absent, so uncertainty must not widen the
    escape. Non-hook-surface PRs pass untouched (the sigil keeps its normal
    meaning there)."""
    files = _pr_changed_files(pr_num, repo=repo)
    if files is None:
        return (
            True,
            (
                f"could not read PR #{pr_num}'s changed files to scope the "
                f"stale-review-override (hook-surface PRs need fallback-review "
                f"evidence). Retry when GitHub answers — this read failing "
                f"closed is deliberate."
            ),
        )
    touched = sorted({f for f in files if _is_hook_surface_path(f)})
    if not touched:
        return False, ""
    head = _pr_head_sha(pr_num, repo=repo)
    if not head:
        return (
            True,
            (
                f"PR #{pr_num} touches the enforcement-hook surface "
                f"({', '.join(touched[:4])}) but its head sha could not be read, "
                f"so fallback-review evidence cannot be verified. Retry."
            ),
        )
    head = head.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        # A network-sourced string becomes a path component below — validate the
        # exact oid shape (mirrors _SCHEDULED_REVIEW_HEAD_RE) so garbage blocks
        # EXPLICITLY instead of via an accidental unmatchable filename.
        return (
            True,
            (
                f"PR #{pr_num}'s head sha read back malformed ({head[:24]!r}) — "
                f"cannot key fallback-review evidence. Retry."
            ),
        )
    # Evidence identity = repo + PR + BASE + head (Codex P2s, rounds 1+2): a
    # commit sha alone does not identify the PR/base whose FULL diff the
    # fallback review covered — the same head can appear on another PR or in a
    # fork, and (round 2) a retarget or advancing default branch changes the
    # effective diff while head stays put; the sigil this path serves also
    # waives _check_base_is_default, so base must be bound HERE. The bound value
    # is the base branch's LIVE tip (never pulls/N.base.sha — a creation-time
    # snapshot; see _pr_base_sha): binding to the live tip over-expires (any
    # base move → re-record) — the safe direction on this rare, user-authorized
    # path. Fail-closed on an unreadable base.
    base = _pr_base_sha(pr_num, repo=repo)
    if not base or not re.fullmatch(r"[0-9a-f]{40}", base.strip().lower()):
        return (
            True,
            (
                f"PR #{pr_num}'s BASE sha could not be read (or was malformed) — "
                f"fallback-review evidence is base-bound and cannot be verified. "
                f"Retry."
            ),
        )
    base = base.strip().lower()
    repo_slug = (_normalize_repo(repo) or "local").replace("/", "_")
    evidence_path = os.path.join(
        _override_evidence_dir(), f"{repo_slug}__{pr_num}__{base[:12]}__{head}.txt"
    )
    try:
        with open(evidence_path, encoding="utf-8", errors="replace") as _ef:
            evidence_text = _ef.read(_MAX_OVERRIDE_EVIDENCE_READ)
    except OSError:
        evidence_text = ""
    # Validate the CONTENT, not just presence (Codex P2, round 2): a stray or
    # rubber-stamp file at the right path must not waive the gate. Require a
    # substantive review that NAMES the exact head it vouches for — the 12-hex
    # head prefix must appear in the body (the procedure below instructs this),
    # and the body must clear a minimum length. Fail-closed on a short/unbound file.
    has_evidence = (
        len(evidence_text.strip()) >= _MIN_OVERRIDE_EVIDENCE_CHARS
        and head[:12] in evidence_text.lower()
    )
    if has_evidence:
        # Residual TOCTOU, accepted as part of the force path's documented
        # "conscious unbound merge" contract: a push landing between this head
        # read and the merge would merge a head the evidence does not name. The
        # window is seconds, re-running re-reads the head, and binding here
        # would force --match-head-commit onto override merges — declined.
        print(
            f"NOTE: hook-surface override on PR #{pr_num} backed by fallback-review "
            f"evidence at {evidence_path} (head {head[:12]}).",
            file=sys.stderr,
        )
        return False, ""
    return (
        True,
        (
            f"'# stale-review-override' is NOT sufficient by itself here: PR "
            f"#{pr_num}'s diff touches the ENFORCEMENT-HOOK surface "
            f"({', '.join(touched[:4])}{', …' if len(touched) > 4 else ''}) — the "
            f"code the merge/push gates themselves run on. Merging it without a "
            f"current GitHub Codex review additionally requires recorded "
            f"fallback-review evidence for the EXACT head {head[:12]}.\n"
            f"Procedure (requires the user's explicit authorization — never "
            f"self-serve):\n"
            f"  1. Get the user's go-ahead for the override.\n"
            f"  2. Run a fallback adversarial review of the full PR diff: local "
            f"`codex exec` when quota allows, else a Claude Code adversarial "
            f"review (genesis-architect).\n"
            f"  3. Record reviewer + findings + dispositions — and reference the "
            f"head sha {head[:12]} in the body — in:\n"
            f"       {evidence_path}\n"
            f"  4. Re-run this merge (same sigil). A new push changes the head "
            f"sha, and a base-branch change (retarget OR base advancing) "
            f"re-keys too — re-review and re-record.\n"
            f"WHY: 2026-08-23 — an unreviewed merge on this surface disarms every "
            f"other gate; the GitHub Codex review is the required evidence class "
            f"(user decision), and this file's own history (#1432: 13 findings "
            f"invisible to a hand-rolled query) is the proof the gate must not "
            f"trust the author's claim of cleanliness."
        ),
    )


def _classify_post_review_delta(reviewed_sha: str, head_sha: str, repo: str | None) -> str | None:
    """Substantiality of what HEAD adds OVER the Codex-reviewed SHA, via the compare API.

    Returns ``"substantial"`` / ``"inline"`` (a real classification of the unreviewed
    delta) or ``None`` when it cannot be classified (API/parse error). The reviewed SHA
    is often NOT in the local object store (a past PR head), so the delta is fetched
    remotely rather than via local ``git diff``. The CALLER decides the fail direction:
    inside the fail-closed freshness gate, only a definitive ``"inline"`` narrows the
    block — ``None`` blocks like ``"substantial"`` (stale is the default-block state;
    triviality is an exception granted only on positive evidence).

    ``status`` semantics (top-level compare status): ``identical`` (trees match) →
    ``"inline"``. ``behind`` is NOT inline — an ancestor head can still carry code the
    reviewed commit removed → it fails closed (None). ``ahead``
    (the normal append case) or ``diverged`` (a rebase/force-push rewrite) → classify
    the actual changed ``files`` — a diverged rewrite must NOT be assumed trivial.
    Compare TRUNCATION guard: GitHub caps ``files`` at 300 per comparison; at the cap a
    substantial code file may sit beyond it → ``"substantial"`` (conservative).

    Tests inject via ``_TEST_GH_COMPARE`` (the JSON object this would fetch).
    """
    raw = os.environ.get("_TEST_GH_COMPARE")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo or ':owner/:repo'}/compare/{reviewed_sha}...{head_sha}",
                    "--jq",
                    "{status: .status, files: [.files[]? | {filename, additions, deletions, "
                    'status, previous_filename, has_patch: has("patch")}]}',
                ],
                capture_output=True,
                text=True,
                # Merge-path timeout budget (see main()): one compare round-trip is
                # fast; 8s keeps the worst case inside the hook's 60s wall-clock.
                timeout=_gh_timeout(8),
            )
            if result.returncode != 0:
                return None
            raw = result.stdout
        except Exception:
            return None
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return None
    if not isinstance(data, dict):  # valid JSON but not an object → unclassifiable
        return None
    status = data.get("status")
    if status == "identical":
        return "inline"  # trees match exactly → nothing unreviewed
    if status not in ("ahead", "diverged"):
        # NOTE `behind` is NOT treated as inline (Codex P1 #1373): head being an
        # ANCESTOR of the reviewed commit does not make head's TREE a content subset
        # — if the reviewed commit deleted/replaced code and the PR is then reset to
        # its parent, head carries code Codex never approved (it reviewed the removal).
        # `behind` falls here → None → fail closed (re-review required).
        # A MISSING/unknown status (`{status: null, …}` from a truncated or
        # unexpected compare response) must fail CLOSED — NOT fall through to file
        # classification, where an empty `files` would read as "inline" and permit
        # a STALE review to bind the merge on a delta that was never verified
        # (Codex P2, #1373). Only the documented linear statuses are classifiable.
        return None
    files = data.get("files")
    if not isinstance(files, list):
        return None
    if len(files) >= 300:
        return "substantial"  # compare file cap hit → can't rule out substantial code
    # Hook-surface teeth rule 1: a delta touching the enforcement-hook surface is
    # NEVER review-trivial, no matter how small — see the block above
    # _is_hook_surface_path for the why. Rename sources count (previous_filename):
    # a guard renamed/moved out of scripts/hooks/ IS a hook change.
    for f in files:
        if not isinstance(f, dict):
            return None
        # A record MUST carry a readable string ``filename`` — a missing/null/empty
        # one means we cannot confirm this path is NOT a hook-surface file, so fail
        # CLOSED (unclassifiable → the caller blocks a stale review) rather than skip
        # it (Codex P2, round 5: a malformed compare record must not let a hook-surface
        # delta read as review-trivial; ``gh --jq`` still builds an object when the
        # upstream field is absent, so this shape is reachable). ``previous_filename``
        # is optional, but when present must likewise be a non-empty string.
        fn = f.get("filename")
        if not isinstance(fn, str) or not fn:
            return None
        prev = f.get("previous_filename")
        if prev is not None and (not isinstance(prev, str) or not prev):
            return None
        for val in (fn, prev):
            if val and _is_hook_surface_path(val):
                return "substantial"
    try:
        # review_scope lives in scripts/ (parent of scripts/hooks/). Lazy import ON
        # PURPOSE: an import failure must degrade THIS classification to None (the
        # caller then blocks — the gate's fail direction), never crash the guard's
        # module load and drop every push/merge protection. De-duped sys.path insert.
        _scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        from review_scope import classify_compare_substantiality

        return classify_compare_substantiality(files)
    except Exception:
        return None


def _check_codex_reviewed_head(
    pr_num: str, *, force: bool = False, repo: str | None = None
) -> tuple[bool, str, str | None]:
    """Block a merge unless Codex has reviewed the PR's CURRENT head commit —
    or the delta since its last review is provably TRIVIAL.

    Compares the PR's ``headRefOid`` against the FULL ``commit_id`` of Codex's
    latest non-dismissed review — an EXACT identity, no prefix match. Fail-CLOSED:
    if the head cannot be read, or Codex has no review, the merge is BLOCKED. A
    STALE review (Codex reviewed an older commit) blocks UNLESS the unreviewed
    delta (``reviewed...head`` via the compare API) classifies as review-trivial
    (``review_scope`` substantiality: docs-only / a small single-file touch-up) —
    the smart-delta narrowing that keeps the gate's teeth pointed at UNREVIEWED
    SUBSTANTIAL CODE instead of taxing every post-review typo fix (measured
    2026-08-11: a binary stale-blocks gate would have blocked 14/18 recent
    merges). An UNCLASSIFIABLE delta blocks — triviality is an exception granted
    only on positive evidence. ``force`` (a ``# stale-review-override`` on the
    merge segment — deliberately NOT ``# review-override``, which waives the P1
    finding scans; the two boundaries are independent) is the conscious escape
    (e.g. a genuine Codex outage). On the HOOK SURFACE ``force`` additionally
    requires recorded fallback-review evidence regardless of head-freshness — the
    same sigil waives ``_check_base_is_default`` and the evidence identity binds the
    BASE, which a head-only review cannot vouch for (see Codex #9 disposition below).

    Returns ``(should_block, message, verified_head)`` — ``verified_head`` is the
    full head oid this check verified/classified against (when not blocked and
    not forced — including the trivial-delta allow, so the merge is still bound
    to the exact head that was assessed); the caller binds the MERGE to it via
    ``--match-head-commit`` so a push landing between this check and the merge
    cannot smuggle an unreviewed head through (TOCTOU — Codex P1, PR #1366).
    """
    if force:
        # Hook-surface teeth rule 2: the sigil alone is not enough when the PR
        # touches the enforcement-hook surface — recorded fallback-review
        # evidence for the exact head is additionally required (fail-closed).
        # verified_head stays None on the pass path: the force path keeps its
        # documented "conscious unbound merge" contract (no --match-head bind).
        #
        # NOTE (Codex #9, dispositioned FALSE-POSITIVE 2026-08-26): #9 proposed
        # skipping this evidence demand when a current at-head Codex review already
        # exists. That is UNSAFE and was reverted: this force path is reached via
        # # stale-review-override, which ALSO waives _check_base_is_default, and
        # _hook_surface_override_check's evidence identity binds the BASE tip — which
        # a head-only Codex review provably cannot vouch for (see _check_base_is_default's
        # docstring: "GitHub's review object records no base, so freshness alone cannot
        # see it"). Skipping evidence on a fresh review would let a hook-surface PR
        # RETARGETED to a non-default base merge with no base-bound review. A fresh
        # head-review is NOT a substitute for base-bound evidence here.
        blocked, msg = _hook_surface_override_check(pr_num, repo=repo)
        if blocked:
            return True, msg, None
        return False, "", None
    head = _pr_head_sha(pr_num, repo=repo)
    if not head:
        return (
            True,
            (
                f"could not read PR #{pr_num}'s head commit to verify a current Codex "
                f"review (GitHub query failed).\n"
                f"Retry, or append '# stale-review-override' to merge anyway."
            ),
            None,
        )
    head = head.strip().lower()
    reviewed = _latest_codex_reviewed_sha(pr_num, repo=repo)
    if reviewed == head:
        return False, "", head
    # The review-object path can't vouch for the current head (Codex has no review, or
    # only a STALE one). A clean Codex RE-review is an ISSUE COMMENT (not a review object)
    # carrying a "Reviewed commit: <sha>" marker — accept it as freshness when it names
    # THIS head (follow-up 7ff0fdc6). Consulted ONLY on the would-block path, so the common
    # green case (a review object already at head, above) adds no extra API call. The
    # comment sha is an abbreviated PREFIX, matched against the AUTHORITATIVE head — no
    # grinding surface (fixed head, bot-verified author); see the helper's docstring.
    clean_short = _latest_codex_clean_comment_sha(pr_num, repo=repo)
    if clean_short and head.startswith(clean_short):
        return False, "", head
    if not reviewed:
        return (
            True,
            (
                f"no Codex review found for PR #{pr_num} at head {head[:12]}.\n"
                f"Codex reviews on PR-open — it does NOT auto-review a later fix-commit; "
                f"comment '@codex review' on the PR to review the current head (then wait), "
                f"or append '# stale-review-override' to merge without a current Codex "
                f"review (e.g. Codex is genuinely down).\n"
                f"NOTE: the GitHub reviewer and the `codex exec` CLI are separate SURFACES; do "
                f"not infer one from the other. OBSERVED once (2026-08-27): the CLI reported "
                f"a two-week usage lockout while the GitHub reviewer, asked minutes later, "
                f"returned a full review on the same commit. Whether that is separate "
                f"metering, a plan-tier difference or a CLI-side fault was NOT established "
                f"— so treat each surface as independently available until proven otherwise: "
                f"post '@codex review' and check for a review at head BEFORE concluding "
                f"Codex is unavailable."
            ),
            None,
        )
    if reviewed != head:
        level = _classify_post_review_delta(reviewed, head, repo)
        if level == "inline":
            # The unreviewed delta is provably review-trivial — allow, but still
            # bind the merge to THIS head (TOCTOU): the triviality claim is about
            # exactly this reviewed...head range, not any later push.
            print(
                f"NOTE: Codex's review on PR #{pr_num} is on {reviewed[:12]} (head "
                f"{head[:12]}), but the delta since is review-trivial — allowing. "
                f"Inspect: git log {reviewed[:12]}..{head[:12]} --oneline",
                file=sys.stderr,
            )
            return False, "", head
        delta_note = (
            "the unreviewed delta is SUBSTANTIAL"
            if level == "substantial"
            else "the unreviewed delta could not be classified (treated as substantial)"
        )
        return (
            True,
            (
                f"Codex's latest review is STALE: it reviewed {reviewed[:12]}, but PR "
                f"#{pr_num} head is {head[:12]}, and {delta_note} — Codex never saw "
                f"this code.\n"
                f"Comment '@codex review' on the PR to re-review the current head (Codex "
                f"does NOT auto-review fix-commits), then wait; or append "
                f"'# stale-review-override' to merge anyway.\n"
                f"NOTE: the GitHub reviewer and the `codex exec` CLI are separate SURFACES; do "
                f"not infer one from the other. OBSERVED once (2026-08-27): the CLI reported "
                f"a two-week usage lockout while the GitHub reviewer, asked minutes later, "
                f"returned a full review on the same commit. Whether that is separate "
                f"metering, a plan-tier difference or a CLI-side fault was NOT established "
                f"— so treat each surface as independently available until proven otherwise: "
                f"post '@codex review' and check for a review at head BEFORE concluding "
                f"Codex is unavailable."
                f"\n"
                f"  (inspect the unreviewed commits: git log {reviewed[:12]}..{head[:12]} "
                f"--oneline)"
            ),
            None,
        )
    return False, "", head


# ── Scheduled Claude review FRESHNESS (a review by the repo OWNER, at HEAD) ──
# A separate, always-fail-closed gate: a "scheduled Claude review" runs on the repo
# OWNER's account (NOT a bot) and posts a comment/review body carrying the marker
# ``<!-- genesis-scheduled-review: head=<full-40-hex-sha> -->``. The gate blocks a
# merge unless such a marker — authored by the owner — names the PR's CURRENT head.
# The marker is the trust anchor (single-author, non-adversarial model): a spoofed
# marker needs the owner's account. Waived by ``# scheduled-review-override`` (the
# conscious "merge without a scheduled review" case — it didn't run / is rate-limited).
# A scheduled-review marker is an HTML comment ``<!-- genesis-scheduled-review:
# head=<40hex> kind=<name> -->``. Each SCHEDULED routine stamps its own ``kind`` so
# the gate can require that EVERY routine in ``_required_scheduled_review_kinds()`` ran
# on the current head — a single routine's marker no longer satisfies the gate on its
# own. head/kind are parsed from the marker BLOCK (order-tolerant) and BOTH must be
# present in the SAME marker to count.
_SCHEDULED_REVIEW_BLOCK_RE = re.compile(
    r"<!--\s*genesis-scheduled-review:\s*(.*?)\s*-->", re.DOTALL
)
# The value must be TERMINATED by whitespace or the end of the marker block — NOT a
# mere word boundary. Otherwise a status-suffixed producer output like `head=<sha>/failed`
# or `kind=leaks/failed` would have its valid PREFIX captured and counted as a clean marker,
# violating fail-closed (a failed/corrupt routine run must NOT satisfy the gate).
_SCHEDULED_REVIEW_HEAD_RE = re.compile(r"\bhead=([0-9a-f]{40})(?=\s|\Z)")
_SCHEDULED_REVIEW_KIND_RE = re.compile(r"\bkind=([a-z0-9][a-z0-9._-]*)(?=\s|\Z)")

# Default scheduled-review kinds the merge gate REQUIRES at head. A PR merges only when
# a valid owner-authored marker for EACH effective required kind names the current head.
# The DEFAULT is the full set (shipped to every install, unchanged); an install may relax
# the OPTIONAL kinds locally — see _required_scheduled_review_kinds() for the lever.
_DEFAULT_REQUIRED_SCHEDULED_REVIEW_KINDS = ("code-review", "leaks")
# The leak/secret scanner is IRREDUCIBLE: always required, never removable by config. A
# secret reaching a public repo is irreversible, so no local policy may waive it.
_IRREDUCIBLE_REQUIRED_SCHEDULED_REVIEW_KINDS = ("leaks",)
# Every kind an install is ALLOWED to name in config. A configured kind outside this set
# (a typo, a wrong type, a stale routine name) can never be satisfied by a real marker, so
# the whole config is treated as invalid and we fail closed to the default rather than let
# it either wedge merges forever or silently narrow the required set.
_KNOWN_SCHEDULED_REVIEW_KINDS = ("code-review", "leaks")


def _validate_configured_kinds(items: object) -> list[str] | None:
    """Lowercase + validate a configured kind list. Returns the cleaned list (possibly
    empty, meaning "only the irreducible kinds"), or None if ANYTHING is off — not a list,
    a non-string element, a blank element, or an unknown kind. None makes the caller fail
    CLOSED to the full default rather than honor a malformed/ambiguous relaxation."""
    if not isinstance(items, list):
        return None
    out: list[str] = []
    for k in items:
        if not isinstance(k, str):
            return None  # wrong type (e.g. [123]) -> invalid -> default
        kk = k.strip().lower()  # normalize to the lowercase marker grammar
        if not kk or kk not in _KNOWN_SCHEDULED_REVIEW_KINDS:
            return None  # blank ([" "]) or unknown ([foo]) -> invalid -> default
        out.append(kk)
    return out


def _required_scheduled_review_kinds() -> tuple[str, ...]:
    """The scheduled-review kinds the merge gate REQUIRES at head, as a tuple.

    Default is the full set (``code-review`` + ``leaks``) — shipped unchanged to every
    install. An install MAY relax the OPTIONAL kinds (e.g. make the structural
    code-review ADVISORY, so its absence no longer blocks — its review still posts on the
    PR to be read/addressed if it ran) via LOCAL config, keeping install policy out of the
    public default:

        # ~/.genesis/config/genesis.yaml
        merge_gate:
          required_scheduled_reviews: [leaks]

    The leak/secret scanner (``_IRREDUCIBLE_...``) is ALWAYS unioned in and CANNOT be
    dropped by config. Fail-CLOSED toward MORE review: a missing key / unreadable file /
    parse error / duplicate key / wrong-type / blank / unknown kind ALL fall back to the
    full default set, never to fewer kinds. Configured kinds are validated against
    ``_KNOWN_SCHEDULED_REVIEW_KINDS`` and lowercased to the marker grammar. Test seam:
    ``_TEST_REQUIRED_SCHEDULED_REVIEWS`` (comma-separated) overrides the config file.
    """
    raw = os.environ.get("_TEST_REQUIRED_SCHEDULED_REVIEWS")
    configured: list[str] | None = None
    if raw is not None:
        # Test seam: comma-list; empties dropped so "" means "only the irreducible kinds".
        configured = _validate_configured_kinds([k.strip() for k in raw.split(",") if k.strip()])
    else:
        try:
            import yaml  # lazy: keep the hook import-light; the genesis venv has pyyaml

            path = os.path.expanduser("~/.genesis/config/genesis.yaml")
            with open(path) as fh:
                text = fh.read()
            # yaml.safe_load silently keeps the LAST value for a repeated key, so a
            # badly-merged file (two merge_gate: or required_scheduled_reviews: lines)
            # could quietly narrow the required set. Catch the realistic cases with a
            # line scan (the repo's idiom — no unsafe custom Loader) and fail closed.
            if (
                len(re.findall(r"(?m)^merge_gate\s*:", text)) > 1
                or len(re.findall(r"(?m)^\s*required_scheduled_reviews\s*:", text)) > 1
            ):
                raise ValueError("duplicate merge_gate/required_scheduled_reviews key")
            cfg = yaml.safe_load(text) or {}
            configured = _validate_configured_kinds(
                (cfg.get("merge_gate") or {}).get("required_scheduled_reviews")
            )
        except Exception:
            configured = None  # fail-closed: fall back to the full default set below
    kinds = configured if configured is not None else list(_DEFAULT_REQUIRED_SCHEDULED_REVIEW_KINDS)
    # leaks (and any irreducible kind) is always required, even if config omits it.
    merged = list(dict.fromkeys([*kinds, *_IRREDUCIBLE_REQUIRED_SCHEDULED_REVIEW_KINDS]))
    return tuple(merged)


# The GitHub Actions workflow name(s) whose PRESENCE in a PR's check rollup the CI
# gate requires before trusting "green" (the required-CI analogue of the scheduled-
# review kinds above). Default = the canonical repo's ci.yml `name: CI`. Unlike the
# scheduled kinds there is NO irreducible floor and NO known-set whitelist: the
# identity is install-specific free text (a fork's suite may be named anything), so
# config must be able to REPLACE the set — but never to EMPTY it (see the validator).
_DEFAULT_REQUIRED_CI_WORKFLOWS = ("CI",)


def _validate_configured_workflows(items: object) -> list[str] | None:
    """Validate a configured required-CI-workflow list. Returns the cleaned list
    (stripped, deduped, case PRESERVED for display; matching is case-insensitive), or
    None if ANYTHING is off — not a list, an EMPTY list, a non-string element, or a
    blank element. None makes the caller fail CLOSED to the default. An empty list is
    deliberately invalid: it would DISABLE the identity check entirely, and the
    per-merge escape for a consciously CI-less merge is ``# ci-override``, not config
    (non-canonical repos are already exempt via _scheduled_gate_applies)."""
    if not isinstance(items, list) or not items:
        return None
    out: list[str] = []
    for w in items:
        if not isinstance(w, str):
            return None  # wrong type (e.g. [123]) -> invalid -> default
        ww = w.strip()
        if not ww:
            return None  # blank ([" "]) -> invalid -> default
        out.append(ww)
    return list(dict.fromkeys(out))


def _required_ci_workflows() -> tuple[str, ...]:
    """The GitHub Actions workflow names (rollup ``workflowName``) the CI gate REQUIRES
    to have contributed a passing verdict before ``_pr_ci_status`` returns "green".

    Default: ``("CI",)`` — the canonical repo's ci.yml workflow, which has NO paths
    filters and therefore always runs on a PR to main. An install whose required suite
    is named differently configures it locally (keeping install policy out of the
    public default):

        # ~/.genesis/config/genesis.yaml
        merge_gate:
          required_ci_workflows: [My Suite]

    Fail-CLOSED toward the default: a missing key / unreadable file / parse error /
    duplicate key / wrong type / EMPTY list / blank element ALL fall back to the full
    default — there is no config value that disables the check. Because free-text
    config can also EXPAND the required set (unlike the whitelist-relaxed scheduled
    kinds, whose default is maximal), a fallback here can silently NARROW a stricter
    declared policy — so when the key is visibly present but its value was discarded,
    a NOTE is printed naming the substitution (the fallback itself is unchanged).
    Test seam: ``_TEST_REQUIRED_CI_WORKFLOWS`` (comma-separated) overrides the config
    file; a blank seam parses to an empty (=invalid) list and also yields the
    default."""
    raw = os.environ.get("_TEST_REQUIRED_CI_WORKFLOWS")
    configured: list[str] | None = None
    key_seen_in_file = False
    if raw is not None:
        configured = _validate_configured_workflows(
            [w.strip() for w in raw.split(",") if w.strip()]
        )
    else:
        try:
            import yaml  # lazy: keep the hook import-light; the genesis venv has pyyaml

            path = os.path.expanduser("~/.genesis/config/genesis.yaml")
            with open(path) as fh:
                text = fh.read()
            # A textual sighting of the KEY LINE (not a comment/prose mention): if the
            # value is then discarded (dup key, parse error, invalid shape), the
            # operator DECLARED a policy we are about to substitute — that must not be
            # silent. A key-absent file (the normal install) stays silent. Checked
            # BEFORE the parse so a yaml error can't skip it.
            key_seen_in_file = bool(re.search(r"(?m)^\s*required_ci_workflows\s*:", text))
            # Same duplicate-key hazard as the scheduled kinds: yaml.safe_load keeps
            # the LAST value silently, so a badly-merged file could swap the required
            # identity. Line-scan the realistic cases and fail closed.
            if (
                len(re.findall(r"(?m)^merge_gate\s*:", text)) > 1
                or len(re.findall(r"(?m)^\s*required_ci_workflows\s*:", text)) > 1
            ):
                raise ValueError("duplicate merge_gate/required_ci_workflows key")
            cfg = yaml.safe_load(text) or {}
            value = (cfg.get("merge_gate") or {}).get("required_ci_workflows")
            configured = _validate_configured_workflows(value)
        except Exception:
            configured = None  # fail-closed: fall back to the default set below
    if configured is None:
        if key_seen_in_file:
            print(
                "NOTE: merge_gate.required_ci_workflows in ~/.genesis/config/"
                "genesis.yaml is present but unreadable/invalid (duplicate key, "
                "wrong type, empty list, or blank element) — enforcing the DEFAULT "
                f"required set {_DEFAULT_REQUIRED_CI_WORKFLOWS} instead of your "
                "configured value. Fix the config to restore your declared policy.",
                file=sys.stderr,
            )
        return _DEFAULT_REQUIRED_CI_WORKFLOWS
    return tuple(configured)


def _canonical_public_repo() -> str | None:
    """The ONE public repo the scheduled-review gate is scoped to, as ``owner/repo``
    (normalized, case-preserved), or None if it cannot be determined.

    Source: the DECLARED ``github.user``/``github.public_repo`` in
    ``~/.genesis/config/genesis.yaml`` — install-agnostic and cwd-independent (the
    identity of "the public repo" does not depend on which checkout a merge runs
    from). Read locally (no ``gh``/network call — this is on the merge hot path).
    Any read/parse failure returns None; the caller then fails CLOSED (see
    ``_scheduled_gate_applies``). Test seam: ``_TEST_CANONICAL_PUBLIC_REPO`` (an
    ``owner/repo`` string, or empty to force the undeterminable/None branch) is
    honored INSTEAD of the config file when set — the config lives outside the repo
    and is absent in CI, so the gate's scope must be injectable to test
    deterministically."""
    override = os.environ.get("_TEST_CANONICAL_PUBLIC_REPO")
    if override is not None:
        return _normalize_repo(override) if override.strip() else None
    path = os.path.expanduser("~/.genesis/config/genesis.yaml")
    try:
        import yaml  # lazy: keep the hook import-light; the genesis venv has pyyaml

        with open(path) as fh:
            cfg = yaml.safe_load(fh) or {}
        gh = cfg.get("github") or {}
        user = (gh.get("user") or "").strip()
        repo = (gh.get("public_repo") or "").strip()
        if user and repo:
            return _normalize_repo(f"{user}/{repo}")
    except Exception:
        return None
    return None


def _scheduled_gate_applies(repo: str | None) -> bool:
    """Whether the scheduled-review gate ENFORCES for a merge targeting ``repo``.

    The gate is scoped to the configured PUBLIC repo ONLY — the user's directive:
    "these are ALL only requirements for the genesis public repo — not pushing work
    anywhere else." The required ``/schedule`` routines run only on that repo, so a
    ``gh pr merge`` targeting any OTHER repo (a private fork, the voice repo,
    backups) must NOT be blocked on their markers.

    Fail-CLOSED bias: only a target that RESOLVES to a repo DIFFERENT from the
    canonical public one no-ops. If the canonical repo is undeterminable, or the
    target is unknown, ENGAGE — silently skipping on uncertainty would be an
    evasion path on the very repo the gate protects."""
    canonical = _canonical_public_repo()
    if not canonical or not repo:
        return True  # uncertain → enforce (never silently disarm the gate)
    target = _normalize_repo(repo)
    if target is None:
        return True  # unnormalizable target → enforce (fail-closed; unreachable in
        # practice — an unresolved merge repo already blocks upstream)
    return target.strip().lower() == canonical.strip().lower()


def _parse_scheduled_jsonl(raw: str | None) -> list[dict]:
    """Parse the ``{login, author_association, body}`` JSONL shape into a list of
    dicts, SKIPPING any malformed/non-object line (fail-closed: a dropped line can
    only make a marker go UNSEEN → a false block, never a false pass). Mirrors
    ``_codex_reviews``' per-line tolerance."""
    rows: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _scheduled_review_rows(pr_num: str, repo: str | None = None) -> list[dict] | None:
    """Combined ``{login, author_association, body}`` rows from BOTH the PR's issue
    comments AND its review bodies, or ``None`` on any API error (distinct from an
    empty list = query succeeded, nothing there — the caller fail-closes on both, but
    the None case is the UNREADABLE one).

    Both endpoints are ``--paginate``d (a marker beyond the first REST page must still
    count). If EITHER endpoint fails to read, the whole result is ``None``: a marker we
    could not see must not be assumed absent-and-safe — it is UNREADABLE, and this gate
    fails closed. Test seam: ``_TEST_GH_SCHEDULED_COMMENTS`` (one JSON object per line,
    combining both sources) is read INSTEAD of gh when set — mirrors
    ``_TEST_GH_CODEX_COMMENTS``. Fail-safe: None on any subprocess/exception.
    """
    raw = os.environ.get("_TEST_GH_SCHEDULED_COMMENTS")
    if raw is not None:
        return _parse_scheduled_jsonl(raw)
    rows: list[dict] = []
    for path in (
        f"repos/{repo or ':owner/:repo'}/issues/{pr_num}/comments",
        f"repos/{repo or ':owner/:repo'}/pulls/{pr_num}/reviews",
    ):
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    path,
                    "--paginate",
                    "--jq",
                    ".[] | {login: .user.login, author_association: .author_association, "
                    "body: .body, state: .state}",  # .state present on reviews, null on issue comments
                ],
                capture_output=True,
                text=True,
                # See the merge-path timeout budget note in main(): fail-safe → None.
                timeout=_gh_timeout(8),
            )
            if result.returncode != 0:
                return None
        except Exception:
            return None
        rows.extend(_parse_scheduled_jsonl(result.stdout))
    return rows


def _scheduled_review_marker_scan(
    pr_num: str, repo: str | None = None
) -> tuple[dict[str, set[str]], dict[str, set[str]]] | None:
    """``(accepted, rejected_not_clean)`` maps of ``head-sha -> {kinds}`` for the PR's
    owner-authored scheduled-review markers, or ``None`` on an UNREADABLE fetch.

    Both maps come from ONE pass so they cannot drift apart. ``accepted`` is what the
    gate honours; ``rejected_not_clean`` is markers that parsed fine and named a head,
    but whose body read as carrying a blocking finding. Only the DIAGNOSTIC message
    consumes the second map — it never widens what satisfies the gate.

    A marker is trusted only when its author is the repo OWNER: ``login == owner`` OR
    ``author_association == "OWNER"`` (belt-and-suspenders — the marker itself is the
    trust anchor in the single-author, non-adversarial model). The owner login is the
    first segment of ``(repo or derived)``; ``derived`` is gh's repo for the cwd when no
    explicit repo was passed. Each marker BLOCK must carry BOTH ``head=<40hex>`` and
    ``kind=<name>`` to count (order-tolerant); the ``kind`` is recorded under that head.
    """
    rows = _scheduled_review_rows(pr_num, repo=repo)
    if rows is None:
        return None
    try:
        owner_repo = repo or _derive_repo_from_cwd(os.getcwd())
    except Exception:
        owner_repo = repo
    owner = (owner_repo.split("/")[0] if owner_repo else "").lower() or None
    accepted: dict[str, set[str]] = {}
    rejected: dict[str, set[str]] = {}
    for row in rows:
        login = (row.get("login") or "").lower()
        assoc = (row.get("author_association") or "").upper()
        if login != owner and assoc != "OWNER":
            continue  # not the repo owner — not a trusted scheduled review
        # A DISMISSED review no longer vouches, and a PENDING review is an UNPUBLISHED
        # draft that never ran publicly — neither should satisfy the gate (mirrors the
        # Codex-freshness path). `state` is present on /pulls/N/reviews rows and null on
        # issue comments, so this only drops review rows; issue comments remain state-less.
        if (row.get("state") or "").upper() in ("DISMISSED", "PENDING"):
            continue
        body = row.get("body") or ""
        # The marker must mean "ran CLEAN", not merely "ran": a scheduled review whose body
        # CONTAINS a blocking finding ([P1]/HARD BLOCK/### ERROR, unless a clean marker
        # overrides — same "clean wins" rule the finding scanners use) does NOT satisfy the
        # gate. Owner-authored review bodies are never seen by _check_pr_review_findings
        # (bots only), so without this a scheduled reviewer that explicitly BLOCKED would
        # still stamp its marker and slip the merge through.
        #
        # Such a marker is RECORDED (not dropped on the floor) so the block message can
        # tell "you posted one and it was rejected" apart from "nobody posted anything" —
        # measured 2026-08-28: those two produced the identical `present: none` line, and
        # the only signal distinguishing an accepted marker from a rejected one was
        # whether its prose happened to contain a _CLEAN_PATTERNS phrase.
        not_clean = any(p.search(body) for p in _BLOCKING_PATTERNS) and not any(
            c.search(body) for c in _CLEAN_PATTERNS
        )
        target = rejected if not_clean else accepted
        # Defense-in-depth follow-up: for rows from /pulls/N/reviews we could ALSO
        # cross-check GitHub's authoritative `commit_id` vs the marker sha (issue comments
        # carry none). Deferred (LOW): the marker sha is matched EXACTLY vs the authoritative
        # HEAD by the caller, so a stale marker can't pass; this only catches a buggy reviewer.
        for block in _SCHEDULED_REVIEW_BLOCK_RE.findall(body):
            head_m = _SCHEDULED_REVIEW_HEAD_RE.search(block)
            kind_m = _SCHEDULED_REVIEW_KIND_RE.search(block)
            if not head_m or not kind_m:
                continue  # a marker must name both a head AND a kind to count
            target.setdefault(head_m.group(1).lower(), set()).add(kind_m.group(1).lower())
    return accepted, rejected


def _check_scheduled_claude_reviewed_head(
    pr_num: str,
    head_sha: str | None = None,
    repo: str | None = None,
    *,
    force: bool = False,
) -> str | None:
    """Block a merge unless EVERY required scheduled Claude review (by the repo OWNER)
    has run on the PR's CURRENT head. Returns ``None`` when a valid owner marker for
    each kind in ``_required_scheduled_review_kinds()`` names ``head_sha`` exactly (full
    40-char), else a BLOCK MESSAGE naming the MISSING kinds.

    Fail-CLOSED — this gate never passes on absence of positive evidence: if the head
    cannot be read, or the comment/review fetch errors (``_scheduled_review_marker_scan`` →
    None), the merge is BLOCKED. A read that simply does not carry every required kind
    at this head (a routine didn't run, ran on a stale commit, or was rate-limited) also
    blocks. The merge path and the report path share this single fail-closed decision,
    so the report can never issue a false all-clear here.

    WHY THE BLOCK MESSAGE PARTITIONS. A missing marker has THREE causes calling for
    DIFFERENT operator actions, all distinguishable from what was already read. The
    missing kinds are PARTITIONED across those causes and EVERY non-empty group is
    reported — this is not a precedence chain that picks one winner:
      * REFUSED at this head — a marker is present on the current commit but read as
        carrying a blocking finding. Neither waiting nor re-reviewing helps; the body's
        wording (or a real finding) is the cause. It is also the cause most easily
        mistaken for "nobody posted anything", since the gate's `present:` line shows
        the same `none` either way.
      * ELSEWHERE — a marker for that kind sits on some OTHER head, ACCEPTED OR REFUSED.
        A routine ran, then a push moved the head. Routines are not generally re-run on a
        push, so waiting is unlikely to help; re-review the current head and post it.
      * ABSENT — no marker for that kind at any head. Nothing has run for it, so on a
        freshly-opened PR a routine may still be in flight and waiting IS the right move.

    Two design rules earned by successive review rounds, both of the SAME shape — a
    decision made from only part of what had been read. Keep them:
      1. Scope every group to ``missing``. A marker for an already-satisfied kind explains
         nothing about this block, and prescribing a remedy for it sends the operator to
         fix something that is not broken (it also let the message contradict its own
         ``present:`` line one row above).
      2. PARTITION rather than prioritise. Different kinds routinely fail for different
         reasons — a refused ``code-review`` beside an absent ``leaks`` — and choosing a
         single winning cause explained one kind while the other silently got no guidance
         at all. Precedence exists only WITHIN a kind (refused-here beats elsewhere).

    An earlier draft collapsed everything into an unconditional "waiting will not clear
    this", which was wrong for the ABSENT case and pushed the operator toward
    ``# scheduled-review-override`` — i.e. toward waiving the IRREDUCIBLE leak gate — in
    the one situation where patience was the correct answer.

    The message deliberately reports only what it OBSERVED (which heads carry markers)
    and hedges the schedule ("generally not re-run"). The routines live OUTSIDE this repo
    and this gate cannot see their triggers, so an unconditional claim about when they
    fire would be asserting a guarantee the code cannot back — and would become actively
    misleading on an install that also runs them on ``synchronize``.

    Head match is EXACT — there is no ancestor walk and no delta tolerance here, unlike
    the Codex freshness gate, which grants relief on a provably trivial delta via
    ``_classify_post_review_delta``. That asymmetry is deliberate for now: that
    classifier judges CODE-REVIEW substantiality by file type and size, and an
    inferential leak (household/schedule/habit detail, not a token a regex can catch)
    arrives in exactly the small doc edit it would wave through. A leak-specific
    tolerance is tracked separately; do not reuse the code-review classifier for it.

    SCOPE: this gate enforces ONLY for a merge targeting the configured PUBLIC repo
    (``_scheduled_gate_applies`` / ``_canonical_public_repo``). A merge to any other
    repo (a private fork, the voice repo, backups) returns None (no-op) — the
    required routines run only on the public repo, so they cannot be required
    elsewhere.

    ``head_sha`` (the caller's authoritative head) is used when given; otherwise the
    head is read via ``_pr_head_sha``. ``force`` (a ``# scheduled-review-override`` on
    the merge segment — an INDEPENDENT sigil from ``# stale-review-override``) waives
    this gate for the conscious "merge without the scheduled reviews" case (e.g. a
    routine is down / rate-limited).
    """
    if force:
        return None  # waived by # scheduled-review-override
    if not _scheduled_gate_applies(repo):
        return None  # out of scope: merge targets a repo other than the public one
    head = (head_sha or "").strip().lower()
    if not head:
        head = (_pr_head_sha(pr_num, repo=repo) or "").strip().lower()
    if not head:
        return (
            f"could not read PR #{pr_num}'s head commit to verify the scheduled Claude "
            f"reviews (GitHub query failed).\n"
            f"Retry, or append '# scheduled-review-override' to merge anyway."
        )
    scan = _scheduled_review_marker_scan(pr_num, repo=repo)
    markers, rejected = (None, {}) if scan is None else scan
    if markers is None:
        return (
            f"could not read PR #{pr_num}'s comments/reviews to verify the scheduled Claude "
            f"reviews at head {head[:12]} — review status UNREADABLE (retry), not clean.\n"
            f"Retry, or append '# scheduled-review-override' to merge anyway."
        )
    kinds_here = markers.get(head, set())
    required = _required_scheduled_review_kinds()
    missing = [k for k in required if k not in kinds_here]
    if not missing:
        return None
    # WHY the marker is missing decides whether waiting is useful, and the two causes are
    # DISTINGUISHABLE from what was actually read — so report the observed state instead
    # of asserting a routine schedule this repo cannot see (the routines live outside it).
    # Every branch below is scoped to the MISSING kinds. A marker for a kind that is
    # already satisfied says nothing about why this merge is blocked, and prescribing a
    # remedy for it sends the operator to fix something that is not broken.
    # PARTITION the missing kinds by cause and report EVERY group — never pick one cause
    # for the whole block. Different kinds routinely fail for different reasons (a refused
    # code-review alongside an absent leaks), and collapsing to a single winner explains
    # one kind while the other silently gets no guidance at all. THREE successive review
    # findings on this function were that same shape — a branch deciding from part of what
    # had been read — so this is the general form rather than a fourth point patch: a total
    # partition needs no precedence BETWEEN groups, only within a single kind.
    missing_set = set(missing)
    refused_kinds = rejected.get(head, set()) & missing_set
    # A marker for a missing kind on some OTHER head — ACCEPTED OR REFUSED. Both prove a
    # routine ran on a different commit; counting only accepted ones sent a
    # refused-at-a-stale-head PR down the "nothing has run, wait for it" path.
    heads_by_kind: dict[str, set[str]] = {}
    for by_head in (markers, rejected):
        for other_head, kinds in by_head.items():
            if other_head == head:
                continue
            for kind in kinds & missing_set:
                heads_by_kind.setdefault(kind, set()).add(other_head[:12])
    # Within a KIND, refused-at-this-head wins: it is the most specific cause, and the only
    # one whose remedy is neither waiting nor re-reviewing.
    elsewhere_kinds = {k for k in missing_set - refused_kinds if k in heads_by_kind}
    absent_kinds = missing_set - refused_kinds - elsewhere_kinds

    parts: list[str] = []
    if refused_kinds:
        parts.append(
            f"{', '.join(sorted(refused_kinds))} — a marker IS present at THIS head, but it "
            f"was REFUSED because its body reads as carrying a blocking finding (matching "
            f"one of [P1] / HARD BLOCK / an '### ERROR' heading) with no explicit clean "
            f"verdict to override it. A marker must mean it ran CLEAN, not merely that it "
            f"ran. If the review really was clean, its prose tripped the check — re-post it "
            f"ending with an explicit verdict line, exactly 'VERDICT: PASS' or "
            f"'PII/Secrets/Wording: CLEAN'. If the finding is real, fix it first."
        )
    if elsewhere_kinds:
        seen = sorted({h for k in elsewhere_kinds for h in heads_by_kind[k]})
        shown = ", ".join(seen[:3]) + (f" (+{len(seen) - 3} more)" if len(seen) > 3 else "")
        parts.append(
            f"{', '.join(sorted(elsewhere_kinds))} — a marker is present for a DIFFERENT "
            f"head ({shown}), so a routine HAS run on this PR, just not on the current "
            f"commit. Routines are generally not re-run when a later push moves the head, "
            f"so waiting is unlikely to clear this on its own: re-run against the current "
            f"head and post the marker yourself."
        )
    if absent_kinds:
        parts.append(
            f"{', '.join(sorted(absent_kinds))} — no marker at ANY head on this PR yet. If "
            f"it was just opened, a routine may still be in flight, and waiting IS the right "
            f"move. If it has been open a while, re-run against the current head and post "
            f"the marker yourself."
        )
    return (
        f"scheduled Claude review(s) missing at head {head[:12]}: {', '.join(missing)} "
        f"(required: {', '.join(required)}; present: "
        f"{', '.join(sorted(kinds_here)) or 'none'}).\n"
        + "".join(f"  * {p}\n" for p in parts)
        + "A marker is a comment/review by the repo OWNER carrying "
        f"'<!-- genesis-scheduled-review: head={head} kind=<name> -->' — the FULL 40-hex "
        "head, exactly as written here.\n"
        "Or append '# scheduled-review-override' to merge without the missing review(s)."
    )


def _pr_base_ref(pr_num: str, repo: str | None = None) -> str | None:
    """The branch this PR MERGES INTO (``baseRefName``), or None on any error.

    Tests inject via ``_TEST_GH_BASE_REF`` (its own seam, mirroring
    ``_pr_head_sha``'s ``_TEST_GH_HEAD_SHA`` — one gh interaction per seam keeps
    each independently fakeable; the extra ``gh pr view`` is cheap on a rare merge).
    """
    raw = os.environ.get("_TEST_GH_BASE_REF")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    pr_num,
                    *_repo_args(repo),
                    "--json",
                    "baseRefName",
                    "--jq",
                    ".baseRefName",
                ],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(
                    6
                ),  # merge-path budget (see main): fail-closed → unreadable base BLOCKS
            )
            raw = result.stdout if result.returncode == 0 else ""
        except Exception:
            return None
    ref = (raw or "").strip()
    return ref or None


def _repo_default_branch(repo: str | None = None) -> str | None:
    """The repo's default branch name (``defaultBranchRef.name``), or None on error.

    ``gh repo view`` takes the repo as a POSITIONAL (``gh repo view OWNER/REPO``),
    not ``--repo`` — verified: ``gh pr view`` has no ``defaultBranchRef`` field, so
    this cannot be folded into the base query. No positional ⇒ gh's cwd repo.
    Tests inject via ``_TEST_GH_DEFAULT_BRANCH``.
    """
    raw = os.environ.get("_TEST_GH_DEFAULT_BRANCH")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "repo",
                    "view",
                    *([repo] if repo else []),
                    "--json",
                    "defaultBranchRef",
                    "--jq",
                    ".defaultBranchRef.name",
                ],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(
                    6
                ),  # merge-path budget (see main): fail-closed → unreadable default BLOCKS
            )
            raw = result.stdout if result.returncode == 0 else ""
        except Exception:
            return None
    name = (raw or "").strip()
    return name or None


#: The pin file the receipt gate compares. Kept here (not imported) so a missing
#: checker module degrades to a NOTE rather than an import error at hook load.
_PIN_FILE_PATH = "scripts/lib/cc_version.sh"


def _load_pin_receipt_checker():
    """Import scripts/check_cc_pin_receipts.py, or None if unavailable.

    Lazy and failure-tolerant on purpose: the checker is a sibling script, not a
    package, and a hook that cannot import it must not stop being a merge gate
    for everything else.
    """
    import importlib.util

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    path = os.path.join(repo_root, "scripts", "check_cc_pin_receipts.py")
    try:
        spec = importlib.util.spec_from_file_location("_cc_pin_receipts", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        # Registered before exec: @dataclass resolves its module from sys.modules.
        sys.modules["_cc_pin_receipts"] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


#: `_pin_file_at_ref` outcomes. ABSENT is a fact about the PR's CONTENT (the API
#: answered, and the file is not there at that ref); UNREADABLE is a fact about the
#: PLUMBING (no slug, timeout, auth, transport). They take OPPOSITE fail directions,
#: so collapsing them — as an earlier revision did by returning None for both — let a
#: PR that DELETES the pin file take the plumbing path and merge unblocked, which
#: contradicts the checker's own stated policy that an unreadable pin BLOCKS.
_PIN_OK = "ok"
_PIN_ABSENT = "absent"
_PIN_UNREADABLE = "unreadable"
#: Test-seam sentinels for the two NON-content outcomes (mirroring
#: `_TEST_GH_PR_FILES`'s `__error__`). Any other seam value — including the EMPTY
#: STRING — is content, because with the JSON contents form an empty file is a real,
#: distinct state from both absence and an unreadable response.
_PIN_SEAM_ABSENT = "__absent__"
_PIN_SEAM_UNREADABLE = "__unreadable__"


def _pin_file_at_ref(ref: str, repo: str | None, *, seam: str) -> tuple[str | None, str]:
    """``(contents, outcome)`` for the pin file at ``ref``, read through the API.

    Used for BOTH sides of the comparison — one code path, so the head and base
    reads cannot drift apart in their fail direction (they did: the base side used
    a hardcoded local ``git show origin/main``, which is neither bound to the repo
    being merged into nor to the PR's actual base branch. On a checkout whose
    ``origin`` is a fork, a genuine forward bump could read as a DOWNGRADE and be
    exempted).

    NOT from the local checkout: the gate runs from the main worktree, which is on
    main, so the PR's version of the file is not on disk here. That is also the
    property making this gate un-editable by the PR — only the DATA comes from the
    PR, never the code reading it.
    """
    raw = os.environ.get(seam)
    if raw is not None:
        # The seam mirrors the live path's THREE outcomes, so a test cannot see
        # behaviour production is incapable of producing. Both non-content outcomes
        # need their own sentinel, because with the JSON form an EMPTY STRING is a
        # legitimate third thing — a file that exists and is empty — and collapsing
        # it into either sentinel would hide the very distinction this seam exists
        # to exercise.
        if raw == _PIN_SEAM_ABSENT:
            return None, _PIN_ABSENT
        if raw == _PIN_SEAM_UNREADABLE:
            return None, _PIN_UNREADABLE
        return raw, _PIN_OK
    # Resolution order matters. `_canonical_public_repo()` reads install config and
    # is legitimately absent on an install that never set `github.*` — the merge
    # gate treats that as "uncertain, enforce", so falling back to the repo the
    # process is actually in keeps the gate WORKING rather than silently
    # unverifying on every such install (measured: it returned None here).
    slug = (_normalize_repo(repo) if repo else None) or _canonical_public_repo()
    if not slug:
        slug = _derive_repo_from_cwd(os.getcwd())
    if not slug:
        return None, _PIN_UNREADABLE
    try:
        result = subprocess.run(
            # The JSON representation, NOT `Accept: raw`. Raw returns bytes, and bytes
            # cannot distinguish "the file is not there" from "the file is empty" from
            # "the response was truncated" — all three arrive as an empty body, which
            # forced the previous revision to GUESS from gh's stderr wording. The JSON
            # form answers directly: `type` and `size` are facts about the tree, and a
            # missing path is a 404. MEASURED: a present file returns
            # {"name":…, "size":21747, "type":"file"}; an absent path returns 404.
            ["gh", "api", f"repos/{slug}/contents/{_PIN_FILE_PATH}?ref={ref}"],
            capture_output=True,
            text=True,
            timeout=_gh_timeout(6),
        )
    except Exception:
        return None, _PIN_UNREADABLE
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout or "")
        except Exception:
            # A zero exit whose body is not JSON is a stub or a truncated response —
            # plumbing. This is the shape a test router's no-op reply takes, and it
            # must not read as a fact about the tree.
            return None, _PIN_UNREADABLE
        if not isinstance(payload, dict):
            # A directory lists as an ARRAY. Either way the pin file is not at this
            # path, which is a fact about the PR's content.
            return None, _PIN_ABSENT
        if payload.get("type") != "file":
            return None, _PIN_ABSENT  # a submodule or symlink-to-dir is not the pin
        if payload.get("encoding") != "base64":
            # >1MB blobs come back with encoding "none" and no content. Not absence —
            # the file is there and we simply cannot read it this way.
            return None, _PIN_UNREADABLE
        try:
            text = base64.b64decode(payload.get("content") or "").decode("utf-8", "replace")
        except Exception:
            return None, _PIN_UNREADABLE
        # An EMPTY file is returned as content, deliberately, not as a state of its
        # own. It exists, so it is not absent — and an empty pin is an UNPARSEABLE
        # pin, which the checker's own policy already blocks. Classifying it here
        # would duplicate that policy in the wiring, which is how the previous
        # revision came to block a DELETED pin file while waving through one
        # truncated to nothing: the same condition, enforced two different ways.
        return text, _PIN_OK
    # Non-zero. With the JSON form the only ambiguity left is which THING was not
    # found, and gh names the ref case explicitly. MEASURED against the live API:
    #   bad ref      -> gh: No commit found for the ref <sha> (HTTP 404)
    #   missing file -> gh: Not Found (HTTP 404)
    #   bad repo     -> gh: Not Found (HTTP 404)
    # A bad ref is PLUMBING. Missing-file and missing-repo share one message — GitHub
    # will not separate them, so as not to leak whether a private repo exists — but
    # this gate is reached only after the head sha and base ref were read SUCCESSFULLY
    # from that same repo, so the repo is known good by then and a bare Not Found is
    # the path being absent.
    stderr = (result.stderr or "").lower()
    if "no commit found for the ref" in stderr:
        return None, _PIN_UNREADABLE
    if "not found" in stderr:
        return None, _PIN_ABSENT
    return None, _PIN_UNREADABLE


def _pr_body_text(pr_num: str, repo: str | None) -> str | None:
    """The PR body. ``None`` means unreadable — distinct from an EMPTY body,
    which is a real state that determines the answer by itself."""
    raw = os.environ.get("_TEST_GH_PR_BODY")
    if raw is not None:
        return raw
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_num, *_repo_args(repo), "--json", "body", "--jq", ".body"],
            capture_output=True,
            text=True,
            timeout=_gh_timeout(6),
        )
    except Exception:
        return None
    return result.stdout if result.returncode == 0 else None


def _check_pin_receipts(pr_num: str, repo: str | None = None) -> tuple[bool, str]:
    """Block a PR that moves the Claude Code pin FORWARD without its gate receipts.

    THIS is the authority for the receipt gates, not a CI status. The body is
    mutable after any CI run finishes, so a status describing it is a claim about
    the past; read at merge time there is no window to edit it afterwards. The
    CI job runs the same checker with ``--advisory`` purely for early feedback.

    BOTH sides are read through the API, against the repo actually being merged
    into and the PR's OWN base branch. An earlier revision read the base with a
    local ``git show origin/main`` — not bound to the merge target and hardcoding
    the branch name — so from a checkout whose ``origin`` is a fork (or whose
    ``origin/main`` is simply unfetched) the comparison ran against the wrong base,
    and a fork sitting on a HIGHER pin turned a genuine forward bump into a
    "downgrade" that the gate exempts. Head still comes through the API, so the
    code doing the reading is always main's copy.

    FAIL DIRECTION, split deliberately — and the split is between CONTENT and
    PLUMBING, not between "worked" and "didn't":
      * The pin moved forward and a receipt is missing, or the pin cannot be READ
        at either side, or the file is ABSENT at either side -> BLOCK. All are
        facts about the PR's content, and publishing a release nobody can
        characterise is the thing to prevent. Absence matters on its own: a PR that
        DELETES the pin file has no readable pin, which the checker's policy says
        must block — routing that through the plumbing path would let the deletion
        merge unexamined.
      * The gate's own PLUMBING failed (no checker module, unreadable head sha or
        base ref, auth/transport error) -> NOTE, do not block. Walling off every
        merge over a check that only ever guards a pin bump would be a worse
        failure than the one it prevents.
    """
    checker = _load_pin_receipt_checker()
    if checker is None:
        return False, "NOTE: pin-receipt checker not importable — receipts NOT verified."

    head_sha = _pr_head_sha(pr_num, repo=repo)
    if not head_sha:
        return False, "NOTE: PR head unreadable — pin receipts NOT verified."

    base_ref = _pr_base_ref(pr_num, repo=repo)
    if not base_ref:
        return False, "NOTE: PR base ref unreadable — pin receipts NOT verified."

    head_text, head_state = _pin_file_at_ref(head_sha, repo, seam="_TEST_GH_HEAD_PIN_FILE")
    if head_state == _PIN_ABSENT:
        return True, (
            f"BLOCKED: {_PIN_FILE_PATH} is ABSENT at the PR head ({head_sha[:12]}). The pin "
            f"cannot be read, so whether this PR moves it forward cannot be established — "
            f"and a PR that removes the pin file is exactly the case this gate must not "
            f"wave through. Restore it, or merge with the gate's own escape if the removal "
            f"is intended."
        )
    if head_text is None:
        return (
            False,
            f"NOTE: could not read {_PIN_FILE_PATH} at the PR head — receipts NOT verified.",
        )

    base_text, base_state = _pin_file_at_ref(base_ref, repo, seam="_TEST_GH_BASE_PIN_FILE")
    if base_state == _PIN_ABSENT:
        return True, (
            f"BLOCKED: {_PIN_FILE_PATH} is ABSENT on the base branch ({base_ref}). There is "
            f"no pin to compare against, so a forward move cannot be ruled out."
        )
    if base_text is None:
        return False, (f"NOTE: {_PIN_FILE_PATH} unreadable on {base_ref} — receipts NOT verified.")

    body = _pr_body_text(pr_num, repo)
    if body is None:
        return False, "NOTE: PR body unreadable — pin receipts NOT verified."

    try:
        verdict = checker.evaluate(base_pin_text=base_text, head_pin_text=head_text, body=body)
    except Exception as exc:  # noqa: BLE001 — plumbing, not policy
        return False, f"NOTE: pin-receipt check errored ({type(exc).__name__}) — NOT verified."

    return verdict.blocked, verdict.message


def _check_base_is_default(
    pr_num: str, *, force: bool = False, repo: str | None = None
) -> tuple[bool, str]:
    """Block unless the PR's base branch == the repo's default branch.

    A PR retargeted AFTER Codex reviewed it still passes the head-freshness gate —
    the head oid never moved — even though the base change can substantially alter
    the effective diff (GitHub's review object records no base, so freshness alone
    cannot see it). This repo's PRs always target the default branch, so a
    non-default base is anomalous → block. Fail-CLOSED: an unreadable base OR
    default is treated as unverifiable → block (matching the rest of this gate).
    ``force`` (a ``# stale-review-override`` on the merge segment — the
    review-CONTEXT sigil, shared with the freshness gate and deliberately NOT
    ``# review-override``, which waives the P1 finding scans) is the conscious
    escape for a deliberate stacked/non-default PR. Declared threat model: this
    guards an ACCIDENTAL retarget on a single-author repo, not an adversary.
    """
    if force:
        return False, ""
    base = _pr_base_ref(pr_num, repo=repo)
    default = _repo_default_branch(repo=repo)
    if not base or not default:
        return (
            True,
            (
                f"could not confirm PR #{pr_num}'s base branch against the repo default "
                f"(base={base or '?'}, default={default or '?'}) — retry, or append "
                f"'# stale-review-override' to merge anyway."
            ),
        )
    if base != default:
        return (
            True,
            (
                f"PR #{pr_num} targets base '{base}', not the default branch '{default}'. "
                f"A retargeted PR's Codex review may not reflect the new diff — re-run "
                f"'@codex review' on the PR, or append '# stale-review-override' for a "
                f"deliberate stacked/non-default PR."
            ),
        )
    return False, ""


def _suggested_merge_cmd(pr_num: str, verified_head: str, repo: str | None) -> str:
    """The exact atomic merge command to copy — PRESERVING an explicit --repo.

    Dropping the target repo from a generated command retargets the merge to the
    cwd repo, which can merge an unrelated same-numbered PR (Codex P1, round 3).
    ``repo`` is the already-normalized OWNER/REPO the gates ran against, or None
    (cwd repo → no --repo).
    """
    repo_part = f"--repo {repo} " if repo else ""
    return f"gh pr merge {pr_num} {repo_part}--squash --admin --match-head-commit {verified_head}"


# gh pr merge flags that CONSUME the FOLLOWING token as their value (separate
# form). When scanning for --match-head-commit these values MUST be skipped, else
# a `--body --match-head-commit=<sha>` — where the sha is --body's VALUE, taken as
# body TEXT by gh with NO head binding — is misread as an active binding while gh
# merges unbound (Codex P1, round 3). --repo is gate-relevant (handled elsewhere)
# but still value-consuming here; --match-head-commit itself consumes its own SHA.
# NOTE: the risk is the PARSE MODEL (pflag argument consumption + short-flag
# clustering), not the flag names — the long value-flag set is identical across
# gh 2.45–2.96 `gh pr merge --help`.
_GH_MERGE_VALUE_FLAGS = frozenset(
    {
        "--body",
        "-b",
        "--body-file",
        "-F",
        "--subject",
        "-t",
        "--author-email",
        "-A",
        "--repo",
        "-R",
        "--match-head-commit",
    }
)
# Content value-flags (LONG forms) that can SHADOW --match-head-commit as their
# value and have NO legitimate use on a gated --admin squash-merge. Their mere
# presence is refused (fail-closed belt over the value-skipping parse). Short
# forms are handled letter-wise in the cluster helpers below.
_GH_MERGE_SHADOW_FLAGS = frozenset({"--body", "--body-file", "--subject", "--author-email"})
# Short flags on gh pr merge. VALUE letters consume a value (glued remainder, else
# the NEXT token); the rest are booleans (-d/-m/-r/-s). A cluster like `-db` is
# -d(bool) + -b(value), so gh's -b swallows the FOLLOWING token — a scan treating
# `-db` as opaque misses that consumption (audit round 4: `-db --match-head-commit=X`
# merges UNBOUND). SHADOW shorts are the content ones (not -R).
_GH_MERGE_VALUE_SHORTS = "bFtAR"
_GH_MERGE_SHADOW_SHORTS = "bFtA"


def _is_short_cluster(tok: str) -> bool:
    return tok.startswith("-") and not tok.startswith("--") and len(tok) > 1


def _short_cluster_consumes_next(tok: str) -> bool:
    """Whether a ``-<letters>`` short cluster makes gh consume the NEXT argv token
    as a value — a value-short letter with NO glued remainder after it. Boolean
    letters before it are consumed in place. Mirrors pflag."""
    if not _is_short_cluster(tok):
        return False
    letters = tok[1:]
    for j, ch in enumerate(letters):
        if ch in _GH_MERGE_VALUE_SHORTS:
            return letters[j + 1 :] == ""  # glued remainder ⇒ value in-token, not next
    return False


def _merge_match_head(argv: list[str]) -> str | None:
    """The value gh will use for ``--match-head-commit`` on a merge argv, or None.

    Parses with gh-equivalent argument consumption: value-taking flags
    (``_GH_MERGE_VALUE_FLAGS``) consume the next token, so a --match-head-commit
    token that is actually ANOTHER flag's value is not misread as a binding. A
    bare ``--`` ends option parsing (everything after is positional; gh rejects
    extra positionals). Returns the LAST occurrence — gh pflag last-value-wins
    (same rule as ``_pr_create_head_raw``); first-wins would let a trailing
    ``--match-head-commit <other>`` be enforced by gh while the hook validated an
    earlier value.
    """
    argv = argv or []
    result: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break  # end of options — the rest are positionals gh would reject
        if tok in _GH_MERGE_VALUE_FLAGS:
            # Separate-form value flag consumes the NEXT token. When the flag is
            # --match-head-commit itself, that next token is the value we want.
            if tok == "--match-head-commit" and i + 1 < len(argv):
                result = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--match-head-commit="):
            result = tok.split("=", 1)[1]
            i += 1
            continue
        if _short_cluster_consumes_next(tok):
            i += 2  # cluster's trailing value-short swallows the next token
            continue
        i += 1
    return result


def _merge_has_shadow_flag(argv: list[str]) -> bool:
    """Whether the merge carries a content flag that could shadow the head-match
    binding — long form, ``=`` form, bare short, or inside a short cluster
    (``-db`` etc.). Refused outright as a fail-closed belt."""
    for tok in argv or []:
        if tok == "--":
            break
        base = tok.split("=", 1)[0]
        if base in _GH_MERGE_SHADOW_FLAGS:
            return True
        if _is_short_cluster(base):
            for ch in base[1:]:
                if ch in _GH_MERGE_SHADOW_SHORTS:
                    return True
                if ch in _GH_MERGE_VALUE_SHORTS:
                    break  # a value-short (e.g. -R): the rest is its glued value
    return False


def _require_match_head(
    merge_argv: list[str],
    pr_num: str,
    bind_head: str,
    repo: str | None,
    source: str,
) -> str | None:
    """Enforce the TOCTOU binding: the merge must carry ``--match-head-commit`` equal to
    ``bind_head`` (a race with a new push then merges an UNREVIEWED head, which GitHub
    rejects server-side). Returns None when bound correctly, else a BLOCK MESSAGE.
    ``source`` names the gate whose head we bind to (e.g. "Codex-verified",
    "scheduled-review-verified"). Shared by every gate that verifies a specific head so
    a passing check is always atomically pinned to the sha it verified.
    """
    # Content value-flags can smuggle a --match-head-commit token as their VALUE
    # (gh takes it as text → no binding) and have no use on a gated squash-merge.
    if _merge_has_shadow_flag(merge_argv):
        return (
            "--body/--subject/--body-file/--author-email are not allowed on a gated "
            "merge — they can shadow the --match-head-commit binding. Remove them "
            "(set a squash message via the GitHub UI if needed)."
        )
    match_head = _merge_match_head(merge_argv)
    if match_head is None:
        return (
            f"merge must be bound to the {source} head commit so a race with a new "
            f"push cannot merge an unreviewed head. Re-run with:\n"
            f"  {_suggested_merge_cmd(pr_num, bind_head, repo)}"
        )
    if match_head.strip().lower() != bind_head:
        return (
            f"--match-head-commit {match_head[:12]} does not equal the {source} head "
            f"{bind_head[:12]} — the branch moved (or the sha is stale). Re-verify and "
            f"use the current verified head."
        )
    return None


def _is_dispatched() -> bool:
    """True in a Genesis-dispatched (autonomous/headless) CC session.

    ``cc/invoker.py`` stamps ``GENESIS_CC_SESSION=1`` on every dispatched
    session; a user-launched foreground session does not carry it. Dispatched
    sessions have no human to answer an ``ask`` prompt and never push via the CC
    Bash tool in normal operation — the executor pushes from the server
    subprocess, scope-gated (``autonomy/executor``) — so they are hard-denied
    rather than prompted.
    """
    return os.environ.get("GENESIS_CC_SESSION") == "1"


# git push flags that consume the NEXT token as their value — so a value that
# happens to start with '+' or contain 'f' is not misread as a force.
_PUSH_VALUE_FLAGS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})

# The first-push-only skip uses an ALLOWLIST posture (git's push surface is too
# flexible to blocklist safely): a push qualifies as a "plain current-branch
# update" ONLY if every flag it carries is ref-set-neutral — verbosity, dry-run,
# upstream tracking, transport — never one that changes WHICH refs are pushed.
# Anything outside these sets (``--all`` / ``--tags`` / ``--mirror`` / ``--delete``
# / ``--prune`` / ``--repo`` / ``--stdin`` / ``--follow-tags`` / a bundled ``-d`` /
# any unknown flag) forces the approval prompt.
_PUSH_SAFE_LONG_FLAGS = frozenset(
    {
        "--set-upstream",
        "--verbose",
        "--quiet",
        "--dry-run",
        "--progress",
        "--no-progress",
        "--porcelain",
        "--atomic",
        "--no-atomic",
        "--ipv4",
        "--ipv6",
        "--thin",
        "--no-thin",
    }
)
# Ref-neutral value flags (skip the flag AND its value token). ``--repo`` (redirects
# the push) and ``--receive-pack``/``--exec`` (select a receive-pack PROGRAM that git
# EXECUTES on local/SSH transports — an arbitrary-code vector) are deliberately
# EXCLUDED, so a push carrying any of them falls through to the approval prompt.
_PUSH_SAFE_VALUE_FLAGS = frozenset({"-o", "--push-option"})
# Ref-neutral short-flag letters (for bundles like ``-uq``). ``o`` is handled
# separately (glued push-option value). ``f`` (force) and ``d`` (delete) are absent
# by design — a bundle containing either is not a plain current-branch update.
_PUSH_SAFE_SHORT_LETTERS = frozenset("uvqn46")


def _push_is_force(argv: list[str]) -> bool:
    """Whether a parsed ``git push`` argv performs a force push.

    Argv comes from ``shell_parse`` (quote-stripped), so a quoted ``'-f'``
    counts and a branch/refspec name that merely contains ``-f`` (a bare
    positional) does not. Catches both the flag forms — ``--force`` /
    ``--force-with-lease`` / ``--force-if-includes`` / ``--mirror`` / ``-f`` /
    bundled ``-uf`` — and the ``+<refspec>`` shorthand (``git push origin +main``),
    which git treats as ``--force`` for that ref. A short cluster stops at
    ``o`` and the value token of ``-o`` / ``--push-option`` / ``--repo`` /
    ``--exec`` / ``--receive-pack`` is skipped, so a push-option value that
    starts with ``+`` or contains ``f`` (e.g. ``-oci.skip``) is not mistaken
    for a force.
    """
    i = 1  # skip argv[0] == "git"
    while i < len(argv):
        tok = argv[i]
        if tok in _PUSH_VALUE_FLAGS:
            i += 2  # skip the flag and its value token
            continue
        if tok == "--force" or tok.startswith("--force-"):
            return True
        if tok == "--mirror":
            # --mirror force-updates EVERY ref and deletes remote refs that are
            # absent locally — an unconditional destructive push, never a plain
            # one, so it must hard-block rather than reach an approvable prompt.
            return True
        if tok.startswith("+"):
            return True  # +<refspec> is git shorthand for --force on that ref
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            for ch in tok[1:]:
                if ch == "o":  # -oVALUE glued push-option — rest is its value
                    break
                if ch == "f":
                    return True
        i += 1
    return False


def _push_named_remote(argv: list[str]) -> str | None:
    """The remote named on a ``git push <remote> …``, else None.

    argv is quote-stripped from ``shell_parse``. Skips git global options (and
    their values), the ``push`` token, and push flags/values; the first bare
    positional after that is the remote. A ``+<refspec>`` positional is NOT a
    remote (it is a force refspec) and is skipped.
    """
    i = 1  # skip argv[0] == "git"
    # advance past git global options to the `push` token
    while i < len(argv):
        t = argv[i]
        if t in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(argv) or argv[i] != "push":
        return None
    i += 1
    while i < len(argv):
        t = argv[i]
        if t in _PUSH_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-") or t.startswith("+"):
            i += 1
            continue
        return t  # first bare positional after `push` is the remote
    return None


def _push_repo_flag(argv: list[str]) -> str | None:
    """The value of a ``--repo <value>`` / ``--repo=value`` on a git push, else None.

    ``git push --repo <dest>`` overrides both the positional remote and the
    branch upstream as the push DESTINATION (P1-C), so it must win when deciding
    what a force push actually targets. The value may be a remote name OR a URL.
    """
    i = 0
    while i < len(argv):
        t = argv[i]
        if t == "--repo" and i + 1 < len(argv):
            return argv[i + 1]
        if t.startswith("--repo="):
            return t.split("=", 1)[1]
        i += 1
    return None


def _push_positionals(argv) -> list[str]:
    """The bare positional args of a ``git push`` (``[remote, refspec, ...]``).

    Parsed from a quote-stripped argv, mirroring ``_push_named_remote``: git global
    options/values, the ``push`` token, and push flags/values are all skipped —
    including no-value flags (``-u`` / ``--set-upstream`` / ``--force-with-lease``)
    which take NO separate token, and value flags (``_PUSH_VALUE_FLAGS``:
    ``-o`` / ``--push-option`` / ``--repo`` / ``--receive-pack`` / ``--exec``) which
    take one. A ``+<refspec>`` (force) positional is excluded. Empty if not a push.
    """
    argv = argv or []
    i = 1  # skip argv[0] == "git"
    # advance past git global options (and their values) to the `push` token
    while i < len(argv):
        t = argv[i]
        if t in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(argv) or argv[i] != "push":
        return []
    i += 1
    out: list[str] = []
    while i < len(argv):
        t = argv[i]
        if t in _PUSH_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-") or t.startswith("+"):
            i += 1
            continue
        out.append(t)
        i += 1
    return out


# push.default modes that push the CURRENT branch to a SAME-NAMED remote ref (or
# refuse). `upstream`/`tracking` push to a possibly-differently-named upstream, and
# `matching` pushes every same-named branch — both broaden beyond a plain
# current-branch update. Unset defaults to `simple` (safe), so it need not appear.
_SAFE_PUSH_DEFAULTS = frozenset({"simple", "current"})


def _git_config_get(base: list[str], key: str, *, all_values: bool = False, as_bool: bool = False):
    """Read a git config value. Returns ``(rc, stripped_stdout)``, or ``None`` on a
    read error / timeout / UNEXPECTED return code.

    git config exits 0 when the key is set and 1 when it is absent; any other code
    (bad config file, etc.) is an error the caller must treat as fail-closed — so
    an empty stdout under rc 2/128 is NOT mistaken for "unset". ``as_bool`` reads via
    ``--type=bool`` so every boolean spelling (``TRUE`` / ``on`` / the valueless
    ``[section]\\n\\tkey`` shorthand) normalizes to a canonical ``"true"``/``"false"``.

    NOTE: this reads the effective REPO config; it does NOT see a command-line
    ``git -c key=val`` override on the push itself (tabled residue — the config
    check is best-effort for the common repo-config case, not a hard boundary).
    """
    args = ["config"]
    if as_bool:
        args.append("--type=bool")
    args.append("--get-all" if all_values else "--get")
    args.append(key)
    try:
        r = subprocess.run(base + args, capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode not in (0, 1):
        return None
    return r.returncode, r.stdout.strip()


# push.recurseSubmodules values that do NOT push submodule commits (safe). Anything
# else (on-demand / only / a boolean-true spelling) side-channels a submodule push.
_SAFE_RECURSE_SUBMODULES = frozenset({"no", "false", "off", "0", "check"})


def _push_config_is_simple(remote: str | None, cwd: str | None = None) -> bool:
    """Whether a bare / remote-only push updates ONLY the current branch under the
    effective REPO config — ALLOWLIST posture (mirrors ``_push_targets_current_branch``).

    Broadening/redirecting knobs checked (each case-insensitive):
      • ``remote.<remote>.push`` refspec (``push = HEAD:main`` → sends HEAD to main);
      • ``remote.<remote>.mirror`` (a bare push mirrors ALL refs — force-updates /
        deletes unrelated remote refs);
      • ``push.default`` other than ``simple``/``current`` (``upstream``/``tracking``
        push cur to a differently-named upstream; ``matching`` pushes every
        same-named branch);
      • ``push.recurseSubmodules``/``submodule.recurse`` (side-channel-publishes
        submodule commits).
    Simple ONLY when NONE is set to a broadening value. Fail-closed: an unresolved
    remote, any of the above, or any config-read error → False (prompt).

    BEST-EFFORT, not a hard boundary (`remote` is already resolved with git's
    pushRemote/pushDefault precedence by the caller). It reads REPO config only, so a
    command-line ``git -c key=val push`` override, ``remote.<remote>.mirror``,
    ``push.followTags``, or a ``url.*.pushInsteadOf`` rewrite are NOT caught — tabled
    adversarial residue (each needs a deliberately unusual command / hostile config,
    and `git config` writes are already soft-warned). Bounded by 5s timeouts.
    """
    if not remote:
        return False
    base = ["git"] + (["-C", cwd] if cwd else [])
    # 1. A configured push refspec can redirect/broaden the destination.
    got = _git_config_get(base, f"remote.{remote}.push", all_values=True)
    if got is None:
        return False
    rc, out = got
    if rc == 0 and out:
        return False
    # 1b. remote.<remote>.mirror makes a bare push mirror EVERY ref (force/delete).
    got = _git_config_get(base, f"remote.{remote}.mirror", as_bool=True)
    if got is None:
        return False
    rc, out = got
    if rc == 0 and out == "true":
        return False
    # 2. push.default must be a same-name mode (unset → simple → safe).
    got = _git_config_get(base, "push.default")
    if got is None:
        return False
    rc, out = got
    if rc == 0 and out and out.lower() not in _SAFE_PUSH_DEFAULTS:
        return False
    # 3. Submodule recursion would also publish submodule commits (a side channel).
    got = _git_config_get(base, "push.recurseSubmodules")
    if got is None:
        return False
    rc, out = got
    if rc == 0 and out and out.lower() not in _SAFE_RECURSE_SUBMODULES:
        return False  # on-demand / only / true → pushes submodule commits
    # submodule.recurse is a boolean — read via --type=bool so TRUE / on / the
    # valueless shorthand all normalize to a canonical "true".
    got = _git_config_get(base, "submodule.recurse", as_bool=True)
    if got is None:
        return False
    rc, out = got
    return not (rc == 0 and out == "true")


def _push_targets_current_branch(
    seg, cur: str | None, remote: str | None, cwd: str | None = None
) -> bool:
    """Whether a non-force ``git push`` seg plainly UPDATES the current branch ``cur``.

    ``remote`` is the destination the push will ACTUALLY go to, resolved by the
    caller with git's pushRemote/pushDefault precedence (``_effective_push_remote``).

    ALLOWLIST posture — the branch checked against the remote is always ``cur``
    itself, never a parsed destination. True ONLY when BOTH hold:
      1. Every flag after ``push`` is ref-set-neutral — a member of
         ``_PUSH_SAFE_LONG_FLAGS`` / ``_PUSH_SAFE_VALUE_FLAGS``, or a short bundle
         whose every letter is in ``_PUSH_SAFE_SHORT_LETTERS`` (``o`` = glued
         push-option value). ANY other flag (``--all`` / ``--tags`` / ``--delete``
         / a bundled ``-d`` / ``--stdin`` / ``--repo`` / unknown) → False.
      2. The positionals name a plain current-branch update:
         • ``git push <remote> <cur>`` (no ``src:dst`` colon) → an explicit refspec
           overrides ``remote.push`` / ``push.default`` / ``pushRemote`` → True;
         • bare ``git push`` / ``git push <remote>`` → True only if
           ``_push_config_is_simple(remote)`` (no redirecting/broadening repo config);
         • a colon refspec, a differently-named branch, or ≥2 refspecs → False.
    Conservative by construction: any unrecognized form re-prompts. argv-based
    (quote-stripped).
    """
    if not cur:
        return False
    argv = getattr(seg, "argv", None) or []
    # Advance past git global options to the `push` token.
    i = 1
    while i < len(argv):
        t = argv[i]
        if t in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(argv) or argv[i] != "push":
        return False
    i += 1
    positionals: list[str] = []
    while i < len(argv):
        t = argv[i]
        if t in _PUSH_SAFE_VALUE_FLAGS:
            i += 2  # ref-neutral value flag: skip the flag and its value token
            continue
        if t.startswith("--"):
            base = t.split("=", 1)[0]
            if "=" in t and base in _PUSH_SAFE_VALUE_FLAGS:
                i += 1  # --push-option=value etc.
                continue
            if "=" not in t and base in _PUSH_SAFE_LONG_FLAGS:
                i += 1
                continue
            return False  # unknown/broadening long flag (or a =form of a no-value flag)
        if t.startswith("+"):
            return False  # +<refspec> force shorthand
        if t.startswith("-") and len(t) > 1:
            # Short single/bundle — every letter must be ref-neutral. An `o` starts a
            # glued push-option value, so the rest of the token is that value.
            safe = True
            for ch in t[1:]:
                if ch == "o":
                    break
                if ch not in _PUSH_SAFE_SHORT_LETTERS:
                    safe = False
                    break
            if not safe:
                return False
            i += 1
            continue
        positionals.append(t)
        i += 1
    if len(positionals) >= 3:
        return False  # multiple refspecs → not a single plain current-branch update
    if len(positionals) == 2:
        refspec = positionals[1]
        # Explicit `<remote> <cur>` — an explicit refspec overrides remote.push /
        # push.default / pushRemote, so it is a plain current-branch update.
        return ":" not in refspec and refspec == cur
    # Bare `git push` or `git push <remote>` → the ref set depends on repo config,
    # keyed on the remote git will ACTUALLY push to (resolved by the caller).
    return _push_config_is_simple(remote, cwd=cwd)


def _resolve_push_remote(seg, cwd: str | None = None) -> str | None:
    """The push DESTINATION (remote name or URL) for a segment, or None if UNKNOWN.

    Resolution order: an explicit ``--repo <dest>`` (P1-C) → an explicitly named
    positional remote → else the current branch's upstream remote (the part
    before ``/`` of ``@{upstream}``); else None. Callers FAIL CLOSED on None — an
    undeterminable destination is treated as origin/public and blocked.
    """
    argv = getattr(seg, "argv", None) or []
    repo = _push_repo_flag(argv)
    if repo:
        return repo
    remote = _push_named_remote(argv)
    if remote:
        return remote
    try:
        args = ["git"]
        if cwd:
            args += ["-C", cwd]
        args += ["rev-parse", "--abbrev-ref", "@{upstream}"]
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            upstream = result.stdout.strip()
            if "/" in upstream:
                return upstream.split("/", 1)[0]
    except Exception:
        pass
    return None


def _effective_push_remote(seg, cur: str | None, cwd: str | None = None) -> str | None:
    """The remote a ``git push`` will ACTUALLY push to, honoring git's precedence.

    An explicit ``--repo`` or positional remote wins. For a bare ``git push`` git
    picks, in order: ``branch.<cur>.pushRemote`` → ``remote.pushDefault`` →
    ``branch.<cur>.remote`` (the ``@{upstream}`` remote) → ``origin``. The
    republish + config checks MUST target this remote, not the fetch/upstream
    remote — otherwise a triangular fork workflow (pull from origin, push to fork)
    checks the wrong remote and can silently allow a first push to the fork.
    """
    argv = getattr(seg, "argv", None) or []
    if _push_repo_flag(argv) or _push_named_remote(argv):
        return _resolve_push_remote(seg, cwd=cwd)  # explicit --repo / positional wins
    base = ["git"] + (["-C", cwd] if cwd else [])
    if cur:
        got = _git_config_get(base, f"branch.{cur}.pushRemote")
        if got and got[0] == 0 and got[1]:
            return got[1]
    got = _git_config_get(base, "remote.pushDefault")
    if got and got[0] == 0 and got[1]:
        return got[1]
    return _resolve_push_remote(seg, cwd=cwd) or "origin"


def _looks_like_url(dest: str) -> bool:
    """Whether a push destination is a URL/path rather than a remote NAME."""
    return (
        "://" in dest
        or "@" in dest
        or ":" in dest  # scp-like git@host:path or host:path
        or dest.startswith(("/", "./", "../", "~"))
    )


def _remote_push_urls(name: str, cwd: str | None = None) -> set[str]:
    """The set of PUSH urls configured for a remote NAME (empty if unresolvable).

    git pushes to the PUSH url, which can differ from the fetch url
    (``git remote set-url --push``), so classifying by the fetch url misses a
    public push target (P1-B). ``--push --all`` returns every push url (one per
    line). An empty set means the name is not a resolvable remote → callers FAIL
    CLOSED.
    """
    try:
        args = ["git"]
        if cwd:
            args += ["-C", cwd]
        args += ["remote", "get-url", "--push", "--all", name]
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return {ln.strip() for ln in result.stdout.splitlines() if ln.strip()}
    except Exception:
        pass
    return set()


def _push_dest_urls(dest: str, cwd: str | None = None) -> set[str]:
    """Push-url set for a destination that may be a remote NAME or a raw URL/path.

    A known remote name resolves to its ``--push`` urls; otherwise, if the
    destination looks like a URL/path (e.g. a ``--repo https://…`` value), it IS
    the target url. An unresolvable name yields an empty set (fail closed).
    """
    urls = _remote_push_urls(dest, cwd)
    if urls:
        return urls
    if _looks_like_url(dest):
        return {dest}
    return set()


def _remote_branch_sha(remote: str, branch: str, cwd: str | None = None) -> str | None:
    """The remote's tip sha for EXACTLY ``refs/heads/<branch>``, or None.

    Queries the LIVE remote via ``git ls-remote`` (not a local remote-tracking
    ref, which goes stale the moment a remote branch is deleted). Accepts only the
    line whose ref path is EXACTLY ``refs/heads/<branch>`` — a bare pattern
    tail-matches namespaced refs. Fail-safe: None on rc!=0 / timeout / any error /
    absent branch — callers treat None as "not confirmed present". Bounded by a 10s
    timeout inside the hook's 60s budget.
    """
    try:
        args = ["git"] + (["-C", cwd] if cwd else []) + ["ls-remote", "--heads", remote, branch]
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        target_ref = f"refs/heads/{branch}"
        for ln in result.stdout.splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[1] == target_ref:
                return parts[0]
    except Exception:
        pass
    return None


def _push_is_republish(remote: str | None, branch: str | None, cwd: str | None = None) -> bool:
    """Whether this push targets a branch ALREADY on ``remote`` (a re-push).

    A branch's FIRST push creates it on the remote (and prompted the user for
    approval); a re-push updates that already-published branch. So a branch present
    on the remote was approved on its first push and must not re-prompt. Fail-safe:
    an unresolved remote/branch, or any ls-remote error/timeout/absence, → False
    (fall through to the approval prompt). Deliberately does NOT check for unpushed
    commits — re-pushing new fixes to an already-published branch is exactly the
    case we allow.
    """
    if not remote or not branch:
        return False
    return _remote_branch_sha(remote, branch, cwd=cwd) is not None


def _ask(reason: str) -> int:
    """Emit a PreToolUse ``ask`` decision — a native approve/deny dialog.

    Prints the hook JSON to stdout and returns 0; Claude Code shows the user a
    permission prompt and runs the tool only on explicit approval — a gate the
    agent cannot self-satisfy. Verified to render in a wrapped child session
    2026-07-27.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


def _allow(reason: str) -> int:
    """Emit a PreToolUse ``allow`` decision — auto-approve, no prompt.

    Prints the hook JSON to stdout and returns 0. An ``allow`` decision bypasses
    Claude Code's own permission prompt, unlike a bare exit-0 (which leaves the
    command to the normal permission flow — and a non-allow-listed ``gh pr create``
    would then still prompt). Used ONLY on the verified-safe pr-create path (the
    branch is already on the remote, or an explicit ``--head`` means gh cannot
    push), so opening the PR rides on the push's approval instead of demanding its
    own.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


def _pr_create_head_raw(argv: list[str]) -> str | None:
    """The RAW ``--head``/``-H`` value (``owner:`` prefix intact), or None.

    Returns the LAST occurrence, mirroring gh's pflag last-value-wins semantics
    for a repeated string flag — ``--head real --head=`` resolves to ``""`` in gh,
    so this must too, else a stale earlier value could wrongly look like a real
    head. ``None`` = no ``--head`` given at all (distinct from an empty value)."""
    result: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--head", "-H") and i + 1 < len(argv):
            result = argv[i + 1]
        elif tok.startswith("--head="):
            result = tok.split("=", 1)[1]
        i += 1
    return result


def _pr_create_would_publish(argv: list[str]) -> bool:
    """Whether a ``gh pr create`` might PUSH/fork its branch (bypassing the push gate).

    Per the gh manual (``gh pr create --help``): only a create with NO ``--head``
    can publish — "when the current branch isn't fully pushed to a git remote, a
    prompt will ask where to push the branch and offer an option to fork ... Use
    ``--head`` to explicitly skip any forking or pushing behavior." So:

    * A NON-EMPTY, plausibly-LITERAL explicit ``--head`` (local, unpushed, or
      ``owner:fork``) → gh does NOT push/fork; it references the head ref as-is
      (erroring if absent) → cannot publish code around this hook → **un-gate**.
      (gh only takes this skip-push path when its resolved ``HeadBranch != ""``.
      An EMPTY head — ``--head=`` / ``--head ""``, or a repeated flag whose LAST
      value is empty — is gh's implicit path, and a value carrying shell-expansion
      metacharacters (``$VAR`` / ``$(...)`` / backticks) could resolve to empty at
      runtime and we can't see through it; both are NOT trusted as a real head and
      fall through to the implicit verification below.)
    * No ``--head`` (or an empty one) → gh may push the CURRENT branch when it
      isn't fully on the remote. Verified against the ACTUAL remote with ``git
      ls-remote`` — not the LOCAL remote-tracking ref, which goes stale the moment
      a merged branch is deleted (a squash-merge auto-delete leaves
      refs/remotes/origin/<branch> pointing at a gone branch, so a local-ref check
      would wrongly report "already pushed"). Accept only the ls-remote line whose
      ref path is EXACTLY ``refs/heads/<branch>`` (a bare pattern tail-matches
      namespaced refs).

    Assumes origin's push and fetch destinations coincide (standard single-remote
    setup). Fail-safe — any uncertainty (network error, timeout, current branch
    not on the remote, unpushed commits, detached HEAD) → True (gate). The
    ls-remote call is bounded by a 10s timeout inside the hook's 60s budget; a
    hung/slow remote fails closed, never open.
    """
    raw_head = _pr_create_head_raw(argv)
    if raw_head and "$" not in raw_head and "`" not in raw_head:
        # A NON-EMPTY, plausibly-LITERAL --head → gh skips push/fork → un-gate.
        # An empty head (--head= / --head "") is gh's implicit path; a value with
        # shell-expansion metacharacters ($VAR / $(...) / `...`) could resolve to
        # empty (→ implicit push) at runtime and we can't see through it — both
        # fall through to the live current-branch verification below (fail-safe).
        return False
    branch = _current_branch()
    if not branch:
        return True  # detached / unknown current branch → can't verify → gate
    try:
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not head_sha:
            return True  # can't resolve HEAD → gate
        # Live-remote check via ls-remote (exact refs/heads/<branch>), shared with
        # the push republish gate. None ⇒ unreachable OR branch absent → gate.
        remote_sha = _remote_branch_sha("origin", branch)
        if not remote_sha:
            return True  # cannot reach the remote, or branch not on it → gh would push → gate
        if head_sha == remote_sha:
            return False  # current branch tip is on the remote → nothing to push
        # HEAD differs from the remote tip: un-gate only if HEAD is already
        # contained in the remote branch (needs the object locally; if absent,
        # is-ancestor fails → gate).
        contained = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", remote_sha],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
        return not contained  # unpushed commits on HEAD → gh may push → gate
    except Exception:
        return True  # fail-safe → gate


def main() -> int:
    try:
        payload = read_payload()
        cmd = field(payload, "command")
        if not cmd:
            return 0

        # Analyze the command into the segments it actually executes (wrappers
        # like sudo/env and /path/to/git stripped, nested `bash -c` recursed,
        # quoted mentions excluded). Each guarded subcommand is matched on real
        # argv, and the `# review-override` approval binds to its OWN segment.
        segs = analyze(cmd)

        push_segs = [s for s in segs if s.exe == "git" and git_subcommand(s.argv) == "push"]
        merge_git_segs = [s for s in segs if s.exe == "git" and git_subcommand(s.argv) == "merge"]
        create_segs = [s for s in segs if gh_pr_subcommand(s.argv) == "create"]
        merge_pr_segs = [s for s in segs if gh_pr_subcommand(s.argv) == "merge"]

        # ── Blind-spot net: unverifiable near a gated op → ask a human ──────
        # analyze()/_argv degrade to a naive split SILENTLY, so an empty segment
        # list is NOT evidence that no gated command is present: an ANSI-C
        # `$'…\'…'` span, or an apostrophe in a here-doc body, is enough to drop
        # a real, executing `git push --force` from the parse (reproduced on both
        # guards). When the raw text names a gated op, the command will not
        # tokenize, and the parse surfaced NO matching segment, the verdict is
        # "unknown" — which earns a human decision, not a silent allow.
        #
        # ASK rather than BLOCK is load-bearing. A refusal has to be surgically
        # precise about which unparseable commands are real, and precision is
        # exactly what an unreliable parse cannot deliver — every narrowing
        # conjunct became a new way to starve the trigger, while over-blocking
        # broke benign shapes. Asking inverts the costs: a false positive is one
        # confirmation, a miss is the pre-existing status quo. That is what lets
        # the predicate stay broad.
        #
        # The reason is DEFERRED to the tail (like ask_reason / push_allow_reason
        # above) so every hard block below — sqlite writes, --no-verify, the
        # dispatched publish denies, the escalation cap — still takes precedence.
        # Returning here would DOWNGRADE those to a prompt (measured).
        # The segment check names ALL FOUR gated ops, not the three the first cut
        # listed. HONEST SCOPE: `create_segs` here is symmetry, not a live guard.
        # Mutation-tested — removing it changes no verdict in any of the four
        # cells (interactive/dispatched x parsed-create/parsed-create-plus-
        # untokenizable), because a create the parser DID resolve is answered by
        # the real create gate first, and this net's verdict is deferred to the
        # tail behind it. Kept so the condition states the whole set rather than
        # an accidental subset, which is what let `create` fall out of the
        # mention test in the first place. Do not cite it as the thing that
        # prevents a double-prompt; that is the deferral's doing.
        blind_spot_reason: str | None = None
        if (
            not (push_segs or merge_pr_segs or merge_git_segs or create_segs)
            and untokenizable(cmd)
            and _mentions_gated_op(cmd)
        ):
            if _is_dispatched():
                # No human is present to answer a prompt, and an unverifiable
                # gated command must not proceed unattended. Mirrors the
                # dispatched deny legs on the push / pr-create asks below.
                print(
                    "BLOCKED: this command cannot be parsed safely (e.g. "
                    "ANSI-C $'...' quoting) and names a gated operation. "
                    "Autonomous sessions cannot proceed on an unverifiable "
                    "command — rewrite it in a directly-parseable form.",
                    file=sys.stderr,
                )
                return 2
            blind_spot_reason = (
                "This command could not be parsed safely (e.g. ANSI-C $'...' "
                "quoting) and mentions a gated operation (push / merge / "
                "gh pr create / --force / --no-verify / --admin), so the guard "
                "cannot verify "
                "what it would actually run. Approve only if you are sure. To "
                "avoid the prompt, rewrite it in a directly-parseable form "
                "(plain quotes, or -F <file>)."
            )

        # Each git push / gh pr merge is a SEPARATE gated action. A single Bash
        # command carrying more than one would collapse into ONE ask/gate
        # (evaluated only for the first), so approving it would run every push —
        # and every UNCHECKED merge — behind it. Reject the compound and require
        # each to be its own separately-gated tool call. (Merges are included so
        # `gh pr merge 1 --admin && gh pr merge 2 --admin` can't smuggle the
        # second past the CI/review gates, which only inspect the first segment.
        # gh pr create is un-gated (#1241) — a review request on already-pushed
        # code, riding a push's approval — so it is NOT counted here; the
        # exception where gh itself would push an unpushed branch is handled by
        # the create arm below.)
        if len(push_segs) + len(merge_pr_segs) > 1:
            print(
                "BLOCKED: multiple publish/merge operations (git push / gh pr "
                "merge) in one command would share a single gate. Run each as "
                "its own command so each is gated separately.",
                file=sys.stderr,
            )
            return 2

        # ── Codex round-escalation gate (`gh pr comment … @codex review`) ──
        # Once a PR already carries ESCALATION_ROUND_CAP Codex reviews,
        # requesting another round is the whack-a-mole moment — force the
        # step-back (triage / mechanism / state-space) before round N+1.
        # Fail-open inside the check; '# escalation-ack' is the conscious
        # continue after a fresh user decision.
        esc_block, esc_msg = _check_codex_round_escalation(segs)
        if esc_block:
            print(esc_msg, file=sys.stderr)
            return 2

        # An interactive push defers to a native approve/deny dialog at the END
        # of main(), so every hard-block below (merge-into-main, the pr-merge
        # gates, sqlite, --no-verify) still takes precedence — a compound
        # `git push && git commit --no-verify` blocks, never asks.
        ask_reason: str | None = None
        # A first-push-only re-push AUTO-ALLOW is ALSO deferred to the END (same
        # reason): emitting `_allow` inline would short-circuit the whole Bash
        # invocation before the hard-blocks run, so `git push <republish> && git
        # commit --no-verify` would sail through. Set the reason here; emit at the tail.
        push_allow_reason: str | None = None

        # ── git push (any branch) ──────────────────────────────────
        # Interactive → the user approves in a dialog only they can satisfy.
        # Dispatched/autonomous → hard-denied (no human to ask; real autonomous
        # delivery goes through the scope-gated server path, not the CC Bash
        # tool). The old `# review-override` token is dropped for push: the
        # dialog replaces it, so the agent can no longer self-approve.
        if push_segs:
            _pcwd = _effective_cwd(cmd, payload, seg=push_segs[0])
            pcwd_unknown = _pcwd is _CWD_UNKNOWN
            pcwd = _pcwd if isinstance(_pcwd, str) else None
            # Force push is destructive. argv-based force detection (via
            # shell_parse) so a quoted `'-f'` / `--force-with-lease` / bundled
            # `-uf` / `+refspec` all still count and cannot evade into a generic
            # prompt.
            #
            # SECURITY INVARIANT: a force push is HARD-BLOCKED in ALL sessions
            # whenever it targets the public repo. "Public" is decided by PUSH URL,
            # not remote name — an explicit `origin`, an UNKNOWN/None destination,
            # an ambiguous cwd, a `--repo origin` (P1-C), OR a differently-named
            # remote whose PUSH url set INTERSECTS origin's (e.g. `git remote add
            # mirror <origin-url>`, or a `set-url --push` to origin's url, P1-B)
            # all count as public ⇒ blocked (fail closed). Only a destination
            # whose push urls resolve AND are DISJOINT from origin's gets the
            # softer cautious-ask path — interactive asks, dispatched denies.
            force_segs = [s for s in push_segs if _push_is_force(s.argv)]
            if force_segs:
                remote = _resolve_push_remote(force_segs[0], cwd=pcwd)
                if pcwd_unknown or remote is None or remote == "origin":
                    print(
                        "BLOCKED: Force push to origin/<public> is not allowed — open a PR.",
                        file=sys.stderr,
                    )
                    return 2
                # Classify by PUSH-url set — a non-"origin" name/url that shares any
                # push url with origin is still a public force. Unresolvable ⇒ block.
                dest_urls = _push_dest_urls(remote, cwd=pcwd)
                origin_urls = _remote_push_urls("origin", cwd=pcwd)
                if not dest_urls or not origin_urls or (dest_urls & origin_urls):
                    print(
                        "BLOCKED: Force push to origin/<public> is not allowed — open a PR.",
                        file=sys.stderr,
                    )
                    return 2
                # Definitely a different repo from origin → cautious, never silent.
                if _is_dispatched():
                    print(
                        f"BLOCKED: force push rewrites remote history on "
                        f"'{remote}'; autonomous/dispatched sessions cannot "
                        f"force-push.",
                        file=sys.stderr,
                    )
                    return 2
                ask_reason = (
                    f"FORCE push detected — this REWRITES remote history on "
                    f"'{remote}' (a non-origin remote). Approve only if you "
                    f"intend to rewrite that remote's history."
                )
                # Fall through: any hard-block below still takes precedence.
            else:
                # Non-force push: interactive asks, dispatched hard-denies.
                _remote, branch = _get_push_remote_and_branch(push_segs[0], cwd=pcwd)
                if _is_dispatched():
                    print(
                        f"BLOCKED: git push requires user approval before "
                        f"publishing code externally (target: {branch or 'default'}).",
                        file=sys.stderr,
                    )
                    print(
                        "Autonomous/dispatched sessions cannot push directly — "
                        "delivery goes through the scope-gated server path.",
                        file=sys.stderr,
                    )
                    return 2
                # Prompt only on the FIRST push of a branch/PR. A branch already on
                # the remote was published — and approved — on its first push, so a
                # re-push of fixes to the SAME branch must not re-prompt (user
                # directive 2026-07-30). The check is deliberately narrow — it fires
                # ONLY for a plain update of the CURRENT branch, and the ref checked
                # against the remote is always ``cur`` itself (never a parsed
                # destination). Each guard fails safe toward the prompt:
                #   • not pcwd_unknown → an ambiguous cwd can't resolve the branch;
                #   • cur truthy → a real (non-detached) current branch;
                #   • cur not in (main, master) → a push to the default branch never
                #     goes silent (it is always on the remote);
                #   • _push_targets_current_branch → a bare / `<remote>` / `<remote>
                #     <cur>` push with only ref-neutral flags and (for bare/remote-only)
                #     a simple repo config — everything else prompts;
                #   • _push_is_republish → live ls-remote confirms `cur` is present on
                #     the remote git will ACTUALLY push to (pushRemote/pushDefault
                #     resolved by _effective_push_remote, so a triangular fork workflow
                #     checks the fork, not the upstream).
                # Dispatched sessions were hard-denied above, so this _allow is
                # unreachable for them (the human-not-agent boundary is preserved;
                # this only relaxes RE-approval of an already-approved branch).
                cur = _current_branch(cwd=pcwd)
                push_remote = _effective_push_remote(push_segs[0], cur, cwd=pcwd)
                if (
                    not pcwd_unknown
                    and cur
                    and cur not in ("main", "master")
                    and _push_targets_current_branch(push_segs[0], cur, push_remote, cwd=pcwd)
                ):
                    # This push plainly updates the current branch `cur`, so it
                    # qualifies for the first-push-only relaxation IF `cur` is
                    # already on the remote. Decide that via the LOCAL allowlist
                    # first (offline — immune to the transient ls-remote failure
                    # that otherwise fail-closes _push_is_republish to a re-prompt),
                    # then fall back to the live ls-remote leg. On an ls-remote HIT,
                    # RECORD the confirmed-on-remote fact so later re-pushes decide
                    # offline. SECURITY: the allowlist is written ONLY here, ONLY on
                    # an ls-remote HIT (which proves `cur` is on the remote → its
                    # first push was already approved), so it can never authorize a
                    # genuine first push; a broken/absent push_allowlist degrades to
                    # the pure ls-remote path (import guarded to None above).
                    urls = _remote_push_urls(push_remote, cwd=pcwd) if push_remote else set()
                    # Deferred (NOT an inline return) so any hard-block in a compound
                    # command still takes precedence — see push_allow_reason above.
                    if push_allowlist is not None and push_allowlist.is_recorded(urls, cur):
                        push_allow_reason = (
                            f"re-push to '{cur}' (recorded as already on the remote — "
                            f"approved on its first push); only the first push prompts."
                        )
                    elif _push_is_republish(push_remote, cur, pcwd):
                        if push_allowlist is not None and urls:
                            push_allowlist.record(urls, cur)
                        push_allow_reason = (
                            f"re-push to '{cur}' (already on the remote — approved on "
                            f"its first push); only the first push of a branch/PR prompts."
                        )
                    else:
                        ask_reason = (
                            f"git push needs your approval before publishing externally "
                            f"(target: {branch or 'default'})."
                        )
                else:
                    ask_reason = (
                        f"git push needs your approval before publishing externally "
                        f"(target: {branch or 'default'})."
                    )

        # ── git merge into main ─────────────────────────────────────
        # Worktree-aware AND compound-aware: EVERY git-merge in the command is
        # checked in the dir it actually runs (git -C / the last cd before it /
        # payload cwd), NOT just the first segment and NOT the hook's own cwd — so
        # a feature-branch merge cannot chaperone a second bare `git merge` into
        # main. A per-segment `# merge-to-main-override` acknowledges an intended
        # on-main merge; an ambiguous cwd fails closed (blocked). See
        # _walk_merge_into_main.
        if merge_git_segs and _walk_merge_into_main(cmd, payload, merge_git_segs):
            print(
                "BLOCKED: Merging into main directly is not allowed.",
                file=sys.stderr,
            )
            print(
                "Use the PR workflow instead.",
                file=sys.stderr,
            )
            return 2

        # ── gh pr create ────────────────────────────────────────────
        # Un-gated when its branch is already on the remote — opening a PR is then
        # just a review request on already-pushed code, so `git push && gh pr
        # create` prompts once (for the push) and the create rides along. BUT a
        # bare create from an UNPUSHED branch makes gh push (and possibly fork) the
        # branch itself — a code-publish that would bypass the push gate — so gate
        # that form like a push: dispatched → deny, interactive → ask.
        if create_segs and any(_pr_create_would_publish(s.argv) for s in create_segs):
            if _is_dispatched():
                print(
                    "BLOCKED: this gh pr create would push a not-yet-pushed branch; "
                    "autonomous/dispatched sessions cannot publish code.",
                    file=sys.stderr,
                )
                return 2
            if ask_reason is None:
                ask_reason = (
                    "gh pr create would push this (not-yet-pushed) branch — approve it like a push."
                )

        # ── gh pr merge ────────────────────────────────────────────
        if merge_pr_segs:
            merge_seg = merge_pr_segs[0]
            if "--admin" not in merge_seg.argv:
                print(
                    "BLOCKED: gh pr merge without --admin is not allowed.",
                    file=sys.stderr,
                )
                print(
                    "Use: gh pr merge --squash --admin",
                    file=sys.stderr,
                )
                return 2

            # Check mergeable status before allowing merge. Merge is
            # the one command that fails CLOSED: if we can't tell which
            # PR this is, we can't run the gates, so we don't merge.
            # An explicit --repo/-R (or PR URL) retargets EVERY gate below —
            # without it, merging a cross-repo PR checked the CWD repo's
            # same-numbered PR (wrong-repo gate; 2026-07-26 incident).
            # Pass the merge SEGMENT's raw text (not the whole compound command)
            # so an unrelated segment's PR URL cannot select the gated repo
            # (`echo …/other/repo/pull/9 && gh pr merge 12` must gate the cwd repo).
            #
            # Arm the SHARED merge-path deadline BEFORE the first gh call: every gate's
            # gh subprocess (repo/PR resolution → mergeable → CI → base → freshness →
            # findings) reads it via _gh_timeout so the AGGREGATE finishes under the
            # hook's ~60s wall-clock, instead of summing per-call caps past it and
            # getting SIGKILLed mid-gate (Codex P1 #1373). Budget < 60s with headroom.
            # Idempotent: the escalation gate may have already armed it for the same
            # command (round-6 P1) — reuse that deadline so the two gates share ONE
            # aggregate budget, never re-extend it here.
            global _merge_deadline
            if _merge_deadline is None:
                _merge_deadline = time.monotonic() + _MERGE_GATE_BUDGET_S
            merge_repo = _merge_target_repo(merge_seg.argv, merge_seg.raw)
            # For a DERIVED (no --repo) merge, the dir gh resolves the repo AND a
            # numberless branch-PR from — threaded to _resolve_pr_number so a
            # numberless `gh pr merge` still resolves (an explicit --repo would
            # error there). None ⇒ an explicit --repo or the legacy no-cwd path.
            merge_cwd: str | None = None
            if merge_repo is None:
                # No explicit --repo/-R: gh resolves the repo from the merge's
                # EFFECTIVE cwd (payload cwd + any preceding `cd`), which can
                # differ from the HOOK process's cwd — so the gates' gh queries
                # (run in the hook's cwd) could check a DIFFERENT repo than the
                # one gh merges in (a bare merge from another checkout, or after
                # a `cd`). Derive the repo gh will actually target and gate THAT.
                # Fail CLOSED only when the cwd is ambiguous (_CWD_UNKNOWN) or gh
                # can't resolve a repo there; no cwd info at all (None) keeps
                # today's cwd-based behavior — there is nothing to derive from.
                eff_cwd = _effective_cwd(cmd, payload, seg=merge_seg)
                if eff_cwd is _CWD_UNKNOWN:
                    merge_repo = _REPO_UNRESOLVED
                elif eff_cwd is not None:
                    merge_repo = _derive_repo_from_cwd(eff_cwd) or _REPO_UNRESOLVED
                    merge_cwd = eff_cwd  # resolve a numberless PR in the same dir
            if merge_repo is _REPO_UNRESOLVED:
                print(
                    "BLOCKED: cannot determine which repository this merge targets "
                    "(an unresolvable --repo/-R value, an enterprise host, an odd "
                    "URL, or an ambiguous working directory).",
                    file=sys.stderr,
                )
                print(
                    "Append --repo OWNER/REPO so the CI/review gates check the "
                    "right repository, not the current directory's.",
                    file=sys.stderr,
                )
                return 2
            pr_num = _resolve_pr_number(cmd, repo=merge_repo, cwd=merge_cwd)
            if pr_num is None:
                print(
                    "BLOCKED: cannot resolve which PR this merges "
                    "(no number in the command and no open PR for the "
                    "current branch).",
                    file=sys.stderr,
                )
                print(
                    "Specify the PR number: gh pr merge <N> --squash --admin",
                    file=sys.stderr,
                )
                return 2
            if pr_num:
                # ── Merge-path gh TIMEOUT BUDGET ──────────────────────────
                # This hook runs under a 60s CC wall-clock (settings.json). A
                # wall-clock overrun SIGKILLs the hook MID-GATE, which "fails
                # toward tool-runs" — i.e. it silently disengages EVERY merge
                # gate at once, precisely when the GitHub API is degraded. So
                # each sequential gh call on this path — INCLUDING the
                # pre-gate repo/PR resolution above (6s each) — carries a tight
                # per-call timeout (6-8s): pre-gates + fail-closed gates
                # (derive 6 + resolve 6 + mergeable 8 + ci 8 + base 6+6 +
                # freshness 6+8 + delta 8 = 62s absolute worst; the FORCE
                # branch swaps freshness+delta for its hook-surface evidence
                # reads, files 8 + head 6 = strictly less) each reach
                # their own block/allow decision at or inside the budget, and
                # any ONE of them timing out fail-closes IMMEDIATELY (the
                # additive worst case needs every call slow-but-successful);
                # the TOCTOU binding runs argv-only right after freshness, so
                # only the tail advisory scanners (fail-OPEN by design) could
                # ever be clipped, which nets the same outcome as their error
                # path.
                # A timing-out fail-closed gate returns BLOCK immediately, so
                # the additive worst case needs every call slow-but-successful.
                mergeable = _check_mergeable(pr_num, repo=merge_repo)
                if mergeable == "CONFLICTING":
                    print(
                        f"BLOCKED: PR #{pr_num} has merge conflicts. Resolve before merging.",
                        file=sys.stderr,
                    )
                    print(
                        "A conflicting branch ALSO suppresses all pull_request CI (GitHub "
                        "cannot build the merge ref), so no CI runs until you merge the "
                        "base branch (usually main) into your PR branch — that resolves "
                        "both at once.",
                        file=sys.stderr,
                    )
                    return 2
                if mergeable != "MERGEABLE":
                    # Allowlist (fail-CLOSED): anything that is not a definite
                    # MERGEABLE is unverifiable — UNKNOWN (GitHub still computing
                    # conflicts), None/"" (the query FAILED; _check_mergeable
                    # fails OPEN to None), or an unrecognized future state. The
                    # old `== "UNKNOWN"` check let None/"" sail through and merge.
                    # Retry is the remedy (a transient gh hiccup resolves).
                    print(
                        f"BLOCKED: PR #{pr_num} mergeable status is "
                        f"'{mergeable or 'unreadable'}', not MERGEABLE.",
                        file=sys.stderr,
                    )
                    print(
                        "GitHub may still be computing it, or the query failed. Wait and "
                        "retry. A conflicting or still-computing branch also suppresses "
                        "pull_request CI, so if CI never appears check "
                        f"`gh pr view {pr_num} --json mergeable` first.",
                        file=sys.stderr,
                    )
                    return 2

                # CI-status gate. On an unprotected default branch,
                # mergeStateStatus=CLEAN is NOT a CI verdict (it only means "no
                # conflict / no REQUIRED check blocking", and nothing is required
                # when the branch is unprotected). So --admin merges would sail
                # past a red `test` job — exactly how main was broken on
                # 2026-07-28. Block red/pending CI unless a conscious, separate
                # `# ci-override` is appended (never waived by --admin or
                # # review-override). Fail-OPEN on "unknown" (a read we could not
                # complete) so a transient API hiccup can't wedge merges — but
                # fail-CLOSED on "absent" (a readable EMPTY check set = CI never ran)
                # ON THE CANONICAL REPO (where CI always runs), so a conflicting branch
                # or dropped pull_request trigger can't slip an un-CI'd merge through
                # (handled just below).
                ci_state, ci_bad = _pr_ci_status(pr_num, repo=merge_repo)
                ci_override = has_trailing_override(merge_seg.raw, "ci-override")
                if ci_state in ("red", "pending") and not ci_override:
                    print(
                        f"BLOCKED: PR #{pr_num} CI is {ci_state.upper()} "
                        f"({', '.join(ci_bad[:6])}).",
                        file=sys.stderr,
                    )
                    print(
                        f"Do NOT merge red/pending CI. Wait for green "
                        f"(gh pr checks {pr_num}). If these checks are a known, "
                        "documented pre-existing flake you are consciously "
                        "accepting, append a trailing '# ci-override' to merge "
                        "anyway (logged).",
                        file=sys.stderr,
                    )
                    return 2
                if ci_state in ("red", "pending"):
                    print(
                        f"NOTE: CI {ci_state.upper()} on #{pr_num} "
                        f"({', '.join(ci_bad[:6])}) — merging via # ci-override "
                        "(consciously accepted).",
                        file=sys.stderr,
                    )
                # "absent" = a readable EMPTY check set (CI never ran). On the canonical
                # public repo CI ALWAYS runs, so an empty set is anomalous → fail-CLOSED
                # (a conflicting branch / dropped pull_request trigger would otherwise
                # merge un-CI'd). Off the canonical repo (may legitimately have no CI)
                # this stays fail-OPEN. Waived by the same conscious `# ci-override`.
                # Scoped via the canonical-repo test (shared with the scheduled gate).
                elif ci_state == "absent" and _scheduled_gate_applies(merge_repo):
                    if not ci_override:
                        print(
                            f"BLOCKED: No CI checks have run on PR #{pr_num} (an empty "
                            "check set). On the canonical repo CI always runs, so "
                            "pull_request CI never fired — most often the branch was "
                            "CONFLICTING (merge origin/main into it to resolve BOTH) or a "
                            "trigger was dropped (push a commit to re-fire). Never merge "
                            "an un-CI'd PR. If you are intentionally merging with no CI, "
                            "append a trailing '# ci-override' (logged).",
                            file=sys.stderr,
                        )
                        return 2
                    print(
                        f"NOTE: No CI checks on #{pr_num} — merging via # ci-override "
                        "(consciously accepted).",
                        file=sys.stderr,
                    )
                # "incomplete" = a NON-empty rollup whose present checks are green, but
                # a REQUIRED workflow (default "CI") contributed no verdict — e.g. a
                # lone green CodeQL after a workflow-specific trigger drop (#1484-P2).
                # Same scoping + valve as "absent": canonical repo only, # ci-override.
                elif ci_state == "incomplete" and _scheduled_gate_applies(merge_repo):
                    if not ci_override:
                        print(
                            f"BLOCKED: required CI workflow(s) missing from PR "
                            f"#{pr_num}'s check rollup: {', '.join(ci_bad[:6])}. The "
                            "checks that ARE present are green, but the required "
                            "workflow never ran — most often a workflow-specific "
                            "trigger drop (push a commit or re-run the workflow to "
                            "re-fire) or a fully-skipped suite. The required set comes "
                            "from merge_gate.required_ci_workflows in "
                            "~/.genesis/config/genesis.yaml (default: CI). If you are "
                            "intentionally merging without it, append a trailing "
                            "'# ci-override' (logged).",
                            file=sys.stderr,
                        )
                        return 2
                    print(
                        f"NOTE: required CI workflow(s) missing on #{pr_num} "
                        f"({', '.join(ci_bad[:6])}) — merging via # ci-override "
                        "(consciously accepted).",
                        file=sys.stderr,
                    )

                force_override = merge_seg.override
                # The review-CONTEXT waiver (freshness + base gates) is a SEPARATE
                # sigil from # review-override: the freshness gate is the
                # high-traffic one (Codex never auto-re-reviews a push), and if its
                # escape also waived the P1 finding scans, the path of least
                # resistance would systematically disarm P1 enforcement (Codex P1 +
                # architect SHOULD-FIX on #1366). One trailing comment may carry
                # both sigils when both waivers are genuinely intended.
                stale_override = has_trailing_override(merge_seg.raw, "stale-review-override")

                # Base-branch invariant: a PR retargeted AFTER Codex reviewed it
                # keeps the SAME head oid, so head-freshness alone can't see the
                # base change that may have altered the effective diff. Require
                # base == the repo's default branch. Waived by
                # # stale-review-override for a deliberate stacked/non-default PR.
                should_block, base_msg = _check_base_is_default(
                    pr_num,
                    force=stale_override,
                    repo=merge_repo,
                )
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} — base branch is not the repo default.",
                        file=sys.stderr,
                    )
                    print(base_msg, file=sys.stderr)
                    return 2

                # Pin receipts. This is the AUTHORITY for the two release gates
                # (changelog read, local-first soak), deliberately not a CI
                # status: the PR body stays mutable after a check run finishes,
                # so only a merge-time read describes the body that merges. It
                # also runs main's copy of the checker, so a PR cannot edit the
                # code that gates it.
                #
                # NO override sigil, deliberately. Every sigil here waives exactly
                # ONE gate so a waiver cannot silently disarm an unrelated one, so
                # reusing # review-override (which waives the FINDING scans) would
                # be exactly that. And a dedicated sigil would be an escape from a
                # demand that takes seconds to satisfy honestly: if a gate really
                # was not run, the action is to run it, not to wave it through.
                # Incident recovery is already covered — a BACKWARD pin is exempt
                # by construction, with no syntax to recall under pressure.
                should_block, receipts_msg = _check_pin_receipts(pr_num, repo=merge_repo)
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} — CC pin moves forward without its gate receipts.",
                        file=sys.stderr,
                    )
                    print(receipts_msg, file=sys.stderr)
                    return 2

                # Codex must have reviewed the CURRENT head (existence + freshness)
                # — not merely have no open findings. This runs BEFORE the finding
                # scans below: a review published between the scans and this check
                # would otherwise pass freshness while its own P1 comments went
                # unscanned (the scans came back empty). Freshness first, then
                # findings. A not-yet-reviewed head blocks; a stale-reviewed head
                # blocks unless the unreviewed delta is provably review-trivial
                # (smart-delta — see _check_codex_reviewed_head). Waived by
                # # stale-review-override (NOT # review-override).
                should_block, fresh_msg, verified_head = _check_codex_reviewed_head(
                    pr_num,
                    force=stale_override,
                    repo=merge_repo,
                )
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} — Codex has not reviewed the current head.",
                        file=sys.stderr,
                    )
                    print(fresh_msg, file=sys.stderr)
                    return 2

                # Bind the MERGE to the verified head (TOCTOU — Codex P1): a push
                # landing between the check above and the merge would otherwise
                # merge an UNREVIEWED head under a stale verification. GitHub
                # enforces `--match-head-commit` server-side (the merge is
                # rejected if the head moved), making check→merge atomic. Only
                # engaged when the freshness check ran (not # stale-review-override).
                # Placed IMMEDIATELY after the freshness gate — BEFORE the two
                # network scanners below — so a hook wall-clock SIGKILL during a slow
                # scan can never skip the binding enforcement (it needs only argv +
                # verified_head, no gh call). The scanners now fail CLOSED on a clipped
                # budget, but a SIGKILL kills the whole hook (which "fails toward tool
                # runs"), so the binding — the one gate that closes the unbound-merge
                # TOCTOU race — must run first, while budget is guaranteed.
                if verified_head:
                    bind_msg = _require_match_head(
                        merge_seg.argv, pr_num, verified_head, merge_repo, "Codex-verified"
                    )
                    if bind_msg:
                        print("BLOCKED: " + bind_msg, file=sys.stderr)
                        return 2

                # Unresolved review findings (review body) — AFTER freshness +
                # binding (see the ordering note above). Waived by
                # # review-override (the FINDINGS sigil — not the stale one).
                should_block, review_msg = _check_pr_review_findings(
                    pr_num,
                    force=force_override,
                    repo=merge_repo,
                )
                if should_block:
                    # Blocks on EITHER an unresolved finding OR an unreadable/incomplete
                    # scan (fail-closed) — review_msg states which.
                    print(
                        f"BLOCKED: PR #{pr_num} — review-body gate did not pass.",
                        file=sys.stderr,
                    )
                    print(review_msg, file=sys.stderr)
                    return 2

                # Inline review comments (Codex P1/P2 badges) — separate
                # endpoint, separate check. P1 blocks; P2 warns.
                should_block, inline_msg = _check_inline_review_findings(
                    pr_num,
                    force=force_override,
                    repo=merge_repo,
                )
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} — inline review gate did not pass.",
                        file=sys.stderr,
                    )
                    print(inline_msg, file=sys.stderr)
                    return 2

                # Scheduled Claude review at HEAD — its OWN fail-closed gate with its OWN
                # sigil (# scheduled-review-override waives ONLY this gate; independent of
                # # stale-review-override). Placed LAST, but ordering is no longer
                # safety-critical: the review-body + inline scanners now ALSO fail CLOSED
                # on a clipped budget (PR #1434 removed their fail-open), so a drained
                # merge-gate deadline BLOCKS at whichever gate hits its 1s floor first —
                # never a silent pass. Uses verified_head from the Codex gate (None under
                # # stale-review-override → re-read).
                sched_override = has_trailing_override(merge_seg.raw, "scheduled-review-override")
                # Provision-or-surface: the scheduled gate no-ops off the configured
                # public repo (by design). A SILENT no-op would hide a drifted
                # genesis.yaml (canonical != the real public repo) disarming the gate
                # on the repo it protects — so surface WHY it didn't apply. Advisory
                # only (never blocks); skipped under the override (already a conscious
                # waive). The report path shows the same via its "n/a" line.
                if not sched_override and not _scheduled_gate_applies(merge_repo):
                    print(
                        f"NOTE: scheduled-review gate n/a for PR #{pr_num} — merge "
                        f"targets {merge_repo}, not the configured public repo "
                        f"({_canonical_public_repo() or 'undetermined'}).",
                        file=sys.stderr,
                    )
                sched_msg = _check_scheduled_claude_reviewed_head(
                    pr_num,
                    verified_head,
                    merge_repo,
                    force=sched_override,
                )
                if sched_msg:
                    print(
                        f"BLOCKED: PR #{pr_num} — required scheduled Claude review(s) "
                        f"missing at the current head.",
                        file=sys.stderr,
                    )
                    print(sched_msg, file=sys.stderr)
                    return 2

        # ── sqlite3 write operations ────────────────────────────────
        # Whole-command match (never misses a fragmented/wrapped invocation),
        # narrowed to DML in statement position so the `replace()` scalar
        # function and bare keywords in a grep pattern no longer false-positive.
        # See _is_sqlite_write / _DML_STATEMENT_RE.
        if _is_sqlite_write(cmd):
            print(
                "BLOCKED: Direct database writes via sqlite3 are not allowed. "
                "Use CRUD modules or MCP tools instead.",
                file=sys.stderr,
            )
            return 2

        # ── git commit --no-verify / -n (any executed segment) ─────
        if any(commit_skips_hooks(s.argv) for s in segs):
            print(
                "BLOCKED: --no-verify / -n bypasses review enforcement hooks. "
                "Remove it and run /review first.",
                file=sys.stderr,
            )
            return 2

        # ── Process kill (soft warn) ──────────────────────────────
        if re.search(r"(?:^|\s|&&|;)\s*(?:kill|killall|pkill)\s", cmd):
            print(
                "⚠️  STOP: Process kill detected. Have you received explicit user approval?",
                file=sys.stderr,
            )

        # ── git config writes (soft warn) ─────────────────────────
        if (
            "git config" in cmd
            and not re.search(r"git config\s+(--get|--list|-l|--show)\b", cmd)
            and re.search(r"git config\s+[\w.-]+\s+\S", cmd)
        ):
            print(
                "⚠️  STOP: git config modification detected. "
                "Have you received explicit user approval?",
                file=sys.stderr,
            )

        # ── Interactive push / PR-create approval prompt (deferred) ──
        # Reached only if no hard-block above returned. Dispatched sessions
        # were already denied inline; here, an interactive human session gets a
        # native approve/deny dialog for its push / PR-create.
        if ask_reason is None and blind_spot_reason is not None:
            ask_reason = blind_spot_reason
        if ask_reason is not None:
            return _ask(ask_reason)

        # A first-push-only re-push auto-allow — emitted ONLY here, after every
        # hard-block has had its chance to return 2, so a compound
        # `git push <republish> && git commit --no-verify` still hard-blocks.
        if push_allow_reason is not None:
            return _allow(push_allow_reason)

        # A standalone, un-gated `gh pr create` (branch already on the remote, or
        # an explicit --head that gh won't push): reaching here means nothing
        # needed approval. Emit an explicit `allow` so CC's OWN permission prompt
        # doesn't fire for this non-allow-listed command — the un-gate is only
        # meaningful if it actually suppresses the prompt. (A push in the same
        # command would have set ask_reason above, so this never overrides a push.)
        if create_segs:
            return _allow(
                "gh pr create cannot publish code here (explicit --head, or current "
                "branch already on the remote) — no push, so no separate approval"
            )

    except (json.JSONDecodeError, KeyError):
        # A malformed/partial payload is a parse-ambiguity fail-open (matches the
        # sibling guards). Any OTHER exception is an orchestration BUG and must
        # NOT silently allow a push/merge — it propagates to run_guard(), which
        # fails CLOSED (exit 2). The per-check network/parse helpers keep their
        # own intentional inner fail-opens; this only removes the blanket
        # swallow-everything that turned real bugs into silent allows.
        return 0

    return 0


# Sentinel: `--check-pr` was given a repo option with an EXPLICITLY EMPTY value
# (`--repo`, `--repo=`, `-R`, `-R=`). Distinct from None ("no repo option → cwd
# repo"): an empty value is a MISTAKE, and silently falling back to the cwd repo
# would report an all-clear (and a suggested merge command) for an unrelated
# same-numbered PR (Codex P2, #1373). The caller rejects it instead.
_CHECK_PR_REPO_EMPTY = object()


def _parse_check_pr_repo(argv: list[str]):
    """The target repo named on a ``--check-pr`` invocation's trailing args.

    Returns the OWNER/REPO string, ``None`` (no repo option → cwd repo), or
    ``_CHECK_PR_REPO_EMPTY`` (an option was given with an empty value → reject).
    Accepts every normal gh spelling — ``--repo X``, ``-R X``, ``--repo=X``,
    ``-R=X``, glued ``-Rowner/repo`` — not just the separate ``--repo`` form: a
    form that went unrecognized silently checked the CWD repo (Codex P2, #1366).
    """
    for i, tok in enumerate(argv):
        if tok in ("--repo", "-R"):
            val = argv[i + 1] if i + 1 < len(argv) else ""
            return val if val else _CHECK_PR_REPO_EMPTY
        if tok.startswith(("--repo=", "-R=")):
            return tok.split("=", 1)[1] or _CHECK_PR_REPO_EMPTY
        if tok.startswith("-R") and not tok.startswith("--") and len(tok) > 2:
            # glued pflag shorthand `-Rowner/repo` — enforcement's
            # _merge_target_repo accepts it, so the report must too.
            return tok[2:]
    return None


def check_pr_report(pr_num: str, repo: str | None = None) -> int:
    """CANONICAL pre-merge report: run the SAME checks the merge gate enforces.

    This exists so sessions never hand-roll the review check with ad-hoc
    gh/jq (2026-08-10: a hand-rolled query used the GraphQL bot login on the
    REST endpoint — `chatgpt-codex-connector` vs `…[bot]` — matched nothing,
    and the empty result was reported as "Codex clean" while 5 P2 findings sat
    unread). Report and enforcement share these functions, so a bug in one is a
    bug in both. Both run the finding scans FAIL-CLOSED — an UNREADABLE or INCOMPLETE
    scan (gh error / clipped budget) shows as a failure here and BLOCKS a merge there;
    neither ever issues a false all-clear (PR #1434 removed the old merge-path fail-open).

    Prints one line per gate; returns 0 when every gate would pass, 1 otherwise.
    (Same CHECKS as enforcement, but the internal ORDER may differ — e.g. the merge
    arm runs the scheduled gate LAST, after the finding scanners; the report is
    order-independent because every scan fails CLOSED, so a clipped scan is a failure
    line regardless of position.)
    """
    failures = 0
    mergeable = _check_mergeable(pr_num, repo=repo)
    print(f"mergeable      : {mergeable or 'unreadable'}")
    # Allowlist, matching the enforcement arm: anything that is not a definite
    # MERGEABLE (UNKNOWN, None/"" query failure, or a new state) counts as a
    # failure — the old `in (UNKNOWN, CONFLICTING)` set let None/"" pass.
    if mergeable != "MERGEABLE":
        failures += 1
    ci_state, ci_bad = _pr_ci_status(pr_num, repo=repo)
    print(f"ci             : {ci_state}{' (' + ', '.join(ci_bad[:6]) + ')' if ci_bad else ''}")
    if ci_state in ("red", "pending"):
        failures += 1
    elif ci_state == "absent" and _scheduled_gate_applies(repo):
        # Mirror the enforcement arm so the report and gate never disagree: a readable
        # empty check set on the canonical repo blocks (CI never ran — likely a
        # conflicting branch or a dropped pull_request trigger).
        print("  ↳ BLOCK — no CI checks have run (empty check set); pull_request CI never fired.")
        failures += 1
    elif ci_state == "incomplete" and _scheduled_gate_applies(repo):
        # Mirror the enforcement arm: a partial rollup missing a required workflow
        # (merge_gate.required_ci_workflows, default CI) blocks on the canonical repo.
        print(
            "  ↳ BLOCK — required CI workflow(s) missing from the check rollup "
            f"({', '.join(ci_bad[:6])}); the required suite never ran."
        )
        failures += 1
    # Order mirrors the gate: base-invariant → freshness → finding scans, so a
    # review published mid-run can't pass freshness with its P1s unscanned.
    blocked, msg = _check_base_is_default(pr_num, repo=repo)
    print(f"base-branch    : {'BLOCK — ' + msg.splitlines()[0] if blocked else 'ok (default)'}")
    failures += 1 if blocked else 0
    # Pin receipts: authoritative HERE, not in CI — the PR body is mutable after
    # a check run completes, so only a merge-time read describes the body that
    # actually merges.
    blocked, msg = _check_pin_receipts(pr_num, repo=repo)
    print(
        f"pin-receipts   : {'BLOCK — ' + msg.splitlines()[0] if blocked else msg.splitlines()[0]}"
    )
    if blocked:
        for line in msg.splitlines()[1:]:
            print(f"  {line}")
    failures += 1 if blocked else 0
    blocked, msg, verified_head = _check_codex_reviewed_head(pr_num, repo=repo)
    if blocked:
        label = "BLOCK — " + msg.splitlines()[0]
    else:
        # Distinguish a genuinely-current review from a stale-but-trivial-delta
        # allow — both return the same tuple, but the report must NOT assert
        # "current" when Codex reviewed an older SHA (Codex P2, #1373). Re-derive
        # the reviewed SHA vs HEAD for an honest label (structured-stdout consumers
        # read this, not the stderr NOTE).
        _reviewed = _latest_codex_reviewed_sha(pr_num, repo=repo)
        _head = _pr_head_sha(pr_num, repo=repo)
        _head_l = _head.strip().lower() if _head else None
        if _head_l is not None and _reviewed == _head_l:
            label = "ok (current)"
        elif (
            _head_l is not None
            and (_clean := _latest_codex_clean_comment_sha(pr_num, repo=repo))
            and _head_l.startswith(_clean)
        ):
            # Freshness satisfied by a clean Codex ISSUE-COMMENT at head (the review
            # object is absent or stale) — the allow path added in follow-up 7ff0fdc6.
            label = "ok (clean comment at head)"
        elif _reviewed is None or _head is None:
            # A transiently-failed re-read must NOT read as "current" (Codex P2
            # #1373): the enforcement gate already passed, but the report must not
            # ASSERT the head was reviewed when it could not confirm it.
            label = "ok (freshness label unverified — re-read failed)"
        elif _reviewed != _head_l:
            label = f"ok (STALE review of {_reviewed[:12]}, delta since is trivial)"
        else:
            label = "ok (current)"
    print(f"codex-at-head  : {label}")
    failures += 1 if blocked else 0
    # Scheduled Claude review at HEAD — the SAME always-fail-closed gate the merge arm
    # enforces (shared function). A missing/stale/unreadable scheduled review is a
    # FAILURE line, never a false all-clear — the report must not diverge from enforcement.
    # Pass the Codex-verified head (as the enforcement path does) so the report is a
    # COHERENT snapshot: if the head moved mid-report, the scheduled check binds to the
    # same head the printed merge-with command does, not an independently re-read newer one.
    # Scoped to the public repo only (mirrors enforcement) — print an honest n/a rather
    # than a misleading "ok" when the target is some other repo.
    if not _scheduled_gate_applies(repo):
        print("scheduled-claude: n/a (scoped to the public repo only)")
    else:
        sched_msg = _check_scheduled_claude_reviewed_head(pr_num, verified_head, repo)
        print(
            f"scheduled-claude: {'BLOCK — ' + sched_msg.splitlines()[0] if sched_msg else 'ok (at head)'}"
        )
        failures += 1 if sched_msg else 0
    # Fail-closed (the only mode now): a scan that could not be READ (gh error/malformed)
    # shows as a failure here, never as "ok" — the report must not issue a false all-clear.
    blocked, msg = _check_pr_review_findings(pr_num, repo=repo)
    print(f"review-body    : {'BLOCK — ' + msg.splitlines()[0] if blocked else 'ok'}")
    failures += 1 if blocked else 0
    blocked, msg = _check_inline_review_findings(pr_num, repo=repo)
    print(
        f"inline-findings: {'BLOCK — ' + msg.splitlines()[0] if blocked else 'ok (P2s, if any, printed above)'}"
    )
    failures += 1 if blocked else 0
    # Emit the actionable merge command ONLY when EVERY gate passed — printing it earlier
    # (right after codex-at-head) suggested a mergeable PR even when the scheduled or finding
    # gate below would block. Bound to the Codex-verified head (the TOCTOU pin).
    if failures == 0 and verified_head:
        print("merge-with     : " + _suggested_merge_cmd(pr_num, verified_head, repo))
    print(
        "verdict        :",
        "MERGEABLE (all gates pass)" if failures == 0 else f"{failures} gate(s) would block",
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    # Report mode: `git_push_guard.py --check-pr <N> [--repo OWNER/REPO]` — the
    # canonical pre-merge check (same functions as enforcement). No hook payload.
    if len(sys.argv) >= 3 and sys.argv[1] == "--check-pr":
        _repo = _parse_check_pr_repo(sys.argv[3:])
        if _repo is _CHECK_PR_REPO_EMPTY:
            print(
                "ERROR: --repo/-R was given an empty value. Specify OWNER/REPO, or "
                "omit the option to check the current repository — an empty value "
                "would silently report the WRONG repo.",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.exit(check_pr_report(sys.argv[2], repo=_repo))
    run_guard(main, "git_push_guard")
