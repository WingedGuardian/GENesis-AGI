#!/usr/bin/env python3
"""Main-checkout guard: keep hand edits out of this install's PRIMARY checkout —
the deployed install that hooks, scripts and the server run from.

WHY. A session that hand-edits a tracked file in the deploy root ships an
unreviewed change into the running install, and leaves the tree dirty, which
makes ``scripts/deploy_code_only.sh`` refuse the next deploy. Changes belong in a
worktree from ``origin/main`` and reach the deploy root through a merged PR and
the deploy scripts.

The guard has two halves, and they work differently on purpose.

1. FILE TOOLS — BLOCKED (PreToolUse on Write / Edit / MultiEdit / NotebookEdit).
   The tool names its path, so the guard asks git about that one path and exits 2
   when it is a TRACKED, non-ephemeral file in the primary checkout this script
   belongs to. The path is first resolved: a leading ``~`` is expanded (Claude
   Code's file tools expand it — MEASURED on 2.1.280), and symlinks are followed,
   final component included, so a path is judged by the file a write through it
   would change. (2.1.280 itself refuses to Write or Edit through a symlink —
   MEASURED — so following the final link matters only on a version that writes
   through one.) It is then judged in three steps, each a git query:
     a. its nearest enclosing checkout is a PRIMARY one (the per-worktree git dir
        is the common dir — the definition ``genesis_is_primary_checkout`` in
        ``scripts/lib/deploy_checkout.sh`` uses, path arms included) AND it is the
        checkout THIS SCRIPT belongs to (located from ``__file__``; the launcher
        runs hooks from the main checkout, so that is the deploy root);
     b. the path is TRACKED there (``git ls-files``);
     c. it is not one of the known-ephemeral paths the deploy scripts also
        tolerate (``EPHEMERAL_DIRTY_RE`` below, a copy of
        ``scripts/lib/deploy_marker.sh``'s, held equal by a parity test).
   Untracked files, linked worktrees (even one nested inside the primary
   checkout's directory), other repositories and paths outside any repository
   are allowed.

2. BASH — DETECTED AFTER THE FACT, NEVER BLOCKED. A shell command does not name
   what it writes, and an earlier version of this guard predicted it from its own
   model of cp/mv/install, the shell and git. A class audit in PR #2900's first
   review round (2026-10-05) reported that model wrong in both directions — about
   15 false blocks and about 40 misses, by that audit's count — so the Bash half
   no longer predicts anything:
     * PreToolUse on Bash records a SNAPSHOT of the deploy root's tracked state —
       HEAD's commit and branch, and for every tracked path that differs from
       HEAD or the index (``git status --porcelain=v2``), its status line and a
       hash of its working-tree content; paths flagged assume-unchanged or
       skip-worktree, which ``git status`` hides, are hashed too — in
       ``${GENESIS_HOME:-~/.genesis}/main_checkout_guard/<tool_use_id>.json``.
       It always exits 0.
     * PostToolUse (and PostToolUseFailure, which Claude Code fires instead for
       a call that errors — READ from the 2.1.280 bundle; which of the two a
       non-zero Bash exit reaches was not measured, so both are wired) on Bash
       takes the same snapshot, compares, and deletes the stored one. A tracked
       path that newly differs, one that was already dirty and changed AGAIN, or
       a moved HEAD produces an ADVISORY to the model (``additionalContext``)
       naming the files. A restore command is offered only when the call pointed
       at the deploy root (its cwd, or the root's path in the command) and no
       merge is in progress there; otherwise the session is told to leave the
       change alone, because it is most likely another actor's. It never tells a
       session to move HEAD. It never exits 2. It is NOT gated on the config
       layers: the snapshot exists only if the guard was on when the call began,
       and the call may itself be what wrote ``enabled: false`` into the tracked
       config — that edit is reported, not obeyed.
   Changes to ephemeral paths alone are not reported, and neither is a path that
   went from dirty to clean (that is what the repair looks like).

WHAT THE BASH HALF CANNOT DO (residuals, stated so nobody reads more into it):
  * it reports after the command ran, so it prevents nothing: a file the same
    command writes and then executes, or a hook edited and run before the post
    check, has already had its effect;
  * it cannot tell who changed the deploy root during the command's run — a
    concurrent deploy, another session, a parallel call of the same session, or a
    timer is reported too; the advisory says so and offers a restore command
    only when the call pointed at the deploy root;
  * a ``run_in_background`` command returns at once, so the post check runs
    before most of its writes and misses them;
  * no snapshot (the pre hook was killed or did not run) means no check: silent.

Untracked files are never reported, and neither the file tools nor Bash cover
file-writing MCP tools (code-intelligence rename and symbol-edit tools), which
this hook's matchers do not see.

FAILURE DIRECTION: OPEN. The file-tool half blocks only on a MEASURED tracked file
in the primary checkout; a git error, a timeout, a missing sibling module or an
internal error allows, with a short note to the model. The Bash half never blocks
at all; when it cannot evaluate (unreadable git, timeout) it allows, and says so
only when the session's working directory or the command text points at the
deploy root. This guard protects review discipline, not data, and the repair path
for a broken hook tree must never run through it.

OFF SWITCHES (both also silence the Bash half; the config one from the next
call on, as above):
  * ``GENESIS_MAIN_CHECKOUT_GUARD=0`` in the environment (exactly ``0``);
  * ``enabled: false`` in ``${GENESIS_HOME:-~/.genesis}/config/
    main_checkout_guard.local.yaml`` — what
    ``settings_update("main_checkout_guard", {"enabled": false})`` writes when
    ``GENESIS_HOME`` is unset (that writer always uses ``~/.genesis/config``). The
    overlay is read ONLY from there, never from a repo-local
    ``config/main_checkout_guard.local.yaml``: that path is gitignored, so it is
    untracked and any session could write it. The rule is "the loaded value is
    the boolean false": PyYAML loads ``false``/``no``/``off`` (any case shown in
    YAML 1.1) as that boolean, so those turn the guard off; ``0``, ``null``,
    ``"false"`` and anything else keep it ON, as does a malformed file.
Sessions spawned by the dashboard update pipeline carry ``GENESIS_UPDATE_TIER=1``
and are exempt — resolving the update's merge in the deploy root is their job.
Only the exact value ``1`` exempts.

The off-switches are levers for the owner, and nothing mechanical reserves them: a
session can call ``settings_update`` too. That matches every other settings domain
here; the block message tells the agent to stop and ask instead.
"""

from __future__ import annotations

import contextlib
import hashlib
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
# degrade to "allow", never to an import traceback (exit 1 is already non-blocking,
# but it says nothing to the model about what went unchecked).
try:
    from hook_input import read_payload as _hook_read_payload
except Exception:  # noqa: BLE001
    _hook_read_payload = None


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

#: Whole-call budget for git subprocesses. The hook is registered at 30s; a killed
#: PreToolUse hook lets the call proceed (this guard's own direction anyway) but
#: silently. Each git call here is a read that normally takes milliseconds; the
#: bound only matters for a hung filesystem.
_BUDGET_S = 20.0

#: Snapshot retention. A snapshot normally lives for one command: the post hook
#: deletes it. Only an orphan (the call was refused by another hook, the session
#: died mid-command, the post hook was killed) outlives that, so the pre hook
#: prunes orphans older than a day, and only once the directory holds more than
#: _PRUNE_OVER files, so the common call does not list the directory at all.
_PRUNE_AGE_S = 86_400
_PRUNE_OVER = 64

#: Working-tree content is hashed up to this many bytes per snapshot in total; a
#: file past the budget is identified by size and mtime instead, which can report
#: a merely touched file as "changed again". The dirty set of a deploy root is
#: normally empty, so this only matters on a badly dirtied tree.
_HASH_BUDGET_BYTES = 64 * 1024 * 1024

#: How many files an advisory names before it says "and N more".
_LIST_MAX = 10

_TAG = "[main-checkout-guard]"
_BLOCK_TAG = f"{_TAG} BLOCKED"

_FILE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_POST_EVENTS = ("PostToolUse", "PostToolUseFailure")


class _Unknown(Exception):
    """The guard could not evaluate something: allow, and say so."""


# ── config ────────────────────────────────────────────────────────────────────


def _genesis_home() -> Path:
    """``$GENESIS_HOME`` when set, else ``~/.genesis`` (mirrors
    ``genesis.env.genesis_home``; this script avoids genesis imports)."""
    value = os.environ.get("GENESIS_HOME")
    return Path(value).expanduser() if value else Path.home() / ".genesis"


def _overlay_path() -> Path:
    """The ONLY overlay location. Never the repo's ``config/`` directory: a
    ``config/*.local.yaml`` there is gitignored, hence untracked, so a session
    could write one past the file-tool half and switch the guard off."""
    return _genesis_home() / "config" / (Path(_CONFIG_NAME).stem + ".local.yaml")


#: Every spelling PyYAML loads as the boolean False (YAML 1.1), plus a tag (``!!``)
#: or a backslash escape, through which a quoted string can load as one. A config
#: layer whose text, comments aside, holds none of these is not parsed at all:
#: importing yaml costs about 50ms (MEASURED with -X importtime), and this hook runs
#: twice on every Bash call. The filter only decides whether to PARSE; a spelling
#: it misses can only skip a layer, which keeps the guard ON, never turns it off.
_MAYBE_FALSE = re.compile(r"\b(?:false|False|FALSE|no|No|NO|off|Off|OFF)\b|!!|\\")
_COMMENT = re.compile(r"(?:^|\s)#.*$", re.MULTILINE)


def _enabled() -> bool:
    """ON unless the env kill switch is exactly ``0`` or a config layer's loaded
    ``enabled`` value IS the boolean ``False``. Any other value, an unreadable
    file or a missing yaml module keeps the guard ON."""
    if os.environ.get(_KILL_SWITCH_ENV) == "0":
        return False
    enabled = True
    for path in (_SELF_ROOT / "config" / _CONFIG_NAME, _overlay_path()):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 — absent or unreadable: this layer says nothing
            continue
        # While the guard is ON, a layer can only matter by loading as False, so
        # one with no false spelling is skipped unparsed. Stripping too much here
        # (a " #" inside a quoted string) can only skip a parse, which keeps the
        # guard ON — never turns it off. Once a layer has said False, the next is
        # always parsed, since any other value of its own turns the guard back on.
        if enabled and not _MAYBE_FALSE.search(_COMMENT.sub("", text)):
            continue
        try:
            import yaml

            loaded = yaml.safe_load(text)
        except Exception:  # noqa: BLE001 — no yaml, or malformed: this layer says nothing
            continue
        if isinstance(loaded, dict) and "enabled" in loaded:
            enabled = loaded["enabled"] is not False
    return enabled


def _update_tier_session() -> bool:
    return os.environ.get(_TIER_STAMP_ENV) == "1"


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
                # --no-optional-locks: these are reads, and must never take the
                # index lock a concurrent deploy's merge needs.
                ["git", "--no-optional-locks", "-C", cwd, *args],
                capture_output=True,
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
            err = res.stderr.decode("utf-8", "replace").lower()
            if "not a git repository" in err or "must be run in a work tree" in err:
                self._checkouts[directory] = None
                return None
            raise _Unknown(f"git could not describe {directory}")
        lines = res.stdout.decode("utf-8", "surrogateescape").splitlines()
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

    def tracked(self, root: str, paths: list[str]) -> list[str]:
        """Repo-relative tracked paths among ``paths`` (literal pathspecs; a
        directory matches the tracked files under it)."""
        res = self.run(root, "--literal-pathspecs", "ls-files", "--full-name", "-z", "--", *paths)
        if res.returncode != 0:
            raise _Unknown("git could not list tracked files")
        return [p for p in res.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


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
        # Resolve FIRST, final component included, and judge the file a write
        # through this path would change: an untracked link (anywhere, even in a
        # worktree) to a tracked deploy-root file is that file, and a link in the
        # root pointing outside it changes nothing tracked there. (CC 2.1.280
        # refuses to write through a symlink at all; this covers a version that
        # does not.)
        real = os.path.realpath(path)
        info = git.checkout(_existing_dir(real))
        if info is None or not info[1]:
            continue
        root = info[0]
        rel = os.path.relpath(real, root)
        if rel == ".." or rel.startswith("../"):
            continue
        by_root.setdefault(root, []).append(rel)
    for root, rels in by_root.items():
        hits = [p for p in git.tracked(root, rels) if not _EPHEMERAL.search(" " + p)]
        if hits:
            return root, hits
    return None


# ── messages ──────────────────────────────────────────────────────────────────


def _shown(files: list[str]) -> str:
    extra = len(files) - _LIST_MAX
    return ", ".join(files[:_LIST_MAX]) + (f" (and {extra} more)" if extra > 0 else "")


def _block_message(root: str, files: list[str], how: str) -> str:
    return (
        f"{_BLOCK_TAG}: {how} would change tracked file(s) {_shown(files)} in {root} — "
        "this install's PRIMARY checkout, i.e. the deployed install that hooks, scripts "
        "and the server run from. A hand edit there bypasses review and leaves the tree "
        "dirty, so scripts/deploy_code_only.sh refuses the next deploy.\n"
        "Instead: make the change in a worktree from origin/main and open a PR. Code "
        "reaches this checkout only through a merged PR deployed with "
        "scripts/deploy_code_only.sh or scripts/update.sh.\n"
        "If an edit already landed here by accident, discarding it back to HEAD "
        "(`git checkout -- <file>` / `git restore <file>`) is allowed. If you believe "
        "this refusal is wrong, stop and tell the user rather than working around it."
    )


def _emit_context(event: str, note: str) -> None:
    """Deliver text on the exit-0 channel the model actually receives
    (``additionalContext`` on stdout; Claude Code discards an exit-0 hook's
    stderr), bounded by ``hook_output`` — imported here, because only a note
    needs it."""
    payload = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": note}}
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


# ── file tools: block ─────────────────────────────────────────────────────────


def _decide_file_tool(payload: dict, tool: str, git: _Git) -> str | None:
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else payload
    raw = ti.get(_FILE_TOOLS[tool]) if isinstance(ti, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    # Claude Code's file tools expand a leading `~` (MEASURED 2026-10-05: a Write
    # to `~/tmp/...` landed in the home directory). The hook runs as the same
    # user, so its HOME is the one the tool expanded.
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise _Unknown("a relative path arrived with no working directory")
        path = os.path.join(cwd, path)
    hit = _blocking_files(git, [path])
    return _block_message(hit[0], hit[1], f"{tool}") if hit else None


# ── Bash: snapshot before, compare after ──────────────────────────────────────


def _content_id(abs_path: str, budget: list[int]) -> str:
    """A cheap identity of a working-tree path's CONTENT: a sha256 of a regular
    file's bytes while ``budget`` lasts, else its size and mtime; a symlink's
    target; or ``absent``."""
    try:
        st = os.lstat(abs_path)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"
    if os.path.islink(abs_path):
        return "link:" + os.readlink(abs_path)
    if not os.path.isfile(abs_path):
        return f"other:{st.st_mode:o}"
    if st.st_size > budget[0]:
        return f"stat:{st.st_size}:{st.st_mtime_ns}"
    budget[0] -= st.st_size
    digest = hashlib.sha256()
    try:
        with open(abs_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:  # vanished or unreadable since the lstat
        return f"unreadable:{type(exc).__name__}"
    return f"{st.st_mode:o}:" + digest.hexdigest()


def _parse_status_v2(raw: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """``(headers, {path: status line without its path})`` from
    ``git status --porcelain=v2 --branch -z``. Untracked and ignored entries are
    skipped (the call asks for none anyway)."""
    headers: dict[str, str] = {}
    entries: dict[str, str] = {}
    records = raw.decode("utf-8", "surrogateescape").split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec:
            continue
        kind = rec[0]
        if kind == "#":
            key, _, value = rec[2:].partition(" ")
            headers[key] = value
        elif kind == "1":
            parts = rec.split(" ", 8)
            entries[parts[8]] = " ".join(parts[:8])
        elif kind == "2":  # a rename: the ORIGINAL path follows as its own record
            parts = rec.split(" ", 9)
            entries[parts[9]] = " ".join(parts[:9])
            if i < len(records):
                entries.setdefault(records[i], "renamed-from")
                i += 1
        elif kind == "u":
            parts = rec.split(" ", 10)
            entries[parts[10]] = " ".join(parts[:10])
    return headers, entries


_FLAGGED = re.compile(rb"\0[a-zS] ([^\0]+)")


def _snapshot(git: _Git, root: str) -> dict:
    """The deploy root's tracked state: HEAD, branch, and an identity for every
    path that differs from HEAD or the index, plus every index-flagged path."""
    res = git.run(
        root,
        "status",
        "--porcelain=v2",
        "--branch",
        "--no-ahead-behind",
        "-z",
        "--untracked-files=no",
        "--no-renames",
        "--ignore-submodules=none",
    )
    if res.returncode != 0:
        raise _Unknown("git could not read the deploy root's status")
    headers, entries = _parse_status_v2(res.stdout)
    # assume-unchanged / skip-worktree hide an edit from `git status` (the same
    # gap genesis_tracked_dirty_paths closes in scripts/lib/deploy_checkout.sh).
    # Rather than clear the flags on a scratch index, as that helper does, hash
    # every flagged path: a change between the two snapshots is caught either way.
    listing = git.run(root, "ls-files", "-v", "-z")
    if listing.returncode != 0:
        raise _Unknown("git could not list the deploy root's index flags")
    # `ls-files -v` tags an assume-unchanged entry with a lowercase letter and a
    # skip-worktree one with S (s when both); a regex over the raw listing, because
    # a per-record Python loop over a repo this size costs ~10ms on every call.
    flagged = [
        m.decode("utf-8", "surrogateescape") for m in _FLAGGED.findall(b"\0" + listing.stdout)
    ]
    budget = [_HASH_BUDGET_BYTES]
    paths: dict[str, list[str]] = {}
    for path, meta in entries.items():
        paths[path] = [meta, _content_id(os.path.join(root, path), budget)]
    for path in flagged:
        if path not in paths:
            paths[path] = ["flagged", _content_id(os.path.join(root, path), budget)]
    return {
        "v": 1,
        "root": root,
        "head": headers.get("branch.oid"),
        "branch": headers.get("branch.head"),
        "paths": paths,
    }


def _snapshot_dir() -> Path:
    return _genesis_home() / "main_checkout_guard"


_ID_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _snapshot_file(tool_use_id: str) -> Path:
    name = (
        tool_use_id
        if _ID_SAFE.match(tool_use_id)
        else hashlib.sha256(tool_use_id.encode("utf-8", "surrogateescape")).hexdigest()[:40]
    )
    return _snapshot_dir() / f"{name}.json"


def _prune(directory: Path) -> None:
    """Delete orphaned snapshots older than a day — only once the directory has
    grown past _PRUNE_OVER entries, so the common call costs one scandir."""
    with contextlib.suppress(OSError):
        entries = list(os.scandir(directory))
        if len(entries) <= _PRUNE_OVER:
            return
        cutoff = time.time() - _PRUNE_AGE_S
        for entry in entries:
            with contextlib.suppress(OSError):
                if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)


def _write_snapshot(target: Path, snap: dict) -> None:
    directory = target.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    tmp = directory / f".{target.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(snap, fh)
    os.replace(tmp, target)
    _prune(directory)


def _claim_snapshot(target: Path) -> dict | None:
    """Read and REMOVE the stored snapshot. Claimed by rename first, so when the
    post hook runs twice for one call (a dispatched session in a checkout loads
    both registrations) exactly one run compares and the other finds nothing."""
    claimed = target.with_name(f"{target.name}.{os.getpid()}.claimed")
    try:
        os.rename(target, claimed)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(claimed.read_text(encoding="utf-8"))
    finally:
        with contextlib.suppress(OSError):
            os.unlink(claimed)
    return data if isinstance(data, dict) else None


def _bash_command(payload: dict) -> str:
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else payload
    cmd = ti.get("command") if isinstance(ti, dict) else None
    return cmd if isinstance(cmd, str) else ""


def _mentions_root(payload: dict, git: _Git) -> bool:
    """Does this call point at the deploy root — its path in the command text,
    or the session's cwd inside it? Gates the can't-check notes, so they appear
    only where they can matter."""
    if str(_SELF_ROOT) in _bash_command(payload):
        return True
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return False
    try:
        info = git.checkout(_existing_dir(os.path.join(cwd, ".")))
    except _Unknown:
        return False
    return bool(info and info[1])


def _repair_lines(root: str, edited: list[str], staged: list[str]) -> list[str]:
    """Restore commands, or — past _LIST_MAX paths, where one command line would
    be too long to read and could be cut by the output budget — a pointer to
    ``git status`` instead."""
    q = shlex.quote
    if len(edited) + len(staged) > _LIST_MAX:
        return [f"  git -C {q(root)} status   # too many files to list a restore command for"]
    lines: list[str] = []
    if edited:
        lines.append(f"  git -C {q(root)} checkout -- " + " ".join(q(p) for p in edited))
    if staged:
        lines.append(
            f"  git -C {q(root)} restore --source=HEAD --staged --worktree -- "
            + " ".join(q(p) for p in staged)
            + "   # also removes a file HEAD does not have"
        )
    return lines


def _meta(entry) -> str:
    return entry[0] if isinstance(entry, list) and entry else ""


def _advisory(before: dict, after: dict, *, attributable: bool) -> str | None:
    """The post-command advisory, or None when nothing that matters changed.

    ``attributable``: the call pointed at the deploy root (its cwd, or the
    root's path in the command). Only then are restore commands offered; a call
    that did not is told what changed and to leave it alone, because every Bash
    call in every session is snapshotted, so the change is most likely another
    actor's — a deploy, the update pipeline, another session, or a parallel
    call of this one."""
    old_paths: dict = before.get("paths") or {}
    new_paths: dict = after.get("paths") or {}

    def keep(p: str) -> bool:
        return not _EPHEMERAL.search(" " + p)

    changed = sorted(
        p for p in new_paths if keep(p) and (p not in old_paths or new_paths[p] != old_paths[p])
    )
    # An index-flagged path is in every snapshot whether or not it is modified,
    # so "already modified before" cannot be said of it.
    flagged = [p for p in changed if _meta(new_paths[p]) == "flagged"]
    newly = [p for p in changed if p not in old_paths and p not in flagged]
    again = [p for p in changed if p in old_paths and p not in flagged]
    head_moved = (before.get("head"), before.get("branch")) != (
        after.get("head"),
        after.get("branch"),
    )
    if not (changed or head_moved):
        return None
    root = after.get("root") or str(_SELF_ROOT)
    merging = any(_meta(v).startswith("u ") for v in new_paths.values())
    parts = [
        f"{_TAG} ADVISORY: tracked state of {root} — this install's PRIMARY checkout, "
        "the deployed install that hooks, scripts and the server run from — changed "
        "while that Bash call ran. This is a report, not a refusal: the guard compares "
        "the deploy root before and after every Bash call in every session, so a "
        "deploy, the update pipeline, another session or a parallel call of yours "
        "that changed it in the same window shows up here too."
    ]
    if head_moved:
        parts.append(
            f"HEAD moved: it was {before.get('head') or '(none)'} on branch "
            f"{before.get('branch') or '(unknown)'}, and is now "
            f"{after.get('head') or '(none)'} on branch {after.get('branch') or '(unknown)'}. "
            "A deploy (scripts/deploy_code_only.sh, scripts/update.sh) moves it on purpose. "
            f"Do not move HEAD back yourself: `git -C {shlex.quote(root)} reflog -3` shows "
            "what moved it; if it was not a deploy, tell the user."
        )
    if newly:
        parts.append(f"Now differ from HEAD: {_shown(newly)}.")
    if again:
        parts.append(
            f"Already modified before the call, and changed AGAIN during it: {_shown(again)}. "
            "Discarding these also discards the earlier change — find out whose it was first."
        )
    if flagged:
        parts.append(
            f"Changed while flagged assume-unchanged/skip-worktree (so `git status` hides "
            f"them): {_shown(flagged)}."
        )
    if changed and attributable and not merging:

        def has_staged(p: str) -> bool:
            fields = _meta(new_paths.get(p)).split(" ")
            return len(fields) > 1 and len(fields[1]) == 2 and fields[1][0] != "."

        staged = [p for p in changed if has_staged(p)]
        edited = [p for p in changed if p not in staged]
        parts.append(
            "If your command made these changes and it was not a deploy: put the work in "
            "a worktree from origin/main and a PR, then restore the deploy root:\n"
            + "\n".join(_repair_lines(root, edited, staged))
        )
    elif changed:
        why = (
            "a merge is in progress there (most likely the update pipeline's)"
            if merging
            else "this call did not point at the deploy root, so another actor most likely "
            "made them"
        )
        parts.append(
            f"No restore command is offered: {why}. Do NOT discard these changes yourself; "
            "tell the user if they look unexpected."
        )
    return "\n".join(parts)


def _bash_pre(payload: dict, git: _Git) -> str | None:
    """Record the snapshot. Returns a note only when the check could not be set up
    and the call points at the deploy root."""
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    try:
        info = git.checkout(str(_SELF_ROOT))
        if info is None or not info[1]:
            return None  # this script's checkout is not the deploy root
        _write_snapshot(_snapshot_file(tool_use_id), _snapshot(git, info[0]))
    except (_Unknown, OSError, ValueError) as exc:
        if _mentions_root(payload, git):
            return _unchecked_note(f"no before-snapshot could be taken ({exc})")
    return None


def _bash_post(payload: dict, git: _Git) -> str | None:
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    try:
        before = _claim_snapshot(_snapshot_file(tool_use_id))
    except (OSError, ValueError):
        before = None
    if before is None or before.get("root") != str(_SELF_ROOT):
        return None  # the pre hook did not run (or ran for another checkout)
    try:
        after = _snapshot(git, str(_SELF_ROOT))
    except (_Unknown, OSError) as exc:
        if _mentions_root(payload, git):
            return _unchecked_note(f"the after-snapshot could not be taken ({exc})")
        return None
    return _advisory(before, after, attributable=_mentions_root(payload, git))


# ── entry point ───────────────────────────────────────────────────────────────


def _read_payload() -> dict:
    if _hook_read_payload is not None:
        return _hook_read_payload()
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _decide(payload: dict) -> tuple[str | None, str | None, str]:
    """``(block message or None, context note or None, event to answer as)``."""
    event = payload.get("hook_event_name")
    event = event if isinstance(event, str) and event else "PreToolUse"
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
        return None, None, event
    if tool == "Bash" and event in _POST_EVENTS:
        # NOT gated on the config layers: a snapshot exists only if the guard was
        # on when the call began, and the call itself may be what wrote
        # `enabled: false` into config/main_checkout_guard.yaml — that edit must
        # be reported, not obeyed. The env kill switch, which no command can set
        # for this hook, still silences it.
        if os.environ.get(_KILL_SWITCH_ENV) == "0":
            return None, None, event
        return None, _bash_post(payload, _Git()), event
    if not _enabled():
        return None, None, event
    git = _Git()
    if tool == "Bash":
        if event == "PreToolUse":
            return None, _bash_pre(payload, git), event
        return None, None, event
    if event != "PreToolUse":
        return None, None, event  # the file tools are judged before they run, only
    try:
        return _decide_file_tool(payload, tool, git), None, event
    except _Unknown as exc:
        return None, _unchecked_note(str(exc)), event


def main() -> int:
    if _update_tier_session():
        return 0
    event = "PreToolUse"
    try:
        payload = _read_payload()
        raw_event = payload.get("hook_event_name")
        if isinstance(raw_event, str) and raw_event:
            event = raw_event
        message, note, event = _decide(payload)
    except Exception as exc:  # noqa: BLE001 — fail OPEN, and say so
        _emit_context(
            event, _unchecked_note(f"the guard hit an internal error ({type(exc).__name__})")
        )
        return 0
    if message:
        print(message, file=sys.stderr)
        return 2
    if note:
        _emit_context(event, note)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001 — never exit with a traceback
        code = 0
    sys.exit(code)
