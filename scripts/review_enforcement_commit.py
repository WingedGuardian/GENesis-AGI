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
    split_segments,
)

# Sentinel: the commit's effective cwd cannot be confidently resolved (a cd into
# a variable/command-substitution, a subshell, or a commit nested at depth>0).
# Fail closed on it — treat as main (block Rule 1) and do NOT take the docs skip.
_CWD_UNKNOWN = object()

# Cheap early-out: a raw command that never mentions "git commit" is not a
# commit. Actual detection (through wrappers, bash -c, etc.) uses shell_parse.
_COMMIT_PATTERN = re.compile(r"\bgit\s+commit\b")


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
    """The dir named by a ``git -C <dir>`` in a segment's argv, else None."""
    argv = argv or []
    for i, tok in enumerate(argv):
        if tok == "-C" and i + 1 < len(argv):
            return argv[i + 1]
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
            get_current_branch,
            has_code_changes,
            has_valid_review_marker,
            is_review_current,
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

    # Docs/config-only skip: a commit whose ENTIRE staged set is documentation
    # or config carries no code to review (adaptive-review "review level: None"),
    # so skip Rule 2 for it. Conservative fail-toward-review default: we skip ONLY
    # when (a) the commit is PURE — it cannot add/select content beyond the
    # current index (no pathspec, no -a/-i/-o/-p, and no git add/rm/mv/reset in
    # the chain, P1-D) — AND (b) the staged set is non-empty and every path is on
    # the docs/config allowlist. An impure commit, an empty staged set, a diff we
    # cannot read, or an ambiguous cwd (already blocked by Rule 1) falls through
    # to normal enforcement — never skipped. Placed AFTER Rule 0 (--no-verify) and
    # Rule 1 (main-branch) so it can never weaken those hard blocks.
    if not _commit_may_add_content(segs):
        staged = _staged_files(cwd)
        if staged and all(_is_docs_or_config(p) for p in staged):
            sys.exit(0)

    # Rule 2: Block commits without review (on branches)
    # Race condition: when command is "git add X && git commit", nothing is
    # staged yet at hook time because git add hasn't run. Detect git add in
    # the same command chain — if present, require the marker file to exist
    # (the PostToolUse hook deletes it after every commit).
    stages_in_same_command = any(git_subcommand(s.argv) == "add" for s in segs)
    if stages_in_same_command:
        # Can't check diff hash (staging hasn't happened yet) — require the
        # marker file to exist and not be expired.
        rule2_blocks = not has_valid_review_marker(cwd=cwd)
    else:
        rule2_blocks = has_code_changes(cwd=cwd) and not is_review_current(cwd=cwd)

    if rule2_blocks:
        # A trailing '# review-override' comment (outside quotes) acknowledges
        # accepted findings and bypasses ONLY this review gate — never Rule 0
        # (--no-verify) or Rule 1 (main-branch), which are checked above.
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
            "Run /review and dispatch the superpowers:code-reviewer agent first, "
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
