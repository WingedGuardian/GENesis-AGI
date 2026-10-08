#!/usr/bin/env python3
"""Native Codex action boundary; activation and mutation policies land separately.

Launch arguments belong to the trusted hook definition. Payload cwd and client
identifiers never establish authority. This is bounded accident prevention;
full-access programs and disabled/failed outer hooks are outside its guarantee.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

# Exact names measured on both clients; later configuration must import this map.
MCP_SERVER_PREFIXES = {
    "mcp__genesis_validator_health__": "health",
    "mcp__genesis_validator_memory__": "memory",
}
RUNTIME_ROOT = Path(__file__).resolve().parents[2]
MAX_PAYLOAD_BYTES = 1024 * 1024


class Refused(ValueError):
    """A static explanation safe to emit without command or argument values."""


@dataclass(frozen=True)
class Action:
    kind: str
    name: str
    arguments: dict


def normalize(payload: object) -> Action:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        raise Refused("Unsupported validator hook event")
    name = payload.get("tool_name")
    arguments = payload.get("tool_input")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise Refused("Malformed validator action")
    if name in {"Bash", "apply_patch"}:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise Refused("Missing validator command")
        return Action("shell" if name == "Bash" else "patch", name, {"command": command})
    if any(name.startswith(prefix) for prefix in MCP_SERVER_PREFIXES):
        return Action("mcp", name, arguments)
    raise Refused("Unsupported validator tool")


def dispatch(action: Action) -> None:
    if action.kind != "mcp":
        # Subsequent reviewed policies replace these explicit closed branches.
        raise Refused("Validator mutation policy is not installed")
    # Fresh-process FastMCP import measured 2.7-3.1s; shell/patch avoid it.
    sys.path.insert(0, str(RUNTIME_ROOT / "src"))
    from genesis.mcp.external_profiles import ExternalProfile, profile_tools

    for prefix, server in MCP_SERVER_PREFIXES.items():
        if action.name.startswith(prefix):
            if action.name[len(prefix):] in profile_tools(server, ExternalProfile.VALIDATOR):
                return
            break
    raise Refused("Tool is unavailable to the validator profile")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--workspace-root", required=True)
    args = parser.parse_args()
    try:
        runtime = Path(args.runtime_root)
        workspace = Path(args.workspace_root)
        if not runtime.is_absolute() or runtime.resolve() != RUNTIME_ROOT:
            raise Refused("Validator runtime does not match its hook")
        if not workspace.is_absolute() or not workspace.is_dir():
            raise Refused("Missing dedicated validator workspace")
        ws, rt = workspace.resolve(), runtime.resolve()
        if ws.is_relative_to(rt) or rt.is_relative_to(ws):
            raise Refused("Validator workspace must be disjoint from the runtime checkout")
        raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise Refused("Validator payload exceeds its size limit")
        # Reuse the pure strict decoder: duplicate keys and nonfinite numbers
        # are invalid input. Its exceptions reach only the static failure path.
        sys.path.insert(0, str(RUNTIME_ROOT / "src"))
        from genesis.eval.qualification.evidence import load_json

        payload = load_json(raw)
        dispatch(normalize(payload))
    except Refused as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
    except Exception:
        # Import/JSON/policy failures must not disclose payloads or permit work.
        print("BLOCKED: Validator action evaluation failed", file=sys.stderr)
    else:
        print("allow")
        return 0
    print("deny")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
