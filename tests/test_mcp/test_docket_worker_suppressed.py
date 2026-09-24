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
