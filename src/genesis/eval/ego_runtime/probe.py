"""Disposable canonical CRUD probes. No runtime, provider or approval simulator."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

import aiosqlite

from genesis.db.crud import ego
from genesis.db.schema import TABLES
from genesis.eval.ego_runtime.cases import CASES, Case, Step
from genesis.eval.qualification.evidence import canonical, digest

SCHEMA = "ego-policy-probe/v1"
PROPOSAL = "synthetic-proposal"
EFFECT = canonical({"fixture": "claim-gated-effect", "proposal_id": PROPOSAL})
LIMITATIONS = [
    "Canonical CRUD baseline only: no candidate runtime or model was executed.",
    "Sequential order forces two race outcomes; this does not test concurrent scheduling.",
    "Clean connection reopen is not a process-crash or power-loss durability test.",
    "Artifacts are claim-gated fixture effects, not production worker outcomes.",
    "This does not qualify feedback learning, generation fencing or the full ego lifecycle.",
    "Source hashes identify inspected files, not authenticated loaded bytecode.",
]


async def operate(db, step: Step):
    if step.action in {"approve", "reject"}:
        return await ego.resolve_proposal(
            db, PROPOSAL, status="approved" if step.action == "approve" else "rejected",
            expected_revision=step.argument_revision,
        )
    if step.action == "revoke":
        return await ego.revoke_proposal(db, PROPOSAL)
    if step.action == "claim":
        return await ego.claim_proposal_for_dispatch(db, PROPOSAL, allow_develop=False)
    if step.action == "revise":
        return await ego.revise_proposal(
            db, PROPOSAL, expected_revision=step.argument_revision,
            content="revised synthetic proposal", revised_by="fixture",
        )
    raise ValueError("unknown probe operation")


async def snapshot(path):
    """A separate read-only connection sees only committed canonical state."""
    async with (
        aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as reader,
        reader.execute(
            "SELECT status, revision_num, scope, ego_source FROM ego_proposals WHERE id = ?",
            (PROPOSAL,),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    if row is None:
        raise ValueError("fixture proposal disappeared")
    return dict(zip(("status", "revision", "scope", "ego_source"), row, strict=True))


def artifacts(root):
    files = sorted(root.glob("effect-*.json"))
    return [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in files]


async def run_case(case: Case, root: Path, *, reopen: bool):
    root.mkdir()
    path = root / "fixture.sqlite3"
    writer = await aiosqlite.connect(path)
    writer.row_factory = aiosqlite.Row
    rows = []
    try:
        for name in ("ego_proposals", "ego_proposal_revisions"):
            await writer.execute(TABLES[name])
        await ego.create_proposal(
            writer, id=PROPOSAL, action_type="investigate", content="synthetic proposal",
            ego_source="genesis_ego_cycle", scope=case.scope, scope_revision=1,
        )
        for index, step in enumerate(case.steps, 1):
            if reopen:
                await writer.close()
                writer = await aiosqlite.connect(path)
                writer.row_factory = aiosqlite.Row
            result = await operate(writer, step)
            state = await snapshot(path)
            if step.action == "claim" and result is True:
                (root / f"effect-{index}.json").write_bytes(EFFECT)
            expected = {"status": step.status, "revision": step.revision,
                        "scope": case.scope, "ego_source": "genesis_ego_cycle"}
            rows.append({
                "step": index, "action": step.action, "argument_revision": step.argument_revision,
                "observed_result": result, "expected_result": step.result,
                "observed_state": state, "expected_state": expected,
                "passed": type(result) is type(step.result) and result == step.result and state == expected,
            })
    finally:
        await writer.close()
    observed_artifacts = artifacts(root)
    expected_hash = hashlib.sha256(EFFECT).hexdigest()
    effects_match = (len(observed_artifacts) == case.effects
                     and all(item["sha256"] == expected_hash for item in observed_artifacts))
    return {
        "case": case.name, "writer_mode": "clean-reopen" if reopen else "continuous",
        "steps": rows, "expected_effects": case.effects, "artifacts": observed_artifacts,
        "artifact_check": effects_match,
        "status": "pass" if all(row["passed"] for row in rows) and effects_match else "fail",
    }


def sources():
    paths = {"crud": Path(inspect.getfile(ego)), "probe": Path(__file__),
             "cases": Path(__file__).with_name("cases.py")}
    return {**{name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()},
            "canonical_ddl": digest({name: TABLES[name] for name in ("ego_proposals", "ego_proposal_revisions")})}


async def run(temp_root: Path):
    """Create only fresh owned fixture children; delete them after closing DBs."""
    before = sources()
    with TemporaryDirectory(prefix="ego-policy-", dir=temp_root) as directory:
        root = Path(directory).resolve()
        results = []
        for index, case in enumerate(CASES):
            for reopen in (False, True):
                results.append(await run_case(case, root / f"case-{index}-{int(reopen)}", reopen=reopen))
        if sources() != before:
            raise ValueError("probe sources changed during execution")
    return {
        "schema": SCHEMA, "scope": "canonical-proposal-operations",
        "status": "pass" if all(row["status"] == "pass" for row in results) else "fail",
        "passed": sum(row["status"] == "pass" for row in results), "total": len(results),
        "matrix_hash": digest([asdict(case) for case in CASES]), "sources": before,
        "cleanup_confirmed": True, "cases": results, "limitations": LIMITATIONS,
    }
