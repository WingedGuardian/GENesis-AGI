#!/usr/bin/env python3
"""PreToolUse hook (Bash): block git commands that SILENTLY discard uncommitted work.

`git checkout <path>`, `git restore <path>`, `git reset --hard`, and `git clean -f`
throw away working-tree changes with no confirmation and no reflog for unstaged
edits — a single fat-fingered path wipes hours of un-committed work. (Origin:
2026-08-22 — a verify-RED experiment restored a temporarily-broken file with
`git checkout <file>`, silently discarding the session's real uncommitted edits to
that file. A memory only helps the session that recalls it; this guard fires
unconditionally.)

DESIGN — ask git, never parse git's ref-vs-path semantics. The hard part of
"is this checkout a branch switch or a file discard?" is delegated to git itself:
for each operand we run `git -C <cwd> status --porcelain -- <operand>` and block
ONLY when git reports that operand as a modified/dirty TRACKED path. A branch
name is not a dirty path, so `git checkout fix/foo` (even a slash-y branch, even
a dirty tree) never false-blocks. This is the canonical-parser discipline
(scripts/hooks/shell_parse.py for tokenization + cwd) applied to a data-loss
guard: shell_parse tokenizes/segments; git adjudicates path-vs-ref and dirtiness.

Two block classes:
  * UNCONDITIONAL (unambiguously discard/delete-intent — block regardless of tree
    state, matching the prior crude guards, deterministic, with the override):
      git reset --hard [<ref>]
      git clean -f / -fd / -fdx / -xf / --force   (closes the old substring bypass)
  * CONDITIONAL (usually a branch switch / index-only op — block ONLY when git
    confirms the operand is a dirty tracked path that would be overwritten):
      git checkout foo.py  |  git checkout -- foo.py   (foo.py has unstaged changes)
      git restore foo.py   |  git restore --worktree foo.py
Allows:
  git checkout <branch> / -b <new> / <clean-path>  ·  git restore --staged foo.py
  (staged-only, no worktree loss)  ·  git reset --soft/--mixed  ·  git clean -n

Escape hatch: append ``# discard-override`` to the git segment (a deliberate,
auditable "yes, discard it"). Fail-OPEN by contract: an unparseable command, an
unresolvable cwd, a non-repo, or any git error → ALLOW (this guard must never
block legitimate work on uncertainty; a missed discard is the status quo, a false
block is a regression). Stdlib-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# Self-locate so sibling hook modules resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Canonical cwd resolution (cd-chains, `git -C`, worktree) currently lives in the
# push guard; reuse it rather than hand-roll a second resolver. (Its natural home
# is shell_parse — consolidating it there is tracked in the hook-review follow-up.)
from git_push_guard import _CWD_UNKNOWN, _effective_cwd  # noqa: E402
from hook_input import field, read_payload, run_guard  # noqa: E402
from shell_parse import (  # noqa: E402
    _GIT_OPTS_WITH_ARG,
    analyze,
    git_subcommand,
    has_trailing_override,
)

_OVERRIDE_SIGIL = "discard-override"
# Substrings that gate the (slightly) expensive parse+git path. If none is present
# the command cannot be a discard op, so we return instantly.
_TRIGGER_SUBSTRINGS = ("checkout", "restore", "reset", "clean")
# Bound the git probe so a hung/huge repo can never wedge the tool call.
_GIT_TIMEOUT_S = 5


def _git(cwd: str, *args: str) -> tuple[int, str]:
    """Run ``git -C <cwd> <args>`` fail-open. Returns (returncode, stdout);
    (-1, "") on any error/timeout so callers treat trouble as "cannot confirm
    loss" → allow."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError):
        return -1, ""


def _operands_have_worktree_changes(cwd: str, paths: list[str]) -> list[str]:
    """Return *paths* if any is a tracked file with UNSTAGED worktree modifications
    a checkout/restore would overwrite, else []. ONE batched
    ``git status --porcelain -- p1 p2 …`` (never one call per operand — M2). Uses
    porcelain's XY status: column Y (worktree) non-space and not '?' = unstaged
    loss. A branch name / clean path / untracked path never shows dirty (no false
    block on branch switches). Fail-open: git error / no output → [] (allow)."""
    if not paths:
        return []
    rc, out = _git(cwd, "status", "--porcelain", "--", *paths)
    if rc != 0 or not out.strip():
        return []
    for line in out.splitlines():
        if len(line) < 2:
            continue
        # Y (worktree) column: ' ' clean, '?' untracked (checkout won't overwrite a
        # tracked file for those); anything else (M/D/R/C/U) = unstaged loss.
        if line[1] not in (" ", "?"):
            # Any dirty match within the requested pathspec set blocks the op; a
            # requested dir can expand to files, so report the requested operands.
            return paths
    return []


def _is_force_cluster(tok: str) -> bool:
    """A short-flag cluster that includes force (``-f``, ``-fd``, ``-xf``…)."""
    return tok.startswith("-") and not tok.startswith("--") and "f" in tok[1:]


def _is_force_cluster_present(argv: list[str]) -> bool:
    """Whether any ``git clean`` token carries force (``-f``/``-fd``/``--force``)."""
    return "--force" in argv or any(_is_force_cluster(t) for t in argv[1:])


def _operands_after_subcommand(argv: list[str]) -> list[str]:
    """Operands of a checkout/restore (paths + tree-ish). LOCATES the subcommand by
    skipping git global options and their values via the canonical
    ``_GIT_OPTS_WITH_ARG`` (so ``git --git-dir X checkout`` finds 'checkout', not
    'X'), then collects tokens after it, dropping option flags and stopping
    flag-parsing at ``--``. The tree-ish is harmless (git status of a ref reports
    nothing). ``--source``/``-s`` take a non-path value that is skipped."""
    i = 1
    while i < len(argv):  # advance to the subcommand
        t = argv[i]
        if t in _GIT_OPTS_WITH_ARG:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        i += 1  # this token IS the subcommand — operands follow
        break
    out: list[str] = []
    flags_done = False
    skip_next = False
    for tok in argv[i:]:
        if skip_next:
            skip_next = False
            continue
        if not flags_done and tok == "--":
            flags_done = True
            continue
        if not flags_done and tok.startswith("-"):
            if tok in ("--source", "-s"):
                skip_next = True
            continue
        out.append(tok)
    return out


def _restore_touches_worktree(argv: list[str]) -> bool:
    """``git restore`` writes the worktree unless it targets ONLY the index.
    Worktree is restored if ``--worktree``/``-W`` is present, OR neither
    ``--staged`` nor ``--worktree`` is given (default target is the worktree)."""
    has_staged = "--staged" in argv or "-S" in argv
    has_worktree = "--worktree" in argv or "-W" in argv
    return has_worktree or not has_staged


def _unconditional_violations(cmd: str) -> list[str]:
    """`reset --hard` / `clean -f` — UNAMBIGUOUSLY discard/delete-intent, blocked
    regardless of tree state (deterministic; matches the prior crude guards; the
    escape is `# discard-override`). PURE ARGV, NO subprocess — so this decision can
    never be starved by a slow git probe under the hook's wall-clock budget (main
    acts on it BEFORE running any conditional probe — M2). Uses the canonical
    ``git_subcommand`` so a separated global value-flag (``git --git-dir X reset
    --hard``) cannot bypass it (M1)."""
    reasons: list[str] = []
    for seg in analyze(cmd):
        if seg.exe != "git":
            continue
        sub = git_subcommand(seg.argv)
        if sub not in ("reset", "clean"):
            continue
        if has_trailing_override(seg.raw, _OVERRIDE_SIGIL):
            continue
        if sub == "reset" and "--hard" in seg.argv:
            reasons.append(
                "`git reset --hard` discards uncommitted work. Stash/commit first, "
                "or append `# discard-override` if you truly mean it."
            )
        elif sub == "clean" and _is_force_cluster_present(seg.argv):
            reasons.append(
                "`git clean` with -f/--force permanently deletes untracked files. "
                "Append `# discard-override` if intended."
            )
    return reasons


def _conditional_violations(cmd: str, payload: dict) -> list[str]:
    """`checkout`/`restore` of a DIRTY tracked path — blocked ONLY when git confirms
    the loss (a branch switch / clean path / `--staged`-only restore passes). One
    batched ``git status`` per segment. Runs only after the unconditional pass is
    clean, so its subprocess latency cannot starve that pass."""
    reasons: list[str] = []
    for seg in analyze(cmd):
        if seg.exe != "git":
            continue
        sub = git_subcommand(seg.argv)
        if sub not in ("checkout", "restore"):
            continue
        if has_trailing_override(seg.raw, _OVERRIDE_SIGIL):
            continue
        if sub == "restore" and not _restore_touches_worktree(seg.argv):
            continue  # --staged-only: index change, no worktree loss
        cwd = _effective_cwd(cmd, payload, seg)
        if cwd is _CWD_UNKNOWN or not cwd or not os.path.isdir(cwd):
            continue  # cannot locate the repo → cannot confirm loss → allow
        for path in _operands_have_worktree_changes(cwd, _operands_after_subcommand(seg.argv)):
            reasons.append(
                f"`git {sub} {path}` would discard your unstaged changes to "
                f"{path} (this is not a branch switch)."
            )
    return reasons


def _violations(cmd: str, payload: dict) -> list[str]:
    """Combined view (used by tests). ``main`` runs the two phases separately so the
    unconditional block is decided with zero subprocess risk."""
    return _unconditional_violations(cmd) + _conditional_violations(cmd, payload)


def main() -> int:
    try:
        payload = read_payload()
        cmd = field(payload, "command")
        if not cmd or "git" not in cmd:
            return 0
        if not any(s in cmd for s in _TRIGGER_SUBSTRINGS):
            return 0

        # Phase 1 — unconditional (argv-only, no subprocess): decided and acted on
        # BEFORE any git probe, so a slow conditional probe can never SIGKILL-starve
        # the block for reset --hard / clean -f (M2).
        reasons = _unconditional_violations(cmd)
        # Phase 2 — conditional probes (subprocess) only if phase 1 is clean.
        if not reasons:
            reasons = _conditional_violations(cmd, payload)
        if reasons:
            for r in reasons:
                print(f"BLOCKED: {r}", file=sys.stderr)
            print(
                "This git command discards uncommitted work. Stage it (`git add`), "
                "stash it (`git stash`), or commit first. If you truly mean to "
                "discard, append `# discard-override` to the git segment.",
                file=sys.stderr,
            )
            return 2
    except (json.JSONDecodeError, KeyError):
        pass  # Fail-open
    except Exception:
        pass  # Any unexpected error → fail-open (never block legitimate work)
    return 0


if __name__ == "__main__":
    run_guard(main, "git_discard_guard")
