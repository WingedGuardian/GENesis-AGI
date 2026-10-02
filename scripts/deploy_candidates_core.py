"""deploy_candidates_core.py — git, GitHub and output plumbing for the engine of
`live` (see deploy_candidates.py for what `live` is and every command).

Flat sibling of deploy_candidates.py, loaded by that module's finder (scripts/
is never on ``sys.path``). Nothing here decides anything about a candidate: that
is deploy_candidates_gate.py.

Every git call on the repository goes through ``Repo.git``, the one hardened
runner (``read_commits`` runs its own ``cat-file --batch`` with the same
hardened environment; ``valid_candidate_name`` runs ``check-ref-format``, which
reads no repository): an allowlisted
environment (no caller GIT_* variable can redirect the repository, the replace
ref base, config or attributes), replace objects and grafts off, and output read
as bytes and decoded with surrogateescape, so a non-UTF-8 path round-trips
instead of crashing. Its answers are three-valued where git's are: a git failure
that is not git's documented "no" raises Unknown, never reads as no.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

LIVE_BRANCH = "live"
LIVE_REF = "refs/heads/live"
BASE_REMOTE = "origin"
BASE_BRANCH = "main"
BASE_REF = "refs/remotes/origin/main"
TRAILER_KEY = "Deploy-rebuild"
CANDIDATE_KEY = "Deploy-candidate"
STALE_DAYS = 7
# Branch names a candidate can never have: `live` and `main` themselves, and any
# name under them (git cannot hold refs/heads/live and refs/heads/live/x at once,
# so a `live/x` candidate would make every rebuild fail at the checkout move).
RESERVED_NAMES = (LIVE_BRANCH, BASE_BRANCH, "master")
RESERVED_PREFIXES = (LIVE_BRANCH + "/", BASE_BRANCH + "/", "master/")

# Bounded waits, each because a lock is held while it runs: rebuild, drop and add
# hold the EXCLUSIVE update.lock, so a hung fetch, GitHub call or status probe
# would stall every deploy and validation behind it. 120 s is
# deploy_code_only.sh's fetch bound. A timeout reads as UNKNOWN, and an unknown
# never moves `live` (the command refuses, nothing changed).
FETCH_TIMEOUT_DEFAULT = 120
GH_TIMEOUT = 120
STATUS_TIMEOUT = 120
SYNC_HOOKS_TIMEOUT = 120

# The rebuild's own commits carry a fixed identity, so a rebuild from a plain
# shell on an install with no git identity configured still works, and so a
# rebuild merge can be told from a hand-made commit that happens to carry the
# trailer. They are never published (the pre-push hook refuses them).
IDENTITY_EMAIL = "deploy-candidates@localhost.invalid"
IDENTITY = {
    "GIT_AUTHOR_NAME": "deploy-candidates",
    "GIT_AUTHOR_EMAIL": IDENTITY_EMAIL,
    "GIT_COMMITTER_NAME": "deploy-candidates",
    "GIT_COMMITTER_EMAIL": IDENTITY_EMAIL,
}

# Every GIT_* variable from the caller is dropped except these: how git talks to
# a remote (ssh, credentials, TLS trust, proxy, allowed protocols), and
# GIT_CONFIG_NOSYSTEM, which only ever reads LESS config. The rest can point git
# at another
# repository, index, object store, replace-ref base, graft file, attribute
# source or config (GIT_CONFIG_COUNT/KEY/VALUE, GIT_CONFIG_PARAMETERS) and so
# change what every plumbing call below answers.
_GIT_ENV_KEEP = frozenset(
    {
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "GIT_ASKPASS",
        "GIT_TERMINAL_PROMPT",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_PROXY_COMMAND",
        "GIT_ALLOW_PROTOCOL",
        "GIT_CONFIG_NOSYSTEM",
    }
)
# gh would query another repository than the checkout's.
_GH_ENV_DROP = frozenset({"GH_REPO", "GH_HOST"})
# Set on every git call: a replace ref (refs/replace/<pinned head>) or a graft
# would make merge-tree, rev-list and ancestry read another commit than the one
# pinned, so `live` could run code that is in neither the candidate nor its PR
# (MEASURED, git 2.43; GIT_GRAFT_FILE pointed at an empty file disables grafts).
GIT_HARDENING = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
# ASCII case folding only, as the pre-push hook's `LC_ALL=C awk tolower` does.
TRAILER_LINE = re.compile(r"^deploy-rebuild[ \t]*:", re.IGNORECASE | re.ASCII)
CANDIDATE_LINE = re.compile(r"^deploy-candidate[ \t]*:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.ASCII)
_BLANK = re.compile(r"[ \t\r]*")  # the pre-push hook's blank(): /^[ \t\r]*$/


def scrub_env(env: Mapping[str, str]) -> dict[str, str]:
    """The caller's environment without what could redirect git or gh."""
    return {
        k: v
        for k, v in env.items()
        if not (k.startswith("GIT_") and k not in _GIT_ENV_KEEP) and k not in _GH_ENV_DROP
    }


def _dec(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


# Used in annotations only (strings under `from __future__ import annotations`).
GhRunner = "Callable[[list[str], str, Mapping[str, str]], tuple[int, str, str]]"


class Refusal(Exception):
    """A refusal: printed as one ERROR, exit 1, nothing changed by the caller."""


class Unknown(Refusal):
    """A fact a decision needs could not be established (a failed fetch, an
    unreadable PR state, git unable to resolve a branch). It is never read as yes or as no: the
    command that needed it refuses and nothing moves."""


@dataclass
class Plan:
    base: str
    tip: str
    merged: list[tuple[str, str]] = field(default_factory=list)  # (branch, head)
    excluded: dict[str, str] = field(default_factory=dict)  # branch -> reason
    contained: list[str] = field(default_factory=list)  # already in the tip: nothing to merge


def out(msg: str = "") -> None:
    print(msg, flush=True)


def after_move(state: str, what: str, fn: Callable[[], object]) -> object:
    """Run a step that comes AFTER the commit point (the checkout, a ref or the
    manifest has already changed). A failure here must not be reported as a
    refusal ("nothing changed" would be false): it is a WARNING that states
    what IS so (``state``) and names the step. Returns fn's result, or None."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - every failure past the commit point is a warning
        out(f"  WARNING: {state}, but {what} failed: {exc}")
        return None


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)  # noqa: UP017


def parse_time(text: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def positive_int(text: str | None, default: int) -> int:
    if text and text.isdigit() and int(text) > 0:
        return int(text)
    return default


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def valid_candidate_name(name: object) -> bool:
    """A local branch name git accepts that is not `live`, `main` or under them,
    and is valid UTF-8: a name with undecodable bytes is written into the
    Deploy-candidate trailer and read back changed (commit-tree re-encodes the
    message), so `drop` could not find it in `live` again (MEASURED)."""
    return (
        isinstance(name, str)
        and bool(name)
        and _utf8(name)
        and not name.startswith("-")
        and name not in RESERVED_NAMES
        and not name.startswith(RESERVED_PREFIXES)
        and subprocess.run(
            ["git", "check-ref-format", "--branch", name], capture_output=True
        ).returncode
        == 0
    )


def gh_default(args: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str, str]:
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


def parse_serving(stdout: str, returncode: int) -> tuple[str | None, str]:
    """The `serving:` line of `deploy_code_only.sh status`: a full commit id, or
    the reason it prints in its place. Anything else is unknown."""
    for line in stdout.splitlines():
        if line.startswith("serving: "):
            value = line[len("serving: ") :].strip()
            if HEX40.match(value):
                return value, ""
            return None, value or "an empty serving line"
    return None, f"deploy_code_only.sh status printed no serving line (exit {returncode})"


def serving_default(root: Path, env: Mapping[str, str]) -> tuple[str | None, str]:
    """The commit genesis-server booted from, per `deploy_code_only.sh status`
    in ``root`` (read-only, takes no lock), which reads it from HEAD's reflog at
    the unit's start time and says why when it cannot."""
    script = Path(root) / "scripts" / "deploy_code_only.sh"
    try:
        p = subprocess.run(
            ["bash", str(script), "status"],
            cwd=str(root),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, f"{script.name} status did not answer within {STATUS_TIMEOUT}s"
    except OSError as exc:
        return None, f"cannot run {script}: {exc}"
    return parse_serving(p.stdout, p.returncode)


class Repo:
    """One checkout, its git, and its GitHub. Every git call runs through the one
    hardened runner (``git``)."""

    def __init__(
        self,
        root: Path,
        env: Mapping[str, str],
        gh: GhRunner | None = None,
        serving: Callable[[Path], tuple[str | None, str]] | None = None,
    ):
        self.root = Path(root)
        # Variables that point git (or gh) at ANOTHER repository, index, object
        # store or config (set inside a git hook, or left by a caller) would make
        # every call below act somewhere other than self.root. Child scripts
        # (the deploy status script, sync-hooks.sh) get this environment too.
        self.env = scrub_env(env)
        self._gh = gh or gh_default
        self._serving = serving or (lambda r: serving_default(r, self.env))
        self.home = Path(self.env.get("HOME") or str(Path.home()))
        genesis_home = self.env.get("GENESIS_HOME") or str(self.home / ".genesis")
        self.lock_path = Path(genesis_home) / "locks" / "update.lock"
        self.update_state = self.home / ".genesis" / "update_state.json"
        self._pr_cache: dict[int, tuple[dict | None, str]] = {}

    # ── git ──────────────────────────────────────────────────────────────
    def git(
        self, *args: str, check: bool = True, input: str | None = None, extra_env=None, timeout=None
    ):
        env = {**self.env, **GIT_HARDENING, **(extra_env or {})}
        raw = subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=env,
            capture_output=True,
            input=None if input is None else input.encode("utf-8", "surrogateescape"),
            timeout=timeout,
        )
        p = subprocess.CompletedProcess(
            raw.args, raw.returncode, _dec(raw.stdout), _dec(raw.stderr)
        )
        if check and p.returncode != 0:
            raise Refusal(f"git {' '.join(args)} failed (exit {p.returncode}): {p.stderr.strip()}")
        return p

    def fetch(self, refspec: str) -> tuple[bool, str]:
        """A bounded fetch of one refspec from origin: (True, "") when it landed,
        else (False, why). The caller decides what an unfetched ref means."""
        timeout = positive_int(self.env.get("GENESIS_DEPLOY_FETCH_TIMEOUT"), FETCH_TIMEOUT_DEFAULT)
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
            return False, f"fetching {refspec} timed out after {timeout}s"
        if p.returncode == 0:
            return True, ""
        return False, f"fetching {refspec} failed ({p.stderr.strip()})"

    @staticmethod
    def _unknown(what: str, p) -> Unknown:
        return Unknown(f"git cannot tell {what} (exit {p.returncode}): {p.stderr.strip()}")

    def resolve(self, ref: str) -> str | None:
        """The commit ``ref`` names, None when it names none (rev-parse -q exits 1:
        a missing ref, path or object, MEASURED git 2.43). Any other failure is
        git unable to answer: Unknown."""
        p = self.git("rev-parse", "--verify", "-q", ref + "^{commit}", check=False)
        text = p.stdout.strip()
        if p.returncode == 1:
            return None
        if p.returncode != 0 or not HEX40.match(text):
            raise self._unknown(f"what {ref} names", p)
        return text

    def is_ancestor(self, a: str, b: str) -> bool:
        """merge-base --is-ancestor: 0 yes, 1 no; anything else (128: a missing
        object) is Unknown, never "no"."""
        p = self.git("merge-base", "--is-ancestor", a, b, check=False)
        if p.returncode in (0, 1):
            return p.returncode == 0
        raise self._unknown(f"whether {a[:12]} is an ancestor of {b[:12]}", p)

    def merge_base(self, a: str, b: str) -> str | None:
        """None when the two share no history (exit 1); Unknown on any other failure."""
        p = self.git("merge-base", a, b, check=False)
        text = p.stdout.strip()
        if p.returncode == 1:
            return None
        if p.returncode != 0 or not HEX40.match(text):
            raise self._unknown(f"the merge base of {a[:12]} and {b[:12]}", p)
        return text

    def rev_list(self, *args: str) -> list[str]:
        text = self.git("rev-list", *args).stdout
        return [x for x in text.split() if x]

    def tree(self, commit: str) -> str:
        return self.git("rev-parse", commit + "^{tree}").stdout.strip()

    def blob_at(self, commit: str, path: str) -> str | None:
        p = self.git("rev-parse", "--verify", "-q", f"{commit}:{path}", check=False)
        if p.returncode == 1:
            return None
        if p.returncode != 0:
            raise self._unknown(f"whether {path} exists at {commit[:12]}", p)
        return p.stdout.strip()

    def show(self, commit: str, path: str) -> str | None:
        p = self.git("show", f"{commit}:{path}", check=False)
        return p.stdout if p.returncode == 0 else None

    def common_dir(self) -> str:
        text = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        if "\n" in text or not os.path.isabs(text):
            raise Refusal(f"cannot read this repository's git directory ({text!r})")
        return os.path.realpath(text)

    def hooks_dir(self) -> Path:
        """Where git runs hooks from: `git rev-parse --git-path hooks`, which
        follows core.hooksPath when it is set."""
        text = self.git("rev-parse", "--path-format=absolute", "--git-path", "hooks").stdout.strip()
        return Path(text)

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
            out(f"(from a linked worktree: reporting the main checkout, {main_root})")
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
        if p.returncode == 1:
            return None  # a detached HEAD
        if p.returncode != 0:
            raise self._unknown("which branch is checked out", p)
        return p.stdout.strip()

    def read_commits(self, oids: list[str]) -> dict[str, tuple[list[str], str, str]]:
        """Each commit's parents, committer email and message, read RAW
        (`cat-file --batch`), so no trailer or encoding setting changes what is
        read. --no-replace-objects: a replace ref would otherwise show another
        commit."""
        if not oids:
            return {}
        p = subprocess.run(
            ["git", "-C", str(self.root), "--no-replace-objects", "cat-file", "--batch"],
            env={**self.env, **GIT_HARDENING},
            input=("\n".join(oids) + "\n").encode(),
            capture_output=True,
        )
        if p.returncode != 0:
            raise Refusal(f"cannot read commits: {p.stderr.decode(errors='replace').strip()}")
        data = p.stdout
        pos = 0
        result: dict[str, tuple[list[str], str, str]] = {}
        for _ in oids:
            nl = data.index(b"\n", pos)
            header = data[pos:nl].decode().split()
            if len(header) != 3 or header[1] != "commit":
                raise Refusal(f"cannot read a commit: {' '.join(header)}")
            size = int(header[2])
            body = data[nl + 1 : nl + 1 + size].decode("utf-8", errors="replace")
            pos = nl + 1 + size + 1
            head, _, msg = body.partition("\n\n")
            parents, committer = [], ""
            for ln in head.split("\n"):
                if ln.startswith("parent "):
                    parents.append(ln.split()[1])
                elif ln.startswith("committer "):
                    m = re.search(r"<([^>]*)>", ln)
                    committer = m.group(1) if m else ""
            result[header[0]] = (parents, committer, msg)
        return result

    @staticmethod
    def body_lines(msg: str) -> list[str]:
        """The message lines after its first paragraph (the subject), the only
        place git ever reads trailers; the same wide rule the pre-push hook uses,
        with its exact line split (LF only) and its blank-line test (ASCII space,
        tab, CR), so the two can never disagree about a line."""
        lines = msg.split("\n")

        def blank(line: str) -> bool:
            return _BLANK.fullmatch(line) is not None

        i = 0
        while i < len(lines) and blank(lines[i]):
            i += 1
        while i < len(lines) and not blank(lines[i]):
            i += 1
        return lines[i:]

    def is_rebuild_merge(self, parents: list[str], committer: str, msg: str) -> bool:
        """A rebuild's own merge: two parents, the engine's committer identity and
        the trailer. A hand-made commit carrying the trailer is not one, so it is
        never taken as the engine's and its second parent is never whitelisted."""
        return (
            len(parents) == 2
            and committer == IDENTITY_EMAIL
            and any(TRAILER_LINE.match(ln) for ln in self.body_lines(msg))
        )

    def live_chain(self, base: str) -> tuple[str | None, list[tuple[str, str]]]:
        """What `live` holds now: the commit its rebuild merges sit on, and the
        (branch, head) each merged, oldest first. (None, []) when `live` does not
        exist; (tip, []) when it has no rebuild merge on top."""
        tip = self.resolve(LIVE_REF)
        if not tip:
            return None, []
        chain = self.rev_list("--first-parent", tip, "--not", base)
        info = self.read_commits(chain)
        merged: list[tuple[str, str]] = []
        live_base = None
        for c in chain:
            parents, committer, msg = info[c]
            if not self.is_rebuild_merge(parents, committer, msg):
                live_base = c
                break
            branch = next(
                (m.group(1) for ln in self.body_lines(msg) if (m := CANDIDATE_LINE.match(ln))),
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

    def refuse_foreign(self, base: str) -> None:
        foreign = self.foreign_commits(base)
        if foreign:
            raise Refusal(
                "`live` holds commits that are neither a rebuild merge nor a candidate's; a rebuild would orphan "
                "them. Move each to a branch (and add it as a candidate) or drop it, then rebuild. If origin/main "
                "was force-pushed, these may be its old commits: check them before discarding anything:\n"
                + "\n".join(
                    f"  {c[:12]} {self.git('log', '-1', '--format=%s', c).stdout.strip()}"
                    for c in foreign
                )
            )

    def live_checked_out_elsewhere(self) -> str | None:
        """The path of a worktree OTHER than this checkout that has `live`
        checked out, read from `git worktree list --porcelain -z`: each field
        ends in NUL and each record in an extra NUL, so a path holding a newline
        cannot be misread (MEASURED, git 2.43). `git switch -C` moves such a
        branch without complaint."""
        text = self.git("worktree", "list", "--porcelain", "-z").stdout
        here = os.path.realpath(self.root)
        for record in text.split("\0\0"):
            fields = record.split("\0")
            path = next((f[len("worktree ") :] for f in fields if f.startswith("worktree ")), None)
            if f"branch {LIVE_REF}" in fields and path and os.path.realpath(path) != here:
                return path
        return None

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
        text = self.git("status", "--porcelain", "--no-renames", "-z").stdout
        lines = [r for r in text.split("\0") if r]
        return [ln for ln in lines if not ln.startswith("??") and not re.search(regex, ln)]

    # ── GitHub ───────────────────────────────────────────────────────────
    def pr_state(self, pr: int) -> tuple[dict | None, str]:
        """The PR's state, base, head and merge commit, read once per run:
        (data, "") or (None, why it could not be read)."""
        if pr in self._pr_cache:
            return self._pr_cache[pr]
        fields = "state,baseRefName,headRefName,headRefOid,mergeCommit,updatedAt"
        rc, text, err = self._gh(
            ["pr", "view", str(pr), "--json", fields], str(self.root), self.env
        )
        result: tuple[dict | None, str]
        if rc != 0:
            result = (None, " ".join((err or text).split()) or f"gh exited {rc}")
        else:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if not isinstance(data, dict) or not isinstance(data.get("state"), str):
                result = (None, "gh printed no PR state")
            else:
                result = (data, "")
        self._pr_cache[pr] = result
        return result

    def serving(self) -> tuple[str | None, str]:
        return self._serving(self.root)
