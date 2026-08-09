#!/usr/bin/env python3
"""PreToolUse hook (Bash): block commits without review.

Two enforcement rules:
1. Block ALL commits directly to main — always require a branch.
2. Block commits on branches if review marker is not current.

Reads the CC hook payload from stdin (via hook_input).

Exit codes:
  0 = allow (tool proceeds)
  2 = deny (tool blocked, message on stderr)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))
from hook_input import field, read_payload  # noqa: E402
from shell_parse import (  # noqa: E402
    analyze,
    commit_skips_hooks,
    git_subcommand,
    has_trailing_override,
    split_segments,
)

# Sentinel: the commit's effective cwd cannot be confidently resolved (a cd into
# a variable/command-substitution, a subshell, or a commit nested at depth>0).
# Fail closed on it — treat as main (block Rule 1) and do NOT take the docs skip.
_CWD_UNKNOWN = object()

# Cheap early-out: a command with no "commit" TOKEN cannot be a git commit, so
# skip the shell_parse pass. It must gate on the token alone, NOT on a rigid
# "git commit" adjacency — `git -C <dir> commit` and `git -c k=v commit` put
# global options between "git" and "commit", so `\bgit\s+commit\b` MISSED them
# and let those commit forms skip the ENTIRE gate (review, --no-verify, and the
# direct-to-main block). Precise detection is still shell_parse's `analyze()` +
# `git_subcommand` below; this only decides whether to run it. Kept identical in
# review_invalidate_on_commit.py (the PostToolUse invalidator) — the pair must
# detect the same set of commits or the marker cleared drifts from the one
# checked. Guarded by test_commit_pattern_matches_git_dash_c_and_dash_C.
_COMMIT_PATTERN = re.compile(r"\bcommit\b")


def _commit_override(command: str, segs: list) -> str:
    """Classify the ``# review-override`` approval for the git-commit segment(s).

    Returns:
      ``"valid"``    — every executed ``git commit`` segment carries a genuine
                       trailing ``# review-override`` shell comment (bound to
                       that segment by shell_parse): an intentional override.
      ``"in_quote"`` — the token appears in the command (e.g. inside the ``-m``
                       message or a heredoc body) but not as a clean trailing
                       comment on the commit, where it would leak into public
                       history and does NOT override.
      ``"none"``     — no override token present.
    """
    commit_segs = [s for s in segs if git_subcommand(s.argv) == "commit"]
    if commit_segs and all(s.override for s in commit_segs):
        return "valid"
    if re.search(r"#\s*review-override\b", command):
        return "in_quote"
    return "none"


# ── Docs/config-only skip (adaptive-review "review level: None") ──────────
# Pure docs/config commits carry no code to review, so the review protocol rates
# them "review level: None". The extension/basename allowlist below is the set we
# will SKIP enforcement for when EVERY staged path matches. Anything else — a
# .py/.js/.ts/.sh/.json, or any unrecognized extension — is treated as code and
# still gated (fail TOWARD requiring review).
_DOCS_CONFIG_EXTS = {".md", ".rst", ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
_DOCS_CONFIG_BASENAMES = {"CHANGELOG", "LICENSE", ".GITIGNORE"}  # compared upper-cased


def _is_docs_or_config(path: str) -> bool:
    """Whether a staged path is a docs/config file (per the conservative allowlist).

    Anything under a ``.github/`` directory is NEVER docs/config: GitHub Actions
    workflows are executable CI config (arbitrary ``run:`` with repo secrets), so
    a workflow-only commit MUST still be reviewed even though it is ``.yml``.
    """
    norm = path.replace("\\", "/")
    if ".github" in norm.split("/"):  # leading `.github/` or any `/.github/` component
        return False
    base = os.path.basename(norm)
    if base.upper() in _DOCS_CONFIG_BASENAMES:
        return True
    _, ext = os.path.splitext(base)
    return ext.lower() in _DOCS_CONFIG_EXTS


def _seg_dash_C(argv) -> str | None:
    """The dir named by a GLOBAL ``git -C <dir>`` in a segment's argv, else None.

    Only the ``-C`` that precedes the git SUBCOMMAND is the "run as if started in
    <dir>" global option. A ``-C`` AFTER the subcommand is that subcommand's own
    flag with a different meaning — for ``git commit -C <commit>`` it reuses that
    commit's message/authorship and ``<commit>`` is a commit-ish (e.g. ``HEAD``),
    NOT a directory. Scanning the whole argv (the old behavior) mistook
    ``git commit -C HEAD`` for a ``-C HEAD`` worktree redirect and resolved a
    bogus ``<cwd>/HEAD`` dir. So walk only the global-option prefix and stop at
    the first non-option token (the subcommand)."""
    argv = argv or []
    i = 1  # argv[0] is the executable ("git")
    while i < len(argv):
        tok = argv[i]
        if tok == "-C" and i + 1 < len(argv):
            return argv[i + 1]
        if tok in _GIT_GLOBAL_VALUE_FLAGS:  # another global value-flag: skip flag+value
            i += 2
            continue
        if tok.startswith("-"):  # a global boolean flag (e.g. --no-pager)
            i += 1
            continue
        break  # reached the subcommand — any later -C belongs to it, not to git
    return None


def _cd_target(raw: str):
    """Classify a top-level command segment as a ``cd``.

    Returns the literal target dir for a plain ``cd <literal-path>`` segment;
    ``_CWD_UNKNOWN`` for a cd we cannot resolve (no arg, ``cd -``, a
    variable/command-substitution/glob target, or a subshell/group); ``None`` if
    it is not a cd. Self-contained (does NOT import git_push_guard).
    """
    s = raw.strip()
    if s.startswith("(") or s.startswith("{"):
        return _CWD_UNKNOWN  # subshell/group scopes its cd
    m = re.match(r"^cd(?:\s+(?P<p>.*))?$", s)
    if not m:
        return None
    p = (m.group("p") or "").strip()
    if not p or p == "-":
        return _CWD_UNKNOWN
    if len(p) >= 2 and p[0] in "'\"" and p[-1] == p[0]:
        inner = p[1:-1]
        if p[0] == '"' and ("$" in inner or "`" in inner):
            return _CWD_UNKNOWN
        return inner
    if " " in p or "\t" in p:
        return _CWD_UNKNOWN
    if p.startswith("~"):
        p = os.path.expanduser(p)
    if any(ch in p for ch in "$`*?"):
        return _CWD_UNKNOWN
    return p


def _resolve_against(current, target: str):
    """Resolve a possibly-relative ``cd``/``-C`` target to an ABSOLUTE path.

    Absolute → normalized (recovers even from a prior UNKNOWN). Relative → joined
    onto ``current``; if ``current`` is unknown/None → ``_CWD_UNKNOWN`` (fail
    closed), since ``git -C <relative>`` from the hook's own cwd would silently
    inspect the wrong tree (P1-A).
    """
    if os.path.isabs(target):
        return os.path.normpath(target)
    if not isinstance(current, str) or not current:
        return _CWD_UNKNOWN
    return os.path.normpath(os.path.join(current, target))


def _effective_diff_cwd(command: str, payload: dict, segs: list):
    """The ABSOLUTE dir whose index the commit inspects — ``str``/``None``/UNKNOWN.

    Resolution mirrors git_push_guard (self-contained; no import):
      1. If the commit is nested (depth>0), UNKNOWN → fail closed.
      2. The LAST top-level ``cd`` before the commit segment (bash applies cds
         sequentially, so the last wins — a decoy ``cd A && …; cd B && git
         commit`` runs in B). Relative cds/-C resolve against the running cwd; an
         unresolvable one ⇒ UNKNOWN. Falls back to the payload cwd as the base.
      3. ``git -C <dir>`` on the commit segment overrides, resolved against that.
    """
    commit_seg = next((s for s in segs if git_subcommand(s.argv) == "commit"), None)
    if commit_seg is not None and getattr(commit_seg, "depth", 0) > 0:
        return _CWD_UNKNOWN

    base = payload.get("cwd") if isinstance(payload, dict) else None
    cur = os.path.normpath(base) if isinstance(base, str) and base else None
    target_raw = getattr(commit_seg, "raw", None) if commit_seg is not None else None
    if target_raw is not None:
        for raw in split_segments(command):
            if raw == target_raw:
                break
            cd = _cd_target(raw)
            if cd is _CWD_UNKNOWN:
                cur = _CWD_UNKNOWN
            elif cd is not None:
                cur = _resolve_against(cur, cd)
    if commit_seg is not None:
        dash_c = _seg_dash_C(getattr(commit_seg, "argv", None))
        if dash_c is not None:
            return _resolve_against(cur, dash_c)
    return cur


def _staged_files(cwd: str | None) -> list[str] | None:
    """Staged paths for the pending commit, or None if the diff cannot be read.

    Uses ``--name-status -M`` so renames/copies surface BOTH sides (P2-E): a
    ``foo.py → README.md`` rename shows only ``README.md`` under ``--name-only``,
    which would wrongly read as docs-only. Both the source and the destination
    are returned, so a code source still forces review. None (command error) is
    the caller's signal to fall back to normal enforcement rather than skip.
    """
    args = ["git"]
    if cwd:
        args += ["-C", cwd]
    args += ["diff", "--cached", "--name-status", "-M"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        paths: list[str] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0]
            # Rename/Copy: `R100<TAB>old<TAB>new` / `C075<TAB>old<TAB>new` — both
            # the source and the destination path are relevant.
            if status[:1] in ("R", "C") and len(parts) >= 3:
                paths.append(parts[1])
                paths.append(parts[2])
            elif len(parts) >= 2:
                paths.append(parts[-1])
        return paths
    except Exception:
        return None


# ── Pure-commit gate for the docs/config skip (P1-D) ─────────────────────
# The staged index at hook time is NOT what a commit necessarily records:
# `git commit -a` stages tracked changes, `git commit -m x app.py` selects a
# pathspec, and `git add app.py && git commit` stages code in a prior segment.
# The docs-only skip may fire ONLY for a "pure" bare commit that provably cannot
# add or select content beyond the current --cached snapshot.
_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
)
# git commit long flags that CONSUME a following value token (so the value is not
# misread as a pathspec).
_COMMIT_VALUE_LONG = frozenset(
    {
        "--message",
        "--file",
        "--author",
        "--date",
        "--template",
        "--reuse-message",
        "--reedit-message",
        "--fixup",
        "--squash",
        "--gpg-sign",
        "--cleanup",
    }
)
# Long flags that select/stage content beyond the index → not a pure commit.
_COMMIT_SELECT_LONG = frozenset(
    {"--all", "--include", "--only", "--patch", "--interactive", "--pathspec-from-file"}
)
_COMMIT_ARG_SHORT = "mFCct"  # short flags consuming a value (t = --template)
_COMMIT_SELECT_SHORT = "aiop"  # -a --all, -i --include, -o --only, -p --patch


def _commit_can_select_content(argv: list[str]) -> bool:
    """Whether a ``git commit`` argv could stage/select content (pathspec or
    -a/-i/-o/-p/--all/…): True ⇒ NOT a pure commit ⇒ do not take the docs skip."""
    # advance to the "commit" token, past git global options
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
    if i >= len(argv) or argv[i] != "commit":
        return True  # can't confirm shape → fail toward enforcement
    i += 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return (i + 1) < len(argv)  # any pathspec after `--`
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if name in _COMMIT_SELECT_LONG:
                return True
            if name in _COMMIT_VALUE_LONG and "=" not in tok:
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            consumes_next = False
            for j, ch in enumerate(tok[1:], 1):
                if ch in _COMMIT_SELECT_SHORT:
                    return True
                if ch == "S":
                    break  # -S[keyid]: rest of token is an optional value
                if ch in _COMMIT_ARG_SHORT:
                    consumes_next = j == len(tok) - 1  # value is next token if last
                    break
                # else a boolean short flag (n/e/s/q/v/u/z/…) — keep scanning
            i += 2 if consumes_next else 1
            continue
        return True  # a bare positional = pathspec
    return False


def _commit_may_add_content(segs: list) -> bool:
    """True if the command could commit content the --cached snapshot doesn't show.

    Covers the commit segment's own pathspec/select flags AND any staging command
    (git add/rm/mv/reset, or restore --staged) elsewhere in the chain (P1-D).
    """
    commit_seg = next((s for s in segs if git_subcommand(s.argv) == "commit"), None)
    if commit_seg is None:
        return True
    if _commit_can_select_content(commit_seg.argv):
        return True
    for s in segs:
        if s is commit_seg:
            continue
        sub = git_subcommand(s.argv)
        if sub in ("add", "rm", "mv", "reset"):
            return True
        if sub == "restore" and "--staged" in s.argv:
            return True
    return False


def main() -> None:
    # Parse tool input
    payload = read_payload()
    command = field(payload, "command")
    if not _COMMIT_PATTERN.search(command):
        sys.exit(0)  # Not a commit, allow

    # Parse the command into the segments it actually executes (through
    # wrappers, bash -c, command substitutions). Reused for Rule 0, the
    # add-chain detection, and the override binding.
    segs = analyze(command)

    # The cheap _COMMIT_PATTERN early-out can match "git commit" mentioned in a
    # string (a reply body, an echo). Confirm a REAL executed commit segment
    # before applying the branch/review rules, else allow.
    if not any(git_subcommand(s.argv) == "commit" for s in segs):
        sys.exit(0)

    # Rule 0: Block --no-verify / -n on ANY executed commit segment — it
    # bypasses ALL pre-commit hooks (review enforcement AND the native secrets /
    # large-file / direct-to-main guards). shell_parse parses real argv, so a
    # flag mentioned inside the commit message doesn't false-block and a bundled
    # / operator-glued / bash -c-nested form isn't missed. Runs BEFORE the
    # override check, so it can never be bypassed by '# review-override'.
    if any(commit_skips_hooks(s.argv) for s in segs):
        _deny(
            "BLOCKED: --no-verify / -n bypasses review enforcement AND the "
            "native pre-commit guards (secrets, large files, direct-to-main). "
            "Remove it and establish a review first via /review."
        )
        return

    # Resolve the dir the commit actually targets (git -C / the LAST cd before
    # the commit segment / payload cwd). A decoy `cd A && …; cd B && git commit`
    # runs in B, and B is what we must inspect. An ambiguous cwd (variable /
    # subshell / depth>0) yields the _CWD_UNKNOWN sentinel → fail closed.
    eff_cwd = _effective_diff_cwd(command, payload, segs)
    cwd_unknown = eff_cwd is _CWD_UNKNOWN
    cwd = eff_cwd if isinstance(eff_cwd, str) else None

    # Import review_state from same directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)

    try:
        from review_state import (
            ESCALATION_ROUND_CAP,
            get_current_branch,
            get_current_diff_hash,
            get_review_round,
            has_code_changes,
            has_valid_review_marker,
            is_review_current,
            reset_review_round,
        )
    except ImportError:
        # If review_state.py is missing, fail open — don't block
        sys.exit(0)

    branch = get_current_branch(cwd=cwd)

    # Rule 1: Block commits on main. Fail closed when the cwd is ambiguous — we
    # cannot prove the commit is NOT landing on main, so treat it as such.
    if cwd_unknown or branch in ("main", "master"):
        _deny(
            "BLOCKED: Direct commits to main are not allowed. "
            "Create a branch first: git checkout -b <scope>/<description>"
        )
        return

    # Rule 3: review escalation cap. After ESCALATION_ROUND_CAP CONSECUTIVE
    # defect-bearing review→fix→re-review rounds on this change (a clean review
    # resets the streak — see review_state.bump_review_round), block the commit
    # until an explicit '# escalation-ack' trailing comment — a machine-enforced
    # STOP so a genuine review→fix loop can't silently run long on a standing
    # "proceed" (see genesis-development SKILL.md), while honestly-clean
    # multi-commit development never trips. Checked BEFORE
    # both the docs/config skip AND Rule 2 ON PURPOSE: the hard stop must not be
    # bypassable by file extension (a reviewed prompt/skill/docs-only commit at the
    # cap would otherwise sneak past via the docs skip), nor by '# review-override'
    # (which exits Rule 2 early) — the two acknowledgments are independent. Like
    # review-override, the ack does NOT bypass Rule 0 (--no-verify) or Rule 1
    # (main-branch), checked above. Recognition mirrors _commit_override: a clean
    # trailing comment on EVERY commit segment (all(), so one ack can't license a
    # chained sibling commit). NOTE: on a nested `bash -c 'git commit …'` the ack
    # must sit on the INNER command (the sigil isn't propagated to wrappers); the
    # documented `git commit … # escalation-ack` form is a plain segment.
    round_n = get_review_round(cwd=cwd)
    commit_segs = [s for s in segs if git_subcommand(s.argv) == "commit"]
    if round_n >= ESCALATION_ROUND_CAP:
        acked = bool(commit_segs) and all(
            has_trailing_override(s.raw, sigil="escalation-ack") for s in commit_segs
        )
        if not acked:
            _deny(
                f"BLOCKED: review escalation cap reached — {round_n} consecutive review "
                f"rounds each surfaced NEW defects (cap {ESCALATION_ROUND_CAP}). The "
                "review→fix loop has run long — the round-2 mode-switch audit did NOT "
                "converge, which means the DESIGN or the problem statement is likely "
                "wrong, not just this fix. STOP and get a FRESH user decision on how to "
                "proceed (robust-by-construction redesign / narrow scope / shelve), then "
                "acknowledge that decision with a trailing shell comment (outside any "
                "quotes):\n"
                '  git commit -m "your message"  # escalation-ack'
            )
            return
        # Acked = a fresh decision to continue → reset the round budget so the next
        # stop is a fresh cap away, not per-commit friction for the branch's whole
        # life. Reset stands even if a later rule blocks THIS commit: the
        # acknowledgment was made, and erring toward less friction only happens
        # AFTER a conscious ack.
        reset_review_round(cwd=cwd)
    elif round_n == ESCALATION_ROUND_CAP - 1:
        # Tier 1 — MODE-SWITCH, one round BEFORE the hard stop. Two consecutive
        # defect-bearing rounds is the signature of fixing the INSTANCE a reviewer
        # named instead of the CLASS: round N's narrow patch leaves sibling
        # edge/boundary/sentinel cases that round N+1 then surfaces. Stopping here
        # only "fails louder"; the fix is to force a DIFFERENT approach so the loop
        # CONVERGES in one more round instead of grinding to the round-3 stop. Block
        # until a '# audit-ack' — NOT a hard stop (the session can proceed once it
        # has done the audit), and deliberately one round earlier than the cap so
        # the intervention lands while the loop is still short. Does NOT reset the
        # counter: a still-narrow fix must still reach the round-3 stop, and a
        # clean review afterwards resets the streak via bump_review_round(clean).
        # Independent of '# review-override' (Rule 2) and '# escalation-ack' (above).
        acked = bool(commit_segs) and all(
            has_trailing_override(s.raw, sigil="audit-ack") for s in commit_segs
        )
        if not acked:
            _deny(
                f"BLOCKED (mode-switch): {round_n} consecutive review rounds each "
                f"surfaced NEW defects (cap {ESCALATION_ROUND_CAP}). You are fixing the "
                "INSTANCE the reviewer named, not the CLASS — a third round is how the "
                "loop runs away. Do NOT commit another one-line patch. STOP and switch "
                "approach:\n"
                "  1. Dispatch a FRESH-CONTEXT adversarial reviewer (a subagent with "
                "clean context) over the ENTIRE diff — tell it to exhaustively "
                "enumerate every edge/boundary/sentinel/hierarchy/error case, "
                "independent of what the bot flagged.\n"
                "  2. For any domain semantics in play (cgroup, systemd, SQLite, async, "
                "timezones, …), READ the authoritative docs/source — do not reason from "
                "assumption; assumption is what produced the serial defects.\n"
                "  3. Fix the WHOLE enumerated class in ONE commit — not just the named "
                "case.\n"
                "Then acknowledge you did the audit (not another blind patch) with a "
                "trailing shell comment (outside any quotes):\n"
                '  git commit -m "your message"  # audit-ack'
            )
            return

    # Docs/config-only skip: a commit whose ENTIRE staged set is documentation
    # or config carries no code to review (adaptive-review "review level: None"),
    # so skip Rule 2 for it. Conservative fail-toward-review default: we skip ONLY
    # when (a) the commit is PURE — it cannot add/select content beyond the
    # current index (no pathspec, no -a/-i/-o/-p, and no git add/rm/mv/reset in
    # the chain, P1-D) — AND (b) the staged set is non-empty and every path is on
    # the docs/config allowlist. An impure commit, an empty staged set, a diff we
    # cannot read, or an ambiguous cwd (already blocked by Rule 1) falls through
    # to normal enforcement — never skipped. Placed AFTER Rule 0 (--no-verify),
    # Rule 1 (main-branch), and Rule 3 (escalation cap) so it can never weaken
    # those hard blocks.
    if not _commit_may_add_content(segs):
        staged = _staged_files(cwd)
        if staged and all(_is_docs_or_config(p) for p in staged):
            sys.exit(0)

    # `git add X && git commit` stages in the SAME command chain: nothing is staged yet
    # at hook time (git add hasn't run), so we can neither classify the current diff nor
    # bind the marker to it. Detected once here and reused by Rule 2.5 and Rule 2.
    stages_in_same_command = any(git_subcommand(s.argv) == "add" for s in segs)

    # Rule 2.5: review DEPTH. A SUBSTANTIAL change needs an ADVERSARIAL audit, not a
    # precision-filtered inline pass — a "no findings" from a confidence-≥80 reviewer
    # is FALSE CONFIDENCE, not clearance. Checked BEFORE Rule 2 (and its override) so
    # '# review-override' waives FINDINGS but NOT the depth requirement (D1). A loud,
    # logged '# depth-ack' is the audited escape for a genuine format mismatch.
    # Fail-OPEN on any classifier/marker error: this is advisory anti-autopilot
    # friction; the CI review-depth check is the real backstop. Runs AFTER the docs
    # skip, so a pure-docs commit never reaches it.
    try:
        from review_scope import classify_change_substantiality

        staged_level = classify_change_substantiality(cwd=cwd)
    except Exception:  # noqa: BLE001 - depth is advisory; never crash the gate
        staged_level = "unknown"
    try:
        from review_state import get_marker_depth

        marker_level, marker_adversarial = get_marker_depth(cwd=cwd)
    except Exception:  # noqa: BLE001 - a marker-read error must not block on the add-chain
        # path (fail OPEN there); on the normal path the is_review_current() guard below
        # re-reads the marker and fails CLOSED, the safe direction for the stricter gate.
        marker_level, marker_adversarial = None, True

    if stages_in_same_command:
        # Index empty at hook time — the staged classify reads "inline", so depth falls
        # back to the marker's RECORDED level (else a substantial change staged in the
        # same chain would slip the gate). We cannot diff-bind here (staging hasn't
        # happened), so the marker's adversarial bit is trusted as recorded — a KNOWN
        # add-chain hash-blindness limitation (tracked follow-up), symmetric with Rule 2.
        depth_level = marker_level
        depth_is_adversarial = marker_adversarial
    else:
        # Normal commit: the staged index IS the commit content, so classify it
        # directly. Crucially, the marker's adversarial clearance counts ONLY when the
        # marker genuinely BINDS this diff — is_review_current (marker.diff_hash == the
        # current staged hash) AND that hash is real, not "unknown"/"clean". Otherwise an
        # adversarial audit of an EARLIER diff A would falsely clear a later substantial
        # diff B that a '# review-override' then waives past Rule 2's staleness block,
        # when B was never audited (the integrity hole this depth gate exists to close).
        # Excluding "unknown" fails CLOSED on a transient stat-diff error (the depth gate
        # is the stricter one and has the '# depth-ack' escape) rather than letting the
        # is_review_current() clean/unknown short-circuit wrong-clear a substantial diff.
        depth_level = staged_level
        depth_is_adversarial = (
            marker_adversarial
            and is_review_current(cwd=cwd)
            and get_current_diff_hash(cwd=cwd) not in ("unknown", "clean")
        )

    if depth_level == "substantial" and not depth_is_adversarial:
        depth_acked = bool(commit_segs) and all(
            has_trailing_override(s.raw, sigil="depth-ack") for s in commit_segs
        )
        if depth_acked:
            print(
                "NOTE: depth-ack honored — substantial-change adversarial-audit "
                "requirement waived by explicit acknowledgment.",
                file=sys.stderr,
            )
        else:
            _deny(
                "BLOCKED (review depth): this is a SUBSTANTIAL change (≥50 reviewable "
                "lines, or >1 code file, or an auth/api/migrations file) but the "
                "review is not an ADVERSARIAL audit. A precision-filtered 'no findings' "
                "inline pass is FALSE CONFIDENCE, not clearance. Dispatch a genesis-architect "
                "adversarial audit (assume bugs, enumerate the edge/boundary/sentinel/"
                "hierarchy class, READ authoritative semantics for any domain code), save it "
                "to ~/.genesis/last_code_review.txt, then re-mark:\n"
                "  python3 scripts/review_state.py mark --agent-output ~/.genesis/last_code_review.txt\n"
                "If the audit genuinely ran but its format isn't recognized, acknowledge with "
                "a trailing shell comment (outside any quotes):  # depth-ack"
            )
            return

    # Rule 2: Block commits without review (on branches).
    if stages_in_same_command:
        # Can't check diff hash (staging hasn't happened yet) — require the
        # marker file to exist and not be expired.
        rule2_blocks = not has_valid_review_marker(cwd=cwd)
    else:
        rule2_blocks = has_code_changes(cwd=cwd) and not is_review_current(cwd=cwd)

    if rule2_blocks:
        # A trailing '# review-override' comment (outside quotes) acknowledges
        # accepted findings and bypasses ONLY this review gate — never Rule 0
        # (--no-verify), Rule 1 (main-branch), or Rule 3 (escalation cap), all
        # checked above.
        override = _commit_override(command, segs)
        if override == "valid":
            print(
                "NOTE: review-override honored — commit review gate bypassed. "
                "Findings acknowledged by session.",
                file=sys.stderr,
            )
            sys.exit(0)
        if override == "in_quote":
            _deny(
                "BLOCKED: '# review-override' is not a clean trailing shell "
                "comment — it sits inside quotes (e.g. the commit message) or is "
                "followed by more command, so it does NOT override and could be "
                "committed into public history. Put it at the very END, outside "
                "any quotes:\n"
                '  git commit -m "your message"  # review-override'
            )
            return
        _deny(
            "BLOCKED: Code changes exist without review. "
            "Run /review and dispatch the genesis-architect agent (adversarial audit) first, "
            "then run: python3 scripts/review_state.py mark --agent-output ~/.genesis/last_code_review.txt\n"
            "If findings are intentionally accepted, append a trailing shell "
            "comment (outside any quotes): '  # review-override'"
        )
        return

    # All checks passed — allow
    sys.exit(0)


def _deny(message: str) -> None:
    """Output denial message and block the tool via exit code 2."""
    print(message, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
