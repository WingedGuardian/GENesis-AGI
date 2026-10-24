"""Real native SDK/MCP path, canonical authority, independent disposable sinks."""

import asyncio
import dataclasses
import hashlib
import importlib.metadata
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import aiosqlite
from headlong_controls import dependency_inventory, preflight, tree_hash
from headlong_native_provider import TOOL, Backend

from genesis.db.crud import ego
from genesis.db.schema import TABLES

KINDS = ("echo", "approved", "pending", "rejected", "revoked", "develop", "duplicate", "foreign", "legacy", "unknown", "provider_error")
EFFECT = b"native-fixture-effect\n"


async def prepare_case(db, kind):
    if kind == "unknown":
        return
    await ego.create_proposal(db, id="proposal", action_type="fixture", content="synthetic owned effect",
                              ego_source=None if kind == "legacy" else "user_ego_cycle" if kind == "foreign" else "genesis_ego_cycle",
                              scope=None if kind == "legacy" else "develop" if kind == "develop" else "operate")
    if kind not in {"echo", "pending", "provider_error", "handler_error"} and not await ego.resolve_proposal(
            db, "proposal", status="rejected" if kind == "rejected" else "approved", expected_revision=1):
        raise ValueError("synthetic owner operation was refused")
    if kind == "revoked" and not await ego.revoke_proposal(db, "proposal"):
        raise ValueError("synthetic owner revocation was refused")


class EffectPort:
    def __init__(self, db, root, inject_error=False):
        self.db, self.root, self.inject_error = db, root, inject_error
        self.calls = []

    async def call(self, arguments):
        call = {"canonical_called": False, "claim": None, "outcome": "started"}
        self.calls.append(call)
        try:
            if set(arguments) != {"proposal_id"} or not isinstance(arguments["proposal_id"], str):
                raise ValueError("invalid protected-tool arguments")
            row = await ego.get_proposal(self.db, arguments["proposal_id"])
            if row is None:
                call["outcome"] = "missing_row"
            elif row["ego_source"] not in (None, "genesis_ego_cycle"):
                call["outcome"] = "source_mismatch"
            else:
                call["canonical_called"] = True
                if self.inject_error:
                    raise RuntimeError("synthetic handler-error instrument control")
                call["claim"] = await ego.claim_proposal_for_dispatch(self.db, arguments["proposal_id"], allow_develop=False)
                call["outcome"] = "refused"
                if call["claim"]:
                    (self.root / "effect.bin").write_bytes(EFFECT)
                    call["outcome"] = "executed"
            return {"content": [{"type": "text", "text": json.dumps({"claimed": call["claim"], "outcome": call["outcome"]})}],
                    "is_error": call["outcome"] != "executed"}
        except Exception as exc:
            call.update(outcome="error", error=f"{type(exc).__name__}: {exc}")
            raise


async def observe(root, port, native, backend):
    async with aiosqlite.connect(f"file:{root / 'fixture.sqlite3'}?mode=ro", uri=True) as reader:
        reader.row_factory = aiosqlite.Row
        row = await ego.get_proposal(reader, "proposal")
    effect = root / "effect.bin"
    return {"native": native, "backend": backend, "calls": port.calls, "state": row,
            "effect": effect.read_bytes().decode() if effect.exists() else None}


def qualify(kind, evidence):
    native, backend, calls = (evidence[key] for key in ("native", "backend", "calls"))
    count = 0 if kind in {"echo", "provider_error"} else 2 if kind == "duplicate" else 1
    if (not native or not native.get("session_id") or not backend["main_requests"] or backend["unexpected"]
            or len(calls) != count or any(call["outcome"] in {"started", "error"} for call in calls)
            or sorted(backend["tool_results"]) != [f"toolu_fixture_{i + 1}" for i in range(count)]):
        return "incomplete"
    if kind == "provider_error":
        if not native["is_error"] or not backend["provider_error"]:
            return "incomplete"
    elif native["is_error"] or native["subtype"] != "success" or native["result"] != f"native-final-{kind}":
        return "incomplete"
    positive = kind in {"approved", "duplicate", "legacy"}
    status = ("executed" if positive else "approved" if kind in {"develop", "foreign"} else
              "rejected" if kind in {"revoked", "rejected"} else "pending")
    row = evidence["state"]
    if (None if row is None else row["status"]) != (None if kind == "unknown" else status):
        return "fail"
    if evidence["effect"] != (EFFECT.decode() if positive else None):
        return "fail"
    expected = ([{"canonical_called": True, "claim": True, "outcome": "executed"}] if positive else [])
    if kind == "duplicate" or kind in {"pending", "rejected", "revoked", "develop"}:
        expected.append({"canonical_called": True, "claim": False, "outcome": "refused"})
    elif kind in {"foreign", "unknown"}:
        expected.append({"canonical_called": False, "claim": None,
                         "outcome": "source_mismatch" if kind == "foreign" else "missing_row"})
    return "pass" if sorted(map(json.dumps, calls)) == sorted(map(json.dumps, expected)) else "fail"


async def native_turn(root, kind, port, backend, cli, messages, stderr):
    # Import lazily: host-safe tests never execute or initialize the native CLI.
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        create_sdk_mcp_server,
        tool,
    )

    @tool("claim_effect", "Claim a proposal and perform the fixed synthetic effect", {"proposal_id": str})
    async def claim_effect(arguments):
        return await port.call(arguments)

    result = None
    home = root / "home"
    home.mkdir()
    options = ClaudeAgentOptions(cli_path=str(cli), cwd=str(root), tools=[],
        system_prompt={"type": "preset", "preset": "claude_code"}, setting_sources=[], strict_mcp_config=True,
        mcp_servers={"fixture": create_sdk_mcp_server(name="fixture", tools=[claim_effect])},
        permission_mode="dontAsk", allowed_tools=[TOOL], model="sonnet", max_turns=4, stderr=stderr.append,
        env={"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "config"), "ANTHROPIC_API_KEY": "dummy-offline-fixture",
             "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{backend.server.server_port}",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    async with ClaudeSDKClient(options=options) as client:
        await client.query(f"fixture-native-{kind}: synthetic offline qualification")
        async for message in client.receive_response():
            messages.append({"type": type(message).__name__, "data": dataclasses.asdict(message)})
            if isinstance(message, ResultMessage):
                result = dataclasses.asdict(message)
    return result, messages, stderr


async def run_case(root, kind, cli):
    root.mkdir()
    async with aiosqlite.connect(root / "fixture.sqlite3") as db:
        db.row_factory = aiosqlite.Row
        for name in ("ego_proposals", "ego_proposal_revisions"):
            await db.execute(TABLES[name])
        await prepare_case(db, kind)
        port = EffectPort(db, root, inject_error=kind == "handler_error")
        result, messages, stderr, error = None, [], [], None
        with Backend(kind, port) as backend:
            try:
                async with asyncio.timeout(40):
                    result, messages, stderr = await native_turn(root, kind, port, backend, cli, messages, stderr)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        evidence = await observe(root, port, result, backend.evidence)
    return {"kind": kind, "qualification": qualify(kind, evidence), **evidence,
            "messages": messages, "stderr": stderr, "driver_error": error}


async def main():
    preflight()
    import claude_agent_sdk

    cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    native = {"sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
              "version": subprocess.check_output([str(cli), "--version"], text=True, timeout=10).strip()}
    canonical = {"crud_sha256": hashlib.sha256(Path(ego.__file__).read_bytes()).hexdigest(),
                 "ddl_sha256": hashlib.sha256(json.dumps({name: TABLES[name] for name in
                     ("ego_proposals", "ego_proposal_revisions")}, sort_keys=True).encode()).hexdigest()}
    sources = {name: tree_hash(Path(path)) for name, path in
               {"headlong": "/headlong", "fixtures": "/fixture", "genesis": "/genesis-src"}.items()}
    cases, instrument = [], None
    with TemporaryDirectory(dir="/work") as directory:
        root = Path(directory)
        for kind in KINDS:
            row = await run_case(root / kind, kind, cli)
            cases.append(row)
            if row["qualification"] != "pass":
                break  # Calibration/transport failures cannot turn into policy success.
        if len(cases) == len(KINDS) and all(row["qualification"] == "pass" for row in cases):
            instrument = await run_case(root / "handler_error", "handler_error", cli)
    unchanged = sources == {name: tree_hash(Path(path)) for name, path in
                           {"headlong": "/headlong", "fixtures": "/fixture", "genesis": "/genesis-src"}.items()}
    passed = (len(cases) == len(KINDS) and all(row["qualification"] == "pass" for row in cases) and unchanged
              and instrument is not None and instrument["qualification"] == "incomplete"
              and instrument["native"] is not None and not instrument["native"]["is_error"]
              and instrument["native"]["result"] == "native-final-handler_error"
              and any(call["outcome"] == "error" for call in instrument["calls"]))
    print(json.dumps({"schema": "headlong-native-action/v1", "status": "pass" if passed else "incomplete",
                      "source_pin": Path("/fixture/genesis-source-pin").read_text().strip(), "sources": sources,
                      "sdk": importlib.metadata.version("claude-agent-sdk"), "native_binary": native,
                      "canonical": canonical,
                      "dependencies": dependency_inventory(), "cases": cases, "instrument_control": instrument,
                      "sources_unchanged": unchanged, "cleanup_confirmed": True,
                      "limits": "Real native tool path with scripted backend only; no inference, production provider or orchestration parity."}))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({"status": "incomplete", "reason": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(2) from exc
