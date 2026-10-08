"""deploy_candidates_gate.py — every decision about whether a candidate may run
on `live`, made at the moment it is needed.

The engine's one rule: a decision is never recorded at `add` and trusted later.
`rebuild` re-derives every one of these for every candidate, every time; `add`
runs them as a preview before it writes; `status` reports them. Each check
answers in one of three ways:

  * passes (None);
  * a KNOWN-NEGATIVE reason (a string): the candidate is excluded, by name;
  * UNKNOWN (raises ``Unknown``): a fact could not be established, so the
    command refuses and nothing moves. An unknown is never read as yes or no.

Flat sibling of deploy_candidates.py (see that file for the commands).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

# Imported only through deploy_candidates.py, whose finder resolves the
# sibling modules from this directory: scripts/ is never on sys.path.
from deploy_candidates_core import (  # noqa: E402
    BASE_BRANCH,
    HEX40,
    TRAILER_LINE,
    Refusal,
    Repo,
    Unknown,
)

# ── Readiness ─────────────────────────────────────────────────────────────
# What the server must be running before anything may go live, checked on the
# newest commit the serving tree shares with origin/main (merge-base), never on
# the serving or working tree, which on `live` carries candidate code.
#
# Changes already merged are named by their merge commit (main is squash-only:
# 0 merge commits in its last 200 first-parent commits, so a PR cannot carry its
# own merge commit id; it is recorded here after it lands).
REQUIRED_MERGED: tuple[tuple[str, str], ...] = (
    (
        "#2673, the live-branch guard fixes (issue #2532)",
        "8fa53412c9f1ffb93e8831d126be962afd90106c",  # pragma: allowlist secret
    ),
)
# PR C (the wipers: host REVERT_CODE, bootstrap crash recovery, the dashboard's
# update routes) has not merged. It creates this file, which holds bootstrap's
# crash-recovery branch guard; until it exists in the serving base, `add` and
# `rebuild` refuse on every install and the engine ships INERT. PR C also adds
# the check that the host guardian's deployed commit contains it, because
# REVERT_CODE runs on the host.
PR_C_MARKER = "scripts/lib/deploy_live.sh"
SYNC_HOOKS = "scripts/hooks/sync-hooks.sh"
_SYNC_ARRAYS = ("HOOKS_TO_SYNC", "HELPERS_TO_SYNC")

# ── Admission (v1) ────────────────────────────────────────────────────────
# Migrations are refused (owner, 2026-09-27); the boot-time schema files carry
# ALTER/DROP/RENAME with no ids, so they are classified with them.
MIGRATION_DIRS = ("src/genesis/db/migrations/", "src/genesis/db/data_migrations/")
SCHEMA_FILES = ("src/genesis/db/schema/_migrations.py", "src/genesis/db/schema/_tables.py")
# What reaches the HOST: the guardian's own files from update.sh's GUARDIAN_PATHS.
# The rest of that list is runtime code the container runs too (GUARDIAN_SHARED_
# RUNTIME); refusing it would refuse most PRs. update.sh, which redeploys the
# guardian, refuses to run on `live` and is itself refused below. A test pins
# that every GUARDIAN_PATHS entry is in exactly one of the two.
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
# What drives the HOST from this checkout on a timer: genesis-cc-align.timer
# runs scripts/cc_align_host.sh (the host's Claude Code and Node, through the
# guardian gateway) and genesis-cc-tmp-align.timer runs scripts/cc_tmp_align_host.sh
# (the host's cc-tmp volume). A test classifies every systemd ExecStart target.
HOST_DRIVER_PREFIXES = (
    "scripts/cc_align_host.sh",
    "scripts/systemd/genesis-cc-align.",
    "scripts/cc_tmp_align_host.sh",
    "scripts/systemd/genesis-cc-tmp-align.",
)
# The git and Claude Code hooks (owner, 2026-10-01): the guards that protect the
# repository never run code that is neither reviewed nor owner-approved for that
# head (#2978: `add --approve-hooks`). Only these two directories (owner ruling
# 87b86c40): the wider hook surface (.claude/settings.json, config/behavioral_rules/,
# the hook scripts at scripts/ root) is admitted, an accepted residual.
HOOK_DIRS = ("scripts/hooks/", ".claude/hooks/")
# The refusals that keep the wipers off `live`, and everything they source before
# their branch check runs (a candidate editing one could switch its own guard
# off). A test re-derives the sourced libs from update.sh and deploy_code_only.sh.
REFUSAL_FILES = (
    "scripts/update.sh",
    "scripts/deploy_code_only.sh",
    "scripts/bootstrap.sh",
    "scripts/lib/deploy_checkout.sh",
    "scripts/lib/deploy_marker.sh",
    "scripts/lib/guardian_pause.sh",
    "scripts/lib/alert_queue.sh",
    "scripts/lib/deploy_status.sh",
    "scripts/lib/live_system_guard.sh",
    # Read with `cat` before the branch check and run as python later: the
    # serving read runs inside `deploy_code_only.sh status`, which readiness runs.
    "scripts/lib/serving_commit.py",
    "scripts/lib/manifest_delta.py",
    # Also read at startup, run after the check: the restart refusal's session
    # scan, and the restarted unit's identity probe (read through its test seam,
    # `${GENESIS_DEPLOY_PORT_PROBE:-…}`, which the `cat` lock below now matches).
    "scripts/lib/server_sessions.py",
    "scripts/lib/port_owned_by.py",
    # The verdict the refusals consult on `live` (read at startup like the
    # above), and the dependency gate the restart on `live` runs by path.
    "scripts/lib/live_checkout.py",
    "scripts/lib/venv_matches_pyproject.py",
    "src/genesis/dashboard/routes/updates.py",
    # PR C's file: readiness reads it from the server's base, and it will hold
    # bootstrap's crash-recovery branch guard.
    PR_C_MARKER,
)
# The engine itself: deploy_candidates, deploy_candidates.py and its siblings.
ENGINE_PREFIX = "scripts/deploy_candidates"


# ── readiness ─────────────────────────────────────────────────────────────
def sync_hook_names(text: str) -> list[str]:
    """Every name in sync-hooks.sh's HOOKS_TO_SYNC and HELPERS_TO_SYNC arrays
    (one quoted name per line). Strict: an append (`+=`), a second assignment,
    an unquoted or one-line array refuses, since a list read short would pass
    over hooks nobody checked."""
    names: list[str] = []
    for array in _SYNC_ARRAYS:
        if re.search(rf"^\s*{array}\+=", text, re.MULTILINE):
            raise Refusal(f"{SYNC_HOOKS} appends to {array}; the readiness check cannot read it")
        blocks = re.findall(rf"^{array}=\(\n(.*?)^\)", text, re.MULTILINE | re.DOTALL)
        assigns = re.findall(rf"^\s*{array}=", text, re.MULTILINE)
        if len(blocks) != 1 or len(assigns) != 1:
            raise Refusal(f"cannot find exactly one {array} list in {SYNC_HOOKS}")
        found = []
        for line in blocks[0].splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.fullmatch(r'"([A-Za-z0-9._-]+)"', line)
            if not m:
                raise Refusal(f"cannot read the {array} line {line!r} in {SYNC_HOOKS}")
            found.append(m.group(1))
        if not found:
            raise Refusal(f"the {array} list in {SYNC_HOOKS} is empty")
        names.extend(found)
    return names


# The merges read their rules with git's GLOBAL option, `git --attr-source=<tree>
# merge-tree` (merge-tree has no such option of its own: given one, git 2.43
# exits 129, MEASURED). Per git's release notes the global option arrived in
# 2.41, and 2.43.0 fixed "git merge-tree used to segfault when the
# --attr-source option is used".
MIN_GIT = (2, 43)


def git_version_failure(repo: Repo) -> str | None:
    """Why this git is too old for the engine, or None."""
    text = repo.git("--version", check=False).stdout.strip()
    m = re.match(r"git version (\d+)\.(\d+)", text)
    if not m:
        return f"cannot read the git version ({text!r})"
    have = (int(m.group(1)), int(m.group(2)))
    if have < MIN_GIT:
        return (
            f"git {have[0]}.{have[1]} is too old: the engine needs git "
            f"{MIN_GIT[0]}.{MIN_GIT[1]} or newer (git --attr-source with merge-tree)"
        )
    return None


def readiness_failures(repo: Repo, base: str) -> list[str]:
    """Why this install cannot run candidates now ([] when it can).

    The server's base is merge-base(serving commit, origin/main): what the
    server runs that is reviewed main. The hooks are compared with the commit
    the checkout holds (HEAD), whose scripts/hooks/ on `live` is reviewed main's
    or an approved candidate's (admission and hook attribution), and against the
    directory git runs them from."""
    fails: list[str] = []
    too_old = git_version_failure(repo)
    if too_old:
        fails.append(too_old)
    serving, why = repo.serving()
    if not serving:
        fails.append(f"the serving commit is unknown ({why}); a candidate needs a running server")
    else:
        mb = repo.merge_base(serving, base)
        if not mb:
            fails.append(f"the serving commit {serving[:12]} shares no history with origin/main")
        else:
            for label, sha in REQUIRED_MERGED:
                have = repo.resolve(sha) if HEX40.match(sha) else None
                if not have:
                    fails.append(f"{label}: {sha[:12]} is not in this repository (fetch origin)")
                elif not repo.is_ancestor(have, mb):
                    fails.append(
                        f"{label}: {have[:12]} is not in the server's base {mb[:12]} (deploy it first)"
                    )
            if repo.blob_at(mb, PR_C_MARKER) is None:
                fails.append(
                    f"PR C (the wipers rebuild `live`) is not in the server's base {mb[:12]}: "
                    f"{PR_C_MARKER} does not exist there (it has not merged, or is not deployed)"
                )
    text = repo.show("HEAD", SYNC_HOOKS)
    if text is None:
        fails.append(f"cannot read {SYNC_HOOKS} at HEAD")
        return fails
    try:
        names = sync_hook_names(text)
    except Refusal as exc:
        fails.append(str(exc))
        return fails
    hooks_dir = repo.hooks_dir()
    installs_into = Path(repo.common_dir()) / "hooks"
    if os.path.realpath(hooks_dir) != os.path.realpath(installs_into):
        # sync-hooks.sh installs into $GIT_COMMON_DIR/hooks, so with
        # core.hooksPath set a rebuild would sync hooks git never runs, and
        # the guards git does run would stay as they are.
        fails.append(
            f"core.hooksPath points git at {hooks_dir}, but sync-hooks.sh installs into "
            f"{installs_into}: the hooks a rebuild syncs would not be the ones git runs. "
            "Unset core.hooksPath to use `live` (or, if it is set globally, set core.hooksPath "
            f"in this repository to {installs_into})."
        )
        return fails
    for name in names:
        want = repo.blob_at("HEAD", f"scripts/hooks/{name}")
        if want is None:
            continue  # sync-hooks.sh skips a name with no source
        kind = non_file_kind(repo, "HEAD", f"scripts/hooks/{name}")
        if kind:
            # Not the bytes sync installs (a link's target path, a tree or a
            # commit id), so no sync could ever make them compare equal.
            fails.append(
                f"scripts/hooks/{name} is a {kind} at HEAD; `live` supports only regular-file "
                "git hooks"
            )
            continue
        dst = hooks_dir / name
        if not dst.is_file():
            fails.append(
                f"the git hook {name} is not installed in {hooks_dir} (run scripts/hooks/sync-hooks.sh)"
            )
            continue
        # --no-filters: the bytes git will run, not what core.autocrlf would store.
        have = repo.git("hash-object", "--no-filters", "--", str(dst)).stdout.strip()
        if have != want:
            fails.append(
                f"the installed git hook {name} differs from scripts/hooks/{name} at HEAD "
                "(run scripts/hooks/sync-hooks.sh)"
            )
        elif not os.access(dst, os.X_OK):
            # git skips a hook that is not executable ("ignored because it's not
            # set as executable", MEASURED git 2.43), so the guard is off.
            fails.append(f"the installed git hook {dst} is not executable (chmod +x {dst})")
    return fails


# ── admission ─────────────────────────────────────────────────────────────
def path_refusal(path: str, hooks_approved: bool = False) -> str | None:
    """Why a changed path keeps a candidate off `live`, or None.
    ``hooks_approved`` skips ONLY the hook rule: every other rule still applies
    to a path under the hook directories (a .gitattributes there, say)."""
    if path.startswith(MIGRATION_DIRS):
        # Any change, not only an addition: a merged migration this install has
        # not applied yet runs at the next boot in whatever form `live` holds.
        return f"changes a migration ({path}); migrations go live only after they merge"
    if path in SCHEMA_FILES:
        return f"changes the boot-time schema ({path}); classified with migrations"
    if path.startswith(GUARDIAN_OWN_PREFIXES) or path in GUARDIAN_OWN_FILES:
        return f"changes the host guardian's own code ({path}), which reaches the host"
    if path == CC_PIN_FILE:
        return f"changes the Claude Code pin ({path}), which reaches the host"
    if path.startswith(HOST_DRIVER_PREFIXES):
        return f"changes what drives the host from this checkout ({path}), which reaches the host"
    if path.startswith(HOOK_DIRS) and not hooks_approved:
        return (
            f"changes a git or Claude Code hook ({path}); hooks go live only after they merge, "
            "or with the owner's approval (add --approve-hooks)"
        )
    if path in REFUSAL_FILES or path.startswith(ENGINE_PREFIX):
        return f"changes what keeps the wipers and this engine safe on `live` ({path})"
    if path == ".gitattributes" or path.endswith("/.gitattributes"):
        # A .gitattributes rule (eol/text/filter/ident) transforms how git writes
        # files on the working-tree switch to `live`, so a broad `* text eol=crlf`
        # could rewrite a protected hook's bytes (CRLF shebang -> unexecutable)
        # even though the hook path itself is refused. The off-tree merge uses
        # --attr-source=base and is unaffected; the checkout switch is not, so the
        # candidate is kept off `live` until it merges.
        return f"changes a .gitattributes ({path}); its rules transform files on checkout, reaching protected paths"
    return None


_NON_FILE_KINDS = {"120000": "symbolic link", "040000": "directory", "160000": "submodule"}


def non_file_kind(repo: Repo, commit: str, path: str) -> str | None:
    """What ``path`` is in ``commit`` when it is present but not a regular file
    (mode 100644 or 100755): "symbolic link", "directory", "submodule", or its
    mode. None for a regular file or an absent path. A hook must be a regular
    file: sync-hooks.sh copies what a link points at and skips anything else,
    and an object id that is not a blob is not the bytes that run."""
    text = repo.git("ls-tree", "-z", commit, "--", path).stdout
    head = text.split("\0", 1)[0]
    if not head:
        return None
    mode = head.split(" ", 1)[0]
    if mode in ("100644", "100755"):
        return None
    return _NON_FILE_KINDS.get(mode, f"mode {mode} entry")


def changed_paths(repo: Repo, base: str, head: str) -> list[str]:
    """Every path this head changes against its merge base with origin/main
    (three dots, so a branch behind main is not charged with main's own
    changes)."""
    text = repo.git("diff", "--no-renames", "--name-only", "-z", f"{base}...{head}").stdout
    return [p for p in text.split("\0") if p]


def admission_failures(repo: Repo, base: str, head: str, hooks_approved: bool = False) -> list[str]:
    """What keeps this head from going live: the diff against its merge base
    with origin/main, and any `Deploy-rebuild:` commit it carries.
    ``hooks_approved`` (the owner approved THIS head's hook changes) waives the
    hook rule only (see path_refusal)."""
    fails: list[str] = []
    changed = changed_paths(repo, base, head)
    for path in changed:
        why = path_refusal(path, hooks_approved)
        if why:
            fails.append(why)
        elif path.startswith(HOOK_DIRS) or any(d.startswith(path + "/") for d in HOOK_DIRS):
            # Only regular files under the hook directories, approved or not:
            # sync-hooks.sh copies what a link points at and skips a directory
            # or submodule, and Claude Code runs a hook through a link, so an
            # approval of such an entry cannot cover what runs. A hook
            # DIRECTORY made a link is the same.
            kind = non_file_kind(repo, head, path)
            if kind:
                fails.append(
                    f"makes {path} a {kind}; only regular files may go under the hook "
                    "directories, since an approval of anything else cannot cover what runs"
                )
    if SYNC_HOOKS in changed:
        # A changed list can name a source this diff never touched: each name it
        # installs must be a regular file too.
        try:
            names = sync_hook_names(repo.show(head, SYNC_HOOKS) or "")
        except Refusal:
            names = []  # an unreadable list is excluded by name at rebuild instead
        for name in names:
            # `.` would make ls-tree list the directory's contents, not itself.
            kind = (
                "directory"
                if name in (".", "..")
                else non_file_kind(repo, head, f"scripts/hooks/{name}")
            )
            if kind:
                fails.append(
                    f"lists scripts/hooks/{name} in {SYNC_HOOKS}, which is a {kind}; only "
                    "regular files may be installed as git hooks"
                )
    commits = repo.rev_list(head, "--not", base)
    info = repo.read_commits(commits)
    for c in commits:
        if any(TRAILER_LINE.match(ln) for ln in repo.body_lines(info[c][2])):
            fails.append(
                f"carries the Deploy-rebuild commit {c[:12]}: it was cut from `live`, which never "
                "bases work; recreate it from origin/main"
            )
            break
    return fails


# ── GitHub ────────────────────────────────────────────────────────────────
def pr_data(repo: Repo, pr: int) -> dict:
    data, why = repo.pr_state(pr)
    if data is None:
        raise Unknown(f"cannot read PR #{pr} ({why})")
    return data


def merge_commit(data: Mapping) -> str | None:
    mc = data.get("mergeCommit")
    oid = mc.get("oid") if isinstance(mc, dict) else None
    return oid if isinstance(oid, str) and HEX40.match(oid) else None


def retirement(repo: Repo, base: str, cand: dict) -> tuple[bool, str | None]:
    """(retire?, why a MERGED PR is NOT retired). Retire only on proof that the
    PR's change is in the FETCHED origin/main: state MERGED, merged into main,
    and its merge commit (gh nominates, git proves) an ancestor of base. The
    merged head need not equal verified_head: a PR that gained commits after
    the candidate was added and then merged retires, since main holds the reviewed final
    version. `rebuild` calls it only after a successful fetch; `status` calls
    it against the last-fetched main and labels its output so."""
    if cand["pr"] is None:
        return False, None
    data = pr_data(repo, cand["pr"])
    if data["state"] != "MERGED":
        return False, None
    if data.get("baseRefName") != BASE_BRANCH:
        return False, f"PR #{cand['pr']} merged into {data.get('baseRefName')!r}, not {BASE_BRANCH}"
    oid = merge_commit(data)
    if oid is None:
        raise Unknown(f"PR #{cand['pr']} is MERGED but gh gave no merge commit")
    if not repo.resolve(oid) or not repo.is_ancestor(oid, base):
        return (
            False,
            f"PR #{cand['pr']}'s merge commit {oid[:12]} is not in origin/main {base[:12]}",
        )
    return True, None


def pr_failure(repo: Repo, cand: dict) -> str | None:
    """A candidate with a PR goes live only while that PR is OPEN against main
    with exactly the added commit at its head."""
    pr = cand["pr"]
    if pr is None:
        return None
    data = pr_data(repo, pr)
    state = data["state"]
    if state == "CLOSED":
        return f"PR #{pr} was closed without merging"
    if state == "MERGED":
        # rebuild asks retirement() first; reaching here means it has merged
        # but is not proven to be in origin/main (or this is `add`).
        return f"PR #{pr} has merged; a merged PR does not go live as a candidate"
    if state != "OPEN":
        return f"PR #{pr} is {state}"
    if data.get("baseRefName") != BASE_BRANCH:
        return f"PR #{pr} targets {data.get('baseRefName')!r}, not {BASE_BRANCH}"
    if data.get("headRefName") != cand["branch"]:
        return f"PR #{pr}'s head branch is {data.get('headRefName')!r}, not {cand['branch']!r}"
    if data.get("headRefOid") != cand["verified_head"]:
        got = str(data.get("headRefOid") or "?")
        return f"PR #{pr}'s head is {got[:12]}, not the pinned {cand['verified_head'][:12]}"
    return None


# ── the per-candidate gate ────────────────────────────────────────────────
def gate_failure(repo: Repo, base: str, cand: dict) -> str | None:
    """Every per-candidate condition, re-derived now: why ``cand`` cannot go
    live (a known negative), None when it can. Raises Unknown."""
    branch, head = cand["branch"], cand["verified_head"]
    # rev-parse --verify -q exits 1 for a ref that does not exist; any other
    # failure is git unable to answer, which is UNKNOWN, never "branch gone".
    p = repo.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}", check=False)
    if p.returncode == 1:
        return "the branch does not exist here"
    tip = p.stdout.strip()
    if p.returncode != 0 or not HEX40.match(tip):
        raise Unknown(f"cannot resolve the branch {branch}: {p.stderr.strip() or 'git failed'}")
    if tip != head:
        return (
            f"the branch moved to {tip[:12]} since {head[:12]} was added: "
            f"add it again to run this commit (scripts/deploy_candidates add {branch} ...)"
        )
    fails = admission_failures(repo, base, head, hooks_approved(cand))
    if fails:
        return "admission: " + "; ".join(fails)
    return pr_failure(repo, cand)


def hooks_approved(cand: dict) -> bool:
    """The owner approved this candidate's hook changes, for exactly the head it
    is pinned at (the validator also requires that; checked again here)."""
    ha = cand.get("hook_approval")
    return isinstance(ha, dict) and ha.get("head") == cand["verified_head"]


def tree_entry(repo: Repo, commit: str, path: str) -> tuple[str, str] | None:
    """(mode, object id) of ``path`` in ``commit``, or None when absent. The mode
    is part of what a hook IS: git can merge one change's bytes with another's
    mode, and a hook that loses its executable bit stops running."""
    text = repo.git("ls-tree", "-z", commit, "--", path).stdout
    head = text.split("\0", 1)[0]
    if not head:
        return None
    meta = head.split("\t", 1)[0].split()
    return (meta[0], meta[2])


def hook_ownership_failures(
    repo: Repo, base: str, tip: str, merged: Mapping[str, str], approved: set[str]
) -> dict[str, str]:
    """Every hook path whose tree entry (mode and bytes) on the rebuilt ``tip``
    differs from ``base`` must have exactly ONE owner: a single merged candidate
    that changed it (against its own merge base), approved for that head, whose
    entry the tip holds unchanged (so origin/main has not changed it since the
    candidate was cut). Ownership replaces matching merged bytes against approved
    heads, which kept admitting entries no approval held (a blend of two
    approved changes, one's bytes with another's mode, a deletion "matched" by an
    approved head that never had the file). Returns the candidates to EXCLUDE by
    name, with why; empty when every changed hook path has its owner. The owner
    is found by the merge STEP that changed the path, not by the candidate's own
    diff (see below). Rebuild and drop (the repair path) both exclude rather
    than refuse; the one Refusal is for a tip build_plan did not build. ``merged`` maps
    branch -> merged head; ``approved`` names the branches approved at that head."""
    text = repo.git("diff", "--no-renames", "--name-only", "-z", base, tip, "--", *HOOK_DIRS).stdout
    paths = [p for p in text.split("\0") if p]
    out: dict[str, str] = {}
    if not paths:
        return out
    # Ownership by MERGE STEP: the rebuild chains one merge per candidate
    # (first parent = the tip so far, second = the candidate's head), so every
    # change between base and tip is made by some step. A candidate's own diff
    # is not enough: git's merge follows a rename origin/main made, so a
    # candidate that edited the old path changes the hook path without touching
    # it (MEASURED, git 2.43; merge-tree has no switch to turn that off).
    branch_of = {h: b for b, h in merged.items()}
    stepped: dict[str, set[str]] = {}
    walk = repo.git("rev-list", "--first-parent", "--parents", tip, "--not", base).stdout
    for line in walk.splitlines():
        ids = line.split()
        if len(ids) == 3 and ids[2] in branch_of:
            changed = repo.git(
                "diff", "--no-renames", "--name-only", "-z", ids[1], ids[0], "--", *HOOK_DIRS
            ).stdout
            stepped[branch_of[ids[2]]] = {q for q in changed.split("\0") if q}
    for path in paths:
        got = tree_entry(repo, tip, path)
        if got == tree_entry(repo, base, path):
            continue  # e.g. only the mode moved and back: nothing differs
        # The union: a step that changed the path (a followed rename included),
        # and a candidate whose own diff changed it even when its step did not
        # (its bytes equal what an earlier candidate merged): one hook, one owner.
        owners = [
            b
            for b, h in merged.items()
            if path in stepped.get(b, set())
            or tree_entry(repo, h, path) != tree_entry(repo, repo.merge_base(base, h) or base, path)
        ]
        if path == SYNC_HOOKS and owners:
            # The restore after a later move reads this list to know what this
            # checkout installed; one it cannot read is unknown there, and a hook
            # only this list named would stay installed after its candidate left.
            try:
                sync_hook_names(repo.show(tip, SYNC_HOOKS) or "")
            except Refusal as exc:
                for b in owners:
                    out[b] = (
                        f"leaves {SYNC_HOOKS} in a form the engine cannot read ({exc}); "
                        "keep each list as one quoted name per line"
                    )
                continue
        if len(owners) > 1:
            for b in owners:
                out[b] = (
                    f"{', '.join(owners)} each change {path}; only one candidate may change "
                    "a hook at a time: drop all but one"
                )
            continue
        if not owners:
            # Every change past base is made by some merge step, so this means
            # the tip was not built the way build_plan builds it.
            raise Refusal(f"{path} changed on the rebuilt `live`, but no merge step changed it")
        [owner] = owners
        head = merged[owner]
        if tree_entry(repo, head, path) == tree_entry(
            repo, repo.merge_base(base, head) or base, path
        ):
            out[owner] = (
                f"its merge changes {path} without its own diff touching it (git followed a "
                "rename origin/main made onto a hook path): merge origin/main into it"
            )
        elif owner not in approved:
            out[owner] = (
                f"changes {path} with no current hook approval; add it again with --approve-hooks"
            )
        elif got != tree_entry(repo, merged[owner], path):
            out[owner] = (
                f"origin/main changed {path} since it was cut, so the merge holds a hook nobody "
                "approved: merge origin/main into it, then add it again with --approve-hooks"
            )
    return out
