"""Self-update routes — check for updates, view status, trigger update."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from flask import jsonify, request

from genesis.cc.child_env import pin_dispatched_env
from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.update_marker import (
    UpdateLockError,
    clear_dead_marker,
    holding_update_lock,
    release_marker,
)
from genesis.db.connection import connect_sqlite_rw
from genesis.env import genesis_home, update_in_progress
from genesis.observability.deploy_record import row_facts

logger = logging.getLogger(__name__)

_HOME = Path.home()
_GENESIS_ROOT = _HOME / "genesis"
_UPDATE_SCRIPT = _GENESIS_ROOT / "scripts" / "update.sh"
_DB_PATH = _GENESIS_ROOT / "data" / "genesis.db"
_FAILURE_FILE = _HOME / ".genesis" / "last_update_failure.json"
_LIVE_CHECKOUT_SCRIPT = _GENESIS_ROOT / "scripts" / "lib" / "live_checkout.py"
_MANAGED_UNITS_SCRIPT = _GENESIS_ROOT / "scripts" / "lib" / "managed_units.py"
_UNIT_DIR = _HOME / ".config" / "systemd" / "user"


def _managed_units_report() -> dict:
    """Which installed systemd units the next update.sh would refuse over.

    Stamp-only (no history scan, so it is cheap enough for a status poll): a unit
    whose genesis-managed stamp no longer matches was edited by hand, and update.sh
    refuses until the change moves into a drop-in or the template is taken. A unit
    from before stamps existed that matches no current template is "unchecked":
    the next update judges it against template history. Run afresh, like
    live_checkout.py, so the copy that answers is the one on disk now.
    """
    try:
        # Local reads only (git ls-tree / show, file hashes); 120 s bounds a
        # request thread against a hung git, as _live_refusal does.
        proc = subprocess.run(
            [sys.executable, "-I", "-S", str(_MANAGED_UNITS_SCRIPT), "check",
             "--repo", str(_GENESIS_ROOT), "--unit-dir", str(_UNIT_DIR),
             "--upto", "HEAD", "--json"],
            capture_output=True, text=True, timeout=120,
        )
        rows = json.loads(proc.stdout) if proc.returncode in (0, 4) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        rows = None
    if not isinstance(rows, list):
        return {"edited": [], "unchecked": [], "error": "could not check the installed systemd units"}
    return {
        "edited": [r["unit"] for r in rows if r.get("state") == "edited"],
        "unchecked": [r["unit"] for r in rows if r.get("state") == "unstamped"],
        "error": None,
    }


def _live_refusal() -> tuple[dict, int] | None:
    """The 409 to answer when the checkout is on `live`, or None to proceed.

    `live` is the integration branch scripts/deploy_candidates rebuilds from the
    deploy manifest. update.sh and the Tier 2/3 sessions it hands off to work on
    `main` (a Tier session checks main out and merges), which would move the
    checkout off `live`, so these routes refuse there and name the two commands
    that move and restart it. A verdict that cannot be read refuses too, and the
    printed word must agree with the exit code: python itself exits 1 on a
    SyntaxError or an uncaught exception, which must never read as "other". The
    script is run afresh, never imported, so the copy that answers is the one on
    disk now rather than the one this server booted with.
    """
    try:
        # Bounds a request thread. The script's own git reads are local and capped
        # at 30 s each (three at most); a hang past that reads as "cannot tell".
        proc = subprocess.run(
            [sys.executable, "-I", "-S", str(_LIVE_CHECKOUT_SCRIPT), str(_GENESIS_ROOT)],
            capture_output=True, text=True, timeout=120,
        )
        verdict = (proc.returncode, proc.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        verdict = (2, "unreadable")
    if verdict == (1, "other"):
        # "other" includes a branch named `live` that no manifest builds; the
        # deploy scripts refuse that by their branch rule (never `live`), so the
        # route, which has no such rule of its own, refuses it here. GIT_* from
        # the server's environment (GIT_DIR, ...) would answer for another
        # repository, so they are dropped, as live_checkout.py does.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        try:
            p = subprocess.run(
                ["git", "-C", str(_GENESIS_ROOT), "symbolic-ref", "-q", "HEAD"],
                capture_output=True, text=True, timeout=10, env=env,
            )
            # 1 with no output is a detached HEAD; any other failure cannot tell.
            ref = p.stdout.strip() if p.returncode in (0, 1) else "refs/heads/live"
        except (OSError, subprocess.SubprocessError):
            ref = "refs/heads/live"  # cannot tell: refuse
        if ref != "refs/heads/live":
            return None
        return {
            "error": (
                "The checkout is on a branch named live that no deploy manifest builds; "
                "an update runs only on main. Nothing was started."
            )
        }, 409
    if verdict == (0, "live"):
        return {
            "error": (
                "This install runs the live integration branch, and an update would "
                "move the checkout off it. Run scripts/deploy_candidates rebuild, then "
                "scripts/deploy_code_only.sh restart."
            )
        }, 409
    return {
        "error": (
            "Cannot tell whether this install runs the live integration branch (the "
            "deploy manifest or git could not be read). Nothing was started."
        )
    }, 409


def _git(*args: str, timeout: int = 10) -> str | None:
    """Run a git command in the Genesis repo. Returns stdout or None on error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_GENESIS_ROOT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _git_result(*args: str, timeout: int = 10) -> tuple[str | None, str]:
    """Like _git but also returns stderr for error diagnostics."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_GENESIS_ROOT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        stdout = result.stdout.strip() if result.returncode == 0 else None
        return stdout, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return None, "git command timed out"
    except OSError as exc:
        return None, str(exc)



def _query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query against genesis.db. Returns list of row dicts.

    Read-only by intent but NOT by mode: this opens read-write (no
    ``mode=ro``), so despite the name it can checkpoint a stale ``-wal`` into
    the main file on close. The write helper directly below already refuses a
    quarantined database via ``connect_sqlite_rw``; this one consults the same
    admission check so the route does not leave a read-write handle open on a
    database the write path is refusing.
    """
    if not _DB_PATH.is_file():
        return []
    from genesis.db.admission import database_is_fenced

    if database_is_fenced(_DB_PATH):
        return []
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except (sqlite3.Error, OSError):
        return []


def _execute_db(sql: str, params: tuple = ()) -> bool:
    """Run a write query against genesis.db. Returns True on success."""
    if not _DB_PATH.is_file():
        return False
    conn = None
    try:
        conn = connect_sqlite_rw(_DB_PATH, timeout=5)
        conn.execute(sql, params)
        conn.commit()
        return True
    except (sqlite3.Error, OSError):
        return False
    finally:
        if conn:
            conn.close()


@blueprint.route("/api/genesis/updates/status")
def update_status():
    """Current version, update availability, and last update result."""
    # Current version — only match release tags (v*), not backup/pre-update tags
    tag = _git("describe", "--tags", "--match", "v*", "--always") or "unknown"
    commit = _git("rev-parse", "--short", "HEAD") or "unknown"

    # Check for unresolved update_available observations
    update_available = None
    obs_rows = _query_db(
        "SELECT content, created_at FROM observations "
        "WHERE type = 'genesis_update_available' AND resolved = 0 "
        "ORDER BY created_at DESC LIMIT 1"
    )
    if obs_rows:
        try:
            content = json.loads(obs_rows[0]["content"])
            update_available = {
                "commits_behind": content.get("commits_behind"),
                "target_tag": content.get("target_tag"),
                "target_commit": content.get("target_commit"),
                "summary": content.get("summary"),
                "detected_at": obs_rows[0]["created_at"],
            }
        except (json.JSONDecodeError, KeyError):
            pass

    # Reconcile: if target_commit is already in HEAD, the update has been
    # applied but GenesisVersionCollector hasn't run yet — auto-resolve.
    if (
        update_available
        and update_available.get("target_commit")
        and _git("merge-base", "--is-ancestor", update_available["target_commit"], "HEAD") is not None
    ):
        _execute_db(
            "UPDATE observations SET resolved = 1, resolved_at = ?, "
            "resolution_notes = 'auto-resolved: target_commit is ancestor of HEAD' "
            "WHERE type = 'genesis_update_available' AND resolved = 0",
            (datetime.now(UTC).isoformat(),),
        )
        update_available = None

    # Last update attempt
    last_update = None
    hist_rows = _query_db(
        "SELECT old_tag, new_tag, old_commit, new_commit, status, "
        "failure_reason, degraded_subsystems, started_at, completed_at "
        "FROM update_history ORDER BY datetime(started_at) DESC LIMIT 1"
    )
    if hist_rows:
        last_update = hist_rows[0]
        # Facts derive from the STORED row, before the failed/rolled_back→
        # success reconciliation below — a reconciled row keeps
        # server_restarted=None rather than making a claim it never earned.
        last_update.update(
            row_facts(
                last_update.get("status"), last_update.get("degraded_subsystems"),
            ).as_dict()
        )

    # Reconcile: if update_history says rolled_back/failed but the target
    # commit actually landed in HEAD, the update succeeded despite the
    # script's rollback (e.g. a CC tier completed it manually).
    if last_update and last_update.get("status") in ("rolled_back", "failed"):
        # Try commit first, fall back to tag (commits may not exist after
        # history rewrites like filter-repo).
        landed = False
        new_commit = last_update.get("new_commit")
        if new_commit and _git("merge-base", "--is-ancestor", new_commit, "HEAD") is not None:
            landed = True
        elif last_update.get("new_tag"):
            landed = _git("merge-base", "--is-ancestor", last_update["new_tag"], "HEAD") is not None
        if landed:
            last_update = dict(last_update)
            last_update["status"] = "success"
            last_update["failure_reason"] = None
            last_update["reconciled"] = True


    # Failure context file (persists until next successful update)
    last_failure = None
    if _FAILURE_FILE.is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            last_failure = json.loads(_FAILURE_FILE.read_text())

    return jsonify({
        "current_version": tag,
        "current_commit": commit,
        "update_available": update_available,
        "last_update": last_update,
        "last_failure": last_failure,
        "managed_units": _managed_units_report(),
        "update_script_found": _UPDATE_SCRIPT.is_file(),
    })


@blueprint.route("/api/genesis/updates/check", methods=["POST"])
def update_check():
    """Force an upstream check (git fetch + tag comparison)."""
    # Fetch branch first (must succeed), then force-update tags separately
    # (tag conflicts from history rewrites should not block the check).
    branch_out, branch_err = _git_result("fetch", "origin", "main", timeout=30)
    if branch_out is None:
        logger.error("git fetch failed: %s", branch_err)
        return jsonify({"error": "git fetch failed"}), 502

    # Best-effort tag sync — force to handle rewritten history
    if _git("fetch", "origin", "--tags", "--force", timeout=15) is None:
        logger.warning("Tag fetch failed (non-fatal): tags may be stale")

    # Compare release tags (robust against squash-merge divergence)
    local_tag = _git("describe", "--tags", "--match", "v*", "--abbrev=0", "HEAD")
    origin_tag = _git(
        "describe", "--tags", "--match", "v*", "--abbrev=0", "origin/main"
    )

    if local_tag and origin_tag and local_tag == origin_tag:
        # Same release tag — up to date
        return jsonify({
            "commits_behind": 0,
            "local_tag": local_tag,
            "target_tag": None,
            "summary": None,
        })

    # Different tags or no tags — count from the DEPLOYED COMMIT, never between
    # the release tags. The second copy of the same defect: this endpoint feeds a
    # dashboard card reading "N commit(s) behind", and counting tag-to-tag made
    # that number describe the release span rather than the reader. See the note
    # in learning/signals/genesis_version.py for the measurement; the correct
    # shape is observability/snapshots/deploy_health.py's
    # `commits_behind_upstream`.
    behind_str = _git("rev-list", "--count", "HEAD..origin/main")
    if behind_str is None or not behind_str.isdigit():
        # A failed measurement is not a distance, and every number we could
        # invent here is a lie in one direction or the other: 0 renders as "up
        # to date" and CLEARS a known update, while 1 fabricates a distance
        # nobody counted. Both wear the grammar of a measurement — the exact
        # defect this endpoint was changed to stop telling. Report the failure
        # through the same 502 the fetch failure above already uses, which the
        # dashboard surfaces as "Check failed" and which cannot overwrite a
        # previously detected update.
        logger.error("git rev-list HEAD..origin/main failed; distance unknown")
        return jsonify({"error": "could not measure distance from origin/main"}), 502
    commits_behind = int(behind_str)
    summary = (
        _git("log", "--oneline", "--no-merges", "HEAD..origin/main")
        if commits_behind > 0
        else None
    )

    return jsonify({
        "commits_behind": commits_behind,
        "local_tag": local_tag or "untagged",
        "target_tag": origin_tag if local_tag != origin_tag else None,
        "summary": summary,
    })


@blueprint.route("/api/genesis/updates/apply", methods=["POST"])
def update_apply():
    """Trigger a CC-supervised update."""
    if not _UPDATE_SCRIPT.is_file():
        return jsonify({"error": "Update script not found"}), 404
    use_cc = request.get_json(silent=True) or {}
    supervised = use_cc.get("supervised", True)

    # Everything from the in-progress check to the marker write runs under
    # update.lock (#2525), so a deploy or a `deploy_candidates` move cannot start
    # in between: neither can take the marker, nor move the checkout to `live`
    # after the live check below said it is not there.
    with holding_update_lock(_PID_FILE) as held:
        if not held:
            return jsonify({"error": _BUSY}), 409
        # The canonical liveness check (env.update_in_progress) also catches a
        # CLI update.sh and restore.sh, which signal via the state file / marker.
        if update_in_progress():
            return jsonify({"error": "Update already in progress"}), 409
        clear_dead_marker(_PID_FILE)
        refusal = _live_refusal()
        if refusal is not None:
            return jsonify(refusal[0]), refusal[1]
        if supervised:
            # Writes the orchestrator's pid as the marker before the lock goes.
            return _apply_supervised(_PID_FILE)
    # The direct path runs update.sh, which takes update.lock itself (`flock -n`)
    # and refuses while anyone holds it, so it starts only after the lock is
    # released. update.sh then refuses `live` on its own branch rule.
    return _apply_direct(_PID_FILE)


def _systemd_run_scope_available(env: dict) -> bool:
    """True only if `systemd-run --user --scope` can actually start a scope here.

    systemd-run can be present but unusable when the user manager / D-Bus is
    unreachable: `Popen` would succeed yet the child dies with "Failed to connect
    to bus", silently no-op'ing the update. Probe with a no-op scope so we can
    fall back deterministically instead of trusting Popen not to raise.
    """
    try:
        r = subprocess.run(
            ["systemd-run", "--user", "--scope", "--", "true"],
            capture_output=True, timeout=10, env=env,
        )
        return r.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def _apply_direct(pid_file: Path) -> tuple:
    """Run update.sh directly (no CC supervision), in its OWN systemd scope.

    Without a scope, update.sh stays in genesis-server.service's cgroup; its own
    `systemctl stop genesis-server` then SIGTERMs update.sh via the default
    KillMode=control-group -- aborting the update, or (at the final restart)
    self-SIGTERMing into a spurious rollback of a healthy deploy. `systemd-run
    --user --scope` gives it its own cgroup (mirrors the supervised orchestrator
    spawn); `start_new_session` alone changes only the session, not the cgroup.
    """
    try:
        log_path = _HOME / ".genesis" / "update_direct.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "w")  # noqa: SIM115
        env = os.environ.copy()
        uid = os.getuid()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
        if _systemd_run_scope_available(env):
            proc = subprocess.Popen(
                ["systemd-run", "--user", "--scope", "--", "bash", str(_UPDATE_SCRIPT)],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
            )
            logger.info("Direct update spawned via systemd-run --scope (pid %d)", proc.pid)
        else:
            # systemd-run absent OR the user manager/D-Bus is unreachable (a scope
            # can't start -- Popen would succeed but the child dies "Failed to
            # connect to bus"). Fall back to a new session in the shared cgroup;
            # update.sh's P4b prestop/rollback traps keep the server from being
            # left down if it self-SIGTERMs at the server stop.
            logger.warning("systemd-run --scope unavailable; falling back to start_new_session")
            proc = subprocess.Popen(
                ["bash", str(_UPDATE_SCRIPT)],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        log_fh.close()  # child inherited the fd; parent can close its copy
        # Deliberately DON'T write the PID marker here: on the systemd-run path
        # proc.pid is systemd-run's (a scope wrapper), which update.sh can't own,
        # and racing to write it after the child starts leaks a stale marker.
        # The direct run signals "in progress" via update.sh's state file (exactly
        # like a CLI run — env.update_in_progress() honors both), and the flock in
        # update.sh is the hard mutual-exclusion guarantee. `pid_file` is unused.
        logger.info("Direct update triggered (pid %d)", proc.pid)
        return jsonify({"status": "triggered", "pid": proc.pid, "supervised": False})
    except Exception as exc:
        logger.error("Failed to trigger update: %s", exc, exc_info=True)
        return jsonify({"error": "Failed to trigger update"}), 500


# ── Three-tier CC update prompts ─────────────────────────────────────

_TIER1_PROMPT = """\
You are supervising a Genesis update. Your role is WATCH AND ESCALATE.

1. Run: bash {update_script} 2>&1
   Capture the FULL output and the exit code.

2. If exit 0 (success):
   - Verify: curl -sf http://localhost:5000/api/genesis/health
   - Verify: python -c "from genesis.runtime import GenesisRuntime"
   - Check version: git -C {genesis_root} describe --tags --match 'v*' --always
   - Write result to {summary_file} (e.g., "success: v3.0a3 -> v3.0a4")
   - Done.

3. If exit 2 (merge conflicts):
   - Read {conflict_file} for the list of conflicted files
   - Write to {summary_file}: "conflicts: <file list>"
   - Write to {escalation_file}: "tier2_needed"
   - Done. Do NOT attempt to resolve conflicts yourself.

4. If exit 1 (script error):
   - Read the error output. If it's a simple retry (network timeout,
     temp file issue, pip cache), retry ONCE.
   - If retry succeeds, continue from step 2.
   - If retry fails or the error is not trivially retryable:
     Write to {escalation_file}: "tier2_needed"
     Write error context to {summary_file}
   - Done. Do NOT attempt code changes.

5. If exit 4 (refused):
   Exit 4 means update.sh REFUSED, before stopping anything, because systemd units
   were edited by hand. Never modify, delete, replace or re-stamp those unit files,
   and never pass --take-template or --take-templates: whether the hand edit goes is
   the user's decision. Write to {summary_file}: "refused: <the units it names>".
   Do not write {escalation_file}. Done.

You are a security guard, not an engineer. Press buttons, report status,
call for backup when needed.\
"""

_TIER2_PROMPT = """\
You are resolving merge conflicts from a Genesis update.

Read {summary_file} and {conflict_file} for context.

IMPORTANT: The main working tree is CLEAN — the merge was aborted so the
system stays operational. You must resolve conflicts in a temporary branch.

If MERGE CONFLICTS:
1. Create a temporary branch: git checkout -b update-merge-resolution
2. Redo the merge: git merge origin/main --no-edit
   (This will reproduce the same conflicts)
3. For each conflicted file, read BOTH sides of every conflict marker
4. Evaluate: are the changes compatible? (same intent, just different history)
5. If ALL conflicts in a file are trivially compatible — resolve them:
   - git checkout --theirs for upstream-only changes
   - git checkout --ours for user-only changes
   - Manual merge where both sides add different things
6. After resolving each file: git add <file>
7. If ANY conflict is ambiguous or involves genuinely different intents:
   - git merge --abort to clean up
   - git checkout main
   - git branch -D update-merge-resolution
   - Write to {escalation_file}: "tier3_needed"
   - Include: which files, what the incompatibility is, your assessment
   - Done.
8. If all conflicts resolved:
   - git commit --no-edit
   - git checkout main
   - git merge update-merge-resolution --ff-only
   - git branch -d update-merge-resolution
   - Run: bash {update_script} --post-merge 2>&1
     (--post-merge skips fetch/merge, runs only bootstrap + health)

If update.sh EXITS 4:
Exit 4 means update.sh REFUSED, before stopping anything, because systemd units
were edited by hand. Never modify, delete, replace or re-stamp those unit files,
and never pass --take-template or --take-templates: whether the hand edit goes is
the user's decision. Write to {summary_file}: "refused: <the units it names>".
Do not write {escalation_file}. Done.

If SCRIPT ERROR:
1. Diagnose the root cause from the error output
2. If fixable (missing dep, config mismatch, import error): fix and retry
3. If not fixable: write report to {summary_file}, write "tier3_needed"
   to {escalation_file}, done

Commit any fixes with conventional format (fix: ...).
Write final outcome to {summary_file}, unless update.sh exited 4: then the summary
stays exactly "refused: <the units it names>".
Log every action taken for user review.\
"""

_TIER3_PROMPT = """\
You are resolving deep merge conflicts from a Genesis update.

Read {escalation_file} for Sonnet's analysis of what couldn't be resolved.

IMPORTANT: The main working tree is CLEAN. Work on a temporary branch.

1. git checkout -b update-merge-resolution-opus
2. git merge origin/main --no-edit (reproduces conflicts)
3. For each conflict, understand the intent of BOTH sides:
   - LOCAL (ours): user customizations, additions, local config
   - REMOTE (theirs): upstream bug fixes, features, improvements
4. Find the resolution that preserves both intents
5. Where intents genuinely conflict:
   - Bug fixes and security patches: upstream wins
   - User identity, config, customizations: user wins
   - Feature additions: merge both, adapting as needed
6. After resolving: git add, git commit --no-edit
7. git checkout main && git merge update-merge-resolution-opus --ff-only
8. git branch -d update-merge-resolution-opus
9. Run: bash {update_script} --post-merge 2>&1

If update.sh EXITS 4:
Exit 4 means update.sh REFUSED, before stopping anything, because systemd units
were edited by hand. Never modify, delete, replace or re-stamp those unit files,
and never pass --take-template or --take-templates: whether the hand edit goes is
the user's decision. Write to {summary_file}: "refused: <the units it names>".
Do not write {escalation_file}. Done.

Write a resolution report to {summary_file} explaining each decision, unless
update.sh exited 4: then the summary stays exactly "refused: <the units it names>".
Use conventional commit format for any fixes (fix: ...).\
"""

# Files used for inter-tier communication
_GENESIS_DIR = _HOME / ".genesis"
_SUMMARY_FILE = _GENESIS_DIR / "last_update_summary.txt"
_ESCALATION_FILE = _GENESIS_DIR / "update_escalation.txt"
_CONFLICT_FILE = _GENESIS_DIR / "update_conflicts.json"
_STATE_FILE = _GENESIS_DIR / "update_state.json"
# The deploy marker, read by env.update_in_progress() and written by the shell
# deploys (scripts/lib/deploy_marker.sh). Same root as theirs and the reader's:
# genesis_home(), which honours GENESIS_HOME.
_PID_FILE = genesis_home() / "update_in_progress.pid"


# A route holds update.lock from its in-progress check to its marker write. That
# includes _live_refusal(), normally milliseconds of local git reads but capped at
# about 130 s if git hangs; for that long restore.sh and a CLI update.sh refuse and
# deploy_code_only.sh queues. Running the live check outside the lock would let a
# rebuild move the checkout to `live` after it answered, which is what #2525 closes.
_BUSY = (
    "A deploy, an update or a validation hold has update.lock; nothing was started. "
    "Try again when it finishes."
)


@blueprint.errorhandler(UpdateLockError)
def _update_lock_unopenable(exc: UpdateLockError):
    """update.lock could not be opened at all: say so, rather than "busy"."""
    logger.error("update.lock: %s", exc)
    return jsonify({"error": f"Cannot open update.lock ({exc}); nothing was started."}), 500


def _apply_supervised(pid_file: Path) -> tuple:
    """Spawn a three-tier CC update pipeline as a detached subprocess.

    The orchestrator MUST be a separate process, not a thread. update.sh
    stops the genesis-server during updates — a daemon thread inside Flask
    would die with it, orphaning the CC session with no one to check
    escalation files or spawn the next tier.

    The subprocess uses start_new_session=True so it survives the server
    shutdown. It runs self-contained Python (no genesis imports) because
    the genesis package may be mid-update.
    """
    _GENESIS_DIR.mkdir(parents=True, exist_ok=True)

    # Clean up ALL state files from prior runs
    for f in (_SUMMARY_FILE, _ESCALATION_FILE, _CONFLICT_FILE, _STATE_FILE):
        f.unlink(missing_ok=True)

    # Build the orchestrator as inline Python — no genesis imports needed.
    # The prompts and paths are baked in as string literals.
    tier1_prompt = _TIER1_PROMPT.format(
        update_script=_UPDATE_SCRIPT,
        genesis_root=_GENESIS_ROOT,
        summary_file=_SUMMARY_FILE,
        escalation_file=_ESCALATION_FILE,
        conflict_file=_CONFLICT_FILE,
    )
    tier2_prompt = _TIER2_PROMPT.format(
        summary_file=_SUMMARY_FILE,
        conflict_file=_CONFLICT_FILE,
        escalation_file=_ESCALATION_FILE,
        update_script=_UPDATE_SCRIPT,
    )

    orchestrator_code = _ORCHESTRATOR_TEMPLATE.format(
        summary_file=str(_SUMMARY_FILE),
        escalation_file=str(_ESCALATION_FILE),
        pid_file=str(pid_file),
        genesis_root=str(_GENESIS_ROOT),
        tier1_prompt=tier1_prompt,
        tier2_prompt=tier2_prompt,
    )

    # Write orchestrator to a temp file (avoids arg-length limits with -c).
    script_fd, script_path = tempfile.mkstemp(
        suffix=".py", prefix="genesis-update-orch-",
        dir=str(Path.home() / "tmp"),
    )
    with os.fdopen(script_fd, "w") as f:
        f.write(orchestrator_code)

    log_file = _GENESIS_DIR / "update_orchestrator.log"
    log_fh = open(log_file, "w")  # noqa: SIM115
    python = str(_GENESIS_ROOT / ".venv" / "bin" / "python")

    # The orchestrator MUST run outside genesis-server.service's cgroup.
    # start_new_session=True only changes the session, not the cgroup —
    # systemd's KillMode=control-group (the default) sends SIGTERM to every
    # process in the service's cgroup when the service is stopped. This kills
    # the orchestrator even though it has its own session.
    #
    # systemd-run --user --scope creates a transient scope unit with its own
    # cgroup. When genesis-server is stopped, only its cgroup is killed;
    # the orchestrator's scope is untouched.
    try:
        # Build env with D-Bus vars so systemd-run can talk to user session
        env = os.environ.copy()
        uid = os.getuid()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        env.setdefault(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus"
        )
        proc = subprocess.Popen(
            ["systemd-run", "--user", "--scope", "--", python, script_path],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(_GENESIS_ROOT),
        )
        logger.info("Orchestrator spawned via systemd-run --scope (pid %d)", proc.pid)
    except (FileNotFoundError, OSError) as exc:
        # Fallback: systemd-run not available (e.g., container restrictions).
        # Use start_new_session=True — orchestrator may not survive server stop.
        logger.warning(
            "systemd-run failed (%s), falling back to start_new_session", exc,
        )
        proc = subprocess.Popen(
            [python, script_path],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(_GENESIS_ROOT),
        )
    log_fh.close()  # Child inherited the fd; parent can close its copy
    # The orchestrator's pid is the deploy marker for the whole run, written while
    # the caller holds update.lock (#2525). MEASURED: `systemd-run --user --scope`
    # execs the command, so proc.pid is the orchestrator's on both paths.
    try:
        pid_file.write_text(str(proc.pid))
    except OSError:
        # An orchestrator no marker names would run unseen by the watchdog and by
        # every deploy; stop it rather than leave it running.
        proc.kill()
        raise
    logger.info("Update orchestrator started (pid %d), log: %s", proc.pid, log_file)

    return jsonify({
        "status": "triggered",
        "supervised": True,
        "tier": 1,
        "orchestrator_pid": proc.pid,
    })


def _spawn_detached_cc(
    prompt: str, model: str, effort: str | None = None,
) -> subprocess.Popen:
    """Spawn a detached CC session. Returns the Popen object (caller waits).

    ``effort`` is emitted as ``--effort`` when set; pass None for models that
    don't use an effort setting (Haiku).
    """
    log_path = _HOME / ".genesis" / f"cc_session_{model.replace('/', '_')}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "w")  # noqa: SIM115
    cmd = ["claude", "-p", prompt, "--model", model]
    if effort:
        cmd += ["--effort", effort]
    cmd.append("--dangerously-skip-permissions")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(_GENESIS_ROOT),
        env=_update_tier_env(pin_dispatched_env(dict(os.environ))),
    )
    log_fh.close()  # child inherited the fd
    return proc


#: Stamped on every update-pipeline session (Tier 1/2 via the orchestrator's
#: spawn_cc, Tier 3 via _spawn_detached_cc). Those sessions run with cwd = the
#: main checkout and are told to resolve the update's merge there, which is the
#: one legitimate hand-edit of the deploy root; scripts/hooks/main_checkout_guard.py
#: exempts a session carrying exactly "1" (it refuses everyone else's file-tool
#: edits there and reports their Bash changes).
UPDATE_TIER_ENV = "GENESIS_UPDATE_TIER"


def _update_tier_env(env: dict[str, str]) -> dict[str, str]:
    env[UPDATE_TIER_ENV] = "1"
    return env


# Template for the orchestrator subprocess. Baked-in paths avoid genesis imports.
# Uses {}-style placeholders filled by _apply_supervised().
_ORCHESTRATOR_TEMPLATE = """\
import os, sys, subprocess, logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [update-orchestrator] %(levelname)s %(message)s",
)
log = logging.getLogger("update-orchestrator")

SUMMARY = Path({summary_file!r})
ESCALATION = Path({escalation_file!r})
PID_FILE = Path({pid_file!r})
GENESIS_ROOT = Path({genesis_root!r})
MY_PID = os.getpid()
SCRIPT_FILE = Path(sys.argv[0])

# The route that started us wrote OUR pid as the deploy marker while it held
# update.lock (systemd-run --scope execs us, so its pid is ours), and we leave it
# alone until we exit: the marker names a live process for the whole run, never a
# finished child's pid that a deploy could take over and we would overwrite.

def cleanup():
    try:
        if PID_FILE.read_text().strip() == str(MY_PID):
            PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    if SCRIPT_FILE and SCRIPT_FILE.name.startswith("genesis-update-orch-"):
        try:
            SCRIPT_FILE.unlink(missing_ok=True)
        except Exception:
            pass

def spawn_cc(prompt, model, effort=None):
    try:
        cmd = ["claude", "-p", prompt, "--model", model]
        # Haiku doesn't use an effort setting — callers pass effort=None for it.
        if effort:
            cmd += ["--effort", effort]
        cmd.append("--dangerously-skip-permissions")
        # Mirrors genesis.cc.child_env.pin_dispatched_env (this script avoids
        # genesis imports): function hooks stay off in dispatched sessions.
        # GENESIS_UPDATE_TIER mirrors _update_tier_env: these sessions resolve the
        # update's merge in the main checkout, which main_checkout_guard allows
        # only for a session carrying it.
        env = {{
            **os.environ,
            "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "0",
            "GENESIS_UPDATE_TIER": "1",
        }}
        proc = subprocess.Popen(
            cmd, start_new_session=True, cwd=str(GENESIS_ROOT), env=env,
        )
        log.info("CC %s started (pid %d)", model, proc.pid)
        proc.wait()
        log.info("CC %s finished (rc=%d)", model, proc.returncode)
        return proc.returncode
    except Exception as e:
        log.error("Failed to spawn CC %s: %s", model, e)
        return 1

# ── Tier 1: Haiku ──
log.info("Starting Tier 1 (Haiku)")
rc1 = spawn_cc({tier1_prompt!r}, "haiku", None)

# ── CC failure detection ──
# If CC exited non-zero but wrote no escalation file, the CC session itself
# failed (rate limit, API outage, crash) — not the update logic.
if rc1 != 0 and not ESCALATION.is_file():
    log.error("Tier 1 CC failed (rc=%d) with no escalation — CC infrastructure issue", rc1)
    SUMMARY.write_text(
        f"tier1_cc_error: CC session exited with code {{rc1}} before completing. "
        "Possible causes: rate limit, API outage, or session crash. "
        "Dismiss and retry when the issue clears."
    )
    cleanup()
    sys.exit(1)

# A refusal over hand-edited systemd units is the user's decision, not a script
# error: no tier above may act on it, whatever a session wrote.
# Decided by the checker's exit code as well as by what a session wrote, so a
# session that overwrote the summary with prose cannot escalate past a refusal.
MANAGED_UNITS = GENESIS_ROOT / "scripts" / "lib" / "managed_units.py"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

def refused():
    try:
        if SUMMARY.read_text().lstrip().startswith("refused:"):
            return True
    except OSError:
        pass
    try:
        rc = subprocess.run(
            [sys.executable, "-I", "-S", str(MANAGED_UNITS), "check",
             "--repo", str(GENESIS_ROOT), "--unit-dir", str(UNIT_DIR),
             "--upto", "HEAD", "--accept-legacy"],
            capture_output=True,
        ).returncode
    except OSError:
        return False
    return rc == 4

if refused():
    log.info("update.sh refused over hand-edited units; not escalating")
    ESCALATION.unlink(missing_ok=True)

# ── Check escalation ──
if ESCALATION.is_file() and "tier2_needed" in ESCALATION.read_text():
    log.info("Tier 1 escalated -> Tier 2 (Sonnet)")
    ESCALATION.unlink(missing_ok=True)
    rc2 = spawn_cc({tier2_prompt!r}, "sonnet", "high")

    if rc2 != 0 and not ESCALATION.is_file():
        log.error("Tier 2 CC failed (rc=%d) with no escalation", rc2)
        SUMMARY.write_text(
            f"tier2_cc_error: Sonnet CC exited with code {{rc2}}. "
            "Conflicts may still need resolution. Dismiss and retry."
        )
        cleanup()
        sys.exit(1)

    # Check if Tier 3 needed
    if refused():
        log.info("update.sh refused over hand-edited units; not escalating")
        ESCALATION.unlink(missing_ok=True)
    if ESCALATION.is_file() and "tier3_needed" in ESCALATION.read_text():
        log.info("Tier 2 escalated -> Tier 3 (user-initiated Opus)")
        SUMMARY.write_text(
            "conflicts_unresolved: Deep merge conflicts require Opus resolution. "
            "Use 'Resolve with Opus' from the dashboard."
        )

cleanup()
log.info("Orchestrator finished")
"""


@blueprint.route("/api/genesis/updates/dismiss", methods=["POST"])
def update_dismiss():
    """Clear stale update state files so the dashboard returns to normal."""
    # Refuse while any deploy is live (marker OR CLI state file) -- don't clear a
    # slow-but-active update's state out from under it. Canonical liveness check
    # so a CLI `update.sh` run counts too, not just the dashboard's PID file.
    # Under update.lock (#2525): update.sh holds it for its whole run, so its
    # state file cannot be deleted between the check and the unlink.
    with holding_update_lock(_PID_FILE) as held:
        if not held or update_in_progress():
            return jsonify({"error": "Update still in progress"}), 409
        for f in (_SUMMARY_FILE, _ESCALATION_FILE, _CONFLICT_FILE, _STATE_FILE):
            f.unlink(missing_ok=True)
        clear_dead_marker(_PID_FILE)
    logger.info("Update state files dismissed by user")
    return jsonify({"status": "dismissed"})


@blueprint.route("/api/genesis/updates/resolve", methods=["POST"])
def update_resolve():
    """User-initiated Tier 3 (Opus) conflict resolution."""
    # Accept resolve when tier3 was explicitly requested OR when tier2 died
    # leaving conflicts behind (failed state with conflict files present)
    has_escalation = _ESCALATION_FILE.is_file()
    has_conflicts = _CONFLICT_FILE.is_file()
    if not has_escalation and not has_conflicts:
        return jsonify({"error": "No conflicts pending"}), 404

    tier3_prompt = _TIER3_PROMPT.format(
        escalation_file=_ESCALATION_FILE,
        summary_file=_SUMMARY_FILE,
        update_script=_UPDATE_SCRIPT,
    )

    # From the in-progress check to the marker write under update.lock (#2525),
    # as in /apply: nothing can take the marker or move the checkout to `live`
    # between the live check and the Tier 3 session's start.
    with holding_update_lock(_PID_FILE) as held:
        if not held:
            return jsonify({"error": _BUSY}), 409
        # Canonical liveness (marker + state file), so a CLI run counts too.
        if update_in_progress():
            return jsonify({"error": "Update session already in progress"}), 409
        clear_dead_marker(_PID_FILE)
        refusal = _live_refusal()
        if refusal is not None:
            return jsonify(refusal[0]), refusal[1]
        proc = _spawn_detached_cc(tier3_prompt, "opus", "max")
        try:
            _PID_FILE.write_text(str(proc.pid))
        except OSError:
            # As in _apply_supervised: a session no marker names would change the
            # checkout unseen by the watchdog and every deploy; stop it.
            proc.kill()
            raise
    logger.info("Tier 3 (Opus) started (pid %d)", proc.pid)

    # Reaper thread prevents zombie — waits for Opus to finish, cleans up PID file.
    # Server stays up during Tier 3 (merge-abort keeps it running), so a daemon
    # thread is safe here (unlike the Tier 1/2 orchestrator).
    import threading

    def _reap():
        proc.wait()
        release_marker(_PID_FILE, proc.pid)
        logger.info("Tier 3 (Opus) finished (rc=%d)", proc.returncode)

    threading.Thread(target=_reap, daemon=True, name="tier3-reaper").start()

    return jsonify({
        "status": "triggered",
        "supervised": True,
        "tier": 3,
        "pid": proc.pid,
    })


@blueprint.route("/api/genesis/updates/progress")
def update_progress():
    """Poll update progress — reads summary, escalation, and state files.

    Returns phase-aware status so the frontend can show appropriate UX:
    - in_progress: update process is alive
    - failed: update finished but with errors/conflicts (not success)
    - stale: state file exists but process is dead and phase != done
    - phase: current update phase from update_state.json
    """
    summary = None
    if _SUMMARY_FILE.is_file():
        with contextlib.suppress(OSError):
            summary = _SUMMARY_FILE.read_text().strip()

    escalation = None
    if _ESCALATION_FILE.is_file():
        with contextlib.suppress(OSError):
            escalation = _ESCALATION_FILE.read_text().strip()

    conflicts = None
    if _CONFLICT_FILE.is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            conflicts = json.loads(_CONFLICT_FILE.read_text())

    # Read update phase from state file
    phase = None
    state_data = None
    if _STATE_FILE.is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            state_data = json.loads(_STATE_FILE.read_text())
            phase = state_data.get("phase")

    # Is an update process still running? Canonical liveness check (marker AND
    # state file) so a CLI `update.sh` run counts too -- which also keeps the
    # state-file GC below from unlinking a live CLI run's crash-recovery state.
    in_progress = update_in_progress()

    # Detect failed/stale states.
    # Guard against false positives during the brief window at update end:
    # PID may die moments before the state file is updated to "done".
    # Use a 60s grace period on the state file timestamp.
    failed = (
        not in_progress
        and summary is not None
        and not summary.lower().startswith("success")
    )

    stale = False
    if not in_progress and state_data is not None and phase not in (None, "done"):
        # Only declare stale if state file is older than 60s
        import time
        try:
            age = time.time() - _STATE_FILE.stat().st_mtime
            stale = age > 60
        except OSError:
            stale = True

        # Clean up stale state file — no process is running and the file
        # is just noise at this point (its purpose is crash recovery). Except
        # after a refusal over hand-edited systemd units: a refused
        # `update.sh --post-merge` keeps it on purpose, because the merge is in
        # and only the file stops deploy_code_only.sh from restarting onto code
        # that bootstrap never ran on.
        if stale and not (summary or "").lstrip().startswith("refused:"):
            with contextlib.suppress(OSError):
                _STATE_FILE.unlink()


    return jsonify({
        "in_progress": in_progress,
        "summary": summary,
        "escalation": escalation,
        "conflicts": conflicts,
        "phase": phase,
        "failed": failed,
        "stale": stale,
    })
