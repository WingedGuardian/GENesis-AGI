"""Seven other caller-file fields: ordinary oracle versus actual stdio/HTTP."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import json
import signal
import sys
from pathlib import Path

import aiosqlite
import httpx
import pytest

from tests.test_mcp.test_shared_cli_startup_contract import shared_install as shared_install
from tests.test_mcp.test_shared_forwarder import frame, receive, send


def cases_for(tmp_path, service, install):
    caller, server = tmp_path / "caller", tmp_path / "server"
    server.mkdir()
    if service == "health":
        plans = Path(install.env["HOME"]) / ".claude/plans"
        plans.mkdir(parents=True)
        caller.symlink_to(plans, target_is_directory=True)
    else:
        caller.mkdir()
    for directory, marker in [(caller, "CALLER"), (server, "SERVER collision")]:
        (directory / "golden.jsonl").write_text(json.dumps({
            "id": "synthetic", "actual": "synthetic", "user_passed": True,
            "scorer_config": {"session_context": marker},
        }) + "\n")
        (directory / "suite.jsonl").write_text(json.dumps({
            "id": "synthetic", "category": "drafting", "prompt": marker, "expected": "synthetic",
        }) + "\n")
        (directory / "resume.txt").write_text(marker + " resume")
        for name in ("index.pdf", "query.pdf"):
            (directory / name).write_text(marker + " synthetic PageIndex upload")
    # A raw string with a server-cwd lookalike must remain literal resume text.
    (server / "literal-resume").write_text("SERVER should never reach reviewer")
    (caller / "plan.md").write_text("\n".join([
        "## Requirements", "CALLER", "## Steps", "## Success Criteria", "## Risks and Failure Modes",
    ]))
    (server / "plan.md").write_text("SERVER invalid plan")
    (caller / "invalid-plan.md").write_text("Missing required sections")

    def case(label, tool, **arguments):
        return {"label": label, "tool": tool, "arguments": arguments}

    if service == "memory":
        return [
            case("index", "document_index", path="index.pdf"),
            case("auto-query", "document_query", path_or_doc_id="query.pdf", question="synthetic"),
            case("id-query", "document_query", path_or_doc_id="pi-direct-fixture", question="synthetic"),
            case("missing-document", "document_index", path="missing.pdf"),
            case("file-resume", "resume_review", resume_source="resume.txt"),
            case("raw-resume", "resume_review", resume_source="literal-resume"),
        ]
    return [
        case("evo", "evo_run", base_prompt="synthetic", n_variants=1, eval_limit=1,
             holdout_limit=1, golden_set_path="golden.jsonl", propose=False),
        case("experiment", "experiment_run", experiment_name="synthetic", control_prompt="control",
             treatment_prompt="treatment", golden_set_path="golden.jsonl"),
        case("replay", "skill_replay_run", skill_name="fixture", old_content="OLD", new_content="NEW",
             suite_path="suite.jsonl"),
        case("plan", "task_submit", plan_path=" plan.md ", description="synthetic"),
        case("invalid-plan", "task_submit", plan_path="invalid-plan.md", description="synthetic"),
        case("outside-plan", "task_submit", plan_path=str(server / "plan.md"), description="synthetic"),
        case("blank-plan", "task_submit", plan_path="   ", description="synthetic"),
        case("empty-golden", "evo_run", golden_set_path="", propose=False),
    ]


def event_groups(path):
    groups, active = {}, None
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if event["event"] == "phase":
            active = event["label"]
            groups[active] = []
        elif active is not None:
            groups[active].append(event)
    return groups


@pytest.mark.parametrize("service", ["health", "memory"])
@pytest.mark.parametrize("era", ["2026-07-28", "2025-06-18"])
async def test_other_file_tools_match_ordinary_caller_semantics(tmp_path, service, era, shared_install):
    install = shared_install
    cases = cases_for(tmp_path, service, install)
    (tmp_path / "tool-cases.json").write_text(json.dumps(cases))
    env = dict(install.env, GENESIS_CC_SESSION="1", GENESIS_SESSION_ORIGIN="external_untrusted")
    headers = {"Authorization": "Bearer " + "f" * 43,
               "X-Genesis-Caller": json.dumps({"dispatched": True, "supervised": False,
                                              "origin": "external_untrusted"})}
    frontend = backend = None
    receipts = []
    with (tmp_path / "backend.stderr").open("wb") as backend_error, \
            (tmp_path / "frontend.stderr").open("wb") as frontend_error:
        try:
            backend = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("shared_paths_backend.py")),
                str(tmp_path), str(install.port), service, cwd=tmp_path / "server", env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=backend_error,
            )
            # Bound the complete readiness operation rather than each HTTP read.
            async with httpx.AsyncClient(trust_env=False, headers=headers, timeout=None) as control, asyncio.timeout(120):  # noqa: S113
                while True:
                    assert backend.returncode is None, (tmp_path / "backend.stderr").read_text()
                    try:
                        if (await control.get(f"http://127.0.0.1:{install.port}/status")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(.02)
            oracle = json.loads((tmp_path / "ordinary-results.json").read_text())
            cases = json.loads((tmp_path / "tool-cases.json").read_text())
            expected_events = event_groups(tmp_path / "path-events.jsonl")
            if service == "health":
                assert [row["status"] for row in oracle[:4]] == ["ok", "ok", "ok", "pending"], oracle
            else:
                assert oracle[0]["status"] == "indexed" and "error" not in oracle[1], oracle
                assert oracle[4]["combined_output"] == "CALLER resume", oracle
            frontend = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("shared_forwarder_child.py")),
                service, str(install.port), cwd=tmp_path / "caller", env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=frontend_error,
            )
            if era == "2025-06-18":
                await send(frontend, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                    "protocolVersion": era, "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
                assert (await receive(frontend))["result"]["protocolVersion"] == era
                await send(frontend, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            for index, case in enumerate(cases):
                events = tmp_path / "path-events.jsonl"
                start = len(events.read_text().splitlines())
                request = frame(index + 1, "tools/call", name=case["tool"], arguments=case["arguments"])
                if era == "2025-06-18":
                    request["params"].pop("_meta")
                await send(frontend, request)
                reply = await receive(frontend)
                assert type(reply["id"]) is int and reply["id"] == index + 1
                assert not reply["result"].get("isError"), reply
                actual = reply["result"]["structuredContent"]
                observed = [json.loads(line) for line in events.read_text().splitlines()[start:]]
                expected = oracle[index]
                if case["tool"] == "task_submit":
                    actual, expected = ({k: v for k, v in value.items() if k != "task_id"}
                                        for value in (actual, expected))
                # Missing-file error text includes normalized arguments; the
                # error itself and the zero-provider predicate are the contract.
                if case["label"] == "missing-document":
                    assert "File not found" in actual["error"] and "File not found" in expected["error"]
                else:
                    assert actual == expected, (case["label"], actual, expected)
                assert observed == expected_events[case["label"]], (case["label"], observed, expected_events)
                assert "SERVER" not in json.dumps(observed), observed
                receipts.append({"label": case["label"], "reply": actual, "events": observed})
            # The oracle must actually reach each intended file consumer.
            if service == "health":
                assert expected_events["evo"][0]["text"] == "CALLER"
                assert expected_events["experiment"][0]["text"] == "CALLER"
                assert expected_events["replay"][0]["text"].startswith("CALLER")
                assert oracle[3]["status"] == "pending"
                assert "missing required sections" in oracle[4]["error"]
                assert "outside allowed directories" in oracle[5]["error"]
                assert oracle[6]["error"] == "plan_path is required"
                async with aiosqlite.connect(install.env["GENESIS_DB_PATH"]) as db:
                    rows = await (await db.execute("SELECT outputs FROM task_states")).fetchall()
                assert rows == [(str((tmp_path / "caller/plan.md").resolve()),)] * 2
            else:
                assert expected_events["index"] == [{"event": "upload", "text": "CALLER synthetic PageIndex upload"}]
                assert expected_events["file-resume"][-1]["text"] == "CALLER resume"
                assert expected_events["raw-resume"][-1]["text"] == "literal-resume"
            (tmp_path / "tool-path-receipts.json").write_text(json.dumps(receipts, indent=2))
            frontend.stdin.close()
            assert await asyncio.wait_for(frontend.wait(), 30) == 0
        finally:
            for child in (frontend, backend):
                if child is not None and child.returncode is None:
                    child.send_signal(signal.SIGTERM)
                    await asyncio.wait_for(child.wait(), 60)
