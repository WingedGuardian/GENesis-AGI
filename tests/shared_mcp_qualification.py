"""Import gate for real exact-stack protocol suites, before SDK imports.

Ordinary installs can use later SDKs for legacy paths. The dedicated job sets
strict mode so missing qualification is an error rather than skipped coverage.
"""

import os

import pytest

from genesis.mcp.shared_http._settings import require_qualified_stack

_mode = os.environ.get("GENESIS_REQUIRE_SHARED_MCP_QUALIFICATION", "0")
if _mode not in {"0", "1"}:
    raise RuntimeError("Invalid shared MCP test qualification mode")
try:
    require_qualified_stack()
except RuntimeError as error:
    if _mode == "1":
        raise
    pytest.skip(str(error), allow_module_level=True)

QUALIFIED_TEST_STACK = True
