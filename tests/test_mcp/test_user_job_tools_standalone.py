"""The user-job tools must be USABLE in the standalone MCP, not merely present.

``mcp/health/__init__.py`` importing a module is what registers its tools — that
is the trap the tool-registration coverage guard exists for. But importing is
only half: these four tools read module-level ``_db``/``_scheduler`` that a
separate ``init_user_job_tools`` call injects, and the only caller used to be
``runtime/init/user_jobs.py``, which runs in the SERVER process. So adding the
import made four tools appear in the standalone MCP and answer
"Database not initialized" to everything — present and broken rather than
absent, which is a different failure, not a fixed one.

The standalone server now wires them DB-only, the same split
``init_campaign_tools(runner=None, db=db)`` already uses — and that is all
four tools, not two: the DB is the shared channel to the server's scheduler,
which reconciles the table into APScheduler on an interval. ``run_now``
stamps ``run_requested_at`` and the server dispatches and clears it.

Note the alternative pattern, so a future author picks deliberately rather than
by accident: ``task_tools`` takes no injection at all in this process — it falls
back to opening its own connection (``db = _db`` then ``if db is None: db =
await _get_db()``). Either shape is fine. A hard ``return {"error": ...}`` with
no fallback and no wiring is the one that is not, and
``test_every_init_backed_tool_module_is_wired_or_self_sufficient`` keeps that a
property of the code rather than of whoever last remembered.

SCHEMA, because the first version of this file got it wrong: the fixture builds
``user_jobs`` from the PRODUCTION DDL and inserts a row. Hand-rolling the table
and asserting on an EMPTY result proved only that the query ran — the row
mapping never executes at ``count == 0``, so a fixture missing the required
``status`` column passed while the same code raised ``KeyError`` on any real row.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _standalone_server_source() -> str:
    import genesis

    path = Path(genesis.__file__).resolve().parents[2] / "scripts" / "genesis_mcp_server.py"
    assert path.exists(), path
    return path.read_text(encoding="utf-8")


def test_the_standalone_server_wires_user_job_tools_with_a_db():
    """A deletion guard, and it checks the ARGUMENT as well as the call.

    Asserting only that the name appears would pass against
    ``init_user_job_tools(db=None, scheduler=None)`` — which is exactly the
    broken state this fix exists to leave. It does not run the lifespan; the
    behavioural tests below cover what the call achieves.
    """
    tree = ast.parse(_standalone_server_source())
    wired = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "init_user_job_tools"
    ]
    assert wired, (
        "the standalone MCP registers the user-job tools but never initialises "
        "them, so all four answer 'Database not initialized'"
    )
    db_args = [kw.value for call in wired for kw in call.keywords if kw.arg == "db"]
    assert db_args, "init_user_job_tools is called without a db= argument"
    assert not any(isinstance(v, ast.Constant) and v.value is None for v in db_args), (
        "init_user_job_tools is wired with db=None, which is the broken state"
    )


def test_every_init_backed_tool_module_is_wired_or_self_sufficient():
    """The rule, mechanised — because the hand-maintained version is the bug.

    This PR added a guard proving every tool module is IMPORTED, after measuring
    that a missing import left 4 of 83 tools dead. The init layer has the same
    shape and the same silence: a module whose tools need injection, registered
    in a process that never injects, ships tools that answer "not initialized".

    Two shapes are acceptable and this accepts either — wired in the standalone
    lifespan, or self-sufficient via a ``_get_db`` fallback. What it refuses is
    the third: an ``init_*`` nobody calls, in a module that cannot open its own
    connection.
    """
    import genesis

    health = Path(genesis.__file__).parent / "mcp" / "health"
    server_calls = {
        node.func.id
        for node in ast.walk(ast.parse(_standalone_server_source()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    unwired: list[str] = []
    for path in sorted(health.glob("*.py")):
        if path.name == "__init__.py":
            continue
        src = path.read_text(encoding="utf-8")
        inits = [
            node.name
            for node in ast.parse(src).body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("init_")
        ]
        if not inits:
            continue
        self_sufficient = "_get_db()" in src
        if not self_sufficient and not any(i in server_calls for i in inits):
            unwired.append(f"{path.name} ({', '.join(inits)})")

    assert not unwired, (
        "these tool modules define an init the standalone MCP never calls, and "
        "cannot open their own DB — their tools will register and then refuse "
        f"every call: {unwired}"
    )


@pytest.mark.asyncio
async def test_db_backed_tools_work_against_the_PRODUCTION_schema(tmp_path):
    """The half the DB-only wiring is FOR, exercised with a real row.

    The row matters: ``user_job_list`` reads ``j["status"]`` as a required key,
    so an empty result set cannot tell a correct projection from one that raises
    on the first record.

    CHARACTERIZATION, not regression — and labelled so nobody reads it as the
    latter. It wires the module itself, so it passes against the unfixed code
    too (MEASURED). What it establishes is that DB-only wiring is worth doing at
    all; that the standalone server actually does it is the AST test above.
    """
    import aiosqlite

    from genesis.db.crud import user_jobs as crud
    from genesis.db.schema import TABLES
    from genesis.mcp.health import user_job_tools as t

    db = await aiosqlite.connect(tmp_path / "t.db")
    db.row_factory = aiosqlite.Row
    await db.execute(TABLES["user_jobs"])
    await db.execute(TABLES["user_job_runs"])
    await db.commit()
    try:
        t.init_user_job_tools(db=db, scheduler=None)
        job_id = await crud.create_job(
            db,
            title="Weekly check",
            cron_expression="0 2 * * 0",
            dispatch_prompt="do the thing",
        )

        listed = await t.user_job_list.fn()
        assert listed.get("success") is True, listed
        assert listed["count"] == 1
        assert listed["jobs"][0]["status"] == "active"

        history = await t.user_job_history.fn(job_id=job_id)
        assert history.get("success") is True, history
    finally:
        t.init_user_job_tools(db=None, scheduler=None)
        await db.close()


@pytest.mark.asyncio
async def test_with_no_db_the_message_does_not_advertise_db_backed_tools():
    """The no-DB branch of the standalone server takes this path.

    ``user_job_control`` checked the scheduler FIRST, so with neither wired it
    returned the scheduler message — which ends "user_job_list and
    user_job_history work here" and is false in exactly that state.
    """
    from genesis.mcp.health import user_job_tools as t

    t.init_user_job_tools(db=None, scheduler=None)
    try:
        created = await t.user_job_create.fn(
            title="x", cron_expression="0 2 * * 0", dispatch_prompt="p"
        )
        assert "Database not initialized" in created["error"], created

        control = await t.user_job_control.fn(job_id="x", action="pause")
        assert "Database not initialized" in control["error"], (
            "with no DB wired, user_job_control must not advertise the two "
            f"DB-backed tools as working: {control}"
        )
    finally:
        t.init_user_job_tools(db=None, scheduler=None)


@pytest.mark.asyncio
async def test_with_a_db_but_no_scheduler_mutations_still_work(tmp_path):
    """The state the standalone MCP actually runs in — and it is not dead.

    Scheduler-less mutations write the DB; the server-side scheduler's
    reconcile loop applies them. ``run_now`` stamps ``run_requested_at``
    rather than erroring — the server dispatches it on its next tick.
    """
    import aiosqlite

    from genesis.db.crud import user_jobs as crud
    from genesis.db.schema import TABLES
    from genesis.mcp.health import user_job_tools as t

    db = await aiosqlite.connect(tmp_path / "t.db")
    db.row_factory = aiosqlite.Row
    await db.execute(TABLES["user_jobs"])
    await db.execute(TABLES["user_job_runs"])
    await db.commit()
    try:
        t.init_user_job_tools(db=db, scheduler=None)

        created = await t.user_job_create.fn(
            title="x", cron_expression="0 2 * * 0", dispatch_prompt="p"
        )
        assert created.get("success") is True, created
        job_id = created["job_id"]

        paused = await t.user_job_control.fn(job_id=job_id, action="pause")
        assert paused == {"success": True, "action": "paused", "job_id": job_id}, paused
        job = await crud.get_job(db, job_id)
        assert job["status"] == "paused"

        run = await t.user_job_control.fn(job_id=job_id, action="run_now")
        assert run["success"] is True and run["action"] == "queued", run
        job = await crud.get_job(db, job_id)
        assert job["run_requested_at"] is not None

        deleted = await t.user_job_control.fn(job_id=job_id, action="delete")
        assert deleted["success"] is True, deleted
        assert await crud.get_job(db, job_id) is None
    finally:
        t.init_user_job_tools(db=None, scheduler=None)
        await db.close()
