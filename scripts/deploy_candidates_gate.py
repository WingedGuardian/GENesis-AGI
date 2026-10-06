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
# repository never run unreviewed code. Only these two directories (owner ruling
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
    the checkout holds (HEAD), whose scripts/hooks/ admission keeps equal to
    reviewed main on `live`, and against the directory git runs them from."""
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
def path_refusal(path: str) -> str | None:
    """Why a changed path keeps a candidate off `live`, or None."""
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
    if path.startswith(HOOK_DIRS):
        return f"changes a git or Claude Code hook ({path}); hooks go live only after they merge"
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


def admission_failures(repo: Repo, base: str, head: str) -> list[str]:
    """What keeps this head from going live: the diff against its merge base
    with origin/main (three dots, so a branch behind main is not charged with
    main's own changes), and any `Deploy-rebuild:` commit it carries."""
    text = repo.git("diff", "--no-renames", "--name-status", "-z", f"{base}...{head}").stdout
    parts = text.split("\0")
    fails: list[str] = []
    i = 0
    while i + 1 < len(parts) and parts[i]:
        why = path_refusal(parts[i + 1])
        if why:
            fails.append(why)
        i += 2
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
    fails = admission_failures(repo, base, head)
    if fails:
        return "admission: " + "; ".join(fails)
    return pr_failure(repo, cand)
