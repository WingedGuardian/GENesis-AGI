"""FastMCP transport smoke tests for the health MCP server.

These tests spin up the real FastMCP server in-process via ``fastmcp.Client``
and round-trip tools through the actual JSON-RPC transport — catching
failures that unit-level ``_impl_*`` tests miss:

1. ``@mcp.tool()`` registration (missing decorator, wrong kwargs)
2. JSON-serializability of return values (accidentally returning a
   ``Path``, ``datetime``, or custom class that breaks over the wire)
3. Tool schema generation (type hints FastMCP can't reflect)
4. Accidental tool deletion (canary sentinel test fires loudly)

**Phase 1 scope:** Canary only. One ``list_tools`` assertion plus a
tiny ``call_tool`` roundtrip on ``health_status``. Validates the
fixture pattern and the result contract.

**Phase 2 scope:** Parameterized matrix across all 14 read-only health
tools. Each case asserts the tool round-trips cleanly without raising
and returns a JSON-serializable payload (``dict`` or ``list`` — FastMCP
wraps ``list[dict]`` return types in Pydantic ``RootModel``).

Note on middleware: we deliberately do NOT call
``init_health_mcp(service, ...)`` here. The smoke test exercises the
raw ``mcp`` object without the ``InstrumentationMiddleware`` attached.
That's the point — we want to verify the pure tool registration and
transport layer, not the runtime-wired stack.

Note on result types: several tools return ``list[dict]``
(``health_errors``, ``health_alerts``, ``settings_list``). FastMCP
wraps those in Pydantic ``RootModel`` instances, so ``result.data`` is
a ``list`` with ``Root()`` entries, not raw dicts. The matrix assertion
accepts both ``dict`` and ``list``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp import Client

from genesis.mcp.health import manifest as _manifest_mod
from genesis.mcp.health import mcp
from genesis.mcp.health import module_ops as _module_ops_mod
from genesis.mcp.health import update_history as _update_history_mod
from genesis.runtime._core import GenesisRuntime

# Sentinel tools that MUST be registered. If any of these disappears,
# something was deleted by accident and we want the canary to shout.
# Picked to span different source files in src/genesis/mcp/health/:
#   - health_status   → status.py
#   - bootstrap_manifest → manifest.py
#   - update_history_recent → update_history.py (this round's addition)
_REQUIRED_TOOLS = frozenset({
    "health_status",
    "bootstrap_manifest",
    "update_history_recent",
})


def _build_fake_adapter() -> SimpleNamespace:
    """Build a minimal external-module adapter stub for the smoke matrix.

    ``_impl_module_list`` walks ``adapter.config.description``,
    ``adapter.enabled``, ``adapter.config.ipc.{method,url}``, and
    ``adapter.list_operations()``. A ``SimpleNamespace`` tree matches
    that shape without dragging in ``ProgramConfig`` /
    ``ExternalProgramAdapter`` or a synthetic YAML on disk.
    """
    return SimpleNamespace(
        config=SimpleNamespace(
            description="smoke fake module (hermetic test fixture)",
            ipc=SimpleNamespace(method="stdio", url="/dev/null"),
        ),
        enabled=False,
        list_operations=lambda: {
            "noop": {"description": "no-op used by smoke matrix"},
        },
    )


@pytest.fixture(autouse=True)
async def _hermetic_health_state(tmp_path, monkeypatch):
    """Hermetic state for the health MCP smoke suite.

    Closes the three non-hermetic gaps the round-2 code review
    surfaced, all in one autouse fixture so no matrix case can slip
    through:

    1. **Production DB access.** ``job_health`` (manifest.py) and
       ``update_history_recent`` (update_history.py) both read
       ``~/genesis/data/genesis.db`` via module-level ``_DB_PATH``
       globals. Point both at a ``tmp_path`` file that does NOT exist
       so the tools take their missing-DB branches and return
       reproducible structured envelopes.

    2. **Runtime singleton leak.** ``_impl_job_health`` /
       ``_impl_bootstrap_manifest`` lazily instantiate
       ``GenesisRuntime._instance``. Without a reset, a prior test
       that populated the singleton would bypass the ``_DB_PATH``
       monkeypatch (the tool would return ``rt.job_health`` from the
       stale runtime instead of reading the patched DB path). Reset
       before each test and again after — belt AND suspenders.

    3. **External module adapter cache + filesystem coupling.**
       ``_impl_module_list`` walks ``module_ops._get_adapters()``
       which lazily loads ``config/modules/*.yaml`` into a module
       global. The cached state leaks across tests AND couples the
       smoke to whatever external modules happen to be checked into
       ``config/modules/``. Reset the cache and replace
       ``_get_adapters`` with a single synthetic fake adapter so the
       matrix exercises the transport layer without depending on
       repo fixtures.
    """
    # 1. Hermetic DB paths
    ghost_db = tmp_path / "smoke_ghost.db"
    monkeypatch.setattr(_manifest_mod, "_DB_PATH", ghost_db)
    monkeypatch.setattr(_update_history_mod, "_DB_PATH", ghost_db)

    # 2. Runtime singleton teardown (before + after). Using ``ashutdown``
    #    rather than sync ``reset`` closes any real DB/task resources the
    #    prior instance owned — see runtime/_core.py docstring. Safe as a
    #    no-op when nothing was bootstrapped.
    await GenesisRuntime.ashutdown()

    # 3. Hermetic module adapter cache
    _module_ops_mod._reset_adapter_cache()
    fake_adapters = {"smoke_fake_module": _build_fake_adapter()}
    monkeypatch.setattr(
        _module_ops_mod, "_get_adapters", lambda: fake_adapters,
    )

    yield

    # Belt-and-suspenders cleanup. ``monkeypatch`` handles the
    # attribute restoration automatically; these two calls cover
    # state that lives outside the monkeypatch surface.
    await GenesisRuntime.ashutdown()
    _module_ops_mod._reset_adapter_cache()


@pytest.fixture()
async def client():
    """In-process FastMCP client wired directly to the health server object."""
    async with Client(mcp) as c:
        yield c


class TestCanary:
    """Phase 1 canary: proves the fixture pattern + result contract."""

    @pytest.mark.asyncio
    async def test_list_tools_non_empty_and_contains_sentinels(
        self, client,
    ) -> None:
        """list_tools must return a non-empty set containing the sentinels.

        Guards against:
        - FastMCP Client fixture pattern breaking (returns 0 tools)
        - Accidental decorator deletion on any sentinel tool
        - Submodule import chain breaking (health package load silently
          drops a submodule, so its @mcp.tool() decorators never fire)
        """
        tools = await client.list_tools()

        assert len(tools) > 0, (
            "list_tools returned zero tools — the FastMCP Client fixture "
            "is not wired to the real server, or no submodule's "
            "@mcp.tool() decorators ran."
        )

        names = {t.name for t in tools}
        missing = _REQUIRED_TOOLS - names
        assert not missing, (
            f"Required health tools missing from registration: {missing}. "
            f"Got {len(names)} tools: {sorted(names)}"
        )

    @pytest.mark.asyncio
    async def test_health_status_roundtrip(self, client) -> None:
        """call_tool roundtrip returns a parsed dict, not an error envelope.

        ``health_status`` is the safest canary target: no runtime deps,
        no DB, no side effects. In standalone mode (no HealthDataService
        wired) it returns ``{"status": "unavailable", ...}`` — still a
        valid dict, still non-error from the transport's perspective.

        Guards against:
        - Tool registration lookup failure (transport returns tool-not-found)
        - Return value not JSON-serializable (transport raises serialization error)
        - ``is_error`` set unexpectedly (bug in the tool wrapper)
        - Empty ``data`` payload (schema generation dropped the return type)
        """
        result = await client.call_tool("health_status", {})

        assert result.is_error is False, (
            f"health_status returned is_error=True: content={result.content}"
        )
        assert result.data is not None, (
            "health_status returned data=None — return type was likely "
            "dropped by schema generation"
        )
        assert isinstance(result.data, dict), (
            f"health_status returned {type(result.data).__name__}, "
            f"expected dict. data={result.data!r}"
        )
        # The dict must have *some* content — a stand-alone status tool
        # returning {} would signal a regression (silent empty). In
        # standalone mode we expect {"status": "unavailable", ...}; in
        # runtime-wired mode we expect the full snapshot.
        assert len(result.data) > 0, (
            "health_status returned an empty dict — violates 'never hide "
            "broken things' rule"
        )


# Phase 2 matrix: (tool_name, args, expected_type).
# ``expected_type`` is a tuple passed to ``isinstance`` so tools that
# return ``list[dict]`` (wrapped in Pydantic RootModel by FastMCP) can
# be covered alongside dict-returning tools without special-casing.
# Tools intentionally exercised with "safe" args that do not mutate
# state: read-only probes only, no writes, no task submissions.
_MATRIX_CASES: list[tuple[str, dict, tuple]] = [
    ("health_status", {}, (dict,)),
    ("health_errors", {}, (dict, list)),
    ("health_alerts", {}, (dict, list)),
    ("bootstrap_manifest", {}, (dict,)),
    ("subsystem_heartbeats", {}, (dict,)),
    ("job_health", {}, (dict,)),
    # provider_activity returns ``list[dict]`` when a tracker is wired
    # (empty provider string → summary of all providers). In standalone
    # mode the tool returns ``{"status": "unavailable", ...}``. Accept
    # both so this case doesn't break the first time someone runs the
    # smoke suite with a wired runtime.
    ("provider_activity", {"provider": ""}, (dict, list)),
    ("module_list", {}, (dict,)),
    ("db_schema", {"table": ""}, (dict,)),
    ("settings_list", {}, (dict, list)),
    ("settings_get", {"domain": "updates"}, (dict,)),
    ("task_list", {"include_completed": False}, (dict,)),
    ("update_history_recent", {}, (dict,)),
    ("task_detail", {"task_id": "nonexistent-id-smoke"}, (dict,)),
]


class TestTransportMatrix:
    """Phase 2: every read-only health tool round-trips cleanly.

    Each case asserts that the tool:
    1. Is registered (``call_tool`` does not raise tool-not-found)
    2. Returns a non-error result (``is_error is False``)
    3. Produces a non-None, JSON-serializable payload
    4. Returns the expected container type (dict, or list for
       tools whose return type is ``list[dict]``)
    5. Returns a non-empty payload — silent empty violates the
       "never hide broken things" rule; standalone-mode tools
       should return structured error envelopes, not ``{}``.

    These are not unit tests — the ``_impl_*`` functions have their
    own coverage. This class is the transport-layer smoke net.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool_name", "args", "expected_type"),
        _MATRIX_CASES,
        ids=[case[0] for case in _MATRIX_CASES],
    )
    async def test_tool_roundtrip(
        self, client, tool_name: str, args: dict, expected_type: tuple,
    ) -> None:
        result = await client.call_tool(tool_name, args)

        assert result.is_error is False, (
            f"{tool_name} returned is_error=True: content={result.content}"
        )
        assert result.data is not None, (
            f"{tool_name} returned data=None — return type was likely "
            "dropped by schema generation"
        )
        assert isinstance(result.data, expected_type), (
            f"{tool_name} returned {type(result.data).__name__}, "
            f"expected one of {[t.__name__ for t in expected_type]}. "
            f"data={result.data!r}"
        )
        # Non-empty invariant. ``dict`` and ``list`` both support ``len()``.
        assert len(result.data) > 0, (
            f"{tool_name} returned an empty {type(result.data).__name__} "
            "— violates 'never hide broken things' rule. Standalone-mode "
            "tools should return a structured error envelope, not empty."
        )
