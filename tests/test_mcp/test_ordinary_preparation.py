"""Prepared ordinary servers match original bootstrap consumers over real SQL."""

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import pytest

from tests.test_mcp.test_stdio_startup_contract import stdio_install as stdio_install


@dataclass(frozen=True)
class Case:
    service: str
    profile: str | None = None
    era: str = "2026-07-28"
    pool: str = "off"
    missing: bool = False


async def consume(install, root, case):
    capsule, base_env = install
    outputs = []
    script = capsule / "scripts/genesis_mcp_server.py"
    candidate = script.read_bytes()
    # Qualification can compare a retained pre-extraction script. Ordinary CI
    # compares the normal bootstrap consumer with the direct preparation API.
    oracle = os.environ.get("GENESIS_ORDINARY_BOOTSTRAP_ORACLE")
    for mode in ("oracle", "prepared"):
        directory = root / mode
        directory.mkdir()
        config = {**vars(case), "mode": mode, "capsule": str(capsule)}
        (directory / "case.json").write_text(json.dumps(config))
        env = dict(base_env, GENESIS_RECALL_READ_POOL_OFF="1" if case.pool == "off" else "0")
        script.write_bytes(Path(oracle).read_bytes() if mode == "oracle" and oracle else candidate)
        try:
            with (directory / "stderr.log").open("w") as errors:
                child = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).with_name("ordinary_prepared_child.py")),
                    str(directory), env=env, stdout=asyncio.subprocess.DEVNULL, stderr=errors,
                )
                try:
                    assert await asyncio.wait_for(child.wait(), 120) == 0
                finally:
                    if child.returncode is None:
                        child.terminate()
                        await child.wait()
        finally:
            script.write_bytes(candidate)
        result = json.loads((directory / "result.json").read_text())
        outputs.append(result)
        # Keep qualification receipts when pytest removes successful temp dirs.
        if evidence := os.environ.get("GENESIS_ORDINARY_PREPARATION_EVIDENCE_DIR"):
            destination = Path(evidence)
            destination.mkdir(parents=True, exist_ok=True)
            label = f"{case.service}-{case.profile}-{case.era}-{case.pool}-{case.missing}-{mode}"
            (destination / (label + ".json")).write_text(json.dumps({"case": vars(case), "mode": mode, "result": result}))
    return outputs


@pytest.fixture
async def seeded_install(stdio_install):
    capsule, env = stdio_install
    async with aiosqlite.connect(env["GENESIS_DB_PATH"]) as db:
        await db.execute("INSERT INTO observations (id,source,type,content,priority,created_at) "
                         "VALUES (?,?,?,?,?,?)", ("ordinary-observation", "fixture", "finding",
                                                 "Synthetic preparation result", "medium", "2026-01-01"))
        await db.commit()
    return capsule, env


CASES = [Case(service, profile, era) for service in ("health", "memory")
         for profile in (None, "external", "validator") for era in ("2025-06-18", "2026-07-28")]
CASES += [Case("memory", pool=pool) for pool in ("on", "failed")]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c.service}-{c.profile}-{c.era}-{c.pool}")
async def test_prepared_consumer_preserves_catalog_ids_sql_and_profiles(seeded_install, tmp_path, case):
    oracle, prepared = await consume(seeded_install, tmp_path, case)
    assert oracle["runner_calls"] == 1 and prepared["runner_calls"] == 0
    assert oracle["catalog"] == prepared["catalog"]
    if case.profile:
        from genesis.mcp.external_profiles import profile_tools

        expected_names = profile_tools(case.service, case.profile)
    else:
        frozen = json.loads((Path(__file__).with_name("fixtures") / "mcp_catalogs.json").read_text())
        expected_names = {tool["name"] for tool in frozen[case.service]}
    assert {tool["name"] for tool in prepared["catalog"]["tools"]} == expected_names
    assert prepared["protocol"] == case.era
    for result in (oracle, prepared):
        assert result["events"] == ["enter", "exit"]
        assert [(type(value), value) for value in result["handler_ids"]] == [(int, 3), (str, "ordinary-original")]
        assert len(result["calls"]) == 2 and all(not row.get("isError") for row in result["calls"])
        if case.service == "health":
            assert all(json.loads(row["content"][0]["text"])["queues"] == {"fixture": 1}
                       for row in result["calls"])
        else:
            assert all([row["id"] for row in call["structuredContent"]["result"]] == ["ordinary-observation"]
                       for call in result["calls"])
            assert result["pending_enabled"] is (case.profile is None)
            assert result["read_pool"] is (case.pool == "on")
            if case.pool == "on":
                assert result["pool_rows"] == ["ordinary-observation"] and result["pool_closed"]
        if case.profile:
            assert result["denied"]["isError"] and "unavailable to this external client profile" in result["denied"]["content"][0]["text"]


@pytest.mark.parametrize("service", ["health", "memory"])
async def test_prepared_consumer_preserves_missing_db_behavior(seeded_install, tmp_path, service):
    oracle, prepared = await consume(seeded_install, tmp_path, Case(service, missing=True))
    assert oracle["missing"] == prepared["missing"] == (service == "memory")
    if service == "health":
        assert oracle["catalog"] == prepared["catalog"]
        assert oracle["runner_calls"] == 1 and prepared["runner_calls"] == 0
        assert oracle["events"] == prepared["events"] == ["enter", "exit"]
        assert all(json.loads(row["content"][0]["text"])["queues"] == {"fixture": 1}
                   for row in prepared["calls"])
    else:
        assert oracle["runner_calls"] == prepared["runner_calls"] == 0
        assert oracle["events"] == prepared["events"] == []
