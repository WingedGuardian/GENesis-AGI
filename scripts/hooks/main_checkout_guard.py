#!/usr/bin/env python3
"""PreToolUse hook (Write|Edit|MultiEdit|NotebookEdit, and Bash): refuse a change
to a TRACKED file in this install's PRIMARY checkout — the deployed install.

WHY. The primary checkout is the deploy root: hooks, scripts and the server run
from it. A session that hand-edits a tracked file there ships an unreviewed change
into the running install, and leaves the tree dirty, which makes
``scripts/deploy_code_only.sh`` refuse the next deploy. Changes belong in a
worktree from ``origin/main`` and reach the deploy root through a merged PR and the
deploy scripts.

WHAT IS REFUSED (exit 2, with a message written for the agent):
  * Write / Edit / MultiEdit / NotebookEdit of a tracked file;
  * from Bash: the destinations of ``cp`` / ``mv`` / ``install`` (``-t DIR``
    included) and the SOURCES of ``mv`` (moving a tracked file out of the tree
    changes it just as surely), plus ``git checkout <ref> -- <paths>`` and
    ``git restore --source=<ref> <paths>`` with a ref other than HEAD. The git
    forms reuse ``git_discard_guard``'s rewind-verb parser by import, so the two
    guards cannot disagree about what such a command reads from.

A path is judged in three MEASURED steps, each asked of git rather than modelled:
  1. its nearest enclosing checkout is a PRIMARY one (the per-worktree git dir is
     the common dir — the definition ``genesis_is_primary_checkout`` in
     ``scripts/lib/deploy_checkout.sh`` uses, path arms included) AND it is the
     checkout THIS SCRIPT belongs to (located from ``__file__``; the launcher runs
     hooks from the main checkout, so that is the deploy root);
  2. the path is TRACKED there (``git ls-files``);
  3. it is not one of the known-ephemeral paths the deploy scripts also tolerate
     (``EPHEMERAL_DIRTY_RE`` below, a copy of ``scripts/lib/deploy_marker.sh``'s,
     held equal by a parity test).

WHAT IS ALLOWED: untracked files, linked worktrees (even one nested inside the
primary checkout's directory), other repositories, paths outside any repository,
the ephemeral paths, and discarding an edit back to HEAD or the index
(``git checkout -- f``, ``git restore f``) — that is the REPAIR path for a dirty
deploy root and must stay open. Sessions spawned by the dashboard update pipeline
carry ``GENESIS_UPDATE_TIER`` and are allowed: resolving the update's merge in the
deploy root is their job.

This guard fails OPEN: it blocks only on a MEASURED tracked file in the primary
checkout, and allows anything it cannot evaluate. An unparseable command, an
unknown working directory (a ``cd`` earlier in the command, ``env -C``), a git
error, a timeout, a missing sibling module or an internal error each ALLOW with a
short note to the model, never a block on uncertainty. That is deliberate: this
guard protects review discipline, not data, and the repair path for a broken hook
tree must never run through it.

Kill switch: ``GENESIS_MAIN_CHECKOUT_GUARD=0`` in the environment, or
``enabled: false`` in ``~/.genesis/config/main_checkout_guard.local.yaml`` (what
``settings_update("main_checkout_guard", {"enabled": false})`` writes). Any other
value, and a malformed file, keep the guard ON.

``cp``/``mv``/``install`` options are read from a CLOSED table of options whose
effect on what is written is modelled. Any other option (``-n``, ``-u``, ``-T``,
``--parents``, links, one nobody listed), or a destination that is an existing
directory (a merge, not a replacement), makes the run uncertain: it is allowed,
with a note naming the tracked files it may change. An unlisted option can
therefore only lose a block, never invent one. A ``git checkout``/``restore`` whose
source resolves to HEAD's commit (``main`` on a deploy root standing on main) is a
discard and is allowed. Notes for a command the guard could not read are emitted
only when it may concern the guarded checkout (its path appears in the command, or
the session's cwd is in it), so they stay rare enough to be read.

DOCUMENTED RESIDUALS (each is a miss — the status quo before this guard — never a
false block): shell redirections (``> file``), ``sed -i``, ``tee``, ``rm``,
``git apply``/``merge``/``stash pop``, a branch switch, ``env -S '<command>'``
(which the shared parser does not open), any command whose destination is
computed at run time, and file-writing MCP tools (code-intelligence rename and
symbol-edit tools), which this hook's matchers do not cover.

The off-switches are levers for the owner, and nothing mechanical reserves them:
a session can call ``settings_update`` too. That matches every other settings
domain here; the block message tells the agent to stop and ask instead.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# EVERY sibling import is guarded: this guard fails OPEN, so a broken sibling must
# degrade to "allow with a note", never to an import traceback (exit 1 is already
# non-blocking, but it says nothing to the model about what went unchecked).
try:
    from hook_input import read_payload as _hook_read_payload
except Exception:  # noqa: BLE001
    _hook_read_payload = None


def _shell_parser():
    """``shell_parse.analyze_checked``, imported LAZILY. The parser and the git
    rewind parser are the expensive imports here, and this hook runs on every
    Bash call: importing them only for a command that mentions a gated program
    keeps the common call to an interpreter start. None if unimportable."""
    try:
        from shell_parse import analyze_checked
    except Exception:  # noqa: BLE001
        return None
    return analyze_checked


def _rewind_parser():
    """``(verb_and_operands, source, segment_cwd)`` from ``git_discard_guard`` —
    the ONE parser of these git verbs — imported lazily; None if unimportable."""
    try:
        from git_discard_guard import (
            _rewind_source,
            _rewind_verb_and_operands,
            _segment_cwd,
        )
    except Exception:  # noqa: BLE001
        return None
    return _rewind_verb_and_operands, _rewind_source, _segment_cwd


#: Copied from ``scripts/lib/deploy_marker.sh`` (the one bash definition, consumed
#: by the deploy scripts against ``git status --porcelain`` lines). Shaped for a
#: porcelain line, so it is matched against ``" " + <repo-relative path>``. Kept
#: equal to the bash value by tests/test_hooks/test_main_checkout_guard.py.
EPHEMERAL_DIRTY_RE = r" AGENTS\.md$| config/procedure_triggers\.yaml$| \.claude/settings\.local\.json$| \.serena/project\.yml$| src/genesis/identity/USER\.md$"
_EPHEMERAL = re.compile(EPHEMERAL_DIRTY_RE)

_KILL_SWITCH_ENV = "GENESIS_MAIN_CHECKOUT_GUARD"
_TIER_STAMP_ENV = "GENESIS_UPDATE_TIER"
_CONFIG_NAME = "main_checkout_guard.yaml"

#: The checkout this script belongs to: scripts/hooks/<this> -> repo root.
_SELF_ROOT = Path(__file__).resolve().parents[2]

#: Whole-call budget for git subprocesses. The hook is registered at 30s and a
#: killed PreToolUse hook lets the call PROCEED — the same direction as this
#: guard's own allow — but silently. Stopping at 20s leaves room to say that the
#: check did not finish. Each git call here is a read (rev-parse / ls-files) that
#: normally takes milliseconds; the bound only matters for a hung filesystem.
_BUDGET_S = 20.0

_TAG = "[main-checkout-guard]"
_BLOCK_TAG = f"{_TAG} BLOCKED"

_FILE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_MENTION = re.compile(r"\b(cp|mv|install|checkout|restore)\b")


class _Unknown(Exception):
    """The guard could not evaluate something: allow, and say so."""


# ── config ────────────────────────────────────────────────────────────────────


def _overlay_path() -> Path:
    """User dir first, then the repo's config/ — the order the settings writer
    and ``worktree_claim._overlay_path`` use (settings_update writes to
    ``~/.genesis/config/<stem>.local.yaml``)."""
    local = Path(_CONFIG_NAME).stem + ".local.yaml"
    user = Path.home() / ".genesis" / "config" / local
    return user if user.is_file() else _SELF_ROOT / "config" / local


def _enabled() -> bool:
    """ON unless the env kill switch is ``0`` or a config layer sets the boolean
    ``false``. Any other value, an unreadable file or a missing yaml module keeps
    the guard ON."""
    if os.environ.get(_KILL_SWITCH_ENV) == "0":
        return False
    try:
        import yaml
    except Exception:  # noqa: BLE001
        return True
    enabled = True
    for path in (_SELF_ROOT / "config" / _CONFIG_NAME, _overlay_path()):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception:  # noqa: BLE001 — malformed: this layer says nothing
            continue
        if isinstance(loaded, dict) and "enabled" in loaded:
            enabled = loaded["enabled"] is not False
    return enabled


def _update_tier_session() -> bool:
    value = os.environ.get(_TIER_STAMP_ENV, "")
    return bool(value) and value != "0"


# ── git, read-only ────────────────────────────────────────────────────────────


class _Git:
    """git read calls under one shared deadline, with location variables removed
    so an inherited GIT_DIR/GIT_WORK_TREE cannot point a query at another repo
    (the launcher scrubs these too; this covers a direct invocation)."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + _BUDGET_S
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("GIT_") or k.startswith("GIT_CONFIG")
        }
        self._checkouts: dict[str, tuple[str, bool] | None] = {}

    def run(self, cwd: str, *args: str) -> subprocess.CompletedProcess:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Unknown("the check ran out of time")
        try:
            return subprocess.run(
                ["git", "-C", cwd, *args],
                capture_output=True,
                text=True,
                env=self.env,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise _Unknown("a git query timed out") from exc
        except OSError as exc:
            raise _Unknown(f"git could not be run ({type(exc).__name__})") from exc

    def checkout(self, directory: str) -> tuple[str, bool] | None:
        """``(toplevel, is_the_guarded_primary)`` for the nearest checkout
        enclosing ``directory``, or None when it is in no work tree."""
        if directory in self._checkouts:
            return self._checkouts[directory]
        res = self.run(
            directory,
            "rev-parse",
            "--show-toplevel",
            "--absolute-git-dir",
            "--git-common-dir",
            "--is-inside-work-tree",
        )
        if res.returncode != 0:
            err = res.stderr.lower()
            if "not a git repository" in err or "must be run in a work tree" in err:
                self._checkouts[directory] = None
                return None
            raise _Unknown(f"git could not describe {directory}")
        lines = res.stdout.splitlines()
        if len(lines) != 4:
            raise _Unknown(f"unexpected rev-parse output for {directory}")
        toplevel, git_dir, common, inside = lines
        common_abs = os.path.realpath(os.path.join(directory, common))
        primary = (
            os.path.realpath(git_dir) == common_abs
            and "/.claude/worktrees/" not in toplevel + "/"
            and "/.worktrees/" not in toplevel + "/"
            and inside == "true"
        )
        guarded = primary and os.path.realpath(toplevel) == str(_SELF_ROOT)
        result = (os.path.realpath(toplevel), guarded)
        self._checkouts[directory] = result
        return result

    def tracked(
        self, root: str, pathspecs: list[str], *, literal: bool, cwd: str | None = None
    ) -> list[str]:
        """Repo-relative tracked paths matching ``pathspecs`` (git's own matching:
        a directory matches the tracked files under it)."""
        args = (["--literal-pathspecs"] if literal else []) + [
            "ls-files",
            "--full-name",
            "-z",
            "--",
            *pathspecs,
        ]
        res = self.run(cwd or root, *args)
        if res.returncode != 0:
            raise _Unknown("git could not list tracked files")
        return [p for p in res.stdout.split("\0") if p]


def _existing_dir(path: str) -> str:
    """The nearest existing directory at or above ``path``'s parent."""
    d = os.path.dirname(path) if not os.path.isdir(path) else path
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return d or "/"


def _blocking_files(git: _Git, abs_paths: list[str]) -> tuple[str, list[str]] | None:
    """The guarded root and the tracked, non-ephemeral repo-relative files among
    ``abs_paths`` (a directory contributes the tracked files under it)."""
    by_root: dict[str, list[str]] = {}
    for path in abs_paths:
        path = os.path.normpath(path)
        parent = _existing_dir(path)
        info = git.checkout(parent)
        if info is None or not info[1]:
            continue
        root = info[0]
        real = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        if os.path.isdir(path):
            real = os.path.realpath(path)
        rel = os.path.relpath(real, root)
        if rel == ".." or rel.startswith("../"):
            continue
        by_root.setdefault(root, []).append(rel)
    for root, rels in by_root.items():
        hits = [p for p in git.tracked(root, rels, literal=True) if not _EPHEMERAL.search(" " + p)]
        if hits:
            return root, hits
    return None


# ── messages ──────────────────────────────────────────────────────────────────


def _block_message(root: str, files: list[str], how: str) -> str:
    shown = ", ".join(files[:5]) + (f" (and {len(files) - 5} more)" if len(files) > 5 else "")
    return (
        f"{_BLOCK_TAG}: {how} would change tracked file(s) {shown} in {root} — this "
        "install's PRIMARY checkout, i.e. the deployed install that hooks, scripts and "
        "the server run from. A hand edit there bypasses review and leaves the tree "
        "dirty, so scripts/deploy_code_only.sh refuses the next deploy.\n"
        "Instead: make the change in a worktree from origin/main and open a PR. Code "
        "reaches this checkout only through a merged PR deployed with "
        "scripts/deploy_code_only.sh or scripts/update.sh.\n"
        "If an edit already landed here by accident, discarding it back to HEAD "
        "(`git checkout -- <file>` / `git restore <file>`) is allowed. If you believe "
        "this refusal is wrong, stop and tell the user rather than working around it."
    )


def _emit_note(note: str) -> None:
    """Deliver a note on the exit-0 channel the model actually receives
    (additionalContext on stdout; Claude Code discards an exit-0 hook's stderr),
    bounded by ``hook_output`` — imported here, because only a note needs it."""
    payload = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": note}}
    try:
        import hook_output
    except Exception:  # noqa: BLE001 — unbounded is still delivered
        print(json.dumps(payload))
        return
    hook_output.print_json_bounded(payload, text_keys=("hookSpecificOutput.additionalContext",))


def _unchecked_note(reason: str) -> str:
    return (
        f"{_TAG} NOTE: this call was NOT checked for changes to tracked files in the "
        f"primary checkout (the deployed install) — {reason}. It was allowed. Do not "
        "hand-edit tracked files there; work in a worktree and open a PR."
    )


# ── file tools ────────────────────────────────────────────────────────────────


def _decide_file_tool(payload: dict, tool: str, git: _Git) -> str | None:
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else payload
    raw = ti.get(_FILE_TOOLS[tool]) if isinstance(ti, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    path = raw
    if not os.path.isabs(path):
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise _Unknown("a relative path arrived with no working directory")
        path = os.path.join(cwd, path)
    hit = _blocking_files(git, [path])
    return _block_message(hit[0], hit[1], f"{tool}") if hit else None


# ── Bash ──────────────────────────────────────────────────────────────────────

#: Per program, a CLOSED table of the options whose effect on WHAT IS WRITTEN is
#: modelled here. ``safe_short`` / ``safe_long`` change nothing about which file a
#: run writes; ``short_val`` / ``long_val`` take a value (``--opt=V`` or
#: ``--opt V``, and GNU's unambiguous prefixes). Any option outside the safe set —
#: ``-n``/``--no-clobber``, ``-u``/``--update``, ``-T``, ``--parents``, links,
#: an option nobody listed — makes the run UNCERTAIN: it is never blocked, and gets
#: a note only when it would otherwise have been refused. So the table can only
#: lose a block, never invent one (Decision test: an unknown flag degrades to the
#: status quo).
_COPY_OPTS: dict[str, dict] = {
    "cp": {
        "safe_short": "fvprRaLPHdbix",
        "short_val": "St",
        "safe_long": (
            "--archive",
            "--backup",
            "--debug",
            "--dereference",
            "--force",
            "--interactive",
            "--no-dereference",
            "--no-preserve",
            "--one-file-system",
            "--preserve",
            "--recursive",
            "--reflink",
            "--remove-destination",
            "--sparse",
            "--suffix",
            "--target-directory",
            "--verbose",
        ),
        "long_val": ("--target-directory", "--suffix", "--sparse", "--no-preserve"),
        "all_long": (
            "--archive",
            "--attributes-only",
            "--backup",
            "--copy-contents",
            "--debug",
            "--dereference",
            "--force",
            "--interactive",
            "--link",
            "--no-clobber",
            "--no-dereference",
            "--no-preserve",
            "--no-target-directory",
            "--one-file-system",
            "--parents",
            "--preserve",
            "--recursive",
            "--reflink",
            "--remove-destination",
            "--sparse",
            "--strip-trailing-slashes",
            "--suffix",
            "--symbolic-link",
            "--target-directory",
            "--update",
            "--verbose",
            "--context",
            "--keep-directory-symlink",
            "--help",
            "--version",
        ),
    },
    "mv": {
        "safe_short": "fvbi",
        "short_val": "St",
        "safe_long": (
            "--backup",
            "--debug",
            "--force",
            "--interactive",
            "--suffix",
            "--target-directory",
            "--verbose",
        ),
        "long_val": ("--target-directory", "--suffix"),
        "all_long": (
            "--backup",
            "--debug",
            "--exchange",
            "--force",
            "--interactive",
            "--no-clobber",
            "--no-copy",
            "--no-target-directory",
            "--strip-trailing-slashes",
            "--suffix",
            "--target-directory",
            "--update",
            "--verbose",
            "--context",
            "--help",
            "--version",
        ),
    },
    "install": {
        "safe_short": "vbpDsc",
        "short_val": "gmoSt",
        "safe_long": (
            "--backup",
            "--group",
            "--mode",
            "--owner",
            "--preserve-timestamps",
            "--strip",
            "--strip-program",
            "--suffix",
            "--target-directory",
            "--verbose",
        ),
        "long_val": (
            "--target-directory",
            "--suffix",
            "--group",
            "--mode",
            "--owner",
            "--strip-program",
        ),
        "all_long": (
            "--backup",
            "--compare",
            "--debug",
            "--directory",
            "--group",
            "--mode",
            "--owner",
            "--preserve-timestamps",
            "--strip",
            "--strip-program",
            "--suffix",
            "--target-directory",
            "--no-target-directory",
            "--verbose",
            "--preserve-context",
            "--context",
            "--help",
            "--version",
        ),
    },
}


def _long_name(token: str, names: tuple[str, ...]) -> str:
    """Resolve an abbreviated long option the way getopt does; an ambiguous or
    unknown prefix is returned unchanged (and is then outside the safe set)."""
    if token in names:
        return token
    matches = [n for n in names if n.startswith(token)]
    return matches[0] if len(matches) == 1 else token


class _CopyPlan:
    """What a cp/mv/install run writes (absolute paths; for mv, the sources it
    removes too), the relative paths it could not place, and why the plan is
    UNCERTAIN, if it is."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.unplaced: list[str] = []
        self.uncertain: str | None = None


def _copy_plan(argv: list[str], cwd: str | None) -> _CopyPlan:
    plan = _CopyPlan()
    prog = os.path.basename(argv[0])
    table = _COPY_OPTS[prog]
    operands: list[str] = []
    target_dir: str | None = None
    i, n = 1, len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            operands.extend(argv[i + 1 :])
            break
        if tok.startswith("--"):
            raw_name, eq, val = tok.partition("=")
            name = _long_name(raw_name, table["all_long"])
            if name in ("--help", "--version"):
                return _CopyPlan()
            if name == "--directory" and prog == "install":
                return _CopyPlan()  # creates directories; writes no file
            if name in table["long_val"] and not eq:
                val = argv[i + 1] if i + 1 < n else ""
                i += 1
            if name == "--target-directory":
                target_dir = val
            if name not in table["safe_long"]:
                plan.uncertain = plan.uncertain or f"`{tok}`"
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            j = 1
            while j < len(tok):
                ch = tok[j]
                if ch in table["short_val"]:
                    val = tok[j + 1 :]
                    if not val:
                        val = argv[i + 1] if i + 1 < n else ""
                        i += 1
                    if ch == "t":
                        target_dir = val
                    break
                if ch == "d" and prog == "install":
                    return _CopyPlan()  # creates directories; writes no file
                if ch not in table["safe_short"]:
                    plan.uncertain = plan.uncertain or f"`-{ch}`"
                j += 1
            i += 1
            continue
        operands.append(tok)
        i += 1

    def place(p: str) -> str | None:
        if os.path.isabs(p):
            return p
        if cwd:
            return os.path.join(cwd, p)
        plan.unplaced.append(p)
        return None

    if target_dir is not None:
        sources, base = operands, place(target_dir)
        into_dir = True
    else:
        if len(operands) < 2:
            return plan
        sources, base = operands[:-1], place(operands[-1])
        into_dir = base is not None and os.path.isdir(base)
    if base is not None:
        if into_dir:
            plan.writes = [os.path.join(base, os.path.basename(s.rstrip("/"))) for s in sources]
        else:
            plan.writes = [base]
    if any(os.path.isdir(w) for w in plan.writes):
        # Writing INTO an existing directory merges rather than replaces, so
        # "every tracked file under it" over-states what changes.
        plan.uncertain = plan.uncertain or "a destination that is an existing directory"
    if prog == "mv":
        plan.writes += [p for p in (place(s) for s in sources) if p is not None]
    return plan


_CHDIR_OPT = re.compile(r"^(-C.*|-D.*|--chdir(=.*)?|--working-directory(=.*)?)$")
_GIT_LOCATION_ASSIGN = re.compile(r"^GIT_(DIR|WORK_TREE|INDEX_FILE|COMMON_DIR)=")


def _raw_prefix(seg) -> list[str] | None:
    """The raw tokens AHEAD of the program word (wrappers, their options, env
    assignments) — shell_parse strips them from argv. None when the raw segment
    does not tokenize."""
    try:
        tokens = shlex.split(seg.raw, comments=True)
    except ValueError:
        return None
    out: list[str] = []
    for tok in tokens:
        if os.path.basename(tok) == seg.exe:
            return out
        out.append(tok)
    return out


def _prefix_relocates(seg) -> bool:
    """A wrapper that changes the directory the program runs in (``env -C``,
    ``env -C/dir``, ``sudo -D``), or an assignment that points git elsewhere."""
    prefix = _raw_prefix(seg)
    if prefix is None:
        return True
    return any(_CHDIR_OPT.match(t) or _GIT_LOCATION_ASSIGN.match(t) for t in prefix)


_GATED_AT_COMMAND = re.compile(r"(?:^|[;&|(])\s*(?:sudo\s+|env\s+)?(cp|mv|install|git)\b")


def _first_line_runs_a_gated_program(cmd: str) -> bool:
    """For an UNREADABLE command only: does its first line put a gated program at
    a command position? Heredoc bodies — the usual reason a command does not parse
    — start on a later line, and merely MENTION these words."""
    first = cmd.split("\n", 1)[0]
    for m in _GATED_AT_COMMAND.finditer(first):
        if m.group(1) != "git" or re.search(r"\b(checkout|restore)\b", first[m.end() :]):
            return True
    return False


class _Concern:
    """Whether a check that could not be completed concerns the guarded checkout
    at all, so a note is emitted only where it can matter (an unconditional note
    on every unreadable command is noise the agent learns to skip)."""

    def __init__(self, payload: dict, cmd: str, git: _Git) -> None:
        self.payload, self.cmd, self.git = payload, cmd, git
        self._cached: bool | None = None

    def path(self, path: str) -> bool:
        info = self.git.checkout(_existing_dir(os.path.join(path, ".")))
        return bool(info and info[1])

    def __bool__(self) -> bool:
        if self._cached is None:
            cwd = self.payload.get("cwd")
            self._cached = str(_SELF_ROOT) in self.cmd or (
                isinstance(cwd, str) and bool(cwd) and self.path(cwd)
            )
        return self._cached


def _git_rewind(seg, base_cwd: str | None):
    """For ``git checkout <ref> -- <paths>`` / ``git restore --source=<ref>``:
    ``("target", cwd, source, pathspecs)``; ``("unknown", reason)`` when it is one
    but its directory or paths cannot be known; None when it is not one."""
    parsers = _rewind_parser()
    if parsers is None:
        return ("unknown", "the git rewind parser (git_discard_guard) could not be imported")
    rewind_verb_and_operands, rewind_source, segment_cwd = parsers
    resolved = rewind_verb_and_operands(seg.argv)
    if resolved is None or resolved[0] not in ("checkout", "restore"):
        return None
    verb, operands, options = resolved
    if {"-p", "--patch"} & set(options):
        return None  # interactive hunk picker: a session cannot drive it
    sentinel = "/\0unknown-cwd"
    cwd = segment_cwd(seg, {"cwd": base_cwd or sentinel})
    cwd_known = bool(cwd) and not cwd.startswith(sentinel)
    source = rewind_source(seg.argv, verb, cwd if cwd_known else None)
    if source is None:
        return None  # from HEAD or the index (the repair path), or a branch op
    if "--" in operands:
        pathspecs = operands[operands.index("--") + 1 :]
    elif verb == "checkout":
        pathspecs = operands[1:]  # operands[0] is the source
    else:
        pathspecs = list(operands)
    if any(o == "--pathspec-from-file" or o.startswith("--pathspec-from-file=") for o in options):
        return ("unknown", "--pathspec-from-file hides the paths from the check")
    if not pathspecs:
        return None  # `git checkout <ref>` switches branch: outside this guard
    verb_at = seg.argv.index(verb) if verb in seg.argv else len(seg.argv)
    if any(t.startswith(("--git-dir", "--work-tree")) for t in seg.argv[1:verb_at]):
        return ("unknown", "--git-dir/--work-tree point git at a repository the check cannot place")
    if not cwd_known:
        return (
            "unknown",
            "an earlier `cd` (or a relocating wrapper) makes git's directory unknown",
        )
    return ("target", cwd, source, pathspecs)


def _same_commit_as_head(git: _Git, cwd: str, source: str) -> bool | None:
    """True when ``source`` names HEAD's commit (``main`` on a deploy root standing
    on main is a discard, not a rewind); False for any other commit, and for a
    TREE-ish source (``HEAD~1:src``), which git also accepts and which is judged
    as a rewind; None when it names neither, in which case git refuses the command
    and nothing changes."""
    rev = "@{-1}" if source == "-" else source
    res = git.run(cwd, "rev-parse", "-q", "--verify", "--end-of-options", "HEAD^{commit}")
    head = res.stdout.strip() if res.returncode == 0 else ""
    res = git.run(cwd, "rev-parse", "-q", "--verify", "--end-of-options", f"{rev}^{{commit}}")
    if res.returncode == 0:
        return bool(head) and res.stdout.strip() == head
    # Not `<rev>^{tree}`: in `HEAD~1:src^{tree}` the path syntax swallows the
    # suffix. Resolve the object, then ask its type.
    res = git.run(cwd, "rev-parse", "-q", "--verify", "--end-of-options", rev)
    if res.returncode != 0:
        return None
    kind = git.run(cwd, "cat-file", "-t", res.stdout.strip())
    return False if kind.returncode == 0 and kind.stdout.strip() == "tree" else None


_HEREDOC_OP = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)[A-Za-z_][\w.-]*\1")


def _heredoc_test(cmd: str):
    """A predicate: is this segment possibly TEXT inside a heredoc body?

    The shared parser returns heredoc body lines as command segments (MEASURED:
    ``cat > f <<'EOF' / cp foo README.md / EOF`` yields a ``cp`` segment), so a
    note being written about a cp would otherwise be refused as a cp. Everything
    after the line that opens the first heredoc is treated as uncertain — allowed
    with a note — because which lines are body and which are commands again after
    the terminator is the shell's grammar, not something to re-derive here."""
    m = _HEREDOC_OP.search(cmd)
    if m is None:
        return lambda seg: None
    eol = cmd.find("\n", m.end())
    head = cmd if eol < 0 else cmd[:eol]
    rest = "" if eol < 0 else cmd[eol:]

    def test(seg) -> str | None:
        raw = seg.raw.strip()
        # Text, not position: a segment whose text ALSO appears after the opening
        # line may be the body copy, so it is uncertain even when it matches the
        # opening line too. This direction can only lose a block.
        if raw and raw in head and raw not in rest:
            return None
        return "sits inside or after a heredoc, whose body the parser reads as commands"

    return test


def _maybe_note(seg, root: str, files: list[str], why: str) -> str:
    return _unchecked_note(
        f"`{seg.raw.strip()}` may change tracked file(s) {', '.join(files[:5])} in "
        f"{root}, but it {why}"
    )


def _bash_command(payload: dict) -> str:
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else payload
    cmd = ti.get("command") if isinstance(ti, dict) else None
    return cmd if isinstance(cmd, str) else ""


def _decide_bash(payload: dict, git: _Git) -> tuple[str | None, list[str]]:
    """``(block message or None, notes)``."""
    cmd = _bash_command(payload)
    if not cmd.strip() or not _MENTION.search(cmd):
        return None, []
    concern = _Concern(payload, cmd, git)
    analyze_checked = _shell_parser()
    if analyze_checked is None:
        note = _unchecked_note("the shell parser could not be imported")
        return None, [note] if concern else []
    segs, blind = analyze_checked(cmd)
    if blind is not None:
        if _first_line_runs_a_gated_program(cmd) and concern:
            return None, [_unchecked_note(f"the command {blind.cause}")]
        return None, []
    notes: list[str] = []
    in_heredoc = _heredoc_test(cmd)
    payload_cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    cwd_known = bool(payload_cwd)
    cd_concern = False  # does any earlier `cd` possibly land in the guarded checkout?
    for seg in segs:
        if seg.exe in ("cd", "pushd", "popd"):
            cwd_known = False
            target = seg.argv[1] if len(seg.argv) > 1 else ""
            if target == "~" or target.startswith("~/"):
                target = os.path.expanduser(target)  # bash expands an unquoted ~
            literal = target and not re.search(r"[$`~*?]", target) and target != "-"
            if literal and os.path.isabs(target):
                cd_concern = cd_concern or concern.path(target)
            else:
                cd_concern = cd_concern or bool(concern)
            continue
        try:
            if seg.exe in _COPY_OPTS:
                here = payload_cwd if cwd_known and not _prefix_relocates(seg) else None
                plan = _copy_plan(seg.argv, here)
                # Unplaced because of an earlier cd: does that cd possibly land in
                # the guarded checkout? Because of a relocating wrapper: is the
                # session itself there?
                if plan.unplaced and (cd_concern if not cwd_known else bool(concern)):
                    notes.append(
                        _unchecked_note(
                            f"`{seg.exe}` names relative path(s) whose working directory is unknown"
                        )
                    )
                hit = _blocking_files(git, plan.writes) if plan.writes else None
                if hit:
                    why = (
                        plan.uncertain and f"uses {plan.uncertain}, whose effect it does not model"
                    )
                    why = why or in_heredoc(seg)
                    if not why:
                        return _block_message(hit[0], hit[1], f"`{seg.raw.strip()}`"), notes
                    notes.append(_maybe_note(seg, hit[0], hit[1], why))
            elif seg.exe == "git":
                base = payload_cwd if cwd_known and not _prefix_relocates(seg) else None
                found = _git_rewind(seg, base)
                if found is None:
                    continue
                if found[0] == "unknown":
                    if cd_concern if not cwd_known else bool(concern):
                        notes.append(_unchecked_note(found[1]))
                    continue
                _, cwd, source, pathspecs = found
                info = git.checkout(_existing_dir(os.path.join(cwd, ".")))
                if info is None or not info[1]:
                    continue
                if _same_commit_as_head(git, cwd, source) is not False:
                    continue  # a discard back to HEAD, or a ref git will reject
                hits = [
                    p
                    for p in git.tracked(info[0], pathspecs, literal=False, cwd=cwd)
                    if not _EPHEMERAL.search(" " + p)
                ]
                if hits:
                    why = in_heredoc(seg)
                    if not why:
                        return _block_message(info[0], hits, f"`{seg.raw.strip()}`"), notes
                    notes.append(_maybe_note(seg, info[0], hits, why))
        except _Unknown as exc:
            notes.append(_unchecked_note(str(exc)))
    return None, notes


# ── entry point ───────────────────────────────────────────────────────────────


def _read_payload() -> dict:
    if _hook_read_payload is not None:
        return _hook_read_payload()
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _decide(payload: dict) -> tuple[str | None, list[str]]:
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else payload
    if not isinstance(tool, str) or not tool:
        # Legacy payload shape: no tool name, infer from the fields present.
        if isinstance(ti, dict) and "command" in ti:
            tool = "Bash"
        elif isinstance(ti, dict) and "notebook_path" in ti:
            tool = "NotebookEdit"
        elif isinstance(ti, dict) and "file_path" in ti:
            tool = "Write"
    if tool not in _FILE_TOOLS and tool != "Bash":
        return None, []
    if tool == "Bash" and not _MENTION.search(_bash_command(payload)):
        return None, []  # the common case, decided before any config read or import
    if not _enabled():
        return None, []
    git = _Git()
    if tool in _FILE_TOOLS:
        try:
            return _decide_file_tool(payload, tool, git), []
        except _Unknown as exc:
            return None, [_unchecked_note(str(exc))]
    if tool == "Bash":
        return _decide_bash(payload, git)
    return None, []


def main() -> int:
    if _update_tier_session():
        return 0
    try:
        payload = _read_payload()
        message, notes = _decide(payload)
    except Exception as exc:  # noqa: BLE001 — fail OPEN, and say so
        _emit_note(_unchecked_note(f"the guard hit an internal error ({type(exc).__name__})"))
        return 0
    if message:
        print(message, file=sys.stderr)
        return 2
    if notes:
        _emit_note("\n".join(dict.fromkeys(notes)))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001 — never exit with a traceback
        code = 0
    sys.exit(code)
