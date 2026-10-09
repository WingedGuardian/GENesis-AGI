"""Receipt-bound previews through the existing verification CLI; never record."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from codex_validator_pilot import (
    LIMIT,
    RECIPES,
    _private_json,
    bound_state,
    configuration,
    deployment_lock,
    finish_shutdown,
    operation_lock,
)
from codex_validator_serving import child_environment

from genesis.eval.qualification.evidence import canonical, digest
from genesis.session_awareness.pr_evidence import parse_evidence
from genesis.util.streams import read_limited

PREVIEW_TIMEOUT = 60.0


async def _cli(runtime: Path, workspace: Path, config: dict, doc, note, park) -> str:
    with tempfile.TemporaryDirectory(prefix="preview-", dir=workspace / ".codex") as temporary:
        evidence = Path(temporary) / "evidence.json"
        fd = os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical(doc.model_dump()))
        argv = [
            str(runtime / ".venv/bin/python"), str(runtime / "scripts/pr_verification.py"),
            "close", "--pr", str(doc.pr), "--evidence-file", str(evidence),
            "--db-path", config["ledger"], "--dry-run",
        ]
        if note is not None:
            argv.append("--note=" + note)
        if park:
            argv.append("--park")
        env = child_environment()
        env["PYTHONPATH"] = str(runtime / "src")
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=runtime, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(PREVIEW_TIMEOUT):
                out, err, code = await asyncio.gather(
                    read_limited(proc.stdout, LIMIT), read_limited(proc.stderr, LIMIT), proc.wait()
                )
            if code or out[1] > LIMIT or err[1] > LIMIT:
                raise ValueError("Verification preview unavailable")
            return out[0].decode("utf-8", errors="strict")
        except BaseException:
            await finish_shutdown(proc)
            raise


def preview(workspace: Path, runtime: Path, request: dict) -> dict:
    pr, receipt_id = request["pr"], request["receipt"]
    if (
        type(pr) is not int or pr <= 0
        or not isinstance(receipt_id, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_id)
        or type(request["park"]) is not bool
        or (request["note"] is not None and (
            not isinstance(request["note"], str) or not 1 <= len(request["note"]) <= 8000
        ))
    ):
        raise ValueError("Unsupported verification preview")
    doc = parse_evidence(request["evidence"])
    with operation_lock(workspace), deployment_lock():
        config = configuration(workspace)
        row = next((row for row in config["rows"] if row["pr"] == pr), None)
        if row is None or (doc.repo, doc.pr, doc.merge_commit) != (
            row["repo"], row["pr"], row["merge_commit"]
        ):
            raise ValueError("Verification identity unavailable")
        receipt = _private_json(workspace / ".codex/receipts" / f"{pr}.json")
        if (
            set(receipt) != {"version", "configuration", "row", "cases", "before", "after", "scope_limit"}
            or type(receipt["version"]) is not int or receipt["version"] != 1
            or digest(receipt) != receipt_id or receipt["configuration"] != digest(config)
            or receipt["row"] != row or receipt["scope_limit"] != RECIPES[row["recipe"]][2]
            or not isinstance(receipt["cases"], list)
            or any(not isinstance(case, str) for case in receipt["cases"])
            or len(set(receipt["cases"])) != len(receipt["cases"])
        ):
            raise ValueError("Verification receipt unavailable")
        expected = Counter({
            node.split("::")[0].removesuffix(".py").replace("/", ".") + "::" + node.split("::")[1]: count
            for node, count in RECIPES[row["recipe"]][0].items()
        })
        if Counter(case.split("[")[0] for case in receipt["cases"]) != expected:
            raise ValueError("Verification case census unavailable")
        for state in (receipt["before"], receipt["after"]):
            if not isinstance(state, dict) or bound_state(
                runtime, config, row, token=state.get("bracket")
            ) != {**state, "verified": True}:
                raise ValueError("Verification bracket changed")
        if receipt["scope_limit"] not in doc.scope_limits:
            doc.scope_limits.append(receipt["scope_limit"])
        output = asyncio.run(_cli(runtime, workspace, config, doc, request["note"], request["park"]))
        bound_state(runtime, config, row, token=receipt["after"]["bracket"])
        return {"pr": pr, "receipt": receipt_id, "preview_only": True, "preview": output}
