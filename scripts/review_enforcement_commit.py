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

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))
from hook_input import field, read_payload, run_guard  # noqa: E402
from shell_parse import (  # noqa: E402
    analyze,
    commit_skips_hooks,
    git_subcommand,
    has_trailing_override,
    split_segments,
    untokenizable,
)

try:  # A refusal discards the WHOLE Bash call, so name any write it took with it.
    from discarded_write import remember as _remember_command  # noqa: E402
    from discarded_write import warn as _warn_discarded  # noqa: E402
except Exception:  # noqa: BLE001

    def _remember_command(_command=None):
        """No-op stand-in.

        The note is cosmetic, but an UNGUARDED import that failed would abort this
        module's load — and CC reads a non-2 exit as a NON-blocking error, so the
        unreviewed commit this hook exists to refuse would proceed. A missing note
        must never become a missing block.
        """

    def _warn_discarded(_command=None):
        """No-op stand-in. See ``_remember_command``."""

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

# Executable prompt/agent/skill SURFACES: files whose content shapes autonomous LLM
# behavior (agent + slash-command + skill definitions, and the in-repo skill library).
# These are NEVER docs/config even when ``.md`` — an autonomous system editing its own
# prompts is high-consequence (genesis-development SKILL.md requires prompt/LLM-behavior
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
    """Whether a ``git switch``/``git checkout`` could move HEAD toward main/master or
    an unverifiable branch.

    Returns ``"main"`` if it could land HEAD on main/master, ``"unknown"`` if the
    target is unresolvable (a variable/subst/glob/``-`` previous-branch), else
    ``None`` — a literal non-main branch (``checkout -b feature``) carries no
    direct-to-main risk, and a FILE-RESTORE form moves no branch at all.

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
    return None


def _worktree_root(cwd: str) -> str:
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
            timeout=5,
        )
        root = r.stdout.strip()
        if r.returncode == 0 and root:
            return os.path.realpath(root)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return os.path.realpath(cwd)


def _merge_note(cwd: str | None) -> str:
    """A hint appended to a cap/mode-switch denial when a merge is mid-flight.

    ADVISORY TEXT ONLY. Deliberately NOT wired into the verdict or the round
    counter: those sentinels are unauthenticated files that any actor with shell
    access can create (``echo x > .git/MERGE_HEAD``), and `git merge --no-commit`
    leaves one indefinitely without any forgery at all. Keying an EXEMPTION off
    them would let the actor this gate exists to constrain silence it
    permanently with one write — measured: a forged sentinel froze the counter
    across three further distinct defect rounds. Telling the author what the
    gate can see is safe; letting that state decide the verdict is not.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return ""
        raw = out.stdout.strip()
        git_dir = Path(raw) if Path(raw).is_absolute() else Path(cwd or ".") / raw
        merging = any(
            (git_dir / n).exists()
            for n in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
        ) or (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
    except (subprocess.SubprocessError, OSError, ValueError):
        return ""
    if not merging:
        return ""
    return (
        "\n\nNOTE: a merge/rebase appears to be in progress. The round counter "
        "keys on the staged diff, so pulling upstream in to resolve a conflict "
        "reads as another round even though it adds no authored code. If this "
        "commit IS only that merge, say so in the ack rather than treating it "
        "as a real round."
    )


def main() -> None:
    # Parse tool input
    payload = read_payload()
    command = field(payload, "command")
    if not _COMMIT_PATTERN.search(command):
        sys.exit(0)  # Not a commit, allow
    _remember_command(command)

    # Parse the command into the segments it actually executes (through
    # wrappers, bash -c, command substitutions). Reused for Rule 0, the
    # add-chain detection, and the override binding.
    segs = analyze(command)

    # The cheap _COMMIT_PATTERN early-out can match "git commit" mentioned in a
    # string (a reply body, an echo). Confirm a REAL executed commit segment
    # before applying the branch/review rules, else allow.
    if not any(git_subcommand(s.argv) == "commit" for s in segs):
        # ── Blind-spot net: unverifiable → ASK the human ────────────────────
        # "No commit segment" is a trustworthy verdict only when the command was
        # PARSEABLE. analyze() mis-parses an ANSI-C `$'…\'…'` command and DROPS
        # the real commit segment, so this very early-out is what lets a
        # commit-to-main / --no-verify / unreviewed commit sail through (a live,
        # reproduced bypass). When the word "commit" appears (guaranteed past the
        # _COMMIT_PATTERN early-out above) but the command is un-parseable, the
        # empty parse is not evidence of absence.
        #
        # The outcome is an approval PROMPT, not a refusal. A hard block here has
        # to be surgically precise about which un-parseable commands are real
        # commits — and precision is exactly what an unreliable parse cannot
        # deliver: every narrowing conjunct became a new way to starve the trigger,
        # while over-blocking broke benign shapes (`git status # don't commit yet`).
        # Asking inverts those costs: a false positive is one confirmation, a miss
        # is the pre-existing status quo.
        #
        # The probe reads the command RAW — the normalizer that used to
        # pre-process it is deleted, so an ordinary contraction inside quoted
        # multi-line input DOES reach this branch. It still does not prompt, but
        # for a different reason than this comment used to give: analyze()
        # resolves the segment, and the net only fires where it found none.
        try:
            if untokenizable(command):
                # EXACT "1", never truthiness. `cc/invoker.py` stamps the marker as
                # "1" and every other consumer compares to it exactly
                # (git_push_guard._is_dispatched, pretool_check, genesis_stop_hook,
                # outcome_verification_hook). A truthiness test also treats
                # GENESIS_CC_SESSION=0 — an operator explicitly turning it OFF — as
                # dispatched, and would then HARD-BLOCK a benign unparseable
                # mention such as `echo $'don\\'t commit this'` that the interactive
                # path is meant to merely ask about. Over-blocking is the failure
                # direction this whole design was chosen to avoid.
                if os.environ.get("GENESIS_CC_SESSION") == "1":
                    # No human present to answer a prompt in a dispatched session.
                    _deny(
                        "BLOCKED: this command cannot be parsed safely (e.g. "
                        "ANSI-C $'...' quoting) and mentions a commit. Autonomous "
                        "sessions cannot proceed on an unverifiable command.\n"
                        "To proceed: if you are WRITING TEXT (a commit message, "
                        "a plan, review notes) whose content merely mentions a "
                        "commit, use the Write tool instead of a here-doc — an "
                        "apostrophe in ordinary prose is what makes this "
                        "unparseable, and re-quoting the here-doc cannot fix "
                        "that. If you are RUNNING a git command, rewrite it in "
                        "a directly-parseable form (plain quotes, or "
                        "`git commit -F <file>`)."
                        # The way OUT belongs here more than on the ask below:
                        # an interactive session can ask a human what it did
                        # wrong, an unattended one cannot. A refusal it cannot
                        # act on is a wall; with the rewrite named it is a cost.
                    )
                _ask(
                    "This command could not be parsed safely (e.g. ANSI-C $'...' "
                    "quoting) and mentions a commit, so review enforcement cannot "
                    "verify what it would actually run. Approve only if you are "
                    "sure. To avoid the prompt, rewrite it in a directly-parseable "
                    "form (plain quotes, or `git commit -F <file>`)."
                )
        except Exception:  # noqa: BLE001 — never crash into a silent allow
            _ask(
                "The commit-guard parseability probe failed, so this command could "
                "not be verified. Approve only if you are sure."
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
            "Remove it and establish a review first via /review."
        )
        return

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
            ESCALATION_ROUND_CAP,
            get_current_branch,
            get_review_round,
            has_code_changes,
            has_valid_review_marker,
            is_review_current,
            marker_content_current,
            reset_review_round,
        )
    except ImportError:
        # If review_state.py is missing, fail open — don't block
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
    # direct-to-main block. Fail closed when a pre-commit branch mutation could reach
    # main/master or is unresolvable; a literal non-main target (`git checkout -b
    # feature && git commit`, the flow this gate itself recommends) carries no
    # direct-to-main risk and stays allowed.
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
                    "BLOCKED: this command switches branches (git switch/checkout to "
                    "main/master or an unresolvable target) before a commit, so the "
                    "guard cannot verify which branch the commit lands on. Run the "
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
        seg_branch = get_current_branch(cwd=seg_cwd if isinstance(seg_cwd, str) else None)
        if seg_branch in ("main", "master"):
            _deny(
                "BLOCKED: Direct commits to main are not allowed. "
                "Create a branch first: git checkout -b <scope>/<description>"
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
        distinct_dirs = {_worktree_root(c) if isinstance(c, str) else c for c in seg_cwds}
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
                + _merge_note(cwd)
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
                + _merge_note(cwd)
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
    # Whether the commit may stage content BEYOND the current index — a pathspec, -a/-i/-o/-p,
    # or a git add/rm/mv/reset in the chain (P1-D). When true the hook-time index does NOT
    # reflect what will be committed, so we can neither classify the staged diff nor bind the
    # marker to it; depth (Rule 2.5) and Rule 2 fall back to the recorded/valid marker. This
    # is the SAME predicate the docs-skip uses, so `git commit -am` / a pathspec commit is
    # handled exactly like `git add && commit` — NOT just a bare `git add` (which the old
    # add-only check missed, letting `commit -am` of unstaged substantial work slip the gate).
    commit_may_add_content = _commit_may_add_content(segs)

    if not commit_may_add_content:
        staged = _staged_files(cwd)
        if staged and all(_is_docs_or_config(p) for p in staged):
            sys.exit(0)

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
        depth_is_adversarial = marker_adversarial and marker_content_current(cwd=cwd)

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
                "  python3 scripts/review_state.py mark\n"
                "If the audit genuinely ran but its format isn't recognized, acknowledge with "
                "a trailing shell comment (outside any quotes):  # depth-ack"
            )
            return

    # Rule 2: Block commits without review (on branches).
    if commit_may_add_content:
        # The staged diff at hook time isn't what will be committed (staging deferred, -a,
        # a pathspec, …) — can't check the diff hash, so require a valid (unexpired) marker.
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
            "save it to `python3 scripts/review_state.py evidence-path`, "
            "then run: python3 scripts/review_state.py mark\n"
            "If findings are intentionally accepted, append a trailing shell "
            "comment (outside any quotes): '  # review-override'"
        )
        return

    # All checks passed — allow
    sys.exit(0)


def _deny(message: str) -> None:
    """Output denial message and block the tool via exit code 2."""
    print(message, file=sys.stderr)
    # Last, so it reads as a footnote to the refusal above. The command was handed
    # over in main(); stdin was consumed by the payload read and cannot be re-read.
    _warn_discarded()
    sys.exit(2)


def _ask(reason: str) -> None:
    """Emit a PreToolUse ``ask`` decision — a native approve/deny dialog.

    For the UNVERIFIABLE path only: a command the parser cannot resolve is not
    evidence of wrongdoing, so it earns a human decision rather than a refusal.
    Claude Code runs the tool only on explicit approval, which the agent cannot
    self-satisfy. Mirrors ``git_push_guard._ask``. Exits 0 with the decision on
    stdout (the hook JSON carries the verdict; the exit code must NOT be 2).
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
    sys.exit(0)


if __name__ == "__main__":
    # Fail CLOSED on an unexpected crash. CC's PreToolUse contract is "exit 2 =
    # block; ANY other code = non-blocking error → the tool RUNS", so a bare
    # main() let every uncaught exception in this gate (Rule 0, the branch/review
    # rules, git_root_for, …) exit 1 = silent FAIL-OPEN on a commit. run_guard
    # converts that to exit 2 and logs loudly; SystemExit (every deliberate
    # _deny/allow path here) propagates untouched, so allow/block decisions are
    # unchanged. Mirrors git_push_guard.py's wiring.
    run_guard(main, "review_enforcement_commit")
