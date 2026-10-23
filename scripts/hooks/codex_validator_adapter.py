#!/usr/bin/env python3
"""Interactive validator adapter; ordinary profiles are never activated here.

main_checkout_guard is file-only (no unmatched Bash snapshots). Excluded:
background_pipe_guard (no run_in_background), review_enforcement_commit,
pre_push_privacy_review, adopt_first_gate, plan_confidence_reminder, ask_gate_menu,
behavioral_linter, procedure_advisor, cbm-discovery-gate, deploy_timeout_advisory
(commit/plan/lifecycle), pretool_check (autonomous admission). Hosted web tools
do not reach hooks. Existing guards' internal fail-open paths remain theirs;
observable failures here deny. Arbitrary programs, later stdin, filesystem races
and failed/killed outer hooks remain outside this accident-prevention layer.
Native 0.163.0 drops shell workdir: require cd/exit and a newline, no rewriting.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import sys
from pathlib import Path

from codex_validator_guard import MAX_PAYLOAD_BYTES, RUNTIME_ROOT, Refused, normalize
from codex_validator_patch import check_patch, patch_targets

sys.path.insert(0, str(RUNTIME_ROOT / "src"))
from genesis.eval.qualification.evidence import load_json

OVERALL_BUDGET = 10
GUARDS = (
    "destructive_command_guard", "repo_routing_guard", "full_suite_guard",
    "tmux_kill_server_guard", "pipe_status_guard", "credential_surface_hook",
    "git_discard_guard", "git_push_guard", "secrets_env_access_guard",
    "protected_paths_guard", "worktree_cwd_guard", "capped_read_advisory",
    "rm_trash_advisory", "pr_close_advisory",
)
# Exact settings.json inline command, held equal by a parity test.
INLINE_BASH_SAFETY = "bash -c 'IN=$(cat); CMD=$(printf %s \"$IN\" | jq -r .tool_input.command 2>/dev/null); if echo \"$CMD\" | grep -qE \"genesis[[:space:]]+serve\" && echo \"$CMD\" | grep -qi \"worktree\"; then echo \"BLOCKED: never boot the full Genesis runtime from/against a worktree.\" >&2; echo \"Children inherit PYTHONPATH and path-keyed subsystems reindex the worktree as a new project — OOM-crashed the container 2026-07-03.\" >&2; echo \"PYTHONPATH to a worktree is for pytest only. Use merge-then-verify or a blueprint-only harness.\" >&2; exit 2; fi; if echo \"$CMD\" | grep -qE \"pip install.*((-e|--ed[a-z]*|[[:space:]]-[a-zA-Z]*e)[=[:space:]]*[^ ]*worktree)\"; then echo \"BLOCKED: pip install -e to a worktree redirects the ENTIRE system genesis imports.\" >&2; echo \"This crashes the live bridge. Use PYTHONPATH instead:\" >&2; echo \"  PYTHONPATH=/path/to/worktree/src pytest tests/\" >&2; exit 2; fi; case \"$CMD\" in *\"git reset --hard\"*) echo \"BLOCKED: git reset --hard destroys uncommitted work. Use git stash or ask the user.\" >&2; exit 2;; *\"nohup\"*\"run_ui\"*|*\"nohup\"*\"bridge\"*|*\"nohup\"*\"genesis\"*) echo \"BLOCKED: AZ and bridge must run via systemd, not nohup.\" >&2; echo \"Use: systemctl --user restart genesis-az\" >&2; echo \"  or: systemctl --user restart genesis-bridge\" >&2; exit 2;; esac; if echo \"$CMD\" | grep -qE \"sqlite3.*genesis\\.db\"; then echo \"WARNING: Direct sqlite3 access to genesis.db detected. Use MCP tools (db_schema, health_status) or genesis.db.crud modules instead. Raw SQL risks write contention and bypasses WAL safety. Proceed only if read-only query.\" >&2; fi'"
# Closed syntax for the anchor only, not a second shell-command parser.
ANCHOR = re.compile(
    r"""\Acd[ \t]+(?P<path>/[^\s;&|<>()$\x60\\'"*?!#\[{]+|'/[^'\r\n]*'|"/[^"$\x60\\\r\n]*")[ \t]+\|\|[ \t]+exit\n(?=\s*\S)"""
)


def hook_command(runtime: Path) -> str:
    """Trusted definition for a later profile; do not activate it here.

    fd 4 preserves advisory stdout while command substitution collects only the
    fd 3 decision. A missing/broken launcher, including a silent exit 0, denies.
    """
    body = '''rc=0
{ verdict=$(bash "$1" 3>&1 1>&4); } 4>&1 || rc=$?
if [ "$rc" -eq 0 ] && [ "$verdict" = allow ]; then exit 0; fi
if [ "$rc" -eq 2 ] && [ "$verdict" = deny ]; then exit 2; fi
printf '%s\\n' 'BLOCKED: Validator hook did not explicitly allow. Stop and raise this with the owner.' >&2
exit 2'''
    return shlex.join(["bash", "-c", body, "codex-validator", str(runtime / "scripts/hooks/codex-validator-adapter")])


def initial_directory(command: str) -> Path:
    match = ANCHOR.match(command)
    if not match:
        raise Refused("Every Bash call requires literal cd /absolute/directory || exit, newline, then command")
    directory = Path(shlex.split(match["path"])[0])
    if ".." in directory.parts:
        raise Refused("Validator command anchor must not contain parent components")
    if not directory.is_dir():
        raise Refused("Validator command anchor is not an existing directory")
    return directory.resolve()


def patch_events(payload: dict) -> list[dict]:
    command = payload["tool_input"]["command"]
    cwd = Path(payload.get("cwd", ""))
    if not cwd.is_absolute():
        raise Refused("Patch has no absolute native working directory")
    home = Path.home()
    output = home / ".genesis/output"
    events = []
    for name in patch_targets(command):
        target = Path(name)
        target = target if target.is_absolute() else cwd / target
        roots = [home / "tmp/validator", output / "validator-runs"]
        if target.is_relative_to(output):
            first = target.relative_to(output).parts
            if first and first[0].startswith("prv-") and first[0] != "prv-":
                roots.append(output / first[0])
        root = next((r for r in roots if target.is_relative_to(r) and r.resolve() == r), None)
        if root is None:
            raise Refused("Patch target is outside the canonical validator scratch roots")
        # Check the absolute lexical native target, never a different rebased file.
        # Keep '..' and symlinks visible to the existing workspace policy.
        check_patch(f"*** Begin Patch\n*** Add File: {target}\n*** End Patch", root)
        events.append({**payload, "tool_name": "Write", "tool_input": {"file_path": str(target)}})
    return events


def prepare(payload: object) -> list[tuple[list[str], dict, float]]:
    action = normalize(payload)
    launcher = str(RUNTIME_ROOT / ".claude/hooks/genesis-hook")

    def call(name, data):
        return ([launcher, f"hooks/{name}.py"], data, 5 if name == "git_push_guard" else 8)

    if action.kind == "shell":
        data = {**payload, "cwd": str(initial_directory(action.arguments["command"]))}
        return [call(name, data) for name in GUARDS] + [
            (["bash", "-c", shlex.split(INLINE_BASH_SAFETY)[2]], data, 8),
        ]
    if action.kind == "patch":
        return [call(name, event) for event in patch_events(payload)
                for name in ("main_checkout_guard", "secrets_env_access_guard")]
    return [call("secrets_env_access_guard", payload)]


def child_result(code: int, stdout: bytes, stderr: bytes) -> str:
    if code == 2:
        raise Refused(stderr.decode(errors="replace").strip() or "Guard denied the action")
    if code:
        raise Refused("Validator guard failed; stop and raise this with the owner")
    notes = [stderr.decode(errors="replace").strip()]
    if stdout.strip():
        result = load_json(stdout)
        if not isinstance(result, dict) or set(result) - {"hookSpecificOutput", "decision", "reason"}:
            raise ValueError("Unexpected guard output")
        if result.get("decision") == "block":
            raise Refused(str(result.get("reason") or "Guard denied the action"))
        if "decision" in result:
            raise ValueError("Unknown guard decision")
        specific = result.get("hookSpecificOutput")
        if (not isinstance(specific, dict) or not specific
                or set(specific) - {"hookEventName", "permissionDecision", "permissionDecisionReason", "additionalContext"}
                or specific.get("hookEventName", "PreToolUse") != "PreToolUse"):
            raise ValueError("Unexpected guard output")
        decision = specific.get("permissionDecision")
        reason = specific.get("permissionDecisionReason", "")
        context = specific.get("additionalContext", "")
        if not isinstance(reason, str) or not isinstance(context, str):
            raise ValueError("Malformed guard explanation")
        if decision in {"ask", "deny"}:
            raise Refused((reason or "Guard requires approval") + "; stop and raise this with the owner")
        if "permissionDecision" in specific and decision != "allow":
            raise ValueError("Unknown guard decision")
        notes.extend((reason, context))
    return "\n".join(note for note in notes if note)


async def run_guard(argv: list[str], payload: dict, budget: float) -> str:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(RUNTIME_ROOT)}
    env.pop("GENESIS_CC_SESSION", None)
    proc = None
    communication = None
    try:
        # Retain ownership even if the overall deadline cancels process creation.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True,
            cwd=payload["cwd"] if payload.get("tool_name") == "Bash" else None,
        ))
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            proc = await spawn
            raise
        communication = asyncio.create_task(proc.communicate(json.dumps(payload).encode()))
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), budget)
        return child_result(proc.returncode, stdout, stderr)
    except TimeoutError:
        raise Refused("Validator guard timed out; stop and raise this with the owner") from None
    except Refused:
        raise
    except Exception:
        raise Refused("Validator guard failed; stop and raise this with the owner") from None
    finally:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            if communication is not None:
                await communication  # Drain pipes after killing; wait alone can deadlock.
            else:
                await proc.wait()


async def evaluate(payload: object) -> str:
    calls = prepare(payload)
    slots = asyncio.Semaphore(15)

    async def invoke(call):
        async with slots:
            return await run_guard(*call)

    tasks = [asyncio.create_task(invoke(call)) for call in calls]
    try:
        notes = await asyncio.wait_for(asyncio.gather(*tasks), OVERALL_BUDGET)
        return "\n".join(note for note in notes if note)
    except TimeoutError:
        raise Refused("Validator evaluation timed out; stop and raise this with the owner") from None
    finally:
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise Refused("Validator payload exceeds its size limit")
        context = asyncio.run(evaluate(load_json(raw)))
    except Refused as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
    except Exception:
        print("BLOCKED: Validator evaluation failed; stop and raise this with the owner", file=sys.stderr)
    else:
        # Private evaluator token; only the shell wrapper consumes it.
        print("allow")
        if context:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "additionalContext": context,
            }}))
        return 0
    print("deny")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
