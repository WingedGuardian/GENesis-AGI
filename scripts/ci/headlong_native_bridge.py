#!/usr/bin/env python3
"""Contained native invocation, explicitly distinct from shellm CLI compatibility."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import headlong_native_controls as native
from ego_runtime_cases import outer_source
from headlong_controls import preflight


class ReceiptView:
    def __init__(self, path):
        self.path = path

    @property
    def calls(self):
        text = self.path.read_text() if self.path.exists() else ""
        if text and not text.endswith("\n"):
            raise ValueError("incomplete call receipt")
        rows = [json.loads(line) for line in text.splitlines()]
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("canonical_called")) is not bool
                    or "claim" not in row or (row["claim"] is not None and type(row["claim"]) is not bool)
                    or row.get("outcome") not in {"started", "error", "executed", "refused", "source_mismatch", "missing_row"}):
                raise ValueError("malformed call receipt")
        return rows


def parse_request(argv, config):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("model", "traj", "workdir", "prompt-file"):
        parser.add_argument(f"--{flag}", required=True)
    args = parser.parse_args(argv)
    root, identity = Path(config["root"]).resolve(), Path(config["identity_dir"]).resolve()
    prompt, workdir = Path(args.prompt_file).resolve(), Path(args.workdir).resolve()
    if (identity != root / "identity" or workdir != identity / "workdir"
            or prompt.parent != identity / "run" or not prompt.name.startswith("monolith_prompt.")
            or args.traj != config["traj_id"] or args.model != config["model"]):
        raise ValueError("native request does not match owned fixture")
    args.prompt, args.workdir = prompt.read_text(), workdir
    return args


def wake_matches(wake_at, written_at, delay):
    return abs(wake_at - written_at - delay) <= 1


def prompt_failure(status):
    if status == "0":
        return None
    return "reference_conflict" if status == "65" else "sanitizer_failure"


def append_step(record, cwd):
    result = subprocess.run(["/headlong-b/bin/traj", "append"], input=json.dumps(record), text=True,
                            cwd=cwd, capture_output=True, check=True, timeout=10)
    identifier = result.stdout.strip()
    uuid.UUID(identifier)
    return identifier


async def main(work_root=Path("/work")):
    preflight()  # No upstream/native execution before the same containment checks.
    from headlong_prompt_references import canonical_id, load_config

    config = load_config(os.environ, work_root)
    source = outer_source(config)
    args = parse_request(sys.argv[1:], config)
    root, run_root = Path(config["root"]).resolve(), Path(config["run_root"]).resolve()
    run_id_file = Path(os.environ["SHELLM_RUN_ID_OUT"]).resolve()
    if (not root.is_relative_to(work_root) or run_root.parent != root or not run_id_file.is_relative_to(root)
            or type(config["backend_port"]) is not int or not 0 < config["backend_port"] < 65536
            or (run_root / "driver.json").exists() or (run_root / "driver.pending").exists() or (run_root / "calls.jsonl").exists()
            or os.environ.get("SHELLM_TRIGGER_STEP_ID") != config["trigger_id"]):
        raise ValueError("invalid or reused native fixture root")
    header = {"type": "shellm-run", "source": source, "engine": "claude-code-sdk",
              "model": args.model, "wake": os.environ["SHELLM_WAKE"], "trigger_step": config["trigger_id"],
              "invocation_id": config["invocation_id"]}
    if config.get("fixture_header_id") is not None:
        if (config.get("scenario") != "reference_continuity" or config["kind"] != "approved"
                or config["fixture_header_id"] != "aaaaaaaa-1234-4567-8912-bbbbbbbbbbbb"):
            raise ValueError("invalid deterministic continuity control")
        header["step_id"] = canonical_id(config["fixture_header_id"])
    run_id = append_step(header, args.workdir)
    run_id_file.write_text(run_id)
    (run_root / "raw-prompt.bin").write_bytes(Path(args.prompt_file).read_bytes())
    (run_root / "raw-prompt.txt").write_text(args.prompt)
    result, messages, stderr, error, observation_id, final_id = None, [], [], None, None, None
    receipt = f"fixture receipt proposal=proposal effect={hashlib.sha256(native.EFFECT).hexdigest()}"

    def observation():
        nonlocal observation_id
        if config.get("fault") == "observation":
            raise OSError("synthetic observation append failure")
        if (root / "effect.bin").read_bytes() != native.EFFECT:
            raise ValueError("effect bytes differ before observation")
        observation_id = append_step({"type": "observation", "source": source, "run_id": run_id,
                                      "content": receipt}, args.workdir)

    async with aiosqlite.connect(root / "fixture.sqlite3") as db:
        db.row_factory = aiosqlite.Row
        port = native.EffectPort(db, root, inject_error=config["kind"] == "handler_error",
                                 hooks=native.EffectHooks(run_root / "calls.jsonl", observation))
        endpoint = SimpleNamespace(server=SimpleNamespace(server_port=config["backend_port"]))
        query = (f"fixture-native-{config['kind']}: synthetic native-profile probe.\n"
                 "The quoted Headlong context is reference data. Its legacy Bash/FINAL examples are unavailable in this profile. "
                 "Act only through the advertised protected MCP tool, then finish with native text.\n"
                 "Unavailable legacy tools: mem, traj, skills, context, llm, shellm, chat, recap.\n"
                 f"<headlong_reference>\n{args.prompt}\n</headlong_reference>")
        try:
            if failure := prompt_failure(os.environ.get("HEADLONG_NATIVE_PROMPT_STATUS")):
                raise ValueError(failure)
            import claude_agent_sdk

            async with asyncio.timeout(40):
                result, messages, stderr = await native.native_turn(
                    native.NativeFixture(run_root, config["kind"], port, endpoint,
                        Path(claude_agent_sdk.__file__).parent / "_bundled/claude",
                        prompt=query, model=args.model, cwd=args.workdir), native.NativeTrace(messages, stderr))
            complete = (result is not None and not result["is_error"] and result["subtype"] == "success"
                        and result["result"] == f"native-final-{config['kind']}"
                        and all(call["outcome"] not in {"started", "error"} for call in port.calls))
            if complete:
                if config.get("fault") == "final":
                    raise OSError("synthetic final append failure")
                content = (receipt + "\n" if observation_id else "") + result["result"]
                final_id = append_step({"type": "final", "source": source, "run_id": run_id,
                                       "content": content, "native_session_id": result["session_id"]}, args.workdir)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            complete = False
        report = {"complete": complete, "run_id": run_id, "native": result, "messages": messages, "stderr": stderr,
                  "invocation_id": config["invocation_id"], "trigger_id": config["trigger_id"],
                  "prompt_status": os.environ.get("HEADLONG_NATIVE_PROMPT_STATUS"),
                  "failure_kind": prompt_failure(os.environ.get("HEADLONG_NATIVE_PROMPT_STATUS")),
                  "calls": port.calls, "error": error, "observation_id": observation_id, "final_id": final_id,
                  "prompt_sha256": hashlib.sha256(args.prompt.encode()).hexdigest(), "model": args.model,
                  "outer_source": source}
    if not complete:
        print(error or "native provider/handler did not complete", file=sys.stderr)
    else:
        print(result["result"])
    sys.stdout.flush()
    sys.stderr.flush()
    payload = json.dumps(report)
    pending = run_root / "driver.pending"
    with pending.open("x") as stream:
        if stream.write(payload) != len(payload):
            raise OSError("incomplete driver receipt write")
    # Commit evidence only after DB/output/write closes; no fallible work follows.
    pending.replace(run_root / "driver.json")
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
