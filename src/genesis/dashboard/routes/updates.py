"""Self-update routes — check for updates, view status, trigger update."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import subprocess
import threading
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint

logger = logging.getLogger(__name__)

# ── Orchestrator state ────────────────────────────────────────────────────────
# Tracks whether a supervised CC update pipeline is currently running in this
# process. Used by update_progress() to auto-recover Tier 2 escalation after
# a Flask restart (daemon thread dies but CC subprocess survives; on the next
# poll, progress endpoint detects tier2_needed + no live orchestrator → spawns).
_orchestrator_lock = threading.Lock()
_orchestrator_alive = False

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
    pid_file = _HOME / ".genesis" / "update_in_progress.pid"
    if pid_file.is_file():
        try:
            old_pid = int(pid_file.read_text().strip())
            # Check if process is still running (kill -0 returns 0 if alive)
            result = subprocess.run(["kill", "-0", str(old_pid)],
                                    capture_output=True, timeout=2)
            if result.returncode == 0:
                return jsonify({
                    "error": "Update already in progress",
                    "pid": old_pid,
                }), 409
            # Process is gone — stale PID file
            pid_file.unlink(missing_ok=True)
        except (ValueError, subprocess.TimeoutExpired, OSError):
            pid_file.unlink(missing_ok=True)

    use_cc = request.get_json(silent=True) or {}
    supervised = use_cc.get("supervised", True)

    if supervised:
        return _apply_supervised(pid_file)
    return _apply_direct(pid_file)


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
   - Run: bash {update_script} --post-merge 2>&1
     (skips re-merge, runs bootstrap + migrations + health on resolved code)

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
9. Run: bash {update_script} --post-merge 2>&1
   (skips re-merge, runs bootstrap + migrations + health on resolved code)

Write a resolution report to {summary_file} explaining each decision.
Use conventional commit format for any fixes (fix: ...).\
"""

# Files used for inter-tier communication
_GENESIS_DIR = _HOME / ".genesis"
_SUMMARY_FILE = _GENESIS_DIR / "last_update_summary.txt"
_ESCALATION_FILE = _GENESIS_DIR / "update_escalation.txt"
_CONFLICT_FILE = _GENESIS_DIR / "update_conflicts.json"


def _apply_supervised(pid_file: Path) -> tuple:
    """Spawn a three-tier CC update pipeline.

    Tier 1 (Haiku): runs update.sh, watches exit code, escalates if needed.
    Tier 2 (Sonnet): resolves trivial conflicts or script errors.
    Tier 3 (Opus): resolves deep merge conflicts requiring judgment.

    Orchestration runs in a background thread so the API returns immediately.
    Each tier checks for escalation files left by the previous tier.
    """
    global _orchestrator_alive

    _GENESIS_DIR.mkdir(parents=True, exist_ok=True)

    # Clean up stale escalation/summary files from prior runs
    for f in (_SUMMARY_FILE, _ESCALATION_FILE):
        f.unlink(missing_ok=True)

    def _run_tiers():
        global _orchestrator_alive
        _orchestrator_alive = True
        try:
            _run_tier1(pid_file)
        except Exception:
            logger.error("Update orchestrator failed", exc_info=True)
        finally:
            _orchestrator_alive = False
            pid_file.unlink(missing_ok=True)
            logger.info("Update orchestrator finished, PID file cleaned up")

    thread = threading.Thread(target=_run_tiers, daemon=True, name="update-orchestrator")
    thread.start()

    return jsonify({
        "status": "triggered",
        "supervised": True,
        "tier": 1,
    })


def _spawn_cc(prompt: str, model: str, pid_file: Path) -> int:
    """Spawn a CC session and wait for it to complete. Returns exit code."""
    try:
        proc = subprocess.Popen(
            [
                "claude", "-p", prompt,
                "--model", model,
                "--dangerously-skip-permissions",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(_GENESIS_ROOT),
        )
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(proc.pid))
        logger.info("CC %s session started (pid %d)", model, proc.pid)
        try:
            proc.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            logger.warning(
                "CC %s session (pid %d) timed out after 3600s — killing",
                model, proc.pid,
            )
            proc.kill()
            proc.wait(timeout=30)
            return 1
        return proc.returncode
    except Exception as exc:
        logger.error("Failed to spawn CC session: %s", exc, exc_info=True)
        return 1


def _run_tier1(pid_file: Path) -> None:
    """Tier 1: Haiku watches update.sh and escalates if needed."""
    prompt = _TIER1_PROMPT.format(
        update_script=_UPDATE_SCRIPT,
        genesis_root=_GENESIS_ROOT,
        summary_file=_SUMMARY_FILE,
        escalation_file=_ESCALATION_FILE,
        conflict_file=_CONFLICT_FILE,
    )

    rc = _spawn_cc(prompt, "haiku", pid_file)
    logger.info("Tier 1 (Haiku) completed with rc=%d", rc)

    # Check if escalation is needed
    if _ESCALATION_FILE.is_file():
        escalation = _ESCALATION_FILE.read_text().strip()
        if "tier2_needed" in escalation:
            logger.info("Tier 1 escalated to Tier 2 (Sonnet)")
            _ESCALATION_FILE.unlink(missing_ok=True)
            _run_tier2(pid_file)


def _run_tier2(pid_file: Path) -> None:
    """Tier 2: Sonnet resolves trivial conflicts or script errors."""
    prompt = _TIER2_PROMPT.format(
        summary_file=_SUMMARY_FILE,
        conflict_file=_CONFLICT_FILE,
        escalation_file=_ESCALATION_FILE,
        update_script=_UPDATE_SCRIPT,
    )

    rc = _spawn_cc(prompt, "sonnet", pid_file)
    logger.info("Tier 2 (Sonnet) completed with rc=%d", rc)

    # Check if further escalation is needed
    if _ESCALATION_FILE.is_file():
        escalation = _ESCALATION_FILE.read_text().strip()
        if "tier3_needed" in escalation:
            logger.info("Tier 2 escalated to Tier 3 (Opus) — notifying user")
            _notify_tier3_needed()


def _run_tier3(pid_file: Path) -> None:
    """Tier 3: Opus resolves deep merge conflicts. User-initiated only."""
    prompt = _TIER3_PROMPT.format(
        escalation_file=_ESCALATION_FILE,
        summary_file=_SUMMARY_FILE,
        update_script=_UPDATE_SCRIPT,
    )

    rc = _spawn_cc(prompt, "opus", pid_file)
    logger.info("Tier 3 (Opus) completed with rc=%d", rc)


def _notify_tier3_needed() -> None:
    """Notify user that deep conflicts need Opus-level resolution."""
    try:
        # Write a summary for the dashboard to pick up
        _SUMMARY_FILE.write_text(
            "conflicts_unresolved: Deep merge conflicts require Opus resolution. "
            "Use 'Resolve with Opus' from the dashboard."
        )

        logger.info(
            "Update has deep conflicts needing Opus resolution. "
            "User should use dashboard 'Resolve with Opus' button."
        )
    except Exception:
        logger.error("Failed to send tier3 notification", exc_info=True)


@blueprint.route("/api/genesis/updates/resolve", methods=["POST"])
def update_resolve():
    """User-initiated Tier 3 (Opus) conflict resolution."""
    if not _ESCALATION_FILE.is_file():
        return jsonify({"error": "No escalation pending"}), 404

    escalation = _ESCALATION_FILE.read_text().strip()
    if "tier3_needed" not in escalation:
        return jsonify({"error": "No Tier 3 escalation pending"}), 404

    pid_file = _HOME / ".genesis" / "update_in_progress.pid"

    # Check if something is already running
    if pid_file.is_file():
        try:
            old_pid = int(pid_file.read_text().strip())
            result = subprocess.run(["kill", "-0", str(old_pid)],
                                    capture_output=True, timeout=2)
            if result.returncode == 0:
                return jsonify({
                    "error": "Update session already in progress",
                    "pid": old_pid,
                }), 409
            pid_file.unlink(missing_ok=True)
        except (ValueError, subprocess.TimeoutExpired, OSError):
            pid_file.unlink(missing_ok=True)

    global _orchestrator_alive

    def _run():
        global _orchestrator_alive
        _orchestrator_alive = True
        try:
            _run_tier3(pid_file)
        except Exception:
            logger.error("Tier 3 (Opus) failed", exc_info=True)
        finally:
            _orchestrator_alive = False
            pid_file.unlink(missing_ok=True)
            logger.info("Tier 3 (Opus) finished, PID file cleaned up")

    thread = threading.Thread(target=_run, daemon=True, name="update-tier3")
    thread.start()

    return jsonify({
        "status": "triggered",
        "supervised": True,
        "tier": 3,
    })


@blueprint.route("/api/genesis/updates/progress")
def update_progress():
    """Poll update progress — reads summary and escalation files."""
    global _orchestrator_alive
    summary = None
    if _SUMMARY_FILE.is_file():
        summary = _SUMMARY_FILE.read_text().strip()

    escalation = None
    if _ESCALATION_FILE.is_file():
        escalation = _ESCALATION_FILE.read_text().strip()

    conflicts = None
    if _CONFLICT_FILE.is_file():
        import contextlib as _ctxlib

        with _ctxlib.suppress(json.JSONDecodeError, OSError):
            conflicts = json.loads(_CONFLICT_FILE.read_text())

    # Check if an update process is still running
    pid_file = _HOME / ".genesis" / "update_in_progress.pid"
    in_progress = False
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
            result = subprocess.run(["kill", "-0", str(pid)],
                                    capture_output=True, timeout=2)
            in_progress = result.returncode == 0
        except (ValueError, subprocess.TimeoutExpired, OSError):
            pass

    # ── Escalation recovery after Flask restart ───────────────────────────────
    # If Flask restarted mid-update (genesis-server was stopped/started by
    # update.sh), the daemon orchestrator thread died. The CC subprocess
    # survived (start_new_session=True) and may have written tier2_needed.
    # When Tier 1 CC exits, the PID goes dead → in_progress=False. On the
    # next poll we detect tier2_needed + no live orchestrator → auto-spawn.
    # Tier 3 is intentionally NOT auto-spawned (user must confirm via dashboard).
    if (
        escalation
        and "tier2_needed" in escalation
        and not in_progress
        and not _orchestrator_alive
    ):
        with _orchestrator_lock:
            # Double-check inside lock to prevent concurrent double-spawn.
            # Set _orchestrator_alive=True here (while lock is held) so that
            # a second concurrent poll that acquires the lock next sees the
            # flag already True before the thread has had a chance to run.
            if not _orchestrator_alive:
                _orchestrator_alive = True
                logger.info(
                    "Escalation recovery: spawning Tier 2 (orchestrator died, "
                    "tier2_needed in escalation file)"
                )
                _ESCALATION_FILE.unlink(missing_ok=True)
                _recovery_pid = _HOME / ".genesis" / "update_in_progress.pid"

                def _recover_tier2() -> None:
                    global _orchestrator_alive
                    try:
                        _run_tier2(_recovery_pid)
                    except Exception:
                        logger.error("Tier 2 escalation recovery failed", exc_info=True)
                    finally:
                        _orchestrator_alive = False
                        _recovery_pid.unlink(missing_ok=True)
                        logger.info("Tier 2 recovery finished, PID file cleaned up")

                threading.Thread(
                    target=_recover_tier2,
                    daemon=True,
                    name="update-tier2-recovery",
                ).start()
                in_progress = True  # reflect spawned state in this response

    return jsonify({
        "in_progress": in_progress,
        "summary": summary,
        "escalation": escalation,
        "conflicts": conflicts,
    })
