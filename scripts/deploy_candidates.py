#!/usr/bin/env python3
"""deploy_candidates.py — the engine of `live`, the local integration branch.

`live` is origin/main plus the candidate branches named in the deploy manifest
(``$HOME/.genesis/deploy_manifest.json``, the path the git hooks and the commit
and merge guards read), merged on top in manifest order. It is a THROW-AWAY
integration branch in the sense of gitworkflows(7) ("You must never base any work
on such a branch"; git.git's own `seen` is rebuilt the same way): every rebuild
starts again from origin/main, and nothing is ever committed on it by hand.

Run it through ``scripts/deploy_candidates``, never directly: the entry holds
update.lock and the deploy marker for the commands that move the checkout, and
passes in the one definition of the dirty-tree excuse list.

Commands:
  add <branch> --owner <who> [--pr N] [--owner-approved]
        Put a local branch in the manifest. Refuses unless the install is READY
        (below) and the branch passes ADMISSION (below). Re-adding a candidate
        records its current head as verified. Prints the next step (rebuild); it
        does not rebuild.
  drop <branch> [--no-rebuild]
        Take a candidate out of the manifest, then rebuild without it when the
        checkout is on `live`. Works from a plain shell (`env -i`): it needs
        python3's standard library, git and flock, nothing from the venv. This is
        the repair path for a candidate whose hook blocks every session.
  rebuild
        fetch origin main; retire candidates whose PR is MERGED (any unreadable PR
        state retires nothing); refuse a dirty checkout, and a commit on `live`
        that is neither a rebuild merge nor a merged candidate's; merge each
        candidate off-tree (`merge-tree --write-tree`, then `commit-tree`, always
        a two-parent merge whose final paragraph carries the `Deploy-rebuild:`
        trailer); EXCLUDE, by name and reason, a candidate that conflicts, fails
        admission, or carries an excluded candidate's unmerged commits; move the
        checkout once, and only when the tree changes; fast-forward local main.
        It does NOT restart the server: it prints the next step.
  list  The manifest as written (checked to be this repository's). No network.
  status
        Per candidate: whether it is in `live` now, whether it would be included
        or excluded against the last-fetched origin/main (a dry run: it writes
        only unreachable objects), whether its head moved since it was verified,
        and its PR's state (stale after 7 days without an update).
  adopt <new-branch> --owner <who>
        Snapshot the checkout's uncommitted TRACKED edits as one commit on top of
        HEAD, on a new branch, without touching the checkout; then report, file
        by file, whether each edit equals origin/main, equals an open PR's head,
        or neither. The branch is the edits' owner of record; the report is what
        decides which of them still need to go live.

READINESS (add refuses unless all hold): every commit in REQUIRED_SERVING_COMMITS
is in the commit the server booted from (the `serving:` line of
`scripts/deploy_code_only.sh status`), and every git hook sync-hooks.sh installs
is byte-identical to its source. The list names the change that teaches the
wipers (update.sh, crash recovery, the guardian's REVERT_CODE, the dashboard's
update prompts) about `live`. Until that change exists its entry is a placeholder
that resolves to no commit, so `add` refuses on every install and the engine
ships INERT.

ADMISSION (v1, owner rulings 2026-09-27): a candidate whose diff against its
merge base with origin/main adds a database or data migration, or touches the
boot-time schema files, the host guardian's own files, or the Claude Code pin,
never goes live before it merges; nor does a branch that carries a
`Deploy-rebuild:` commit (it was cut from `live`). A dispatched session's add,
and a Devin-authored PR, need --owner-approved.

The manifest is INTENT only: {version, repo, candidates: [{branch, pr,
owner_session, added_at, verified_head, owner_approved}]}. What IS live is read
from git, from the rebuild merges on `live`. A manifest this engine cannot read
is never taken as empty: every command that changes anything refuses.

The reflog of `live` is kept through `git gc` and `git reflog expire --all`
(gc.refs/heads/live.reflogExpire[Unreachable] = never). It is NOT kept through
an explicit `git reflog expire --expire=now`, which overrides the configuration
(MEASURED, git 2.43).

Exit codes: 0 done, 1 refused or failed (the message says which; a refusal
changes nothing), 2 usage. The entry adds 200: update.lock still held after the
wait.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_VERSION = 1
LIVE_BRANCH = "live"
LIVE_REF = "refs/heads/live"
BASE_REMOTE = "origin"
BASE_BRANCH = "main"
BASE_REF = "refs/remotes/origin/main"
TRAILER_KEY = "Deploy-rebuild"
CANDIDATE_KEY = "Deploy-candidate"
STALE_DAYS = 7

# The commits the server must be running before anything may go live. Each is
# (what it is, the commit). PR C (the wipers rebuild `live` instead of merging
# into it or resetting it) has not merged: its entry is a placeholder that no
# repository resolves, so READINESS fails and `add` refuses everywhere. PR C
# replaces it with its merge commit.
PR_C_PLACEHOLDER = "PR-C-HAS-NOT-MERGED"
REQUIRED_SERVING_COMMITS: tuple[tuple[str, str], ...] = (
    (
        "#2673, the live-branch guard fixes (issue #2532)",
        "8fa53412c9f1ffb93e8831d126be962afd90106c",
    ),
    ("PR C, the wipers (update.sh, crash recovery, REVERT_CODE) rebuild `live`", PR_C_PLACEHOLDER),
)

# ── Admission ─────────────────────────────────────────────────────────────
# Migrations are refused in v1 (owner, 2026-09-27); the boot-time schema files
# carry ALTER/DROP/RENAME with no ids, so they are classified with them.
MIGRATION_DIRS = ("src/genesis/db/migrations/", "src/genesis/db/data_migrations/")
SCHEMA_FILES = ("src/genesis/db/schema/_migrations.py", "src/genesis/db/schema/_tables.py")
# What reaches the HOST: the guardian's own files from update.sh's GUARDIAN_PATHS.
# The rest of that list is runtime code the container runs too (GUARDIAN_SHARED_
# RUNTIME); refusing it would refuse most PRs, and the host guardian is deployed
# from origin/main, never from `live`. A test pins that every GUARDIAN_PATHS entry
# is in exactly one of the two.
GUARDIAN_OWN_PREFIXES = ("src/genesis/guardian/", "config/genesis-guardian")
GUARDIAN_OWN_FILES = (
    "config/guardian-claude.md",
    "scripts/install_guardian.sh",
    "scripts/guardian-gateway.sh",
    "scripts/lib/host_swap.sh",
    "scripts/lib/cc_tmp_volume.sh",
)
GUARDIAN_SHARED_RUNTIME = (
    "src/genesis/util",
    "src/genesis/env.py",
    "src/genesis/observability",
    "src/genesis/db",
    "pyproject.toml",
)
CC_PIN_FILE = "scripts/lib/cc_version.sh"
# What else drives the HOST from this checkout: genesis-cc-align.timer runs
# scripts/cc_align_host.sh from the tree every night (its unit template
# names __REPO_DIR__/scripts/cc_align_host.sh), aligning the host's Claude
# Code and Node through the guardian gateway.
HOST_DRIVER_PREFIXES = ("scripts/cc_align_host.sh", "scripts/systemd/genesis-cc-align.")
DEVIN_LOGIN = "devin-ai-integration"

# Bounded waits, each because a lock is held while it runs: a rebuild holds the
# EXCLUSIVE update.lock, so a hung fetch or GitHub call would stall every deploy
# and validation behind it. 120 s is deploy_code_only.sh's fetch bound; on a
# timeout the fetch keeps the last-fetched main and a PR's state reads unknown
# (which retires nothing).
FETCH_TIMEOUT_DEFAULT = 120
GH_TIMEOUT = 120

# The rebuild's own commits carry a fixed identity, so a rebuild from a plain
# shell on an install with no git identity configured still works. They are
# never published (the pre-push hook refuses them).
_IDENTITY = {
    "GIT_AUTHOR_NAME": "deploy-candidates",
    "GIT_AUTHOR_EMAIL": "deploy-candidates@localhost.invalid",
    "GIT_COMMITTER_NAME": "deploy-candidates",
    "GIT_COMMITTER_EMAIL": "deploy-candidates@localhost.invalid",
}

_GIT_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_TRAILER_LINE = re.compile(r"^deploy-rebuild[ \t]*:", re.IGNORECASE)
_CANDIDATE_LINE = re.compile(r"^deploy-candidate[ \t]*:[ \t]*(\S+)[ \t]*$", re.IGNORECASE)

# Used in annotations only (strings under `from __future__ import annotations`).
GhRunner = "Callable[[list[str], str, Mapping[str, str]], tuple[int, str, str]]"


class Refusal(Exception):
    """A refusal: printed as one ERROR, exit 1, nothing changed by the caller."""


@dataclass
class Plan:
    base: str
    tip: str
    merged: list[tuple[str, str]] = field(default_factory=list)  # (branch, head)
    excluded: dict[str, str] = field(default_factory=dict)  # branch -> reason
    contained: list[str] = field(default_factory=list)  # already in the tip: nothing to merge


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)  # noqa: UP017


def _parse_time(text: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def hooks_to_check(root: Path) -> list[str]:
    """The git hooks sync-hooks.sh installs, read from its HOOKS_TO_SYNC array
    (one quoted name per line). Raises Refusal when the array cannot be read: the
    check would otherwise pass over hooks nobody listed."""
    path = root / "scripts" / "hooks" / "sync-hooks.sh"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refusal(f"cannot read {path}: {exc}") from exc
    m = re.search(r"^HOOKS_TO_SYNC=\(\n(.*?)^\)", text, re.MULTILINE | re.DOTALL)
    if not m:
        raise Refusal(f"cannot find the HOOKS_TO_SYNC list in {path}")
    names = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        nm = re.fullmatch(r'"([A-Za-z0-9._-]+)"', line)
        if not nm:
            raise Refusal(f"cannot read the HOOKS_TO_SYNC line {line!r} in {path}")
        names.append(nm.group(1))
    if not names:
        raise Refusal(f"the HOOKS_TO_SYNC list in {path} is empty")
    return names


def _gh_default(args: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except FileNotFoundError:
        return 127, "", "gh is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"gh did not answer within {GH_TIMEOUT}s"
    return p.returncode, p.stdout, p.stderr


def _serving_default(root: Path, env: Mapping[str, str]) -> tuple[str | None, str]:
    """The commit genesis-server booted from: the `serving:` line of
    `deploy_code_only.sh status` (read-only, takes no lock), which reads it from
    HEAD's reflog at the unit's start time and says why when it cannot."""
    script = Path(__file__).resolve().parent / "deploy_code_only.sh"
    run_env = dict(env)
    if Path(__file__).resolve().parents[1] != root.resolve():
        run_env["GENESIS_DEPLOY_ROOT"] = str(root)
    try:
        p = subprocess.run(
            ["bash", str(script), "status"],
            cwd=str(root),
            env=run_env,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return None, f"cannot run {script}: {exc}"
    for line in p.stdout.splitlines():
        if line.startswith("serving: "):
            value = line[len("serving: ") :].strip()
            if _HEX40.match(value):
                return value, ""
            return None, value
    return None, f"{script.name} status printed no serving line (exit {p.returncode})"


class Engine:
    def __init__(
        self,
        root: Path,
        env: Mapping[str, str],
        gh: GhRunner | None = None,
        serving: Callable[[Path], tuple[str | None, str]] | None = None,
    ):
        self.root = Path(root)
        # Variables that point git at ANOTHER repository, index or object store
        # (set inside a git hook, or left by a caller) would make every git call
        # below act somewhere other than self.root.
        self.env = {k: v for k, v in env.items() if k not in _GIT_LOCATION_VARS}
        self._gh = gh or _gh_default
        self._serving = serving or (lambda r: _serving_default(r, self.env))
        home = self.env.get("HOME") or str(Path.home())
        self.home = Path(home)
        self.manifest_path = self.home / ".genesis" / "deploy_manifest.json"
        self.manifest_lock_path = self.home / ".genesis" / "deploy_manifest.json.lock"
        genesis_home = self.env.get("GENESIS_HOME") or str(self.home / ".genesis")
        self.lock_path = Path(genesis_home) / "locks" / "update.lock"
        self.update_state = self.home / ".genesis" / "update_state.json"

    # ── git ──────────────────────────────────────────────────────────────
    def git(
        self, *args: str, check: bool = True, input: str | None = None, extra_env=None, timeout=None
    ):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        p = subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=env,
            capture_output=True,
            text=True,
            input=input,
            timeout=timeout,
        )
        if check and p.returncode != 0:
            raise Refusal(f"git {' '.join(args)} failed (exit {p.returncode}): {p.stderr.strip()}")
        return p

    def fetch(self, refspec: str) -> str:
        """A bounded fetch of one refspec from origin. Returns "" on success, or
        why it did not happen (the caller says what it falls back to)."""
        timeout = _positive_int(self.env.get("GENESIS_DEPLOY_FETCH_TIMEOUT"), FETCH_TIMEOUT_DEFAULT)
        try:
            p = self.git(
                "-c",
                "gc.autoDetach=false",
                "fetch",
                "-q",
                BASE_REMOTE,
                refspec,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"fetching {refspec} timed out after {timeout}s"
        return "" if p.returncode == 0 else f"fetching {refspec} failed ({p.stderr.strip()})"

    def resolve(self, ref: str) -> str | None:
        p = self.git("rev-parse", "--verify", "-q", ref + "^{commit}", check=False)
        out = p.stdout.strip()
        return out if p.returncode == 0 and _HEX40.match(out) else None

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0

    def rev_list(self, *args: str) -> list[str]:
        out = self.git("rev-list", *args).stdout
        return [x for x in out.split() if x]

    def common_dir(self) -> str:
        out = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        if "\n" in out or not os.path.isabs(out):
            raise Refusal(f"cannot read this repository's git directory ({out!r})")
        return os.path.realpath(out)

    def place(self, cmd: str) -> None:
        """From a linked worktree, the read-only commands report the main
        checkout (whose `live` and manifest they are about); every command that
        changes anything refuses, so only the main checkout's own copy of this
        engine ever writes."""
        gd = os.path.realpath(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        common = self.common_dir()
        if gd == common:
            return
        main_root = Path(common).parent if os.path.basename(common) == ".git" else None
        if cmd in ("list", "status") and main_root is not None:
            _out(f"(from a linked worktree: reporting the main checkout, {main_root})")
            self.root = main_root
            return
        where = (
            f"{main_root}/scripts/deploy_candidates"
            if main_root
            else "the main checkout's scripts/deploy_candidates"
        )
        raise Refusal(f"{self.root} is a linked worktree; run {where} {cmd} instead.")

    def require_main_checkout(self) -> None:
        gd = self.git("rev-parse", "--absolute-git-dir").stdout.strip()
        if os.path.realpath(gd) != self.common_dir():
            raise Refusal(f"{self.root} is a linked worktree; `live` belongs to the main checkout.")

    def current_branch(self) -> str | None:
        p = self.git("symbolic-ref", "--short", "-q", "HEAD", check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    def read_commits(self, oids: list[str]) -> dict[str, tuple[list[str], str]]:
        """Each commit's parents and message, read RAW (`cat-file --batch`), so no
        trailer or encoding setting changes what is read. --no-replace-objects:
        a replace ref would otherwise show another commit."""
        if not oids:
            return {}
        p = subprocess.run(
            ["git", "-C", str(self.root), "--no-replace-objects", "cat-file", "--batch"],
            env=self.env,
            input=("\n".join(oids) + "\n").encode(),
            capture_output=True,
        )
        if p.returncode != 0:
            raise Refusal(f"cannot read commits: {p.stderr.decode(errors='replace').strip()}")
        data = p.stdout
        pos = 0
        result: dict[str, tuple[list[str], str]] = {}
        for _ in oids:
            nl = data.index(b"\n", pos)
            header = data[pos:nl].decode().split()
            if len(header) != 3 or header[1] != "commit":
                raise Refusal(f"cannot read a commit: {' '.join(header)}")
            size = int(header[2])
            body = data[nl + 1 : nl + 1 + size].decode("utf-8", errors="replace")
            pos = nl + 1 + size + 1
            head, _, msg = body.partition("\n\n")
            parents = [ln.split()[1] for ln in head.splitlines() if ln.startswith("parent ")]
            result[header[0]] = (parents, msg)
        return result

    @staticmethod
    def _body_lines(msg: str) -> list[str]:
        """The message lines after its first paragraph (the subject), the only
        place git ever reads trailers; the same wide rule the pre-push hook uses."""
        lines = msg.splitlines()
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        while i < len(lines) and lines[i].strip():
            i += 1
        return lines[i:]

    def is_rebuild_merge(self, parents: list[str], msg: str) -> bool:
        return len(parents) == 2 and any(_TRAILER_LINE.match(ln) for ln in self._body_lines(msg))

    def live_chain(self, base: str) -> tuple[str | None, list[tuple[str, str]]]:
        """What `live` holds now: the commit its rebuild merges sit on, and the
        (branch, head) each merged, oldest first. (None, []) when `live` has no
        rebuild merge on top (or does not exist)."""
        tip = self.resolve(LIVE_REF)
        if not tip:
            return None, []
        chain = self.rev_list("--first-parent", tip, "--not", base)
        info = self.read_commits(chain)
        merged: list[tuple[str, str]] = []
        live_base = None
        for c in chain:
            parents, msg = info[c]
            if not self.is_rebuild_merge(parents, msg):
                live_base = c
                break
            branch = next(
                (m.group(1) for ln in self._body_lines(msg) if (m := _CANDIDATE_LINE.match(ln))),
                "?",
            )
            merged.append((branch, parents[1]))
        if live_base is None:
            # Every commit down to base is a rebuild merge: they sit on the first
            # parent of the oldest one.
            live_base = info[chain[-1]][0][0] if chain else tip
        merged.reverse()
        return live_base, merged

    def foreign_commits(self, base: str) -> list[str]:
        """Commits on `live` (not on origin/main) that are neither a rebuild merge
        nor reachable from a candidate head a rebuild merged. `switch -C` would
        orphan them silently."""
        tip = self.resolve(LIVE_REF)
        if not tip:
            return []
        commits = self.rev_list(tip, "--not", base)
        info = self.read_commits(commits)
        merges = {c for c in commits if self.is_rebuild_merge(*info[c])}
        heads = sorted({info[c][0][1] for c in merges})
        allowed = set(self.rev_list(*heads, "--not", base)) if heads else set()
        return [c for c in commits if c not in merges and c not in allowed]

    def dirty_lines(self) -> list[str]:
        """Tracked changes in the checkout, less the machine-written files the
        deploy scripts excuse: `git status --porcelain --no-renames` filtered by
        EPHEMERAL_DIRTY_RE exactly as deploy_code_only.sh filters it, read with
        -z so a path is never quoted."""
        regex = self.env.get("DEPLOY_CANDIDATES_EPHEMERAL_RE")
        if not regex:
            raise Refusal(
                "EPHEMERAL_DIRTY_RE was not passed in: run scripts/deploy_candidates, which sources it "
                "from scripts/lib/deploy_marker.sh."
            )
        out = self.git("status", "--porcelain", "--no-renames", "-z").stdout
        lines = [r for r in out.split("\0") if r]
        return [ln for ln in lines if not ln.startswith("??") and not re.search(regex, ln)]

    # ── manifest ─────────────────────────────────────────────────────────
    def load_manifest(self) -> dict | None:
        """The manifest, validated, or None when there is none. Anything this
        engine cannot read raises Refusal: never "empty"."""
        if not self.manifest_path.exists():
            return None
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Refusal(f"the deploy manifest {self.manifest_path} is unreadable: {exc}") from exc
        why = _manifest_problem(data)
        if why:
            raise Refusal(
                f"the deploy manifest {self.manifest_path} is malformed: {why}. Fix it by hand; nothing was changed."
            )
        if os.path.realpath(data["repo"]) != self.common_dir():
            raise Refusal(
                f"the deploy manifest {self.manifest_path} belongs to another repository ({data['repo']}), "
                f"not {self.common_dir()}."
            )
        return data

    def _write_manifest(self, data: dict) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_name(self.manifest_path.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.manifest_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def update_manifest(self, change: Callable[[dict | None], dict | None]) -> dict | None:
        """Read-modify-write under the manifest's own lock, so an add, a drop and
        a rebuild's retirement never lose each other's edit. ``change`` gets the
        validated manifest (or None) and returns what to write (None: nothing)."""
        self.manifest_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_lock_path, "a") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            data = self.load_manifest()
            new = change(data)
            if new is not None:
                self._write_manifest(new)
            return new

    def require_update_lock(self) -> None:
        """The rebuild moves the checkout, so it must run under update.lock held
        EXCLUSIVE by the entry: the fd it passes must be that file, and this run
        must be able to hold it exclusively (it already does, through the same
        open file)."""
        fd_text = self.env.get("DEPLOY_CANDIDATES_LOCK_FD", "")
        hint = "run scripts/deploy_candidates, which takes update.lock exclusive for this command"
        if not fd_text.isdigit():
            raise Refusal(
                f"this command moves the checkout and needs {self.lock_path} (update.lock): {hint}."
            )
        fd = int(fd_text)
        try:
            st, lst = os.fstat(fd), os.stat(self.lock_path)
        except OSError as exc:
            raise Refusal(f"cannot check the update.lock fd {fd}: {exc}; {hint}.") from exc
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise Refusal(f"fd {fd} is not {self.lock_path} (update.lock); {hint}.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Refusal(f"update.lock is held by another process; {hint}.") from exc

    # ── checks ───────────────────────────────────────────────────────────
    def readiness_failures(self) -> list[str]:
        fails: list[str] = []
        serving, why = self._serving(self.root)
        if not serving:
            fails.append(
                f"the serving commit is unknown ({why}); a candidate needs a running server to go live on"
            )
        for label, ref in REQUIRED_SERVING_COMMITS:
            sha = self.resolve(ref) if _HEX40.match(ref) else None
            if not sha:
                fails.append(f"{label}: {ref} resolves to no commit here (it has not merged)")
            elif serving and not self.is_ancestor(sha, serving):
                fails.append(
                    f"{label}: {sha[:12]} is not in the serving commit {serving[:12]} (deploy it first)"
                )
        try:
            names = hooks_to_check(self.root)
        except Refusal as exc:
            fails.append(str(exc))
            names = []
        hooks_dir = Path(self.common_dir()) / "hooks"
        for name in names:
            src = self.root / "scripts" / "hooks" / name
            if not src.is_file():
                continue
            dst = hooks_dir / name
            if not dst.is_file():
                fails.append(
                    f"the git hook {name} is not installed in {hooks_dir} (run scripts/hooks/sync-hooks.sh)"
                )
            elif _sha256(src) != _sha256(dst):
                fails.append(
                    f"the installed git hook {name} differs from scripts/hooks/{name} (run scripts/hooks/sync-hooks.sh)"
                )
        return fails

    def admission_failures(self, base: str, head: str) -> list[str]:
        """What keeps this head from going live: the diff against its merge base
        with origin/main (three dots, so a branch behind main is not charged with
        main's own changes), and any `Deploy-rebuild:` commit it carries."""
        out = self.git("diff", "--no-renames", "--name-status", "-z", f"{base}...{head}").stdout
        parts = [x for x in out.split("\0")]
        fails: list[str] = []
        i = 0
        while i + 1 < len(parts) and parts[i]:
            status, path = parts[i], parts[i + 1]
            i += 2
            if status.startswith("A") and path.startswith(MIGRATION_DIRS):
                fails.append(f"adds a migration ({path}); migrations go live only after they merge")
            elif path in SCHEMA_FILES:
                fails.append(f"changes the boot-time schema ({path}); classified with migrations")
            elif path.startswith(GUARDIAN_OWN_PREFIXES) or path in GUARDIAN_OWN_FILES:
                fails.append(
                    f"changes the host guardian's own code ({path}), which reaches the host"
                )
            elif path == CC_PIN_FILE:
                fails.append(f"changes the Claude Code pin ({path}), which reaches the host")
            elif path.startswith(HOST_DRIVER_PREFIXES):
                fails.append(
                    f"changes what drives the host from this checkout ({path}), which reaches the host"
                )
        commits = self.rev_list(head, "--not", base)
        info = self.read_commits(commits)
        for c in commits:
            if any(_TRAILER_LINE.match(ln) for ln in self._body_lines(info[c][1])):
                fails.append(
                    f"carries the Deploy-rebuild commit {c[:12]}: it was cut from `live`, which never "
                    "bases work; recreate it from origin/main"
                )
                break
        return fails

    def pr_state(self, pr: int, fields: str) -> tuple[dict | None, str]:
        rc, out, err = self._gh(["pr", "view", str(pr), "--json", fields], str(self.root), self.env)
        if rc != 0:
            return None, " ".join((err or out).split()) or f"gh exited {rc}"
        try:
            data = json.loads(out)
        except ValueError:
            return None, "gh printed something that is not JSON"
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            return None, "gh printed no PR state"
        return data, ""

    # ── the plan ─────────────────────────────────────────────────────────
    def build_plan(self, base: str, candidates: list[dict], rebuild_id: str) -> Plan:
        """Merge each candidate onto base in order, off the working tree. Writes
        only objects (unreachable until a ref points at them). Raises Refusal on a
        merge git cannot even attempt.

        Exclusions for a missing branch, failed admission or a derived dependency
        are STICKY; a conflict is recomputed on every pass, because a candidate
        that conflicted only with one excluded later can merge after all. Each
        pass only ever adds sticky exclusions, so the loop ends."""
        sticky: dict[str, str] = {}
        heads: dict[str, str] = {}
        for c in candidates:
            b = c["branch"]
            head = self.resolve(f"refs/heads/{b}")
            if not head:
                sticky[b] = "the branch does not exist here"
                continue
            heads[b] = head
            fails = self.admission_failures(base, head)
            if fails:
                sticky[b] = "admission: " + "; ".join(fails)
        commits = {b: set(self.rev_list(h, "--not", base)) for b, h in heads.items()}
        while True:
            excluded = dict(sticky)
            tip = base
            merged: list[tuple[str, str]] = []
            contained: list[str] = []
            for c in candidates:
                b = c["branch"]
                if b in excluded:
                    continue
                head = heads[b]
                # Nothing to merge: the head is already in the tip (a branch at
                # origin/main, or one an earlier candidate contains). A merge
                # commit with that head as second parent collapses to ONE parent
                # when head == tip, which later rebuilds would read as foreign.
                if self.is_ancestor(head, tip):
                    contained.append(b)
                    continue
                p = self.git(
                    "merge-tree",
                    "--write-tree",
                    "--name-only",
                    "--no-messages",
                    tip,
                    head,
                    check=False,
                )
                if p.returncode == 1:
                    files = [ln for ln in p.stdout.splitlines()[1:] if ln.strip()]
                    excluded[b] = (
                        "conflicts with origin/main or an earlier candidate"
                        + (f" in {', '.join(files)}" if files else "")
                        + "; merge origin/main into it"
                    )
                    continue
                if p.returncode != 0:
                    raise Refusal(
                        f"git merge-tree could not merge {b} ({head[:12]}): {p.stderr.strip()} — nothing changed."
                    )
                tree = p.stdout.splitlines()[0].strip()
                msg = (
                    f"Deploy rebuild {rebuild_id}: merge {b} ({head[:12]})\n\n"
                    f"{TRAILER_KEY}: {rebuild_id}\n{CANDIDATE_KEY}: {b}\n"
                )
                tip = self.git(
                    "commit-tree", tree, "-p", tip, "-p", head, input=msg, extra_env=_IDENTITY
                ).stdout.strip()
                merged.append((b, head))
            # A candidate carrying an excluded candidate's unmerged commits carries
            # its code too: it goes out with it (see carries()).
            new = False
            for b in [m for m, _ in merged] + contained:
                for e in excluded:
                    if e == b or e not in commits:
                        continue
                    others = set().union(*(commits[x] for x in commits if x not in (b, e)))
                    if carries(commits[b], commits[e], others):
                        sticky[b] = f"derived from excluded {e} (it carries {e}'s unmerged commits)"
                        new = True
                        break
            if not new:
                return Plan(
                    base=base, tip=tip, merged=merged, excluded=excluded, contained=contained
                )

    # ── commands ─────────────────────────────────────────────────────────
    def cmd_list(self) -> int:
        data = self.load_manifest()
        if data is None:
            _out(f"No deploy manifest ({self.manifest_path}): nothing is meant to be live.")
            return 0
        if not data["candidates"]:
            _out("The deploy manifest lists no candidates.")
            return 0
        for i, c in enumerate(data["candidates"], 1):
            pr = f"PR #{c['pr']}" if c["pr"] is not None else "no PR"
            approved = ", owner-approved" if c.get("owner_approved") else ""
            _out(
                f"{i}. {c['branch']}  {pr}  owner {c['owner_session']}  added {c['added_at']}  "
                f"verified {c['verified_head'][:12]}{approved}"
            )
        return 0

    def cmd_add(self, branch: str, owner: str, pr: int | None, owner_approved: bool) -> int:
        if branch in (LIVE_BRANCH, BASE_BRANCH, "master") or not _valid_branch(branch):
            raise Refusal(f"{branch!r} cannot be a candidate.")
        # Readiness first: on an install that is not ready nothing is read or
        # written beyond these checks.
        fails = self.readiness_failures()
        if fails:
            raise Refusal(
                "this install is not ready to run candidates on `live`:\n  - "
                + "\n  - ".join(fails)
            )
        self.load_manifest()  # a malformed or foreign manifest refuses before anything else
        head = self.resolve(f"refs/heads/{branch}")
        if not head:
            raise Refusal(f"there is no local branch {branch}.")
        base = self.resolve(BASE_REF)
        if not base:
            raise Refusal(f"{BASE_REF} does not resolve; fetch origin first.")
        fails = self.admission_failures(base, head)
        dispatched = self.env.get("GENESIS_CC_SESSION") == "1"
        devin = False
        if pr is not None:
            data, why = self.pr_state(pr, "state,headRefName,headRefOid,author")
            if data is None:
                raise Refusal(f"cannot read PR #{pr} ({why}); nothing was added.")
            if data["state"] != "OPEN":
                fails.append(f"PR #{pr} is {data['state']}, not open")
            if data.get("headRefName") != branch:
                fails.append(
                    f"PR #{pr}'s head branch is {data.get('headRefName')!r}, not {branch!r}"
                )
            login = str((data.get("author") or {}).get("login") or "")
            devin = DEVIN_LOGIN in login
        if (dispatched or devin) and not owner_approved:
            who = "a Devin-authored PR" if devin else "a dispatched session's candidate"
            fails.append(
                f"{who} goes live only with the owner's approval, per candidate: pass --owner-approved once they give it"
            )
        if fails:
            raise Refusal(f"{branch} cannot go live:\n  - " + "\n  - ".join(fails))
        common = self.common_dir()
        stamp = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        result = {}

        def change(data: dict | None) -> dict:
            data = data or {"version": MANIFEST_VERSION, "repo": common, "candidates": []}
            for c in data["candidates"]:
                if c["branch"] == branch:
                    c.update(
                        pr=pr,
                        owner_session=owner,
                        verified_head=head,
                        owner_approved=owner_approved,
                    )
                    result["kind"] = "re-verified"
                    return data
            data["candidates"].append(
                {
                    "branch": branch,
                    "pr": pr,
                    "owner_session": owner,
                    "added_at": stamp,
                    "verified_head": head,
                    "owner_approved": owner_approved,
                }
            )
            result["kind"] = "added"
            return data

        self.update_manifest(change)
        _out(f"{branch} {result['kind']} at {head[:12]} ({self.manifest_path}).")
        _out("Next: scripts/deploy_candidates rebuild")
        return 0

    def cmd_drop(self, branch: str, no_rebuild: bool) -> int:
        # No manifest: nothing to drop, and nothing is written (not even the
        # manifest's lock file) on an install that never added a candidate.
        if not self.manifest_path.exists():
            raise Refusal(
                f"{branch} is not a candidate: there is no deploy manifest ({self.manifest_path})."
            )
        on_live = self.current_branch() == LIVE_BRANCH
        rebuild = on_live and not no_rebuild
        if rebuild:
            # Every refusal the rebuild can give before it moves anything is
            # checked BEFORE the manifest changes: a drop that cannot take the
            # code out must not report it as gone.
            self.require_update_lock()
            self._rebuild_preflight()
            last = self.resolve(BASE_REF)
            if last:
                self._refuse_foreign(last)
        found = {}

        def change(data: dict | None) -> dict | None:
            if data is None:
                return None
            keep = [c for c in data["candidates"] if c["branch"] != branch]
            if len(keep) == len(data["candidates"]):
                return None
            found["yes"] = True
            data["candidates"] = keep
            return data

        base = self.resolve(BASE_REF)
        dropped_head = self.resolve(f"refs/heads/{branch}")
        after = self.update_manifest(change)
        if not found:
            raise Refusal(f"{branch} is not a candidate in {self.manifest_path}.")
        _out(f"{branch} dropped from {self.manifest_path}.")
        # A candidate stacked on the dropped one still carries its code; the
        # rebuild only excludes those derived from an EXCLUDED candidate, so name
        # them here.
        if base and dropped_head and after:
            theirs = set(self.rev_list(dropped_head, "--not", base))
            sets = {}
            for c in after["candidates"]:
                head = self.resolve(f"refs/heads/{c['branch']}")
                if head:
                    sets[c["branch"]] = set(self.rev_list(head, "--not", base))
            for b, mine in sets.items():
                others = set().union(*(s for x, s in sets.items() if x != b))
                if carries(mine, theirs, others):
                    _out(
                        f"  WARNING: {b} carries {branch}'s unmerged commits: drop it too to take that code out."
                    )
        if not rebuild:
            _out(
                "The checkout is not rebuilt"
                + (" (--no-rebuild)." if no_rebuild else " (it is not on `live`).")
            )
            return 0
        try:
            return self.cmd_rebuild()
        except Refusal as exc:
            raise Refusal(
                f"{exc}\nThe manifest change WAS saved ({branch} is no longer a candidate), but `live` still "
                "runs it: fix the above, then run scripts/deploy_candidates rebuild."
            ) from exc

    def _rebuild_preflight(self) -> tuple[dict, str]:
        """Every refusal a rebuild gives before it fetches, in order. Returns the
        manifest and the checkout's branch."""
        self.require_main_checkout()
        data = self.load_manifest()
        if data is None:
            raise Refusal(
                f"no deploy manifest ({self.manifest_path}): nothing to rebuild. Add a candidate first."
            )
        if self.update_state.exists():
            try:
                phase = json.loads(self.update_state.read_text()).get("phase")
            except (OSError, ValueError, AttributeError):
                phase = None
            if phase != "done":
                raise Refusal(
                    f"{self.update_state} records an unfinished update.sh run; finish it with scripts/update.sh --post-merge."
                )
        branch = self.current_branch()
        if branch not in (LIVE_BRANCH, BASE_BRANCH):
            raise Refusal(
                f"{self.root} is on {branch or 'a detached HEAD'}; a rebuild starts from main or `live`."
            )
        elsewhere = self.live_checked_out_elsewhere()
        if elsewhere:
            raise Refusal(
                f"`live` is checked out in another worktree ({elsewhere}); moving it would change the files under "
                "that worktree. Switch that worktree to another branch first."
            )
        dirty = self.dirty_lines()
        if dirty:
            raise Refusal(
                f"{self.root} has uncommitted tracked changes; a rebuild never stashes or discards them. "
                "Turn them into a candidate with `scripts/deploy_candidates adopt <branch> --owner <who>`:\n"
                + "\n".join("  " + ln for ln in dirty)
            )
        return data, branch

    def live_checked_out_elsewhere(self) -> str | None:
        """The path of a worktree OTHER than this checkout that has `live`
        checked out, read from `git worktree list --porcelain` (a documented,
        stable format: a `worktree <path>` line opens each record and a
        `branch <ref>` line names its branch). `git switch -C` moves such a
        branch without complaint (MEASURED, git 2.43)."""
        out = self.git("worktree", "list", "--porcelain").stdout
        here = os.path.realpath(self.root)
        path = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
            elif line == f"branch {LIVE_REF}" and path and os.path.realpath(path) != here:
                return path
        return None

    def _refuse_foreign(self, base: str) -> None:
        foreign = self.foreign_commits(base)
        if foreign:
            raise Refusal(
                "`live` holds commits that are neither a rebuild merge nor a candidate's; a rebuild would orphan "
                "them. Move each to a branch (and add it as a candidate) or drop it, then rebuild:\n"
                + "\n".join(
                    f"  {c[:12]} {self.git('log', '-1', '--format=%s', c).stdout.strip()}"
                    for c in foreign
                )
            )

    def cmd_rebuild(self) -> int:
        self.require_update_lock()
        data, branch = self._rebuild_preflight()
        rebuild_id = _now().strftime("%Y%m%dT%H%M%SZ")
        _out(f"deploy_candidates rebuild {rebuild_id}")
        # Fetch main. A failed fetch keeps the last-fetched main and says so.
        fetch_note = self.fetch(f"refs/heads/{BASE_BRANCH}:{BASE_REF}")
        base = self.resolve(BASE_REF)
        if not base:
            raise Refusal(f"{BASE_REF} does not resolve; nothing changed.")
        if fetch_note:
            _out(
                f"  WARNING: {fetch_note}; rebuilding on the last-fetched origin/main {base[:12]}."
            )
        self._refuse_foreign(base)
        # Retire candidates whose PR merged. Any unreadable state retires nothing.
        retire: list[tuple[str, int]] = []
        unknown: list[str] = []
        for c in data["candidates"]:
            if c["pr"] is None:
                continue
            state, why = self.pr_state(c["pr"], "state,mergedAt,headRefOid")
            if state is None:
                unknown.append(f"{c['branch']} (PR #{c['pr']}: {why})")
            elif state["state"] == "MERGED":
                retire.append((c["branch"], c["pr"]))
        if unknown:
            _out("  PR state unknown for " + "; ".join(unknown) + " — retired nothing this run.")
            retire = []
        candidates = [c for c in data["candidates"] if c["branch"] not in {b for b, _ in retire}]
        plan = self.build_plan(base, candidates, rebuild_id)
        # Nothing has changed yet beyond the fetch and unreachable objects.
        _out(f"  base: origin/main {base[:12]}")
        for b, head in plan.merged:
            _out(f"  live:     {b} ({head[:12]})")
        for b in plan.contained:
            _out(
                f"  contained: {b} — already in origin/main or an earlier candidate; nothing to merge"
            )
        for b, reason in plan.excluded.items():
            _out(f"  EXCLUDED: {b} — {reason}")
        self._ensure_reflog_kept()
        self._move_checkout(plan, branch)
        # Retirements are saved only once `live` no longer holds them, so a
        # refused checkout leaves the manifest as it was.
        if retire:
            gone = {b for b, _ in retire}
            self.update_manifest(
                lambda d: (
                    None
                    if d is None
                    else {
                        **d,
                        "candidates": [c for c in d["candidates"] if c["branch"] not in gone],
                    }
                )
            )
            for b, n in retire:
                _out(f"  retired: {b} — PR #{n} merged")
        self._fast_forward_main(base)
        _out(
            "  The server was not restarted: it runs what it loaded until it does. Next step, when no"
        )
        _out(
            "  validation holds the lock: scripts/deploy_code_only.sh restart (launch it detached)."
        )
        return 0

    def _ensure_reflog_kept(self) -> None:
        for key in (
            "gc.refs/heads/live.reflogExpire",
            "gc.refs/heads/live.reflogExpireUnreachable",
        ):
            if self.git("config", "--get", key, check=False).stdout.strip() != "never":
                self.git("config", key, "never")
        cur = (
            self.git("config", "--get", "core.logAllRefUpdates", check=False).stdout.strip().lower()
        )
        if cur in ("false", "no", "off", "0"):
            self.git("config", "core.logAllRefUpdates", "true")
            _out("  NOTE: core.logAllRefUpdates was off; turned on so `live` keeps a reflog.")

    def _move_checkout(self, plan: Plan, branch: str | None) -> None:
        live_base, live_merged = self.live_chain(plan.base)
        cur_tip = self.resolve(LIVE_REF)
        if (
            branch == LIVE_BRANCH
            and cur_tip
            and live_base == plan.base
            and live_merged == plan.merged
        ):
            _out(f"  checkout: unchanged ({cur_tip[:12]}): same origin/main, same candidate heads.")
        elif branch == LIVE_BRANCH and cur_tip and self._tree(cur_tip) == self._tree(plan.tip):
            # Same files under new commits: move the ref, not the working tree.
            self.git(
                "update-ref",
                "-m",
                f"deploy-candidates: rebuild (tree unchanged) onto {plan.base[:12]}",
                LIVE_REF,
                plan.tip,
                cur_tip,
            )
            _out(
                f"  checkout: tree unchanged; `live` moved to {plan.tip[:12]} without touching the files."
            )
        else:
            # ONE checkout. --no-overwrite-ignore: git otherwise overwrites an
            # ignored file in the way without asking (MEASURED, git 2.43); with it,
            # as with an untracked one, git refuses and changes nothing.
            p = self.git(
                "switch", "--no-overwrite-ignore", "-C", LIVE_BRANCH, plan.tip, check=False
            )
            if p.returncode != 0:
                raise Refusal(
                    f"git refused to move the checkout to {plan.tip[:12]}; nothing moved:\n{p.stderr.strip()}"
                )
            _out(f"  checkout: moved to {plan.tip[:12]}.")
        up = self.git(
            "branch", "--set-upstream-to", f"{BASE_REMOTE}/{BASE_BRANCH}", LIVE_BRANCH, check=False
        )
        if up.returncode != 0:
            _out(
                f"  WARNING: could not set `live` to track {BASE_REMOTE}/{BASE_BRANCH}: {up.stderr.strip()}"
            )

    def adds_nothing(self, base: str, head: str) -> bool:
        """Would merging ``head`` onto ``base`` leave base's files unchanged? True
        for a change that reached main another way (a squash merge), which a
        candidate with no PR never retires on its own."""
        p = self.git("merge-tree", "--write-tree", "--no-messages", base, head, check=False)
        return p.returncode == 0 and p.stdout.splitlines()[0].strip() == self._tree(base)

    def _tree(self, commit: str) -> str:
        return self.git("rev-parse", commit + "^{tree}").stdout.strip()

    def _fast_forward_main(self, base: str) -> None:
        """Advance local main to origin/main by fetching into it: git refuses a
        branch checked out in another worktree and anything but a fast-forward
        (MEASURED, git 2.43), where `update-ref` would move a checked-out branch
        under that worktree."""
        if self.resolve(f"refs/heads/{BASE_BRANCH}") == base:
            _out(f"  main: already at {base[:12]}.")
            return
        p = self.git("fetch", "-q", ".", f"{base}:refs/heads/{BASE_BRANCH}", check=False)
        if p.returncode == 0:
            _out(f"  main: fast-forwarded to {base[:12]}.")
        else:
            _out(
                f"  WARNING: local main not fast-forwarded ({p.stderr.strip() or 'exit ' + str(p.returncode)})."
            )

    def cmd_status(self) -> int:
        data = self.load_manifest()
        branch = self.current_branch()
        head = self.resolve("HEAD")
        base = self.resolve(BASE_REF)
        _out(
            f"checkout: {branch or 'detached'} at {head[:12] if head else '?'}; origin/main (last fetched) {base[:12] if base else '?'}"
        )
        if data is None:
            _out(f"No deploy manifest ({self.manifest_path}): nothing is meant to be live.")
            return 0
        live_tip = self.resolve(LIVE_REF)
        live_merged: dict[str, str] = {}
        if live_tip and base:
            _, chain = self.live_chain(base)
            live_merged = dict(chain)
            if branch == LIVE_BRANCH and head != live_tip:
                _out("  WARNING: HEAD is not the tip of `live`.")
        if not data["candidates"]:
            _out("The deploy manifest lists no candidates.")
            return 0
        plan = self.build_plan(base, data["candidates"], "status-dry-run") if base else None
        for c in data["candidates"]:
            b = c["branch"]
            cur = self.resolve(f"refs/heads/{b}")
            _out(
                f"{b}  {'PR #' + str(c['pr']) if c['pr'] is not None else 'no PR'}  owner {c['owner_session']}"
            )
            if b in live_merged:
                _out(f"    live at {live_merged[b][:12]}")
            else:
                _out("    not in `live`")
            if plan is None:
                _out("    would be: unknown (origin/main does not resolve)")
            elif b in plan.excluded:
                _out(f"    would be EXCLUDED at the next rebuild: {plan.excluded[b]}")
            elif b in plan.contained:
                _out(
                    "    contained: already in origin/main or an earlier candidate; nothing to merge"
                )
            else:
                _out("    would be included at the next rebuild")
            if cur and base and self.adds_nothing(base, cur):
                _out("    adds nothing beyond origin/main (its change is already there): drop it?")
            if cur and cur != c["verified_head"]:
                _out(
                    f"    head {cur[:12]} — unverified since rework (verified {c['verified_head'][:12]})"
                )
            if c["pr"] is not None:
                st, why = self.pr_state(c["pr"], "state,updatedAt,mergedAt")
                if st is None:
                    _out(f"    PR state unknown ({why})")
                else:
                    updated = _parse_time(str(st.get("updatedAt") or ""))
                    line = f"    PR {st['state']}"
                    if st["state"] == "MERGED":
                        line += " — retires at the next rebuild"
                    if updated:
                        line += f", updated {updated.strftime('%Y-%m-%d')}"
                        if st["state"] == "OPEN" and _now() - updated > datetime.timedelta(
                            days=STALE_DAYS
                        ):
                            line += f" — stale (no update in {STALE_DAYS} days): keep or drop?"
                    _out(line)
        return 0

    def cmd_adopt(self, branch: str, owner: str) -> int:
        if not _valid_branch(branch) or branch in (LIVE_BRANCH, BASE_BRANCH, "master"):
            raise Refusal(f"{branch!r} cannot be the adopted branch's name.")
        if self.resolve(f"refs/heads/{branch}"):
            raise Refusal(f"the branch {branch} already exists; choose a new name.")
        head = self.resolve("HEAD")
        if not head:
            raise Refusal("HEAD does not resolve.")
        dirty = self.dirty_lines()
        if not dirty:
            raise Refusal("no uncommitted tracked edits: nothing to adopt.")
        paths = [ln[3:] for ln in dirty]
        idx = (
            Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
            / f"deploy-candidates-adopt.{os.getpid()}.index"
        )
        try:
            env = {"GIT_INDEX_FILE": str(idx), "GIT_LITERAL_PATHSPECS": "1"}
            self.git("read-tree", head, extra_env=env)
            self.git("add", "-A", "--", *paths, extra_env=env)
            tree = self.git("write-tree", extra_env=env).stdout.strip()
        finally:
            idx.unlink(missing_ok=True)
        msg = (
            "adopt: uncommitted edits from the live checkout\n\n"
            f"A snapshot of {len(paths)} tracked file(s) edited in place on top of {head[:12]}.\n\n"
            f"Adopted-by: {owner}\n"
        )
        commit = self.git(
            "commit-tree", tree, "-p", head, input=msg, extra_env=_IDENTITY
        ).stdout.strip()
        # Create, never overwrite: an empty old value refuses an existing ref.
        self.git(
            "update-ref",
            "-m",
            f"deploy-candidates: adopt by {owner}",
            f"refs/heads/{branch}",
            commit,
            "",
        )
        _out(
            f"adopted {len(paths)} file(s) as {branch} at {commit[:12]} (on top of {head[:12]}); the checkout is untouched."
        )
        self._adopt_report(paths)
        _out("Next: decide which edits still need to go live (the report above), then")
        _out(f"  scripts/deploy_candidates add {branch} --owner {owner}")
        return 0

    def _adopt_report(self, paths: list[str]) -> None:
        notes = []
        fetch_note = self.fetch(f"refs/heads/{BASE_BRANCH}:{BASE_REF}")
        if fetch_note:
            notes.append(f"{fetch_note}; compared with the last-fetched origin/main")
        base = self.resolve(BASE_REF)
        prs: list[tuple[int, str]] = []
        rc, out, err = self._gh(
            ["pr", "list", "--state", "open", "--limit", "1000", "--json", "number,headRefOid"],
            str(self.root),
            self.env,
        )
        if rc != 0:
            notes.append(
                f"open PR heads unavailable ({' '.join((err or out).split())}): no file was compared with a PR"
            )
        else:
            try:
                rows = json.loads(out)
                prs = [(int(r["number"]), str(r["headRefOid"])) for r in rows]
                if len(rows) >= 1000:
                    notes.append(
                        "the open PR list hit its limit of 1000: some PRs were not compared"
                    )
            except (ValueError, KeyError, TypeError):
                notes.append("the open PR list is unreadable: no file was compared with a PR")
        available: list[tuple[int, str]] = []
        missing: list[str] = []
        for n, sha in prs:
            if not self.resolve(sha):
                self.fetch(f"refs/pull/{n}/head")
            if self.resolve(sha):
                available.append((n, sha))
            else:
                missing.append(f"#{n}")
        if missing:
            notes.append(
                f"the heads of {len(missing)} open PR(s) could not be fetched and were not compared: {' '.join(missing)}"
            )
        _out("Per file (working tree compared with origin/main and each open PR's head):")
        width = max(len(p) for p in paths)
        for path in paths:
            mine = self._worktree_blob(path)
            verdict = []
            if base is not None and self._blob_at(base, path) == mine:
                verdict.append("equals origin/main")
            for n, sha in available:
                if self._blob_at(sha, path) == mine:
                    verdict.append(f"equals PR #{n}'s head")
            _out(
                f"  {path.ljust(width)}  {', '.join(verdict) if verdict else 'neither (not origin/main, not an open PR head)'}"
            )
        for note in notes:
            _out(f"  NOTE: {note}.")

    def _worktree_blob(self, path: str) -> str | None:
        full = self.root / path
        if not full.exists() and not full.is_symlink():
            return None
        return self.git("hash-object", "--", path).stdout.strip()

    def _blob_at(self, commit: str, path: str) -> str | None:
        p = self.git("rev-parse", "--verify", "-q", f"{commit}:{path}", check=False)
        return p.stdout.strip() if p.returncode == 0 else None


def carries(mine: set[str], theirs: set[str], others: set[str]) -> bool:
    """Does a branch whose unmerged commits are ``mine`` carry the code of an
    excluded one whose unmerged commits are ``theirs``? ``others`` are the
    commits of every OTHER candidate.

    It does when they share a commit that no other candidate also holds (a
    commit two siblings share because both are stacked on a third candidate is
    that candidate's code), UNLESS every commit of ``mine`` is in ``theirs``:
    then the excluded branch is stacked ON this one, and its commits are this
    one's own.

    Git cannot say which of two branches OWNS a commit both hold. A branch cut
    from an early commit of another, and a branch the other was cut from that
    then gained a commit, are the same graph; both read as carrying it, and the
    rule excludes (the safe side: nothing goes live that might be the excluded
    candidate's code, and the exclusion is reported by name)."""
    return bool((mine & theirs) - others) and not mine <= theirs


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _positive_int(text: str | None, default: int) -> int:
    if text and text.isdigit() and int(text) > 0:
        return int(text)
    return default


def _valid_branch(name: str) -> bool:
    return (
        bool(name)
        and subprocess.run(
            ["git", "check-ref-format", "--branch", name], capture_output=True
        ).returncode
        == 0
        and not name.startswith("-")
    )


def _manifest_problem(data: object) -> str:
    """Why this is not a manifest this engine wrote, or "" when it is."""
    if not isinstance(data, dict):
        return "not a JSON object"
    if data.get("version") != MANIFEST_VERSION:
        return f"version {data.get('version')!r}, not {MANIFEST_VERSION}"
    repo = data.get("repo")
    if not isinstance(repo, str) or not os.path.isabs(repo):
        return '"repo" is not an absolute path'
    cands = data.get("candidates")
    if not isinstance(cands, list):
        return '"candidates" is not a list'
    seen = set()
    for i, c in enumerate(cands):
        if not isinstance(c, dict):
            return f"candidate {i} is not an object"
        if not isinstance(c.get("branch"), str) or not c["branch"]:
            return f"candidate {i} has no branch"
        if not _valid_branch(c["branch"]) or c["branch"] in (LIVE_BRANCH, BASE_BRANCH, "master"):
            return f"candidate {c['branch']!r} is not a branch name a candidate can have"
        if c["branch"] in seen:
            return f"candidate {c['branch']} is listed twice"
        seen.add(c["branch"])
        pr = c.get("pr")
        if pr is not None and (not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0):
            return f"candidate {c['branch']} has a pr that is not a positive number"
        for key in ("owner_session", "added_at"):
            if not isinstance(c.get(key), str) or not c[key]:
                return f"candidate {c['branch']} has no {key}"
        if not isinstance(c.get("verified_head"), str) or not _HEX40.match(c["verified_head"]):
            return f"candidate {c['branch']} has no full verified_head"
        if not isinstance(c.get("owner_approved"), bool):
            return f"candidate {c['branch']} has no owner_approved flag"
    return ""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy_candidates", description="The engine of `live`, the local integration branch."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("branch")
    a.add_argument("--owner", required=True)
    a.add_argument("--pr", type=int)
    a.add_argument("--owner-approved", action="store_true")
    d = sub.add_parser("drop")
    d.add_argument("branch")
    d.add_argument("--no-rebuild", action="store_true")
    sub.add_parser("list")
    sub.add_parser("status")
    sub.add_parser("rebuild")
    ad = sub.add_parser("adopt")
    ad.add_argument("branch")
    ad.add_argument("--owner", required=True)
    return p


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    gh: GhRunner | None = None,
    serving: Callable[[Path], tuple[str | None, str]] | None = None,
) -> int:
    env = dict(os.environ if env is None else env)
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code in (0, None) else 2
    # GENESIS_DEPLOY_CANDIDATES_ROOT is a TEST seam (scratch repositories); unset,
    # the checkout is the one this script lives in.
    root = Path(env.get("GENESIS_DEPLOY_CANDIDATES_ROOT") or Path(__file__).resolve().parents[1])
    engine = Engine(root, env, gh=gh, serving=serving)
    try:
        engine.place(args.cmd)
        if args.cmd == "list":
            return engine.cmd_list()
        if args.cmd == "status":
            return engine.cmd_status()
        if args.cmd == "add":
            return engine.cmd_add(args.branch, args.owner, args.pr, args.owner_approved)
        if args.cmd == "drop":
            return engine.cmd_drop(args.branch, args.no_rebuild)
        if args.cmd == "rebuild":
            return engine.cmd_rebuild()
        if args.cmd == "adopt":
            return engine.cmd_adopt(args.branch, args.owner)
    except Refusal as exc:
        _err(f"ERROR: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
