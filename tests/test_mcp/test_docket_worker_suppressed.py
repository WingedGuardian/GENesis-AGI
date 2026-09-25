"""The MCP servers must not start FastMCP's docket task-queue worker.

MEASURED 2026-09-24, this install: 41 live `genesis_mcp_server.py` processes
burned **1.37 CPU cores continuously while idle**, 41/41 of them active and
uniform across all five server types. None of it is Genesis code — the wrapper
has no loop, no sleep, no timer, and creates zero background tasks.

The chain, read in fastmcp 2.14.6:

* `FastMCP.run_async` enters `self._docket_lifespan()` as a SIBLING of the user
  lifespan in one `async with` (server.py:572-575), so Genesis assigning
  `mcp._lifespan` cannot displace it.
* Its only escape hatch is `if self._is_mounted: yield; return` (server.py:403).
  Genesis's servers are top-level, so it never fires: a `Docket(url="memory://")`
  is built and `asyncio.create_task(worker.run_forever())` runs (server.py:476).
* `memory://` resolves to **fakeredis**, whose pubsub read is a literal
  `await asyncio.sleep(0.01)` poll loop — its own comment calls it "kludge it
  with a sleep/poll loop" (fakeredis/aioredis.py:143-154). That 100 Hz spin is
  the dominant cost, joined by a 250 ms Lua EVAL and a 2 s heartbeat.

**Genesis registers ZERO tools with that worker**, which is what makes the
suppression safe rather than a trade: `tasks` is never passed to `FastMCP`, so
`_support_tasks_by_default` is False, every tool resolves through
`TaskConfig.from_bool(False)` to ``mode="forbidden"``, and the registration loop
(server.py:418-423) skips every forbidden tool. The worker polls an EMPTY queue
forever. Suppressing it removes no capability.

Upstream confirms both the mechanism and the direction: prefecthq/fastmcp#2887,
where the maintainer writes that this is "docket starting aggressively which has
been removed as a default in 3.0". This is a backport of upstream's own decision,
not a fight with the library — and it must be DELETED when Genesis moves to
fastmcp 3.x/4.x.

`_is_mounted` is a private attribute, so the tests below are the thing standing
between a fastmcp bump and a silent return of the burn. Three of them exist only
because a first draft of this module asserted its claims against a SYNTHETIC
one-tool server it built itself — which could not observe anything about the real
servers, while a docstring claimed it did. Assert against the real thing or do not
make the claim.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import fastmcp
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.tasks.config import TaskConfig
from mcp.shared.exceptions import McpError

from tests.conftest import private_module

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_SCRIPT = _REPO_ROOT / "scripts" / "genesis_mcp_server.py"

#: The five servers `.mcp.json` actually spawns, by import path. Each exposes a
#: module-level ``mcp``. Verified 2026-09-24: 81 + 35 + 13 + 11 + 3 = 143 tools.
_REAL_SERVER_MODULES = (
    "genesis.mcp.health",
    "genesis.mcp.memory",
    "genesis.mcp.outreach_mcp",
    "genesis.mcp.recon_mcp",
    "genesis.mcp.discord_bot_mcp",
)


def _server_module():
    """Load the wrapper script without leaking its name to the whole session."""
    return private_module("_genesis_mcp_server_under_test", _SERVER_SCRIPT)


def _tool_server(name: str) -> FastMCP:
    """A bare FastMCP with one tool — used ONLY where a real server is overkill.

    Deliberately NOT used for the premise test: a synthetic server can only tell
    you about fastmcp's constructor defaults, never about Genesis's servers.
    """
    mcp = FastMCP(name)

    @mcp.tool
    async def echo(value: int) -> int:
        """Return the value unchanged."""
        return value

    return mcp


@pytest.mark.asyncio
async def test_suppress_docket_worker_stops_the_worker_starting():
    """The invariant, driven through the REAL function rather than a hand-assign.

    Calling `_suppress_docket_worker` (not `mcp._is_mounted = True`) is what makes
    this a test of the fix instead of a test of fastmcp — otherwise the only thing
    connecting the two files is the string `_is_mounted` appearing in both.
    """
    module = _server_module()
    mcp = _tool_server("suppressed")

    module._suppress_docket_worker(mcp)

    async with Client(mcp) as client:
        assert mcp._docket is None, (
            "a Docket was constructed despite the suppression — fastmcp's gate "
            "at server.py:403 no longer works the way this fix assumes"
        )
        assert mcp._worker is None, "a docket Worker was started despite the suppression"
        # The suppression must not cost tool dispatch.
        result = await client.call_tool("echo", {"value": 42})
        assert result.content[0].text == "42"


@pytest.mark.asyncio
async def test_control_docket_does_start_without_the_suppression():
    """NO-OP ARM. Without the call, a Docket IS built.

    If this ever fails, the mechanism being suppressed has gone away upstream and
    the sibling test above is passing for the wrong reason. That is a prompt to
    DELETE this fix, not to relax the assertion.
    """
    mcp = _tool_server("control")

    async with Client(mcp):
        assert mcp._docket is not None, (
            "fastmcp no longer starts a Docket by default — the idle-burn fix "
            "this module pins is now redundant and should be removed along with "
            "these tests (see prefecthq/fastmcp#2887: removed as a default in 3.0)"
        )


@pytest.mark.parametrize("module_path", _REAL_SERVER_MODULES)
def test_no_real_genesis_tool_opts_into_background_tasks(module_path):
    """The PREMISE: suppression is free only because NOTHING is registered.

    Asserted against the REAL servers. A first draft asserted it against a
    synthetic FastMCP, which meant adding ``tasks=True`` to a real server — the
    exact change that would break the suppression — left the suite green while
    this module's docstring claimed otherwise.
    """
    import importlib

    server = importlib.import_module(module_path).mcp

    assert server._support_tasks_by_default is False, (
        f"{module_path} now enables background tasks by default; the docket "
        "suppression in _run_mcp would silently disable them. Reconsider the fix "
        "(see _suppress_docket_worker) before shipping this."
    )
    # Guard the guard: an import that registered nothing passes an empty loop.
    assert server._tool_manager._tools, f"{module_path} registered no tools — test is vacuous"
    assert TaskConfig.from_bool(False).mode == "forbidden"

    for tool in server._tool_manager._tools.values():
        assert tool.task_config.mode == "forbidden", (
            f"{module_path}:{tool.key} opts into background tasks, which the "
            "docket suppression would break"
        )

    # TOOLS ARE NOT THE ONLY THING DOCKET COVERS. fastmcp's registration loop
    # (server.py:418-453) walks five registries, and this test inspects one.
    # MEASURED 2026-09-25 across all five Genesis servers: the other four are
    # EMPTY, which is why checking only tools is sufficient TODAY and why
    # `_support_tasks_by_default is False` above carries the default case.
    #
    # This is a tripwire rather than a check: it deliberately does not guess at a
    # task-config API for prompts and resources that nothing here exercises. It
    # fires the moment the premise gets wider than the thing being verified, so
    # the next author extends the inventory instead of inheriting a silent gap.
    for registry, holder, attr in (
        ("prompts", server._prompt_manager, "_prompts"),
        ("resources", server._resource_manager, "_resources"),
        ("templates", server._resource_manager, "_templates"),
        ("mounted servers", server, "_mounted_servers"),
    ):
        assert not getattr(holder, attr, None), (
            f"{module_path} now registers {registry}, which fastmcp's task "
            "registration also covers but this inventory does not inspect. The "
            "docket suppression's premise is 'nothing opts into background "
            f"tasks' — extend the check to {registry} before trusting it again."
        )


def _modules_imported_by_the_real_servers() -> set[str]:
    """What `sys.modules` holds after importing the five servers AND NOTHING ELSE.

    Asked in a FRESH INTERPRETER on purpose. Reading `sys.modules` from inside the
    pytest process answers a different question — it reflects everything the whole
    suite has imported — and the difference is not academic: MEASURED 2026-09-25,
    a single `import genesis.mcp.health.user_job_tools` before this test (which is
    exactly what `genesis/runtime/init/user_jobs.py:21` does during runtime
    bootstrap) makes the allowance below look stale and FAILS the test, for a
    reason that has nothing to do with the property under test.

    So the subprocess is not belt-and-braces; it is the only way to ask "does a
    SERVER import this?" rather than "has ANYTHING in this session imported it?".
    """
    import json
    import subprocess
    import sys
    import textwrap

    probe = textwrap.dedent(
        f"""
        import importlib, json, sys
        for name in {list(_REAL_SERVER_MODULES)!r}:
            importlib.import_module(name)
        print(json.dumps(sorted(m for m in sys.modules if m.startswith("genesis."))))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=600,
    )
    assert result.returncode == 0, (
        f"the import probe failed, so this test proved nothing:\n{result.stderr[-2000:]}"
    )
    return set(json.loads(result.stdout))


#: Modules that carry ``@mcp.tool`` but are deliberately NOT imported when the
#: five server modules are. Each entry is a hole in the inventory below, so each
#: needs a reason, and a SIXTH one fails the test rather than joining the list
#: silently.
_TOOL_MODULES_NOT_SERVED_BY_MCP = {
    # 4 tools bound to the same `genesis.mcp.health.mcp` object as everything
    # else, but reached only through `genesis/runtime/init/user_jobs.py:21` —
    # the in-process runtime, not the standalone MCP child that `_run_mcp`
    # starts. So in the process the docket suppression actually runs in, this
    # module is never imported and its tools never register. MEASURED
    # 2026-09-25: importing it takes the health server from 81 tools to 85, and
    # all 4 are `forbidden`, so it is a gap in what the inventory can SEE rather
    # than a live break. If user-job tools are ever wired into standalone MCP
    # the way direct_session_tools already were, delete this entry — the
    # inventory will then cover them on its own.
    "genesis.mcp.health.user_job_tools",
}


def test_every_tool_registering_module_is_visible_to_the_inventory():
    """The inventory above is a complete premise only if nothing registers later.

    Raised in review: the per-module inventory runs on a freshly imported server,
    so a tool registered LATER — during a lifespan — is never inspected, and could
    opt into background tasks while the docket suppression silently disabled it.
    The review named two modules the health lifespan imports, direct_session_tools
    and campaign_tools.

    Those two do not hold. What the lifespan imports from them is
    ``init_direct_session_tools`` and ``init_campaign_tools``
    (scripts/genesis_mcp_server.py:181 and :196) — WIRING functions handing a db
    handle to tools that already exist — and both modules are imported at
    genesis/mcp/health/__init__.py:74 and :68, so by the time any lifespan runs
    they are in sys.modules and no decorator re-runs. MEASURED 2026-09-25: 81
    tools before the health lifespan, 81 after.

    But answering it with a check on THOSE TWO NAMES would be a denylist built
    from the two instances a reviewer happened to think of, and an adversarial
    audit found a third — ``user_job_tools`` — that walked straight through such
    a check while both guards stayed green. So this is written the other way
    round: DERIVE every module that registers tools, and require each to be
    imported by a server module. A module added next year is covered by
    construction, which is the property a denylist cannot have.

    Enumeration bound, stated rather than implied: the scan finds the
    ``@mcp.tool`` decorator spelling. An aliased decorator, a bare
    ``mcp.add_tool(...)`` call, or a mounted sub-server would not be found —
    a repo-wide search for those returned zero at the time of writing, but
    "zero found" is not "impossible", so treat this as the spellings checked.
    """
    import re

    imported = _modules_imported_by_the_real_servers()

    mcp_src = _REPO_ROOT / "src" / "genesis" / "mcp"
    decorator = re.compile(r"^\s*@mcp\.tool\b", re.MULTILINE)

    registering: set[str] = set()
    for path in sorted(mcp_src.rglob("*.py")):
        if not decorator.search(path.read_text(encoding="utf-8", errors="ignore")):
            continue
        rel = path.relative_to(_REPO_ROOT / "src")
        registering.add(str(rel.with_suffix("")).replace("/", ".").removesuffix(".__init__"))

    # Guard the guard: a broken scan finds nothing and passes an empty loop.
    assert len(registering) > 20, (
        f"the decorator scan found only {len(registering)} tool-registering modules, "
        "which is far below the known population — the scan is broken, not the code"
    )

    unimported = sorted(m for m in registering if m not in imported)
    unexpected = sorted(set(unimported) - _TOOL_MODULES_NOT_SERVED_BY_MCP)

    assert not unexpected, (
        f"{unexpected} carry @mcp.tool decorators but are NOT imported when the five "
        "server modules are. If any runtime path imports one, its tools register "
        "where test_no_real_genesis_tool_opts_into_background_tasks cannot see them, "
        "and the docket suppression in _run_mcp no longer has a complete premise. "
        "Either import it from the server module that owns it, or add it to "
        "_TOOL_MODULES_NOT_SERVED_BY_MCP with the reason it can never reach a "
        "standalone MCP process."
    )

    # The allowance is not a free pass: an entry that has since become imported
    # is stale, and leaving it would quietly widen the exemption next time.
    # Reliable only because `imported` came from a fresh interpreter — against
    # this process's own sys.modules it would fire on unrelated test order.
    stale = sorted(_TOOL_MODULES_NOT_SERVED_BY_MCP - set(unimported))
    assert not stale, (
        f"{stale} are now imported by a server module, so the inventory covers them "
        "— remove them from _TOOL_MODULES_NOT_SERVED_BY_MCP"
    )


def test_is_mounted_is_still_read_only_by_the_docket_gate():
    """The whole safety argument is 'only the docket lifespan reads this flag'.

    Re-derived from the INSTALLED fastmcp rather than trusted from a comment, so a
    2.x patch that broadens the attribute's meaning fails HERE instead of changing
    Genesis's behaviour silently. The pin is ``>=2.0,<3.0``, which is a wide band
    to be relying on a private attribute in.
    """
    source = Path(fastmcp.server.server.__file__).read_text()
    reads = [
        line.strip()
        for line in source.splitlines()
        if "self._is_mounted" in line and "=" not in line
    ]
    assert len(reads) == 1 and reads[0].startswith("if self._is_mounted:"), (
        f"fastmcp {fastmcp.__version__} reads _is_mounted in {len(reads)} place(s): "
        f"{reads} — setting it may no longer be docket-only. Re-audit "
        "_suppress_docket_worker before trusting the suppression."
    )


def test_suppression_warns_instead_of_silently_doing_nothing(caplog):
    """Version drift must be LOUD at runtime, not only in CI.

    ``FastMCP`` defines no ``__slots__``, so assigning ``_is_mounted`` SUCCEEDS on
    a version that removed it — creating a dead attribute while ~1.4 cores of burn
    quietly return. The guard therefore checks the attribute EXISTS first. This
    test is the one that fails if someone "simplifies" it back to a bare assign.
    """
    module = _server_module()

    class _NoSuchAttribute:
        """Stands in for a future fastmcp that renamed the flag."""

    target = _NoSuchAttribute()
    with caplog.at_level("WARNING"):
        module._suppress_docket_worker(target)

    assert not hasattr(target, "_is_mounted"), (
        "the suppression blind-assigned a private attribute that does not exist "
        "on this object — on a renamed fastmcp that is a silent no-op"
    )
    assert any(
        "_is_mounted" in r.message or "_is_mounted" in r.getMessage() for r in caplog.records
    ), (
        "no warning was emitted when the attribute was absent, so a fastmcp "
        "rename would restore the idle burn with no runtime signal at all"
    )


def test_every_bootstrapper_routes_through_the_run_mcp_chokepoint():
    """The suppression sits in `_run_mcp` because ALL servers route through it.

    Asserted rather than claimed in prose: a sixth bootstrapper added later that
    calls `mcp.run()` directly would bypass the suppression entirely, and nothing
    else in this module would notice.
    """
    module = _server_module()
    bootstrappers = module._BOOTSTRAPPERS

    assert set(bootstrappers) == {"health", "memory", "outreach", "recon", "discord-bot"}, (
        f"the server set changed ({sorted(bootstrappers)}) — confirm the new one "
        "routes through _run_mcp and add it to _REAL_SERVER_MODULES"
    )

    for name, fn in bootstrappers.items():
        source = inspect.getsource(fn)
        assert "_run_mcp(" in source, (
            f"bootstrapper {name!r} does not call _run_mcp — it bypasses the "
            "docket suppression and will idle-spin"
        )
        assert ".run(" not in source.replace("_run_mcp(", ""), (
            f"bootstrapper {name!r} calls .run() directly, bypassing the _run_mcp chokepoint"
        )


def test_run_mcp_suppresses_before_handing_off_to_run():
    """Ordering matters: the lifespan is entered INSIDE run(), so setting the flag
    afterwards would be too late."""
    module = _server_module()

    class _StubServer:
        def __init__(self) -> None:
            self._is_mounted = False
            self.run_called_with: dict | None = None
            self.mounted_at_run_time: bool | None = None

        def run(self, **kwargs) -> None:
            self.mounted_at_run_time = self._is_mounted
            self.run_called_with = kwargs

    stub = _StubServer()
    module._run_mcp(stub, {"transport": "stdio"})

    assert stub.run_called_with == {"transport": "stdio"}, "transport kwargs were altered"
    assert stub.mounted_at_run_time is True, (
        "_run_mcp did not suppress the docket worker before calling run() — "
        "every MCP server will spin at ~4% of a CPU core while idle"
    )


@pytest.mark.asyncio
async def test_task_lookups_error_rather_than_returning_data(caplog):
    """The one reachable protocol delta, pinned as a CONSCIOUS contract.

    Raised on review (Devin, PR #2316): with docket suppressed, the three
    ``tasks/*`` LOOKUP handlers return INTERNAL_ERROR ("Background tasks require
    Docket") where they previously returned INVALID_PARAMS ("Task <id> not found").

    Accepted rather than fixed, and this test is what makes that a decision instead
    of an accident:

    * No task can ever EXIST to be looked up. `server.py:705-721` raises
      METHOD_NOT_FOUND for a `task_config.mode == "forbidden"` tool before docket is
      consulted, and all 143 Genesis tools are forbidden (see the premise test
      above). So every lookup is for an id that cannot exist, in both arms.
    * Both arms ERROR. Neither returns data, and neither reports a task as missing
      when it is present. The difference is which error code an impossible lookup
      carries.
    * The capability advertisement is NOT ours to change:
      `low_level.py:187` sets `capabilities.tasks = get_task_capabilities()`
      unconditionally, so fastmcp over-advertises relative to 143 forbidden tools
      with or without this fix. Suppressing the advertisement would mean a SECOND
      private-API intervention — more surface than the one being justified.

    If a future change makes tasks reachable, `test_no_real_genesis_tool_opts_into_
    background_tasks` fails first and names the server that did it.
    """
    module = _server_module()
    mcp = _tool_server("tasks")
    module._suppress_docket_worker(mcp)

    async with Client(mcp) as client:
        # list_tasks stays functional — measured identical in both arms. It returns
        # a dict ({"tasks": [...], "nextCursor": ...}), so read the key rather than
        # iterating the mapping, which yields the KEY NAMES and would pass/fail for
        # entirely the wrong reason.
        listed = await client.list_tasks()
        assert listed["tasks"] == [], "a task existed on a server where none can be created"

        for label, coro in (
            ("get_task_status", client.get_task_status("missing-123")),
            ("get_task_result", client.get_task_result("missing-123")),
            ("cancel_task", client.cancel_task("missing-123")),
        ):
            # McpError SPECIFICALLY, not bare Exception. A first draft caught
            # Exception and asserted only that one was raised, which would have
            # passed on an ImportError, a typo in this test, or a transport
            # failure — accepting unrelated failures as if they were the
            # behaviour under test (raised in review, PR #2316).
            with pytest.raises(McpError) as excinfo:
                await coro
            # The CONTRACT is "an impossible lookup errors at the PROTOCOL level
            # and returns no data" — deliberately not the specific error code,
            # since pinning that would make this fail on an upstream code change
            # that harms nobody, which is how a test becomes noise.
            assert str(excinfo.value), f"{label} raised an McpError with no message"
