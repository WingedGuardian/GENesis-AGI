"""Self-update routes — check for updates, view status, trigger update."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import subprocess
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint

logger = logging.getLogger(__name__)

_HOME = Path.home()
_GENESIS_ROOT = _HOME / "genesis"
_UPDATE_SCRIPT = _GENESIS_ROOT / "scripts" / "update.sh"
_DB_PATH = _GENESIS_ROOT / "data" / "genesis.db"
_FAILURE_FILE = _HOME / ".genesis" / "last_update_failure.json"


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


def _query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query against genesis.db. Returns list of row dicts."""
    if not _DB_PATH.is_file():
        return []
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except (sqlite3.Error, OSError):
        return []


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

    # Last update attempt
    last_update = None
    hist_rows = _query_db(
        "SELECT old_tag, new_tag, old_commit, new_commit, status, "
        "failure_reason, started_at, completed_at "
        "FROM update_history ORDER BY started_at DESC LIMIT 1"
    )
    if hist_rows:
        last_update = hist_rows[0]

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
        "update_script_found": _UPDATE_SCRIPT.is_file(),
    })


@blueprint.route("/api/genesis/updates/check", methods=["POST"])
def update_check():
    """Force an upstream check (git fetch + tag comparison)."""
    # Fetch with tags so we can compare release versions
    if _git("fetch", "origin", "main", "--tags", timeout=30) is None:
        return jsonify({"error": "git fetch failed"}), 502

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

    # Different tags or no tags — count commits and build summary
    commits_behind = 0
    summary = None

    if local_tag and origin_tag:
        # Count between tags (meaningful range, ignores squash noise)
        behind_str = _git("rev-list", "--count", f"{local_tag}..{origin_tag}")
        commits_behind = int(behind_str) if behind_str and behind_str.isdigit() else 1
        summary = _git("log", "--oneline", "--no-merges", f"{local_tag}..{origin_tag}")
    else:
        # Fallback to commit-based when tags are missing
        behind_str = _git("rev-list", "--count", "HEAD..origin/main")
        commits_behind = int(behind_str) if behind_str and behind_str.isdigit() else 0
        if commits_behind > 0:
            summary = _git("log", "--oneline", "--no-merges", "HEAD..origin/main")

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

    # Check if an update is already in progress (simple PID file guard)
    if _PID_FILE.is_file():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            result = subprocess.run(["kill", "-0", str(old_pid)],
                                    capture_output=True, timeout=2)
            if result.returncode == 0:
                return jsonify({
                    "error": "Update already in progress",
                    "pid": old_pid,
                }), 409
            _PID_FILE.unlink(missing_ok=True)
        except (ValueError, subprocess.TimeoutExpired, OSError):
            _PID_FILE.unlink(missing_ok=True)

    use_cc = request.get_json(silent=True) or {}
    supervised = use_cc.get("supervised", True)

    if supervised:
        return _apply_supervised(_PID_FILE)
    return _apply_direct(_PID_FILE)


def _apply_direct(pid_file: Path) -> tuple:
    """Run update.sh directly (no CC supervision)."""
    try:
        proc = subprocess.Popen(
            ["bash", str(_UPDATE_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(proc.pid))
        logger.info("Direct update triggered (pid %d)", proc.pid)
        return jsonify({"status": "triggered", "pid": proc.pid, "supervised": False})
    except Exception as exc:
        logger.error("Failed to trigger update: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


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
   - Run: bash {update_script} 2>&1
     (update.sh will see the code is merged and run bootstrap + health)

If SCRIPT ERROR:
1. Diagnose the root cause from the error output
2. If fixable (missing dep, config mismatch, import error): fix and retry
3. If not fixable: write report to {summary_file}, write "tier3_needed"
   to {escalation_file}, done

Commit any fixes with conventional format (fix: ...).
Write final outcome to {summary_file}.
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
9. Run: bash {update_script} 2>&1

Write a resolution report to {summary_file} explaining each decision.
Use conventional commit format for any fixes (fix: ...).\
"""

# Files used for inter-tier communication
_GENESIS_DIR = _HOME / ".genesis"
_SUMMARY_FILE = _GENESIS_DIR / "last_update_summary.txt"
_ESCALATION_FILE = _GENESIS_DIR / "update_escalation.txt"
_CONFLICT_FILE = _GENESIS_DIR / "update_conflicts.json"
_STATE_FILE = _GENESIS_DIR / "update_state.json"
_PID_FILE = _GENESIS_DIR / "update_in_progress.pid"


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

    log_file = _GENESIS_DIR / "update_orchestrator.log"
    log_fh = open(log_file, "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [str(_GENESIS_ROOT / ".venv" / "bin" / "python"), "-c", orchestrator_code],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(_GENESIS_ROOT),
    )
    log_fh.close()  # Child inherited the fd; parent can close its copy
    # Write orchestrator PID so dismiss/concurrency checks see a live process
    # even between tiers. The orchestrator overwrites with CC PIDs while they run.
    pid_file.write_text(str(proc.pid))
    logger.info("Update orchestrator started (pid %d), log: %s", proc.pid, log_file)

    return jsonify({
        "status": "triggered",
        "supervised": True,
        "tier": 1,
        "orchestrator_pid": proc.pid,
    })


def _spawn_detached_cc(prompt: str, model: str) -> subprocess.Popen:
    """Spawn a detached CC session. Returns the Popen object (caller waits)."""
    return subprocess.Popen(
        ["claude", "-p", prompt, "--model", model, "--dangerously-skip-permissions"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(_GENESIS_ROOT),
    )


# Template for the orchestrator subprocess. Baked-in paths avoid genesis imports.
# Uses {}-style placeholders filled by _apply_supervised().
_ORCHESTRATOR_TEMPLATE = """\
import os, subprocess, logging
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

def spawn_cc(prompt, model):
    try:
        proc = subprocess.Popen(
            ["claude", "-p", prompt, "--model", model, "--dangerously-skip-permissions"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(GENESIS_ROOT),
        )
        PID_FILE.write_text(str(proc.pid))
        log.info("CC %s started (pid %d)", model, proc.pid)
        proc.wait()
        log.info("CC %s finished (rc=%d)", model, proc.returncode)
        # Restore orchestrator PID so dismiss/concurrency checks see us
        # alive between tiers (prevents race where stale CC PID allows dismiss)
        PID_FILE.write_text(str(MY_PID))
        return proc.returncode
    except Exception as e:
        log.error("Failed to spawn CC %s: %s", model, e)
        PID_FILE.write_text(str(MY_PID))
        return 1

# ── Tier 1: Haiku ──
log.info("Starting Tier 1 (Haiku)")
rc1 = spawn_cc({tier1_prompt!r}, "haiku")

# ── Check escalation ──
if ESCALATION.is_file() and "tier2_needed" in ESCALATION.read_text():
    log.info("Tier 1 escalated -> Tier 2 (Sonnet)")
    ESCALATION.unlink(missing_ok=True)
    rc2 = spawn_cc({tier2_prompt!r}, "sonnet")

    # Check if Tier 3 needed
    if ESCALATION.is_file() and "tier3_needed" in ESCALATION.read_text():
        log.info("Tier 2 escalated -> Tier 3 (user-initiated Opus)")
        SUMMARY.write_text(
            "conflicts_unresolved: Deep merge conflicts require Opus resolution. "
            "Use 'Resolve with Opus' from the dashboard."
        )

# Clean up PID file — orchestrator is done
PID_FILE.unlink(missing_ok=True)
log.info("Orchestrator finished")
"""


@blueprint.route("/api/genesis/updates/dismiss", methods=["POST"])
def update_dismiss():
    """Clear stale update state files so the dashboard returns to normal."""
    # Don't delete PID file if a process is still alive — prevents
    # removing the concurrency guard during a slow-but-active update.
    pid_alive = False
    if _PID_FILE.is_file():
        with contextlib.suppress(ValueError, subprocess.TimeoutExpired, OSError):
            pid = int(_PID_FILE.read_text().strip())
            result = subprocess.run(["kill", "-0", str(pid)],
                                    capture_output=True, timeout=2)
            pid_alive = result.returncode == 0

    if pid_alive:
        return jsonify({"error": "Update still in progress"}), 409

    for f in (_SUMMARY_FILE, _ESCALATION_FILE, _CONFLICT_FILE, _STATE_FILE, _PID_FILE):
        f.unlink(missing_ok=True)
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

    # Check if something is already running
    if _PID_FILE.is_file():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            result = subprocess.run(["kill", "-0", str(old_pid)],
                                    capture_output=True, timeout=2)
            if result.returncode == 0:
                return jsonify({
                    "error": "Update session already in progress",
                    "pid": old_pid,
                }), 409
            _PID_FILE.unlink(missing_ok=True)
        except (ValueError, subprocess.TimeoutExpired, OSError):
            _PID_FILE.unlink(missing_ok=True)

    tier3_prompt = _TIER3_PROMPT.format(
        escalation_file=_ESCALATION_FILE,
        summary_file=_SUMMARY_FILE,
        update_script=_UPDATE_SCRIPT,
    )

    proc = _spawn_detached_cc(tier3_prompt, "opus")
    _PID_FILE.write_text(str(proc.pid))
    logger.info("Tier 3 (Opus) started (pid %d)", proc.pid)

    # Reaper thread prevents zombie — waits for Opus to finish, cleans up PID file.
    # Server stays up during Tier 3 (merge-abort keeps it running), so a daemon
    # thread is safe here (unlike the Tier 1/2 orchestrator).
    import threading

    def _reap():
        proc.wait()
        _PID_FILE.unlink(missing_ok=True)
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

    # Check if an update process is still running
    in_progress = False
    if _PID_FILE.is_file():
        try:
            pid = int(_PID_FILE.read_text().strip())
            result = subprocess.run(["kill", "-0", str(pid)],
                                    capture_output=True, timeout=2)
            in_progress = result.returncode == 0
        except (ValueError, subprocess.TimeoutExpired, OSError):
            pass

    # Detect failed/stale states.
    # Guard against false positives during the brief window at update end:
    # PID may die moments before the state file is updated to "done".
    # Use a 60s grace period on the state file timestamp.
    failed = (
        not in_progress
        and summary is not None
        and not summary.startswith("success")
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

    return jsonify({
        "in_progress": in_progress,
        "summary": summary,
        "escalation": escalation,
        "conflicts": conflicts,
        "phase": phase,
        "failed": failed,
        "stale": stale,
    })
