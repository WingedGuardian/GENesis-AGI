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

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))
try:
    from hook_input import degraded_exit, field, read_payload, run_guard  # noqa: E402
except Exception as _helper_exc:  # noqa: BLE001 — a missing NEW helper must block.
    if __name__ != "__main__":
        raise
    # Reverse version skew: this guard may be newer than hook_input.py. Nothing
    # imported from that older helper can recover us, so fail closed locally. Do
    # not render the exception — even __str__ can raise — and use os._exit so a
    # broken diagnostic stream cannot replace exit 2 during interpreter shutdown.
    try:
        sys.stderr.write(
            "GUARD DEGRADED (review_enforcement_commit): shared hook_input is incompatible; "
            "BLOCKING until the hook tree is repaired.\n"
        )
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001 — diagnostics cannot change fail direction.
        pass
    os._exit(2)

try:
    from native_approval import emit_native_ask  # noqa: E402
except Exception as _approval_exc:  # noqa: BLE001 — missing new helper must block.
    if __name__ != "__main__":
        raise
    try:
        sys.stderr.write(
            "GUARD DEGRADED (review_enforcement_commit): native approval helper is "
            "incompatible; BLOCKING until the hook tree is repaired.\n"
        )
        sys.stderr.flush()
    except BaseException:
        pass
    os._exit(2)

try:
    from review_deadline import (  # noqa: E402
        Deadline,
        DeadlineExpired,
        bounded_timeout,
        propagate_deadline_timeout,
    )
except Exception as _deadline_exc:  # noqa: BLE001 — a broken deadline can fail open.
    if __name__ != "__main__":
        raise
    try:
        sys.stderr.write(
            "GUARD DEGRADED (review_enforcement_commit): deadline helper is incompatible; "
            "BLOCKING until the hook tree is repaired.\n"
        )
        sys.stderr.flush()
    except BaseException:
        pass
    os._exit(2)

# DEGRADED-path mention set, defined ABOVE the guarded import so it survives that
# import failing. Same single verb as `_COMMIT_PATTERN` further down; kept separate
# because sharing would put the constant after the import it has to outlive. The
# scope is broad on purpose — every gate in this file (unreviewed commit, commit to
# main, --no-verify) hangs off a commit, so a mention of one is the whole trigger.
_DEGRADED_GATED = r"\bcommit\b"

try:
    from shell_parse import (  # noqa: E402
        analyze_checked,
        commit_skips_hooks,
        gh_pr_subcommand,
        git_subcommand,
        has_trailing_override,
        mentions,
        split_segments,
        unresolved_verb_programs,
    )
except Exception as _exc:  # noqa: BLE001 — exit 1 is NON-blocking; see degraded_exit.
    if __name__ != "__main__":
        raise
    # `review-override` is NOT honoured on this path. An earlier version honoured it,
    # on the reasoning that refusing a recoverable local commit would strand an operator
    # mid-repair — but the check was a bare substring, and MEASURED on a poisoned tree
    # it waived this gate from a sigil sitting inside a QUOTED STRING in an unrelated
    # segment. Binding a sigil to the segment it waives needs the shared parser, whose
    # absence is why this path runs at all. Nothing about a broken sibling module
    # requires a commit to repair, so the operator is not in fact stranded.
    degraded_exit("review_enforcement_commit", gated=_DEGRADED_GATED, exc=_exc)

try:  # noqa: E402
    import discarded_write
except Exception:  # noqa: BLE001 — GUARDED ON PURPOSE. An unguarded import that
    # failed would abort this module's load → exit 1 → which CC reads as a
    # NON-blocking error → the commit RUNS. A cosmetic note must never be able to
    # fail this gate open. Sentinel + null-check is the house pattern
    # (git_push_guard.py's push_allowlist).
    discarded_write = None  # type: ignore[assignment]

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
_GIT_WORD = re.compile(r"\bgit\b")

#: A git command whose SUBCOMMAND the parse cannot read: a variable, a
#: substitution or an escape stands where it goes, or an option before it that
#: the parser cannot classify. It may be a commit, and nothing in its text says
#: so, which is why the early exit below cannot be what lets it through. The
#: push guard refuses the same shape for the same reason.
_HIDDEN_GIT_VERB_MSG = (
    "BLOCKED: this command may run a git operation whose subcommand review "
    "enforcement cannot read or reliably establish (a variable, a substitution, "
    "an escape, unreadable nested syntax, or an unclassified Git global option "
    "obscures it), so it cannot tell whether a commit would run.\n"
    "To proceed: for an unclassified Git global option, omit it or configure it "
    "another way; split unreadable compounds into separate "
    "commands, write the git subcommand out literally, or write prose or "
    "script text to a file. For prose that merely mentions a commit, use the "
    "Write tool instead of a here-doc."
)


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

# Executable prompt/agent/skill SURFACES: files whose content shapes autonomous LLM
# behavior (agent + slash-command + skill definitions, and the in-repo skill library).
# These are NEVER docs/config even when ``.md`` — an autonomous system editing its own
# prompts is high-consequence (build-session SKILL.md requires prompt/LLM-behavior
# changes to get adversarial review + a CI warning). User-sovereign top-level CAPS behavior
# docs (SOUL.md, USER.md, CLAUDE.md) are deliberately NOT here: the user editing their own
# behavior files is not gated (user sovereignty). Directory prefixes, `/`-normalized.
_PROMPT_SURFACE_PREFIXES = (
    ".claude/agents/",
    ".claude/commands/",
    ".claude/skills/",
    "src/genesis/skills/",
    "src/genesis/identity/",  # runtime prompt templates (CODE_AUDITOR, INBOX_EVALUATE, …)
)


def _is_prompt_surface(path: str) -> bool:
    """Whether a path is an executable prompt/agent/skill surface (behavior-shaping).

    Covers the fixed prompt roots above PLUS any ``prompts/`` package under the runtime
    tree (``src/genesis/autonomy/executor/prompts/``, ``src/genesis/sentinel/prompts/``, …)
    — the class, not just the named roots. Top-level user CAPS docs stay exempt (they do
    not live under these paths).
    """
    norm = path.replace("\\", "/")
    return norm.startswith(_PROMPT_SURFACE_PREFIXES) or (
        norm.startswith("src/genesis/") and "/prompts/" in norm
    )


def _is_docs_or_config(path: str) -> bool:
    """Whether a staged path is a docs/config file (per the conservative allowlist).

    Anything under a ``.github/`` directory is NEVER docs/config: GitHub Actions
    workflows are executable CI config (arbitrary ``run:`` with repo secrets), so
    a workflow-only commit MUST still be reviewed even though it is ``.yml``.
    Likewise an executable prompt/agent/skill SURFACE is never docs/config even when
    ``.md`` — it shapes autonomous behavior and must reach the review + depth gates.
    """
    norm = path.replace("\\", "/")
    if ".github" in norm.split("/"):  # leading `.github/` or any `/.github/` component
        return False
    if _is_prompt_surface(norm):  # behavior surface — reviewable, never docs-skipped
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
        # Double quotes still expand $/`, and consume a backslash ONLY before
        # $ ` " \ or newline (bash manual "Double Quotes"; verified vs bash 5.2:
        # `"\q"` keeps the backslash literally, `"\$"`/`"\\"` collapse). A literal
        # read is unfaithful exactly for those — UNKNOWN. A backslash before an
        # ordinary char (`"/tmp/repo\q"`) is part of the literal pathname and
        # stays allowed (Codex P2, #1371). Single quotes suppress ALL expansion,
        # so the literal read is always faithful.
        if p[0] == '"' and (any(ch in inner for ch in "$`") or re.search(r'\\[$`"\\\n]', inner)):
            return _CWD_UNKNOWN
        return inner
    if " " in p or "\t" in p:
        return _CWD_UNKNOWN
    if p.startswith("~"):
        # Faithful for every non-adversarial command (30% of real commits use
        # `cd ~/…`). ACCEPTED RESIDUE, deliberately outside the threat model: a
        # same-command `HOME=` reassignment would make bash expand `~` differently
        # than the hook — but that is deliberate evasion, not an accident, and the
        # gate is an accident-prevention/friction layer, not a sandbox (a
        # deliberate evader already has `python -c 'subprocess.run(["git", …])'`,
        # which no Bash-string guard can see). Denying tilde would tax every
        # legitimate commit to block an evasion class that stays open elsewhere.
        p = os.path.expanduser(p)
    # Any special char bash would act on (the manual's closed lists — expansion
    # triggers: brace `{`, parameter `$`, command-subst backtick, escapes `\`,
    # globs `*?[`, tilde handled above; plus the in-segment metacharacters
    # `()<>`, e.g. process substitution `<(…)`): bash would transform the word or
    # parse it as syntax, so our literal join would check a DIFFERENT dir than
    # bash enters, and a planted look-alike ("shadow") dir would even pass the
    # isdir validation. Unresolvable → UNKNOWN.
    if any(ch in p for ch in "$`*?[{\\()<>"):
        return _CWD_UNKNOWN
    # A RELATIVE target with CDPATH in the environment: bash's cd consults CDPATH
    # and may enter a directory OUTSIDE the resolved join — unverifiable. EXEMPT
    # a first pathname component of `.` or `..`: POSIX cd (and bash 5.2, verified)
    # skips the CDPATH search for those, so `cd ./wt` stays deterministically
    # resolvable even under CDPATH (Codex P2, #1371).
    if not os.path.isabs(p) and os.environ.get("CDPATH") and p.split("/", 1)[0] not in (".", ".."):
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


def _existing_dir_or_unknown(resolved):
    """POSITIVE validation of a resolved effective cwd: a ``str`` that is not an
    existing directory becomes ``_CWD_UNKNOWN``.

    This is the categorical closure for every unexpanded-token form (`-C 'main*'`,
    `-C ~/x` quoted, `cd main\\o`, glob chars, future metachars we didn't
    enumerate): if the token had shell expansion we didn't perform, the joined
    literal path almost surely does not exist — and a branch read against a
    nonexistent dir returned "unknown"/OSError, which used to slip PAST Rule 1
    (audit finding, 2026-08-11, confirmed by execution). A legit commit always
    runs in a real directory, so requiring existence adds no friction. ``None``
    passes through (no payload cwd — pre-existing fallback semantics).
    """
    if isinstance(resolved, str) and not os.path.isdir(resolved):
        return _CWD_UNKNOWN
    return resolved


def _effective_diff_cwd(command: str, payload: dict, segs: list, commit_seg=None):
    """The ABSOLUTE dir whose index the commit inspects — ``str``/``None``/UNKNOWN.

    Resolution mirrors git_push_guard (self-contained; no import):
      1. If the commit is nested (depth>0), UNKNOWN → fail closed.
      2. The LAST top-level ``cd`` before the commit segment (bash applies cds
         sequentially, so the last wins — a decoy ``cd A && …; cd B && git
         commit`` runs in B). Relative cds/-C resolve against the running cwd; an
         unresolvable one ⇒ UNKNOWN. Falls back to the payload cwd as the base.
      3. ``git -C <dir>`` on the commit segment overrides, resolved against that.
      4. A resolved dir that does not EXIST ⇒ UNKNOWN (positive validation — see
         :func:`_existing_dir_or_unknown`).

    ``commit_seg`` selects WHICH commit segment to resolve for (default: the
    first) — a chained ``git commit … && git -C /elsewhere commit …`` has a
    DIFFERENT effective cwd per segment, and Rule 1 must check each.
    """
    if commit_seg is None:
        commit_seg = next((s for s in segs if git_subcommand(s.argv) == "commit"), None)
    if commit_seg is not None and getattr(commit_seg, "depth", 0) > 0:
        return _CWD_UNKNOWN

    base = payload.get("cwd") if isinstance(payload, dict) else None
    cur = os.path.normpath(base) if isinstance(base, str) and base else None
    target_raw = getattr(commit_seg, "raw", None) if commit_seg is not None else None
    if target_raw is not None:
        # Locate commit_seg by POSITION (occurrence index among top-level segments
        # sharing its raw), not by first-raw-match: in a chain of commits with
        # IDENTICAL raw text (`cd /wt && git commit -m same; cd /other && git
        # commit -m same`) a raw-equality break stopped at the FIRST occurrence
        # for BOTH segments, so the second commit inherited the first's cwd —
        # bypassing the direct-to-main check AND blinding the different-dirs
        # check (Codex P1, #1371). Identity (`s is commit_seg`) picks the right
        # occurrence; depth-0 filtering keeps the count aligned with
        # split_segments (nested segments don't appear as top-level raws).
        same_raw = [
            s for s in segs if getattr(s, "depth", 0) == 0 and getattr(s, "raw", None) == target_raw
        ]
        occ = next((i for i, s in enumerate(same_raw) if s is commit_seg), 0)
        seen = 0
        for raw in split_segments(command):
            if raw == target_raw:
                if seen == occ:
                    break
                seen += 1
                continue  # an earlier same-raw occurrence (a commit, never a cd)
            cd = _cd_target(raw)
            if cd is _CWD_UNKNOWN:
                cur = _CWD_UNKNOWN
            elif cd is not None:
                cur = _resolve_against(cur, cd)
    if commit_seg is not None:
        dash_c = _seg_dash_C(getattr(commit_seg, "argv", None))
        if dash_c is not None:
            if dash_c.startswith("~") or any(ch in dash_c for ch in "$`\\*?[{()<>"):
                # Any special char in a `-C` target (the bash manual's closed
                # lists — expansion triggers: tilde, parameter `$`, command-subst
                # backtick, escapes `\`, globs `*?[`, brace `{`; in-segment
                # metacharacters `()<>` incl. process substitution): UNRESOLVABLE,
                # same class as `cd "$WT"`. The argv token has lost its quoting,
                # so we cannot tell an expanding form from a quoted-literal one —
                # and a planted shadow dir matching the LITERAL token would pass
                # the isdir validation while bash expands into a different dir.
                # NEVER resolved from context — see the Rule-1 teach-only note.
                return _CWD_UNKNOWN
            return _existing_dir_or_unknown(_resolve_against(cur, dash_c))
    return _existing_dir_or_unknown(cur)


def _staged_files(cwd: str | None, *, deadline: float | None = None) -> list[str] | None:
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
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=bounded_timeout(deadline, 10),
        )
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
    except subprocess.TimeoutExpired as exc:
        propagate_deadline_timeout(deadline, exc)
        return None
    except DeadlineExpired:
        raise
    except Exception:
        return None


# ── Pure-commit gate for the docs/config skip (P1-D) ─────────────────────
# The staged index at hook time is NOT what a commit necessarily records:
# `git commit -a` stages tracked changes, `git commit -m x app.py` selects a
# pathspec, and `git add app.py && git commit` stages code in a prior segment.
# The docs-only skip may fire ONLY for a "pure" bare commit that provably cannot
# add or select content beyond the current --cached snapshot.
# MEASURED against the installed binary, never `git -h` (which omits
# `--attr-source` while git accepts it). Locked identical to the copies in
# shell_parse / git_push_guard / pre_push_privacy_review by
# tests/test_hooks/test_value_flag_consistency.py.
_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--attr-source",
        "--shallow-file",
    }
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
# Staging subcommands that put content in the index AFTER this hook has read it.
# Named here rather than inline so the depth note can DERIVE its warning from the
# same set the predicate decides on — see _content_selecting_forms().
_STAGING_SUBCOMMANDS = ("add", "rm", "mv", "reset")


def _content_selecting_forms() -> str:
    """The forms that can put content in a commit beyond what ``--cached`` shows.

    DERIVED from the predicate's own constants, never typed alongside them. The
    depth note tells an author what to inspect before writing ``# depth-ack``,
    and three separate review findings have now been the same defect: the NOTE
    was narrower than the PREDICATE, so the gate permitted a form the advice
    never warned about and the careful reader was the one who got caught. Adding
    the missing form each time is predicate #4; deriving the sentence closes the
    class, because a form added to `_COMMIT_SELECT_*` or `_STAGING_SUBCOMMANDS`
    now appears in the warning automatically.
    """
    selectors = sorted(_COMMIT_SELECT_LONG) + [f"-{c}" for c in sorted(_COMMIT_SELECT_SHORT)]
    staging = [f"git {v}" for v in _STAGING_SUBCOMMANDS] + ["git restore --staged"]
    return (
        f"{', '.join(selectors)} or a pathspec on the commit itself; "
        f"{', '.join(staging)} earlier in the chain"
    )


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
        if sub in _STAGING_SUBCOMMANDS:
            return True
        if sub == "restore" and "--staged" in s.argv:
            return True
    return False


# ── Chain state-mutation guards (Codex P1 #1371) ─────────────────────────────
# The Rule-1 branch read and the review marker are both sampled ONCE at hook time,
# before any segment runs — so a chain that CHANGES the branch or STAGES new content
# between commits lands a later commit in a state the hook never inspected. Two
# conservative, fail-closed checks close that (see main()).
# git_subcommand cannot resolve user ALIASES (they live in git config), so a
# habitual `co=checkout` (`git co main && git commit`) moves HEAD to main undetected
# — and it also defeats Rule 1's own hook-time branch read the same way. Accepted
# residue: resolving aliases would require reading git config, outside this static
# accident-prevention guard's model (a deliberate evader already has `python -c`).
_BRANCH_MUTATING_SUBCMDS = frozenset({"switch", "checkout"})
# Characters that make a branch-target token unresolvable to a literal name (a
# variable/command-subst/glob/escape) — same intent as _cd_target's set. A target
# carrying any of these can't be proven ≠ main, so it fails closed.
_UNRESOLVABLE_TARGET_CHARS = "$`*?[{\\()<>~"


def _branch_mutation_risk(argv: list[str]) -> str | None:
    """Whether a ``git switch``/``git checkout`` can move HEAD before a commit.

    Returns ``"main"`` for main/master, ``"unknown"`` for an unresolvable target,
    ``"branch"`` for another literal branch, and ``None`` only for a file-restore
    form. The review budget is branch-specific, so every HEAD-moving form must run
    separately from the commit; this hook can only inspect pre-command state.

    Only HEAD-MOVING forms are assessed, to avoid false-blocking file restores
    (``git checkout HEAD~1 -- f`` / ``git checkout main f.py``, which leave HEAD put):
      * the branch-create flags ``-c/-C`` (switch) / ``-b/-B`` (checkout) → the name
        they carry is created+checked-out (HEAD-moving). Their value may be a
        SEPARATE token (``-B main``), ATTACHED (``-Bmain``), inside a short CLUSTER
        (``-qBmain`` / ``-qB main``), or a long ``--create=``/``--force-create=``
        form (Codex P1 #1371 — a bare-token check let ``-Bmain`` reach main);
      * bare ``git switch <branch>`` → HEAD-moving;
      * bare ``git checkout <x>`` → HEAD-moving ONLY as the pure switch form: no
        ``--`` and exactly ONE operand (``checkout <ref> <path>`` or ``checkout --
        <path>`` is a restore → not assessed).
    argv is quote-stripped by shell_parse, so a ``$VAR`` target survives as a literal
    ``$…`` token and reads as unresolvable. ACCEPTED RESIDUE (deliberate-evasion,
    outside the accident model — like the alias/symbolic-ref note above): a git long-
    option ABBREVIATION of the create flags (``--force-c=main``) is not matched."""
    sub = git_subcommand(argv)
    # advance past git global options to just after the subcommand token
    i = 1
    while i < len(argv):
        if argv[i] in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if argv[i].startswith("-"):
            i += 1
            continue
        break
    i += 1
    new_branch_vals: list[str] = []  # branch-create flag values → definitely HEAD-moving
    positionals: list[str] = []
    has_dashdash = False
    _CREATE_SHORTS = "bBcC"  # checkout -b/-B, switch -c/-C — all NAME the new branch
    _CREATE_LONGS = ("--create", "--force-create")  # switch long forms; value = branch
    while i < len(argv):
        t = argv[i]
        if t == "--":
            has_dashdash = True
            break
        if t == "-":  # previous-branch shorthand (a bare positional, not a flag)
            positionals.append("-")
            i += 1
            continue
        if t.startswith("--"):
            name, eq, val = t.partition("=")
            if name in _CREATE_LONGS:
                if eq:
                    new_branch_vals.append(val)  # --force-create=main
                elif i + 1 < len(argv):
                    new_branch_vals.append(argv[i + 1])  # --create main
                    i += 1
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            # Decompose the short cluster letter-wise (pflag semantics — see
            # feedback_cli_guard_pflag_clustering): a branch-create letter takes the
            # REST of the token as its value if any (``-Bmain`` / ``-qBmain``), else
            # the NEXT token (``-B main`` / ``-qB main``). Boolean letters (q/f/…)
            # before it are consumed in place.
            letters = t[1:]
            for j, ch in enumerate(letters):
                if ch in _CREATE_SHORTS:
                    rest = letters[j + 1 :]
                    if rest:
                        new_branch_vals.append(rest)
                    elif i + 1 < len(argv):
                        new_branch_vals.append(argv[i + 1])
                        i += 1
                    break
            i += 1
            continue
        positionals.append(t)
        i += 1

    if new_branch_vals:
        targets = new_branch_vals
    elif sub == "switch":
        targets = positionals
    elif has_dashdash or len(positionals) != 1:
        targets = []  # checkout file-restore form → HEAD not moved
    else:
        targets = positionals  # bare `git checkout <branch>` → the switch form

    for t in targets:
        if t in ("main", "master"):
            return "main"
        if t == "-" or any(ch in t for ch in _UNRESOLVABLE_TARGET_CHARS):
            return "unknown"
    # Every valid `git switch` changes/selects HEAD. Even when option parsing
    # cannot recover a target (`switch -- <branch>`, `switch --detach`, or a
    # syntactically incomplete command), it cannot safely share a commit's
    # pre-command branch budget. Checkout alone has file-restore forms.
    return "branch" if sub == "switch" or targets else None


def _git_common_dir(cwd: str | None, *, deadline: float | None = None) -> str | None:
    """Absolute git common dir for ``cwd`` (shared by all its worktrees), or None.

    With ``deadline`` (the hook's aggregate one) the probe draws from it like the
    neighbouring git probes: an overrun raises ``DeadlineExpired``, which
    ``run_guard`` turns into a refusal, instead of each call spending its own
    5 seconds against a 10-second harness kill that would let the commit run."""
    try:
        args = ["git"] + (["-C", cwd] if cwd else [])
        args += ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=bounded_timeout(deadline, 5)
        )
        out = result.stdout.strip()
        # One absolute line or nothing: git before 2.31 does not know
        # `--path-format`, exits 0, and echoes the option back beside a relative
        # `.git`, which must read as unreadable (fail closed), never as an identity.
        if result.returncode != 0 or "\n" in out or not os.path.isabs(out):
            return None
        return os.path.realpath(out)
    except subprocess.TimeoutExpired as exc:
        propagate_deadline_timeout(deadline, exc)
        return None
    except DeadlineExpired:
        raise
    except Exception:
        return None


def _live_manifest_binding() -> tuple[str, str | None]:
    """What the deploy manifest says about WHICH repository it belongs to.

    The manifest may carry a top-level ``"repo"``: the ABSOLUTE git common dir of
    the checkout it belongs to. Returns one of:

    * ``("bound", <canonical path>)`` — the key names an existing git common dir
      (a directory holding ``objects/`` and ``HEAD``);
    * ``("absent", None)`` — a JSON object without the key: the caller keeps its
      rule for manifests written before the key existed;
    * ``("malformed", None)`` — anything else: unreadable or malformed JSON, not a
      JSON object, or a key that is not the absolute path of an existing git
      common dir (a work-tree path, a checkout that has since moved). The caller
      ARMS, for every target: a broken manifest fails closed.

    The same rule is kept in ``git_push_guard._live_manifest_binding`` and in the git hooks'
    ``live_manifest_applies``; ``TestManifestRepoBinding`` pins all four copies
    to one verdict table."""
    try:
        with open(Path.home() / ".genesis" / "deploy_manifest.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — unreadable fails closed, never "disarmed"
        return ("malformed", None)
    if not isinstance(data, dict):
        return ("malformed", None)
    if "repo" not in data:
        return ("absent", None)
    repo = data["repo"]
    if (
        isinstance(repo, str)
        and os.path.isabs(repo)
        and os.path.isdir(os.path.join(repo, "objects"))
        and os.path.isfile(os.path.join(repo, "HEAD"))
    ):
        return ("bound", os.path.realpath(repo))
    return ("malformed", None)


def _live_integration_repo(cwd: str | None, *, deadline: float | None = None) -> bool:
    """Whether ``cwd``'s repository runs a local integration branch named ``live``.

    The deploy manifest declares that this install runs one (without it a
    branch named ``live`` is ordinary), and the commit must be in the repository
    the manifest is about (``_live_manifest_binding``): when it names one, that
    repository and no other; when it has no ``"repo"`` key, THIS repository or
    one of its worktrees, since a session commits in unrelated checkouts too and
    their own ``live`` branches are none of this gate's business. A malformed
    manifest, or an identity that cannot be read, fails closed, as this gate does
    for an unverifiable branch. ``deadline`` bounds both identity probes (see
    ``_git_common_dir``)."""
    if not (Path.home() / ".genesis" / "deploy_manifest.json").is_file():
        return False
    kind, bound = _live_manifest_binding()
    if kind == "malformed":
        return True
    there = _git_common_dir(cwd, deadline=deadline)
    if kind == "bound":
        return there is None or there == bound
    here = _git_common_dir(os.path.dirname(os.path.abspath(__file__)), deadline=deadline)
    if here is None or there is None:
        return True
    return here == there


def _worktree_root(cwd: str, *, deadline: float | None = None) -> str:
    """The git worktree ROOT that owns ``cwd`` (canonicalized), or ``realpath(cwd)``
    as a fallback. Two different SUBDIRECTORIES of one worktree — and a symlink alias
    of it — resolve to the same root, so a legitimate same-worktree commit chain
    (which shares ONE index + review marker) is not mistaken for different-directory
    commits (Codex P2 #1371). Fail-safe: any git error falls back to realpath."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=bounded_timeout(deadline, 5),
        )
        root = r.stdout.strip()
        if r.returncode == 0 and root:
            return os.path.realpath(root)
    except subprocess.TimeoutExpired as exc:
        propagate_deadline_timeout(deadline, exc)
    except (FileNotFoundError, OSError):
        pass
    return os.path.realpath(cwd)


def _integration_in_progress(cwd: str | None, *, deadline: float | None = None) -> bool | None:
    """Whether a merge, rebase, cherry-pick, revert or squash merge is mid-flight
    in ``cwd``'s repository, by its sentinel files. None when that cannot be read.

    The sentinels are unauthenticated files, so a caller may use this only in a
    direction where forging one hurts the forger: refusing to treat a commit as
    a reflection, or advisory text. Never to grant an exemption."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=bounded_timeout(deadline, 5),
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        raw = out.stdout.strip()
        git_dir = Path(raw) if Path(raw).is_absolute() else Path(cwd or ".") / raw
        return (
            any(
                (git_dir / n).exists()
                for n in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "SQUASH_MSG")
            )
            or (git_dir / "rebase-merge").exists()
            or (git_dir / "rebase-apply").exists()
        )
    except (DeadlineExpired, subprocess.SubprocessError, OSError, ValueError):
        return None


def _merge_note(
    cwd: str | None, *, gate: str = "depth", deadline: float | None = None
) -> str:
    """A hint appended to the depth denial when a merge is mid-flight.

    ``gate`` names the consequence clause. Only the depth gate's remains: the
    round counter's tiers, which a merge misled differently, are retired.

    ADVISORY TEXT ONLY. Deliberately NOT wired into the verdict, the round
    counter, or the depth classification: those sentinels are unauthenticated
    files that any actor with shell access can create (``echo x >
    .git/MERGE_HEAD``), and `git merge --no-commit` leaves one indefinitely
    without any forgery at all. Keying an EXEMPTION off them would let the actor
    this gate exists to constrain silence it permanently with one write —
    measured: a forged sentinel froze the counter across three further distinct
    defect rounds. Telling the author what the gate can see is safe; letting that
    state decide the verdict is not. Adding a second caller does not weaken that:
    this still only ever returns TEXT.
    """
    if not _integration_in_progress(cwd, deadline=deadline):
        return ""
    if gate == "depth":
        return (
            "\n\nNOTE: a git integration sentinel is present — a merge, squash merge, rebase, "
            "cherry-pick or revert is in progress. Content that operation brought "
            "in counts toward substantiality exactly like code you wrote (via the "
            "staged diff on a normal commit, via the recorded marker level on an "
            "-a/pathspec one), which is why a commit that is only the operation "
            "can land here.\n"
            "That is NOT an exemption, and this hook cannot tell the two apart: "
            "only content that arrived already reviewed on its own PR is somebody "
            "else's audited work. A cherry-pick, a revert, and every conflict "
            "resolution are YOURS and still need the audit — ack only once you "
            "have inspected the PROSPECTIVE commit, which is not the same thing as "
            "the index. For a plain index-only commit the staged set is the whole "
            "story. Every one of these can add more, and this hook runs BEFORE any "
            "of them, so none of it shows in `git diff --cached` yet:\n"
            f"  {_content_selecting_forms()}\n"
            "That list is GENERATED from the same constants the gate decides on, so "
            "it cannot drift narrower than what the gate actually permits — which it "
            "did three times, each time letting an honest ack cover content the "
            "prescribed check never showed.\n"
            "The ack clears THIS gate only. If the operation also left the review "
            "marker no longer binding the staged diff — usual for a merge, but NOT "
            "if you re-marked afterwards — the review-current gate blocks next and "
            "the full ack is:  # depth-ack review-override\n"
            "Add that second sigil ONLY once that gate actually fires: it records "
            "findings as accepted, which is a false statement when there were none. "
            "And sigils bind PER COMMIT SEGMENT: one comment at the very end of a "
            "chain binds only the LAST segment, so the chain is refused. Do not "
            "reach for a multi-line command to get a sigil onto each one — run each "
            "commit as its OWN command instead. A shell comment ends at the physical "
            "line, so the only way to carry a sigil per segment is a newline between "
            "them — and a newline is not `&&`. `&&` short-circuits correctly; a "
            "NEWLINE does not, so the second commit runs even when the first one "
            "FAILED. Put `git commit -m …` and `git commit --amend --no-edit` on "
            "two lines and a commit-msg rejection on the first leaves --amend "
            "rewriting the PREVIOUS commit with this staged content, while the "
            "whole command still exits 0. Separate commands keep each failure "
            "visible and each sigil bound."
        )
    # The round counter's wording went with the streak tiers it explained.
    return ""


# ── Rule R: a round's reflection comes before its fixes ─────────────────────
#
# While a PR's open review round owes a reflection (review_reflection's answer,
# the same one `review_reflection.py status` prints), a content commit is denied
# until an empty reflection commit answers the owed keys. It demands work, never
# permission, so a dispatched session gets the same verdict. Everything it
# cannot read with certainty ALLOWS with an advisory (owner ruling a34481ca):
# only a known, owed round blocks.

#: Open PRs that were merge-eligible at go-live owe nothing for rounds opened
#: before ``review_reflection.REFLECTION_CUTOVER_ISO`` (owner ruling 50cc9dec).
#: A reviewed snapshot, set once with the cutover; a PR opened later always owes.
#: MEASURED at the cutover (2026-10-22T04:04Z): `git_push_guard.py --check-pr` on
#: all 68 open PRs (`gh pr list --limit 500`, a complete read), ignoring only the
#: scheduled-review row; 1 of 68 passed every other gate.
REFLECTION_EXEMPT_PRS: frozenset[int] = frozenset({3535})
REFLECTION_EXEMPT_REPO = "WingedGuardian/GENesis-AGI"

#: Rule R's share of the hook budget: at most this, and never so much that the
#: rest of the gate is left less than the reserve after it.
_REFLECTION_SLICE = 1.0
_REFLECTION_RESERVE = 1.5
_REFLECTION_FILE_LIMIT = 64 * 1024

#: Advisories for the agent, delivered with the allow (stderr is discarded on an
#: exit-0 hook) or with the ask.
_ADVISORIES: list[str] = []


def _reflection_message_file(seg) -> str | None:
    """The ``-F`` file of a commit shaped exactly like a reflection, or None.

    An ALLOWLIST: ``--allow-empty``, one ``-F``/``--file`` and nothing but
    ``-q``/``--quiet``/``--cleanup=verbatim``. Any other option (``-m``, ``-a``,
    ``--amend``, ``--fixup``, ``-c``/``-C``, a pathspec) could carry content or a
    message the file does not hold."""
    argv = seg.argv
    try:
        i = argv.index("commit") + 1
    except ValueError:
        return None
    path: str | None = None
    empty = False
    while i < len(argv):
        tok = argv[i]
        if tok == "--allow-empty":
            empty = True
        elif tok in ("-q", "--quiet", "--cleanup=verbatim"):
            pass
        elif tok == "--cleanup" and i + 1 < len(argv) and argv[i + 1] == "verbatim":
            i += 1
        elif tok in ("-F", "--file") and i + 1 < len(argv) and path is None:
            path = argv[i + 1]
            i += 1
        elif tok.startswith("--file=") and path is None:
            path = tok.split("=", 1)[1]
        elif tok.startswith("-F") and len(tok) > 2 and path is None:
            path = tok[2:]
        else:
            return None
        i += 1
    return path if empty and path and path != "-" else None


def _looks_like_reflection(seg, cwd: str | None, rr) -> bool:
    """A commit that was MEANT as a reflection: ``--allow-empty``, or a message
    file whose first line starts the reflection header. Used only to word a
    refusal, never to admit anything. The file is read by ``rr._read_evidence``
    (non-blocking open, regular files only), so a FIFO named as the message
    cannot hang the hook into its timeout, which would let the commit through."""
    if "--allow-empty" in seg.argv:
        return True
    for i, tok in enumerate(seg.argv):
        path = seg.argv[i + 1] if tok in ("-F", "--file") and i + 1 < len(seg.argv) else None
        if path:
            text, _, _ = rr._read_evidence(path, cwd or ".")
            if text is not None and text.startswith("Round-reflection:"):
                return True
    return False


def _key_rounds(
    cwd: str | None, budget: dict, state: dict, keys: set[str], rr, *, deadline: float
) -> dict[str, list[int | None]]:
    """Every round the debt reader judges each of ``keys`` under, for the owed
    keys among them: the PR's current round for an open-round key, and a late
    key's own round contexts (``review_late._contexts``, ALL of which must pass
    there). None is a late key with no earlier round, which owes the grammar
    alone. A late key whose round cannot be placed gets no entry: it can never
    be credited, so it never admits. The late read runs inside ``deadline``."""
    owed = set(state.get("owed") or [])
    late_keys = set(state.get("late_keys") or [])
    current = int(budget.get("count") or 0)
    rounds: dict[str, list[int | None]] = {k: [current] for k in (keys & owed) - late_keys}
    named_late = keys & owed & late_keys
    if named_late:
        import review_late  # noqa: PLC0415 — lazy, like review_reflection

        with rr.deadline_scope(deadline):
            fix_time = review_late.fix_time_reader(cwd or ".", str(budget.get("current_head")))
            late = review_late.late_findings(budget, state.get("cutover"), fix_time)
        if late is None:
            raise rr.Refused("the late findings could not be read")
        _, ancestors = review_late._ancestry(budget)
        now = datetime.now(UTC)
        for item in late:
            key = item.get("key")
            if key in named_late:
                contexts = review_late._contexts(budget, item, now, ancestors)
                if contexts:
                    rounds[key] = [context.round_number or None for context in contexts]
    return rounds


def _reflection_problem(
    segs, command: str, cwd: str | None, budget: dict, rr, *, deadline: float, state: dict | None = None
) -> str | None:
    """Why this commit is NOT a reflection the gate may admit, or None when it is.

    Admitted only when it is one plain ``git commit`` (``_exemption_shape_ok``)
    in the reflection shape, nothing is staged, no merge/rebase/cherry-pick/
    revert is in progress, and the message file is a regular UTF-8 file whose
    text passes the reflection grammar for this round and names the current
    head. Whether it credits anything is judged later, by the same reader that
    owes the keys; an empty commit adds no content either way."""
    if not _exemption_shape_ok(segs, command):
        return (
            "run the reflection commit as one plain `git commit` (or `git -C <dir> commit`) "
            "command, nothing chained and no env prefix"
        )
    path = _reflection_message_file(segs[0])
    if path is None:
        return (
            "the reflection form is exactly "
            "`git commit --allow-empty --cleanup=verbatim -F <file>` (no -m, -a, "
            "--amend or pathspec)"
        )
    staged = _staged_files(cwd, deadline=deadline)
    if staged is None:
        return "the staged set could not be read"
    if staged:
        return f"files are staged ({len(staged)}): unstage them; a reflection carries no content"
    if _integration_in_progress(cwd, deadline=deadline) is not False:
        return "a merge, rebase, cherry-pick or revert is in progress (or unreadable): finish or abort it first"
    # Non-blocking open, type from the opened descriptor: a path swapped for a
    # FIFO between any check and the read is refused, never waited on.
    text, _, why = rr._read_evidence(path, cwd or ".")
    if text is None:
        return f"{path} {why}"
    if len(text.encode("utf-8")) > _REFLECTION_FILE_LIMIT:
        return f"{path} is larger than {_REFLECTION_FILE_LIMIT} bytes"
    gate_lane = bool(budget.get("gate_surface"))
    parsed = rr.parse(text, gate_lane=gate_lane)  # the grammar alone, and its keys
    if parsed.problems:
        return "the reflection does not pass its grammar: " + "; ".join(parsed.problems[:5])
    if state is None:  # no owed state (direct callers): the current round, as before
        rounds = {k: [int(budget.get("count") or 0)] for k in parsed.keys}
    else:
        named = set(parsed.keys)
        owed = set(state.get("owed") or [])
        if not named & owed:
            return (
                "the reflection names none of the owed keys "
                f"({', '.join(sorted(owed)) or 'none'}), so it would credit nothing"
            )
        rounds = _key_rounds(cwd, budget, state, named, rr, deadline=deadline)
    # Admitted when some named owed key passes under EVERY round the debt
    # reader judges it under (review_late.covered_late_keys for a late key, the
    # current round for an open one); which keys it credits is still that
    # reader's call. A late key whose round cannot be placed admits nothing.
    problems: list[str] = []
    for key in sorted(rounds):
        failed = [
            problem
            for round_n in rounds[key]
            for problem in rr.parse(text, round_number=round_n, gate_lane=gate_lane).problems
        ]
        if not failed:
            break
        problems = problems or failed
    else:
        if not problems:
            return "the round of the owed keys it names cannot be placed, so it would credit nothing"
        return "the reflection does not pass its grammar: " + "; ".join(problems[:5])
    if parsed.head != budget.get("current_head"):
        return f"the reflection names head {str(parsed.head)[:12]}, not the PR head {str(budget.get('current_head'))[:12]}"
    return None


def _owed_message(state: dict, budget: dict, rr) -> str:
    """The deny for a content commit while a reflection is owed."""
    round_n = int(budget.get("count") or 0)
    gate_lane = bool(budget.get("gate_surface"))
    audit, premise = rr.obligations(round_n, gate_lane)
    owed = ", ".join(state.get("owed") or [])
    lines = [
        f"BLOCKED: review round {round_n} of PR #{budget.get('pr')} owes a round "
        f"reflection before any further commit. Owed: {owed}.",
    ]
    if not state.get("settled") and state.get("settle_until"):
        lines.append(
            f"Findings may still arrive until {str(state['settle_until'])[:16]} UTC. A "
            "reflection made now credits the keys it names; findings that land after it "
            "owe another."
        )
    needs = []
    if audit:
        needs.append("an Audit-evidence: line citing a fresh-context class audit of this round")
    if premise:
        needs.append("a Premise-evidence: line citing an independent premise check")
    lines += [
        f"This round ({'gate' if gate_lane else 'ordinary'} lane) needs "
        + ("; ".join(needs) if needs else "the reflection alone")
        + ".",
        "1. `python3 scripts/review_reflection.py status` shows what is owed.",
        f"2. Write the reflection and check it: `python3 scripts/review_reflection.py "
        f"validate --round {round_n}{' --gate' if gate_lane else ''} <file>`.",
        "3. Commit it alone: `git commit --allow-empty --cleanup=verbatim -F <file>`.",
        "Then commit the fix. Reflect first, fix second: the reflection reads the round "
        "as a class before any instance is patched.",
        "If a committed reflection still leaves a key owed, `status` and `validate` say "
        "why. Usually the reflection failed its full check (`Escalate: yes`, an evidence "
        "file written before this round started, an acceptance point with no `Scope: "
        "covered:` line): correct it and commit a second reflection. Only when a fix of "
        "that key already sits between the round head and every reflection can none at "
        "this head clear it; then push what is committed, and the next review opens a "
        "new round. If this verdict looks wrong, stop and ask the owner; do not work "
        "around it.",
    ]
    return "\n".join(lines)


def _reflection_rule(
    segs, commit_segs, command: str, cwd: str | None, budget: dict | None, *, hook_deadline
) -> tuple[str, str | None]:
    """Rule R's verdict: ``("continue", None)``, ``("reflection", None)`` for an
    admitted reflection commit, or ``("deny", message)``. Every unknown is
    ``continue`` with an advisory queued in :data:`_ADVISORIES`."""
    if budget is None or budget.get("status") != "ok":
        return "continue", None
    pr, repo = budget.get("pr"), budget.get("repo")
    if not isinstance(pr, int) or not isinstance(repo, str):
        _ADVISORIES.append("reflection gate: the PR identity is unknown, so nothing was checked.")
        return "continue", None
    try:
        import review_reflection as rr  # noqa: PLC0415 — lazy: only a PR with a budget pays

        remaining = (hook_deadline or 0.0) - time.monotonic()
        slice_s = min(_REFLECTION_SLICE, remaining - _REFLECTION_RESERVE)
        if hook_deadline is None or slice_s <= 0:
            raise TimeoutError("no time left in the hook budget")
        state = rr.owed_from_budget(
            cwd or ".",
            budget,
            repo,
            pr,
            now=datetime.now(UTC),
            cutover=rr.REFLECTION_CUTOVER_ISO,
            deadline=time.monotonic() + slice_s,
        )
    except Exception as exc:  # noqa: BLE001 — unknown is not owed (owner ruling a34481ca)
        _ADVISORIES.append(
            f"reflection gate: what this round owes could not be read ({type(exc).__name__}: "
            f"{str(exc)[:160]}), so the commit is allowed. Run "
            "`python3 scripts/review_reflection.py status` to see it."
        )
        return "continue", None
    owed = state.get("owed")
    if owed is None:
        _ADVISORIES.append(
            "reflection gate: this round's owed keys are unknown, so the commit is allowed. "
            "`python3 scripts/review_reflection.py status` names the cause."
        )
        return "continue", None
    if not owed:
        return "continue", None
    if (
        repo.casefold() == REFLECTION_EXEMPT_REPO.casefold()
        and pr in REFLECTION_EXEMPT_PRS
        and _opened_before_cutover(state, rr.REFLECTION_CUTOVER_ISO)
    ):
        # The snapshot exempts the old open contribution, never new late debt.
        # Intersect with remaining debt so credited late keys stay credited.
        late_keys = set(state.get("late_keys") or [])
        owed = [key for key in owed if key in late_keys]
        if not owed:
            return "continue", None
        state = {**state, "owed": owed}
    mode = _reflection_gate_mode()
    if len(commit_segs) == 1 and _looks_like_reflection(commit_segs[0], cwd, rr):
        try:
            problem = _reflection_problem(
                segs, command, cwd, budget, rr, deadline=hook_deadline, state=state
            )
        except Exception as exc:  # noqa: BLE001 — never admit what was not fully checked
            problem = f"it could not be checked ({type(exc).__name__}); run it again"
        if problem is None:
            return "reflection", None
        message = f"BLOCKED: this looks like a round reflection, but: {problem}."
    else:
        message = _owed_message(state, budget, rr)
    if mode == "advisory":
        _ADVISORIES.append("reflection gate (advisory mode, NOT enforced): " + message)
        return "continue", None
    return "deny", message


def _opened_before_cutover(state: dict, cutover: str | None) -> bool:
    """Whether the open round began before the go-live cutover, so a PR in the
    exempt snapshot owes nothing for it. Only an OPEN round with a readable start
    qualifies: late keys are owed only from the cutover on, so a PR owing late
    keys alone owes debt that arose after the snapshot, and an unreadable start
    proves nothing about when the round began."""
    if cutover is None:
        return True
    started = state.get("round_started")
    if state.get("round_state") != "open" or not isinstance(started, str):
        return False
    try:
        return datetime.fromisoformat(started) < datetime.fromisoformat(cutover)
    except ValueError:
        return False  # unparseable proves nothing, as an absent start does


#: Rule R's kill switch: ``hooks.reflection_gate: enforce | advisory`` in the
#: install's config. A switch on a BLOCK, so it is read here and not by
#: ``hook_ask_policy``, whose contract is that it can only ever turn off an ask.
_REFLECTION_CONFIG = "~/.genesis/config/genesis.yaml"
_REFLECTION_SEAM = "_TEST_REFLECTION_GATE"


def _reflection_path_problem(yaml, text: str) -> str | None:
    """Why ``hooks.reflection_gate`` cannot be read with certainty, or None.

    A node-graph walk, as ``hook_ask_policy`` does for its own path: a repeated
    key or a YAML merge key anywhere along it means safe_load's last-wins answer
    may not be the one the operator reads in the file."""
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    for label in ("hooks", "reflection_gate"):
        if not isinstance(node, yaml.MappingNode):
            return None
        keys = [k for k, _ in node.value if isinstance(k, yaml.ScalarNode)]
        if any(k.tag == "tag:yaml.org,2002:merge" for k in keys):
            return "uses a YAML merge key (<<)"
        values = [v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == label]
        if len(values) > 1:
            return f"declares {label} more than once"
        if not values:
            return None
        node = values[0]
    return None


def _reflection_gate_mode() -> str:
    """``"advisory"`` only when the install declares exactly that; else ``"enforce"``.

    Advisory prints what the gate would deny and allows. Every failure — no
    file, no pyyaml, a parse error, a duplicate or merge key on the path, any
    other value — enforces, with an advisory where the operator wrote something
    this reader refused: a kill switch that fails toward "off" is a bypass."""
    raw = os.environ.get(_REFLECTION_SEAM)
    if raw is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        return "advisory" if raw == "advisory" else "enforce"
    try:
        with open(os.path.expanduser(_REFLECTION_CONFIG)) as fh:
            text = fh.read()
        import yaml  # noqa: PLC0415 — lazy: the genesis venv has pyyaml

        problem = _reflection_path_problem(yaml, text)
        if problem:
            _ADVISORIES.append(
                f"hooks.reflection_gate in {_REFLECTION_CONFIG} {problem}: refused, so the "
                "reflection gate ENFORCES."
            )
            return "enforce"
        cfg = yaml.safe_load(text)
        hooks = cfg.get("hooks") if isinstance(cfg, dict) else None
        value = hooks.get("reflection_gate") if isinstance(hooks, dict) else None
    except FileNotFoundError:
        return "enforce"
    except Exception:  # noqa: BLE001 — an unreadable switch is no switch: enforce
        _ADVISORIES.append(
            f"hooks.reflection_gate in {_REFLECTION_CONFIG} could not be read: the "
            "reflection gate ENFORCES."
        )
        return "enforce"
    if value is None or value == "enforce":
        return "enforce"
    if value == "advisory":
        return "advisory"
    _ADVISORIES.append(
        f"hooks.reflection_gate in {_REFLECTION_CONFIG} is {str(value)[:40]!r}, not "
        "enforce or advisory: the reflection gate ENFORCES."
    )
    return "enforce"


def _emit_advisories() -> None:
    """Deliver queued advisories as context on an allow. Never raises."""
    if not _ADVISORIES:
        return
    text = "\n".join(_ADVISORIES)
    payload = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}
    try:
        from hook_output import print_json_bounded  # noqa: PLC0415

        print_json_bounded(payload, text_keys=("hookSpecificOutput.additionalContext",))
    except Exception:  # noqa: BLE001 — an advisory must never change the verdict
        with contextlib.suppress(Exception):
            print(json.dumps(payload))


def _is_dispatched() -> bool:
    """Whether this hook runs inside a Genesis autonomous CC session."""
    return os.environ.get("GENESIS_CC_SESSION") == "1"


def _native_ask(reason: str) -> None:
    """Emit a native PreToolUse decision and exit successfully for CC to ask."""
    emit_native_ask(reason)
    sys.exit(0)


def _unknown_budget(reason: str) -> dict:
    return {
        "status": "unknown",
        "reason": "evidence_unknown",
        "errors": [reason],
        "approval_required": True,
        "commit_approval_required": True,
        "strongly_discouraged": True,
    }


def _current_branch_pr_identity(raw: str) -> tuple[str, int] | None | dict:
    """Parse ``gh pr status --json`` for the current branch.

    ``None`` is a positive no-open-PR result.  A dict is an unknown-budget
    result.  The status command is used instead of ``pr list --head`` because
    it follows GitHub CLI's current-branch/remotes resolution and therefore
    also identifies cross-repository PRs and branches with unpushed commits.
    """
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return _unknown_budget("commit_pr_status_malformed")
    if not isinstance(payload, dict):
        return _unknown_budget("commit_pr_status_malformed")
    current = payload.get("currentBranch")
    if current is None:
        return None
    if not isinstance(current, dict):
        return _unknown_budget("commit_pr_status_malformed")
    state = current.get("state")
    if state in {"CLOSED", "MERGED"}:
        # ``gh pr status`` exposes only the NEWEST match for the branch. A closed
        # singular record does not prove no older OPEN PR exists for it, and an
        # unseen open PR may already have exhausted its budget — so "closed" is
        # unknown evidence, never proof of absence.
        return _unknown_budget("commit_pr_status_closed_only")
    number, url = current.get("number"), current.get("url")
    if state != "OPEN" or not isinstance(number, int) or not isinstance(url, str):
        return _unknown_budget("commit_pr_identity_malformed")
    match = re.fullmatch(r"https?://[^/]+/([^/]+/[^/]+)/pull/(\d+)(?:[/?#].*)?", url)
    if match is None or int(match.group(2)) != number:
        return _unknown_budget("commit_pr_identity_malformed")
    return match.group(1), number


def _normalize_public_repo(value: str) -> str | None:
    """A plain ``owner/repo``, or None. Mirrors ``git_push_guard._normalize_repo``;
    ``test_canonical_public_repo_matches_the_push_guards_reading`` locks the two
    readings of the config together."""
    if not value or "$" in value or "`" in value:
        return None
    v = value.strip().split("://", 1)[-1]
    parts = [p for p in v.split("/") if p]
    if parts and parts[0] == "github.com":
        parts = parts[1:]
    if len(parts) != 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def _canonical_public_repo() -> str | None:
    """The configured public repo (``github.user``/``github.public_repo`` in
    ``~/.genesis/config/genesis.yaml``) as ``owner/repo``, or None when it cannot
    be determined. Same source, seam (``_TEST_CANONICAL_PUBLIC_REPO``) and result
    as the push guard's ``_canonical_public_repo``; kept local because importing
    that module would load the whole push guard onto the commit path."""
    override = os.environ.get("_TEST_CANONICAL_PUBLIC_REPO")
    if override is not None:
        return _normalize_public_repo(override) if override.strip() else None
    try:
        import yaml  # lazy: the genesis venv has pyyaml; absence → None → engage.

        with open(os.path.expanduser("~/.genesis/config/genesis.yaml")) as fh:
            cfg = yaml.safe_load(fh) or {}
        gh = cfg.get("github") or {}
        user = (gh.get("user") or "").strip()
        repo = (gh.get("public_repo") or "").strip()
        if user and repo:
            return _normalize_public_repo(f"{user}/{repo}")
    except Exception:  # noqa: BLE001 — undeterminable is None, and None engages.
        return None
    return None


def _outside_public_repo(repo: str) -> bool:
    """True only when ``repo`` is PROVABLY not the configured public repo.

    An undeterminable public repo returns False — the budget still applies, which
    is the direction the gate had before it was scoped (uncertain → engage).

    Proof requires a different repository NAME, not merely a different slug. The
    configured OWNER is the weak half of that config: the shipped example carries
    a placeholder, setup can default it to a display name, and a contributor
    install names its own fork. Any of those would otherwise read the real public
    repo's PRs as "unrelated" and switch the budget off. Comparing names keeps a
    same-named fork in scope, which is the conservative direction."""
    canonical = _canonical_public_repo()
    if canonical is None:
        return False
    target = str(repo).strip().lower().removesuffix(".git")
    wanted = canonical.lower().removesuffix(".git")
    return target != wanted and target.rsplit("/", 1)[-1] != wanted.rsplit("/", 1)[-1]


#: Remote URL prefixes that are filesystem paths. gh resolves a repository only
#: from remotes on a GitHub host, so a remote spelled like this can never yield a
#: pull request. Deliberately a short closed set: any other spelling (including a
#: bare relative path) is treated as possibly-GitHub, so a miss here keeps the
#: pre-existing unknown result rather than dropping the budget.
#:
#: The set EXCLUDES two spellings that look like paths but carry a host: gh parses
#: ``//host/owner/repo`` and ``file://host/owner/repo`` to that host (MEASURED on
#: gh 2.100 — both resolved the public repo's PRs). Hence ``file:///`` (empty
#: host) rather than ``file://``, and a lone leading ``/`` checked separately.
_LOCAL_REMOTE_PREFIXES = ("./", "../", "file:///")


def _is_local_remote_url(url: str) -> bool:
    """Whether ``url`` is a filesystem path gh cannot resolve to any host."""
    if url.startswith(_LOCAL_REMOTE_PREFIXES):
        return True
    return url.startswith("/") and not url.startswith("//")


#: Git GLOBAL options the exemption tolerates before the subcommand: ``-c`` sets a
#: config value for that one invocation; ``-C`` changes directory. Neither can
#: point git at a repository other than the one the effective cwd resolves to,
#: provided ``-C`` appears at most once (``_effective_diff_cwd`` reads the first).
#: (A ``-c core.hooksPath=…`` hook could itself commit elsewhere; so could a
#: repository's own hooks, on the base code too. That is arbitrary code the
#: operator installed, outside what a pre-command check can model.)
_EXEMPTION_GIT_GLOBALS = frozenset({"-c", "-C"})


def _exemption_shape_ok(segs, command: str) -> bool:
    """Whether the probed cwd is PROVABLY the repository this command commits in.

    The remote probe below reads ``cwd`` at HOOK time. Every way to make the
    commit land somewhere else — ``--git-dir``, ``GIT_DIR=`` prefixes or exports,
    ``pushd``, ``env -C``, a second ``-C``, a wrapper, and any EARLIER segment
    that changes the directory layout before the commit runs (``git worktree
    add`` into the probed path, a ``cd`` that fails at run time under ``;`` or
    ``||``) — would make a remote-less cwd prove nothing about the repository
    actually committed to. That set is open, so this is an ALLOWLIST of one
    shape that cannot do any of it; anything else keeps the unknown result.

    Accepted: the whole command is ONE top-level ``git … commit …`` segment,
    verb literal, raw text starting with ``git`` (so no env-assignment or
    wrapper prefix was stripped), global options only ``-c <value>`` and at most
    one ``-C <dir>``. A single segment has nothing that runs before it, which is
    what closes the hook-time-versus-run-time gap — except process substitution,
    whose body the parser leaves inside the segment, so that is refused by text. ``cd … && git commit`` in a
    scratch repo therefore still asks, as it did before this scoping: that is a
    known, fail-closed residue, and ``git -C <dir> commit`` is the form to use."""
    if not segs or len(segs) != 1:
        return False
    seg = segs[0]
    if seg.depth != 0 or seg.verb_unresolved or not seg.argv:
        return False
    # The one way a single parsed segment still runs a second command: process
    # substitution. shell_parse leaves `<(…)`/`>(…)` bodies inside argv rather
    # than emitting them as segments (its documented gap), while bash runs them.
    # Tested on the RAW COMMAND: the segment's own ``raw`` has the `<`/`>`
    # already stripped (MEASURED), so it cannot see the construct. A plain
    # substring test is safe here: a false match only keeps unknown.
    if "<(" in command or ">(" in command:
        return False
    if seg.argv[0] != "git" or not seg.raw.lstrip().startswith("git"):
        return False
    dash_c = 0
    i = 1
    while i < len(seg.argv) and seg.argv[i].startswith("-"):
        opt = seg.argv[i]
        if opt not in _EXEMPTION_GIT_GLOBALS or i + 1 >= len(seg.argv):
            return False
        dash_c += opt == "-C"
        i += 2
    if dash_c > 1:
        return False
    return git_subcommand(seg.argv) == "commit" and i < len(seg.argv) and seg.argv[i] == "commit"


def _has_possibly_github_remote(cwd: str, deadline: Deadline) -> bool | None:
    """Whether ``cwd``'s repository has anything gh might resolve to GitHub.

    Reads ``git remote -v``, which is also what gh reads, and which applies
    ``url.<base>.insteadOf`` rewrites (MEASURED: a filesystem remote rewritten to
    a GitHub URL is listed as the GitHub URL). Also reads the branch push/pull
    targets: ``git push -u <url>`` leaves ``branch.<b>.remote`` set to a URL with
    no remote defined, and that branch can have an open PR even though gh finds
    no remote (MEASURED by audit). None = could not tell.

    A branch target is USUALLY a remote NAME, not a URL: ``git clone <path>``
    writes ``branch.<b>.remote=origin`` (MEASURED, git 2.43). A name is resolved
    through the remotes ``git remote -v`` just listed, and it is possibly GitHub
    exactly when one of that remote's URLs is — which the first loop has already
    answered, so a name that resolves there adds nothing. Only a value that is
    NOT a listed remote is classified as a URL/path in its own right; an unknown
    name therefore stays possibly-GitHub, the conservative direction."""
    try:
        read = subprocess.run(
            ["git", "-C", cwd, "remote", "-v"],
            capture_output=True,
            text=True,
            timeout=deadline.timeout(3.0),
            check=False,
        )
        targets = subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "config",
                "--get-regexp",
                r"^(branch\..*\.(remote|pushremote)|remote\.pushdefault)$",
            ],
            capture_output=True,
            text=True,
            timeout=deadline.timeout(3.0),
            check=False,
        )
    except Exception:  # noqa: BLE001 — could not tell; the caller keeps unknown.
        return None
    if read.returncode != 0:
        return None
    # `git config --get-regexp` exits 1 when nothing matches; anything else is an error.
    if targets.returncode not in (0, 1) or (targets.returncode == 1 and targets.stdout.strip()):
        return None
    remote_names: set[str] = set()
    for line in read.stdout.splitlines():
        if not line.strip():
            continue
        name, sep, rest = line.partition("\t")
        if not sep:
            return None
        remote_names.add(name.strip())
        url = rest.rsplit(" (", 1)[0].strip()
        # A remote whose listed URL is EMPTY gives gh nothing to resolve. The common
        # cause: any `remote.<name>.*` key makes git list the name, so one global key
        # (say `remote.origin.prune` in ~/.gitconfig) puts a URL-less `origin` in
        # every repository that has no remote (MEASURED: a bare `origin<TAB>` line).
        # It is not the only cause — an empty or whitespace-only `url`, a remote with
        # only gh's own `gh-resolved` key, and an `insteadOf` rewrite that empties
        # the URL all list the same way — and the skip covers every one, because the
        # test is on the listed URL, not on how it came to be empty. A URL git does
        # know is listed on its own line, a push-only URL included.
        #
        # Skipping it hides nothing ONLY because gh resolves a repository from this
        # same `git remote -v` listing: MEASURED with gh 2.101 and git 2.43, gh
        # answered "no git remotes found" in each of the states above. A gh that
        # resolved from raw config instead would turn them into repositories this
        # gate skips; re-check this premise when the gh version moves.
        if not url:
            continue
        if not _is_local_remote_url(url):
            return True
    # Every listed remote's fetch AND push URL is a filesystem path by now.
    for line in targets.stdout.splitlines():
        if not line.strip():
            continue
        _key, sep, value = line.partition(" ")
        value = value.strip()
        if not sep or not value:
            return None
        # "." is the local repository itself. A listed remote NAME resolves to
        # the URLs just classified, all local. A filesystem path is not GitHub.
        # Anything else — an unlisted name or a URL — might be, so it counts.
        if value == "." or value in remote_names:
            continue
        if not _is_local_remote_url(value):
            return True
    return False


def _no_pr_possible_or_unknown(
    cwd: str, deadline: Deadline, reason: str, segs, command: str
) -> dict | None:
    """After a failed PR lookup: None when gh cannot resolve any repository here.

    A repository with nothing gh could resolve to GitHub (no remote or branch
    target that might be GitHub, no ``GH_REPO``) gives gh no repository to find a pull
    request in, so there is no budget to consult: a scratch or lab repo. The
    claim is about what gh can resolve, not about GitHub at large, so it is only
    made when the command provably commits in the probed repository
    (``_exemption_shape_ok``). Any doubt keeps the unknown result, which asks in
    the foreground and denies when dispatched."""
    if os.environ.get("GH_REPO", "").strip():
        return _unknown_budget(reason)
    if any(os.environ.get(name) for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")):
        return _unknown_budget(reason)
    if not _exemption_shape_ok(segs, command):
        return _unknown_budget(reason)
    if _has_possibly_github_remote(cwd, deadline) is False:
        return None
    return _unknown_budget(reason)


#: This hook is registered in .claude/settings.json with `"timeout": 10`, and an
#: overrun SIGKILLs it -- which FAILS OPEN: the commit proceeds with neither the
#: budget check nor the review-current and depth checks that run after it. The
#: lookup is several serial GitHub calls of 6-8s each, so a degraded-but-not-dead
#: GitHub keeps every individual call inside its own cap while the total runs far
#: past 10s. These bound the whole lookup instead.
#:
#: The split leaves ~2.5s for everything else this hook does after the lookup
#: (diff classification, marker reads, message rendering), all of it local.
_COMMIT_HOOK_REGISTERED_TIMEOUT = 10.0
_COMMIT_BUDGET_LOOKUP_SECONDS = 7.5
_COMMIT_POST_LOOKUP_SECONDS = 2.0


def _branch_review_budget(
    cwd: str | None,
    branch: str | None,
    *,
    budget_seconds: float = _COMMIT_BUDGET_LOOKUP_SECONDS,
    deadline: Deadline | None = None,
    segs=None,
    command: str = "",
) -> dict | None:
    """The current branch PR's shared budget; None means no budget applies.

    SCOPE: the budget belongs to an open PR on the configured PUBLIC repo, and to
    nothing else. Two cases therefore return None, and only these two:

    * the lookup failed, but the repository has no remote gh could resolve to
      GitHub (no remote or branch target that might be GitHub — a target naming
      a remote counts by that remote's URLs — and ``GH_REPO`` unset) and the
      command provably commits in that repository — gh has no repository to find
      a pull request in (``_no_pr_possible_or_unknown``; ``segs`` is the parsed
      command, and None keeps the unknown result);
    * the branch's open PR lives in a repository that is PROVABLY not the
      configured public one (``_outside_public_repo``).

    Everything else keeps the pre-existing direction. A read/import/identity
    failure where a GitHub remote exists is an ``unknown`` result, never the same
    as no pull request. So is running out of time: one aggregate deadline covers
    every network call here, and exhausting it asks a human rather than letting
    the harness kill the hook and allow the commit unchecked. Tests select a PR
    with ``_TEST_REVIEW_BUDGET_PR`` and feed the shared evaluator through its
    endpoint seams.
    """
    if not cwd or not branch:
        return _unknown_budget("commit_pr_identity_unknown")
    lookup_deadline = (
        deadline.capped_after(budget_seconds, reserve=_COMMIT_POST_LOOKUP_SECONDS)
        if deadline
        else Deadline.after(budget_seconds)
    )
    try:
        import review_budget
    except Exception:  # noqa: BLE001 — reverse version skew asks/denies.
        return _unknown_budget("review_budget_unavailable")

    test_pr = os.environ.get("_TEST_REVIEW_BUDGET_PR")
    if test_pr is not None:
        if test_pr == "none":
            return None
        repo = os.environ.get("_TEST_REVIEW_BUDGET_REPO", "owner/repo")
        if not test_pr.isdigit():
            return _unknown_budget("commit_pr_identity_unknown")
        if _outside_public_repo(repo):
            return None
        remaining = lookup_deadline.remaining()
        result = review_budget.evaluate_pr(
            repo, test_pr, budget_seconds=max(0.0, remaining or 0.0)
        )
        result["pr"] = int(test_pr)
        result["repo"] = repo
        return result

    try:
        try:
            pr_read = subprocess.run(
                [
                    "gh",
                    "pr",
                    "status",
                    "--json",
                    "number,state,url",
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                # Share the one deadline: a flat 8 here plus the evaluator's own
                # serial calls is exactly what overran the harness window.
                timeout=lookup_deadline.timeout(8.0),
                check=False,
            )
        except (FileNotFoundError, PermissionError):
            # gh could not be launched at all. Same question as a failed read.
            return _no_pr_possible_or_unknown(
                cwd, lookup_deadline, "commit_pr_gh_unavailable", segs, command
            )
        if pr_read.returncode != 0:
            return _no_pr_possible_or_unknown(
                cwd, lookup_deadline, "commit_pr_list_unreadable", segs, command
            )
        identity = _current_branch_pr_identity(pr_read.stdout)
        if identity is None:
            return None
        if isinstance(identity, dict):
            return identity
        repo, number = identity
        if _outside_public_repo(repo):
            return None
        remaining = lookup_deadline.remaining()
        result = review_budget.evaluate_pr(
            repo, number, budget_seconds=max(0.0, remaining or 0.0)
        )
        result["pr"] = number
        result["repo"] = repo
        return result
    except Exception:  # noqa: BLE001 — an uncertain budget cannot allow silently.
        return _unknown_budget("commit_pr_lookup_failed")


def _commit_budget_reason(result: dict) -> str:
    if result.get("status") != "ok":
        return (
            "The open pull request's review history could not be read reliably. "
            "Approve this one commit only if you independently verified that the "
            "review budget permits it; autonomous sessions are denied."
        )
    pr = result.get("pr")
    count = int(result.get("count") or 0)
    # The fix-commit approval is protected exactly like the review-request one, so
    # it owes the owner the same terminal framing — without it, continued round-5
    # work reads as routine at the one surface where the decision is actually made.
    # Single-sourced from the evaluator so the two gates cannot drift: the push
    # guard's `_review_budget_message` states the identical rule. Imported locally,
    # mirroring `_branch_review_budget`; reaching this point required that import
    # to have already succeeded there, since `status == "ok"` comes from it.
    import review_budget  # noqa: PLC0415 — local by the same convention as above.

    if result.get("gate_surface"):
        # Unlike the push guard's gate-surface branch, this one has no
        # still-within-budget sub-state to get wrong: reaching here needs
        # `commit_approval`, which is `count > GATE_DISCOVERY_ROUND_LIMIT` or the
        # limit with its one confirmation already spent, while
        # `confirmation_exempt` requires the limit with it UNSPENT. The
        # two are mutually exclusive, so the budget genuinely IS spent and the
        # terminal decision is the truthful thing to name. Recorded so the next
        # audit does not re-raise the push guard's defect against this branch.
        return (
            f"PR #{pr} changes the review-gate surface and has {count} review "
            f"rounds. Its {review_budget.GATE_DISCOVERY_ROUND_LIMIT} discovery "
            "rounds plus confirmation are spent. "
            f"{review_budget.TERMINAL_DECISION} Approve this one additional fix "
            "commit only; earlier approval does not carry forward."
        )
    if result.get("strongly_discouraged"):
        # TERMINAL_DECISION, never ORDINARY_TERMINAL_RULE: this branch fires at
        # STRONGLY_DISCOURAGED_REVIEWED_HEADS, so the rule's "there is no ordinary
        # round 5" would be rendered to someone who already holds five heads.
        return (
            f"PR #{pr} has {count} review rounds — past the terminal "
            "boundary. Further work is strongly discouraged: stop, narrow or "
            "redesign, accept documented residue, or abandon it. "
            f"{review_budget.TERMINAL_DECISION} Approve only this single additional "
            "fix commit; every commit past the boundary needs its own approval, and "
            "earlier approval never carries forward."
        )
    # The RULE, not the NOTICE: this gate authorizes a COMMIT, so the notice's
    # "approve one further round" clause would be false here and would contradict
    # the sentence after it. The boundary claim is true in this branch, where the
    # count is exactly the ordinary limit.
    return (
        f"PR #{pr} has {count} review rounds; standing authorization ended "
        f"after {review_budget.STANDING_REVIEWED_HEAD_LIMIT}. "
        f"{review_budget.ORDINARY_TERMINAL_RULE} Approve this single fix commit only — "
        "it authorizes no review round, and a previous approval cannot authorize "
        "another commit."
    )


def main() -> None:
    # Parse tool input
    payload = read_payload()
    command = field(payload, "command")
    # Hand the command over ONCE, here, where it is already extracted: stdin is
    # consumed by read_payload, so nothing further down can read it again.
    if discarded_write is not None:
        discarded_write.remember(command)
    # Read through `mentions`: a word the shell assembles from quotes,
    # backslashes or joined lines is not spelled in the raw text, and a raw test
    # would skip the parse that resolves it. Widen-only — the parse below still makes every decision. Keep
    # identical to the invalidator's early-out, or a commit this gate checks can
    # leave that module's marker standing.
    if not mentions(command, _COMMIT_PATTERN):
        # Nothing in the text names a commit, and one could still run: a git
        # subcommand the shell builds says nothing about which it is. Asked only
        # when the text names git, and answered by the parse, so an ordinary
        # variable in an ARGUMENT is not refused. The invalidator's early exit is
        # deliberately NOT widened to match: it runs after a command succeeds,
        # and every command this branch catches is refused before it runs.
        if mentions(command, _GIT_WORD) and "git" in unresolved_verb_programs(command):
            _deny(_HIDDEN_GIT_VERB_MSG)
        sys.exit(0)  # Not a commit, allow

    # Parse the command into the segments it actually executes (through
    # wrappers, bash -c, command substitutions). Reused for Rule 0, the
    # add-chain detection, and the override binding.
    #
    # `blind` is the same parse telling us whether it could read ALL of the command
    # — an empty result means "found no commit" only when it is not also "stopped
    # looking". One call answers both, so the two can never disagree.
    segs, blind = analyze_checked(command)

    # The cheap _COMMIT_PATTERN early-out can match "git commit" mentioned in a
    # string (a reply body, an echo). Confirm a REAL executed commit segment
    # before applying the branch/review rules, else allow.
    if not any(git_subcommand(s.argv) == "commit" for s in segs):
        # A missing segment is not proof of absence when the same checked
        # parse reports blindness. Refuse in every session type: user ruling
        # 2026-09-08 prices a false positive in an agent rewrite, not a human
        # approval. Keep main's bounds refusal and its cause-specific remedy.
        if blind is not None:
            _deny(
                f"BLOCKED: this command {blind.cause} and mentions a commit; a "
                "Git operation may run, but review enforcement cannot establish "
                "whether it is a commit.\n"
                f"To proceed: {blind.hint}. If you are WRITING TEXT (a commit "
                "message, a plan, review notes) whose content merely mentions "
                "a commit, use the Write tool instead of a here-doc. If you are "
                "RUNNING a git command, split unreadable compounds or rewrite "
                "it in a directly-parseable form (plain quotes, or `git commit "
                "-F <file>`)."
            )
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
            "Remove it and establish a review first (`/review` where the optional "
            "`superpowers` plugin is installed, else `/deep-review`, or review and "
            "then `python3 scripts/review_state.py mark --agent-output <file>`)."
        )
        return

    # One aggregate deadline for the WHOLE hook, opened before the first local
    # git probe. The hook is registered with `"timeout": 10`, and an overrun is a
    # SIGKILL — which FAILS OPEN: the commit proceeds with neither the budget
    # check nor the review-current/depth checks behind it. Per-call subprocess
    # caps each reset the clock, so serial local probes (worktree/branch/counter
    # reads, then the budget lookup) could overrun the window in aggregate; every
    # probe below draws from this one deadline instead. The small reserve keeps
    # room to print the decision and exit before the kill lands.
    hook_budget = Deadline.after(_COMMIT_HOOK_REGISTERED_TIMEOUT - 0.5)
    hook_deadline = hook_budget.expires_at

    # Resolve the dir the commit actually targets (git -C / the LAST cd before
    # the commit segment / payload cwd). A decoy `cd A && …; cd B && git commit`
    # runs in B, and B is what we must inspect. An ambiguous cwd (variable /
    # subshell / depth>0 / nonexistent dir) yields the _CWD_UNKNOWN sentinel →
    # fail closed (the Rule-1 loop below denies it for every commit segment).
    # `cwd` (first commit segment's dir) feeds the round/staged/marker checks.
    eff_cwd = _effective_diff_cwd(command, payload, segs)
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
            marker_content_current,
        )
    except Exception:  # noqa: BLE001 — ANY load failure, not just absence.
        # If review_state cannot be loaded, fail open — don't block.
        #
        # WIDENED from `except ImportError` deliberately, and the wrapper below is
        # exactly why. A SyntaxError (or any top-level runtime error) in
        # review_state is NOT an ImportError, so it used to escape this handler
        # and exit 1 — non-blocking, so the commit proceeded. Now that main() runs
        # under run_guard, that same escape converts to a hard BLOCK instead, and
        # MEASURED it blocks EVERY commit on the box: the trailing
        # `# review-override` cannot rescue it either, because the override is
        # parsed after this import. That includes the commit that would repair
        # review_state, so the gate would wall off its own fix.
        #
        # The sibling (scripts/hooks/git_push_guard.py, the ESCALATION_ROUND_CAP
        # import) widened the same handler for the same reason, but its degrade
        # is NOT the same and the difference matters: it falls back to a default
        # constant and keeps every gate armed, whereas this one abandons the
        # whole gate — the direct-to-main rule, the review-marker requirement and
        # the round rules all stop applying. That is a big loss, chosen over a
        # bigger one, and it is the pre-existing behaviour for the absent case
        # rather than something new. (The narrower handler was not "no guard": it
        # caught absence and missed everything else.)
        print(
            "WARNING (review_enforcement_commit): review_state failed to load — "
            "the review gate is DISABLED for this commit (fail-open). Fix "
            "scripts/review_state.py; commits are unguarded until you do.",
            file=sys.stderr,
        )
        sys.exit(0)

    # Rule 1: Block commits on main. Fail closed when the cwd is ambiguous — we
    # cannot prove the commit is NOT landing on main, so treat it as such. The two
    # causes get DISTINCT messages: an unresolvable cwd is NOT a branch violation,
    # and labeling it "Direct commits to main" sent sessions chasing a branch
    # problem they didn't have (the recurring worktree false-block, root-caused
    # 2026-08-10). cwd_unknown is checked FIRST — when the cwd can't be resolved,
    # a branch read would use the hook's own cwd and is meaningless.
    #
    # DELIBERATELY teach-only: the gate never resolves a `$VAR` cwd, even from a
    # same-command literal assignment. Four adversarial-review rounds (2026-08-11)
    # each found a distinct false-ALLOW-to-main in static variable resolution
    # (source/eval/read reassignment, export multi-assign, shell functions,
    # $PWD/CDPATH semantics, printf -v, multiple -C) — bash variable STATE is not
    # statically resolvable, so we don't try (see
    # feedback_no_handrolled_shell_parsing_for_guards).
    #
    # EVERY commit segment is checked, not just the first — a chained
    # `git commit … && git -C /elsewhere commit …` runs its second commit in a
    # DIFFERENT dir (audit finding 3, 2026-08-11, confirmed by execution).
    all_commit_segs = [s for s in segs if git_subcommand(s.argv) == "commit"]

    # Branch-mutation guard (Codex P1 #1371): get_current_branch() below reads the
    # branch ONCE at hook time, before any segment runs. A `git switch main` /
    # `git checkout main` (or an unresolvable target) EARLIER in the chain than a
    # commit lands that commit on a branch this read cannot see — slipping the
    # direct-to-main block and the per-branch review budget. Refuse every recognized
    # HEAD-moving form before a commit: even a literal non-main target can have a
    # different PR budget from the branch sampled at hook entry. File-restore forms
    # still pass because they do not move HEAD.
    #
    # SCOPE IS CONSCIOUSLY BOUNDED — do NOT keep adding forms one at a time (user
    # decision 2026-08-12, after the escalation cap fired on this axis). This covers
    # the COMMON, accident-plausible ways HEAD reaches main before a commit — bare
    # `switch/checkout main`, `-b/-B/-c/-C main` (separate / attached / clustered /
    # long `--create=`), `-`/`$VAR` targets. The exotic HEAD-movers — a git long-option
    # ABBREVIATION (`--force-c=main`), reflog `@{-1}`, a remote-tracking ref resolving
    # to main, `git symbolic-ref`, and user checkout ALIASES (`co`) — are ACCEPTED
    # RESIDUE: they are deliberate-evasion, outside this accident-prevention gate's
    # threat model (a deliberate evader already has `python -c 'subprocess…'`, which no
    # Bash-string guard can see). A future Codex/architect finding of one of THOSE is
    # a `tabled` residue record, NOT another patch here (feedback_no_handrolled_shell_
    # parsing_for_guards / feedback_cli_guard_pflag_clustering).
    if all_commit_segs:
        last_commit_i = max(i for i, s in enumerate(segs) if git_subcommand(s.argv) == "commit")
        for i, s in enumerate(segs):
            if i >= last_commit_i:
                break
            if git_subcommand(s.argv) in _BRANCH_MUTATING_SUBCMDS and _branch_mutation_risk(s.argv):
                _deny(
                    "BLOCKED: this command switches branches before a commit, so the "
                    "guard cannot verify the destination branch's review budget. Run the "
                    "branch switch and the commit as SEPARATE commands."
                )
                return

    seg_cwds = []
    for seg in all_commit_segs:
        seg_cwd = _effective_diff_cwd(command, payload, segs, commit_seg=seg)
        if seg_cwd is _CWD_UNKNOWN:
            _deny(
                "BLOCKED: cannot verify which branch this commit lands on — its "
                "working directory comes from a shell variable, command "
                "substitution, subshell, or unexpanded/nonexistent path that this "
                "guard cannot safely evaluate, so it fails closed. Re-run with a "
                "LITERAL absolute path: `cd /abs/path && git commit …` or "
                '`git -C /abs/path commit …` (not `cd "$VAR"`).'
            )
            return
        seg_cwds.append(seg_cwd)
        seg_branch = get_current_branch(
            cwd=seg_cwd if isinstance(seg_cwd, str) else None, deadline=hook_deadline
        )
        if seg_branch in ("main", "master"):
            _deny(
                "BLOCKED: Direct commits to main are not allowed. "
                "Create a branch first: git checkout -b <scope>/<description>"
            )
            return
        if seg_branch == "live" and _live_integration_repo(
            seg_cwd if isinstance(seg_cwd, str) else None, deadline=hook_deadline
        ):
            # `live` is the local integration branch: origin/main plus the
            # candidate branches in ~/.genesis/deploy_manifest.json, rebuilt with
            # `git commit-tree`. A hand commit here is one the planned rebuild
            # engine (not built yet) will refuse as unknown, so it belongs on a
            # branch cut from origin/main.
            _deny(
                "BLOCKED: Commit on 'live', the local integration branch. It is "
                "rebuilt from origin/main plus the candidates in "
                "~/.genesis/deploy_manifest.json. Commit on a branch cut from "
                "origin/main (git worktree add --no-track "
                ".claude/worktrees/<name> -b <scope>/<desc> origin/main), then "
                "add it as a candidate."
            )
            return
    # Commits chained into DIFFERENT dirs share this one gate, but the review
    # marker checked below (Rule 2) belongs to the FIRST commit's worktree only —
    # a second repo's commit would ride an approval that never inspected it.
    # Mirror the push guard's multiple-publish rule: one gated commit dir per
    # command. (Same-dir chains — `git commit … ; git commit --amend` — pass.)
    # ``None`` (no payload cwd → the hook's own cwd) counts as its own dir: it is
    # not provably equal to an explicit path, so a mixed chain fails closed.
    # Compare by GIT WORKTREE IDENTITY, not the raw path: the marker and index are
    # owned by the worktree ROOT, so two different SUBDIRECTORIES of one worktree —
    # and a symlink alias of it — are ONE gated unit and must not read as different
    # dirs (Codex P2, #1371; a bare-realpath compare still false-blocked the
    # different-subdir case). Only resolved when ≥2 commits actually chain, to avoid
    # the extra git call on the common single-commit path.
    if len(all_commit_segs) >= 2:
        distinct_dirs = {
            _worktree_root(c, deadline=hook_deadline) if isinstance(c, str) else c
            for c in seg_cwds
        }
        if len(distinct_dirs) > 1:
            _deny(
                "BLOCKED: this command chains git commits into DIFFERENT worktrees, "
                "which would share a single review gate (the review marker covers only "
                "the first commit's worktree). Run each commit as its own command so "
                "each is gated separately."
            )
            return

        # Content-binding guard (Codex P1 #1371): a same-worktree chain can record
        # UNREVIEWED content in a later commit under the first commit's marker — the
        # marker + index are sampled ONCE at hook time. ALLOWLIST the one shape the
        # hook can prove records the reviewed diff, and fail closed on everything else:
        # a blocklist of "dangerous staging subcommands" leaked repeatedly (a `git add`
        # between, `-am`/pathspec on a later commit, `git stash pop --index`,
        # cherry-pick) — the architecture signal from
        # feedback_no_handrolled_shell_parsing_for_guards. Provably safe iff EVERY
        # commit after the first is a PURE `--amend` (no -a/-i/-o/-p/pathspec, so it
        # re-records the first commit's content, not new material) AND NO other git
        # command runs between the first and last commit (a non-commit git segment
        # could re-stage; an unknown git subcommand fails closed). Non-git commands
        # (echo/sed/build steps) are fine — a pure `--amend` never stages unstaged
        # working-tree edits. Anything else → run each commit as a separate command.
        commit_positions = [i for i, s in enumerate(segs) if git_subcommand(s.argv) == "commit"]
        impure_later_commit = any(
            "--amend" not in s.argv or _commit_can_select_content(s.argv)
            for s in all_commit_segs[1:]
        )
        git_between_commits = any(
            git_subcommand(segs[i].argv) not in (None, "commit")
            for i in range(commit_positions[0] + 1, commit_positions[-1])
        )
        if impure_later_commit or git_between_commits:
            _deny(
                "BLOCKED: this chains multiple commits in one worktree in a shape the "
                "single review gate cannot bind to the reviewed diff — a later commit "
                "stages its own content (-a/-i/-o/-p or a pathspec), is not a pure "
                "`--amend`, or another git command runs between the commits. Run each "
                "commit as a SEPARATE command so each is gated against its own "
                "reviewed diff."
            )
            return

    # Rule R: a round's reflection comes before its fixes (see _reflection_rule).
    # It reads the same budget as the round-approval ask below, and sits where the
    # retired streak tiers sat ON PURPOSE: before the docs/config skip, so a
    # prose-only fix cannot slip past an owed reflection, and before Rule 2, so
    # '# review-override' does not waive it. Rules 0 and 1 (above) still apply to
    # a reflection commit.
    commit_segs = [s for s in segs if git_subcommand(s.argv) == "commit"]

    branch = get_current_branch(cwd=cwd, deadline=hook_deadline)
    cloud_budget = _branch_review_budget(
        cwd, branch, deadline=hook_budget, segs=segs, command=command
    )
    pending_round_approval = bool(
        cloud_budget is not None
        and (
            cloud_budget.get("status") != "ok"
            or cloud_budget.get("commit_approval_required")
        )
    )

    if pending_round_approval:
        separately_gated = [
            s
            for s in segs
            if git_subcommand(s.argv) in {"push", "merge"}
            # EVERY ``gh pr comment`` counts separately: an inline body can hide a
            # review request the argv scan sees, but a ``--body-file``/editor body
            # is opaque here — gating on the visible ``@codex review`` token let one
            # approval cover a request it never read.
            or gh_pr_subcommand(s.argv) in {"create", "merge", "close", "comment"}
        ]
        if len(commit_segs) != 1 or separately_gated:
            _deny(
                "BLOCKED: this review-budget approval may authorize exactly one fix "
                "commit. Run multiple commits and push/merge/close/PR-create/review "
                "actions separately so each receives its own decision."
            )
            return

    def _allow() -> None:
        """Exit through the one approval chokepoint after all hard checks pass."""
        # Defense in depth: even a future helper that accidentally degrades a
        # deadline failure must not reach an allow after the hook budget expired.
        hook_budget.timeout(0.001)
        if pending_round_approval and cloud_budget is not None:
            reason = _commit_budget_reason(cloud_budget)
            # A stale worktree still emits the retired terminal sigil. It is
            # parsed but authorizes nothing — name that so an old habit does
            # not read the ask as a gate misfire.
            if commit_segs and all(
                has_trailing_override(s.raw, "final-round-accept") for s in commit_segs
            ):
                reason += (
                    "\n\nNOTE: `# final-round-accept` is retired and no longer "
                    "authorizes any gate; only this native approval admits the "
                    "commit."
                )
            if _is_dispatched():
                _deny("BLOCKED: " + reason)
                return
            if _ADVISORIES:
                reason += "\n\n" + "\n".join(_ADVISORIES)
            _native_ask(reason)
        _emit_advisories()
        sys.exit(0)

    verdict, rule_r_message = _reflection_rule(
        segs, commit_segs, command, cwd, cloud_budget, hook_deadline=hook_deadline
    )
    if verdict == "deny":
        _deny(rule_r_message or "BLOCKED: a round reflection is owed.")
        return
    if verdict == "reflection":
        # An admitted reflection is empty and is the step a round past its
        # budget should take, so it skips the round-approval ask (plan N7).
        _emit_advisories()
        sys.exit(0)

    # Docs/config-only skip: a commit whose ENTIRE staged set is documentation
    # or config carries no code to review (adaptive-review "review level: None"),
    # so skip Rule 2 for it. Conservative fail-toward-review default: we skip ONLY
    # when (a) the commit is PURE — it cannot add/select content beyond the
    # current index (no pathspec, no -a/-i/-o/-p, and no git add/rm/mv/reset in
    # the chain, P1-D) — AND (b) the staged set is non-empty and every path is on
    # the docs/config allowlist. An impure commit, an empty staged set, a diff we
    # cannot read, or an ambiguous cwd (already blocked by Rule 1) falls through
    # to normal enforcement — never skipped. Placed AFTER Rule 0 (--no-verify),
    # Rule 1 (main-branch), and Rule R (the round reflection) so it can never
    # weaken those hard blocks.
    # Whether the commit may stage content BEYOND the current index — a pathspec, -a/-i/-o/-p,
    # or a git add/rm/mv/reset in the chain (P1-D). When true the hook-time index does NOT
    # reflect what will be committed, so we can neither classify the staged diff nor bind the
    # marker to it; depth (Rule 2.5) and Rule 2 fall back to the recorded/valid marker. This
    # is the SAME predicate the docs-skip uses, so `git commit -am` / a pathspec commit is
    # handled exactly like `git add && commit` — NOT just a bare `git add` (which the old
    # add-only check missed, letting `commit -am` of unstaged substantial work slip the gate).
    commit_may_add_content = _commit_may_add_content(segs)

    if not commit_may_add_content:
        staged = _staged_files(cwd, deadline=hook_deadline)
        if staged and all(_is_docs_or_config(p) for p in staged):
            _allow()

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

        staged_level = classify_change_substantiality(cwd=cwd, deadline=hook_deadline)
    except DeadlineExpired:
        raise
    except Exception:  # noqa: BLE001 - depth is advisory; never crash the gate
        staged_level = "unknown"
    try:
        from review_state import get_marker_depth

        marker_level, marker_adversarial = get_marker_depth(cwd=cwd, deadline=hook_deadline)
    except DeadlineExpired:
        raise
    except Exception:  # noqa: BLE001 - a marker-read error must not block on the add-chain
        # path (fail OPEN there); on the normal path the is_review_current() guard below
        # re-reads the marker and fails CLOSED, the safe direction for the stricter gate.
        marker_level, marker_adversarial = None, True

    if commit_may_add_content:
        # The hook-time index does NOT reflect what will be committed (`git add && commit`,
        # `git commit -am`, a pathspec/-i/-o/-p commit, …), so the staged classify reads
        # "inline"; depth falls back to the marker's RECORDED level (else a substantial
        # change committed this way would slip the gate). We cannot content-bind here
        # (the real diff isn't staged yet), so the marker's adversarial bit is trusted as
        # recorded — a KNOWN content-blindness limitation (tracked follow-up), symmetric
        # with Rule 2 below.
        depth_level = marker_level
        depth_is_adversarial = marker_adversarial
    else:
        # Normal commit: the staged index IS the commit content, so classify it
        # directly. Crucially, the marker's adversarial clearance counts ONLY when the
        # marker genuinely BINDS this diff's CONTENT — marker_content_current compares the
        # recorded FULL-content hash to the staged content (not the stat-only diff_hash,
        # which a same-shape swap collides under) and fails CLOSED on mismatch/absence/
        # error. Otherwise an adversarial audit of an EARLIER diff A would falsely clear a
        # later substantial diff B that a '# review-override' then waives past Rule 2's
        # staleness block, when B was never audited (the integrity hole this gate closes).
        depth_level = staged_level
        depth_is_adversarial = marker_adversarial and marker_content_current(
            cwd=cwd, deadline=hook_deadline
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
                "lines, or >1 code file, or an auth/api/migrations file, or a prompt/agent/"
                "skill surface) but the review is not an ADVERSARIAL audit. A "
                "precision-filtered 'no findings' "
                "inline pass is FALSE CONFIDENCE, not clearance. Dispatch a genesis-architect "
                "adversarial audit (assume bugs, enumerate the edge/boundary/sentinel/"
                "hierarchy class, READ authoritative semantics for any domain code), save it "
                "to the per-worktree path from `python3 scripts/review_state.py evidence-path` "
                "(concurrent sessions don't clobber it), then re-mark:\n"
                "  python3 scripts/review_state.py mark   # a genesis-architect audit is "
                "INTERNAL — it satisfies this depth gate and never moves the cross-model "
                "streak; no outcome flag needed\n"
                "If the audit genuinely ran but its format isn't recognized, acknowledge with "
                "a trailing shell comment (outside any quotes):  # depth-ack"
                + _merge_note(cwd, gate="depth", deadline=hook_deadline)
            )
            return

    # Rule 2: Block commits without review (on branches).
    if commit_may_add_content:
        # The staged diff at hook time isn't what will be committed (staging deferred, -a,
        # a pathspec, …) — can't check the diff hash, so require a valid (unexpired) marker.
        rule2_blocks = not has_valid_review_marker(cwd=cwd, deadline=hook_deadline)
    else:
        rule2_blocks = has_code_changes(cwd=cwd, deadline=hook_deadline) and not is_review_current(
            cwd=cwd, deadline=hook_deadline
        )

    if rule2_blocks:
        # A trailing '# review-override' comment (outside quotes) acknowledges
        # accepted findings and bypasses ONLY this review gate — never Rule 0
        # (--no-verify), Rule 1 (main-branch), or Rule R (the round reflection),
        # all checked above.
        override = _commit_override(command, segs)
        if override == "valid":
            print(
                "NOTE: review-override honored — commit review gate bypassed. "
                "Findings acknowledged by session.",
                file=sys.stderr,
            )
            _allow()
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
            "Run an adversarial audit first — `/review` where the optional `superpowers` "
            "plugin is installed, else `/deep-review`, or dispatch the genesis-architect "
            "agent — save it to `python3 scripts/review_state.py evidence-path`, then "
            "re-mark: `python3 scripts/review_state.py mark --agent-output <that file>` "
            "(a genesis-architect audit is INTERNAL and satisfies this gate; it never "
            "moves the cross-model streak — no outcome flag needed). Only a "
            "non-Anthropic cross-model review (Codex/Kimi) is marked `--source external "
            "--defects|--clean`.\n"
            "If findings are intentionally accepted, append a trailing shell "
            "comment (outside any quotes): '  # review-override'"
        )
        return

    # All checks passed — allow
    _allow()


def _deny(message: str) -> None:
    """Output denial message and block the tool via exit code 2.

    Every refusal in this file funnels through here, so the discarded-command
    note is emitted once, at the single chokepoint, rather than at its 14 call
    sites. (It was 15 when this was written; the blind-spot net's own refusal was
    removed upstream in the interval, which is the ordinary fate of a count kept
    in prose — the load-bearing claim is "sole chokepoint", and that is checked:
    this file has exactly one ``sys.exit(2)``, the one below.)
    """
    # Advisories queued before the deny (e.g. why the reflection kill switch was
    # refused) belong with it: a deny is the one exit whose stderr is delivered.
    if _ADVISORIES:
        message = message + "\n\n" + "\n".join(_ADVISORIES)
    print(message, file=sys.stderr)
    if discarded_write is not None:
        discarded_write.warn()
    sys.exit(2)


if __name__ == "__main__":
    # Fail CLOSED on an unexpected crash. CC's PreToolUse contract is "exit 2 =
    # block; ANY other code = non-blocking error → the tool RUNS", so a bare
    # main() let every uncaught exception in this gate (Rule 0, the branch/review
    # rules, git_root_for, …) exit 1 = silent FAIL-OPEN on a commit. run_guard
    # converts that to exit 2 and logs loudly; SystemExit (every deliberate
    # _deny/allow path here) propagates untouched, so allow/block decisions are
    # unchanged. Mirrors git_push_guard.py's wiring.
    run_guard(main, "review_enforcement_commit")
