"""litellm is imported on first use, never as a side effect of reading routing config.

Importing litellm costs several seconds. Session hooks (the onboarding floor
check) and every standalone MCP server only read routing config or build a
router at startup, so a top-level ``import litellm`` in the delegate made each of
them pay for a library they never call (#3558). These tests pin the deferral and
the behaviour that has to survive it.

The import checks run in a fresh interpreter: litellm is often already loaded in
the test process, so ``sys.modules`` there says nothing.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


def _run(code: str) -> str:
    """Run ``code`` in a fresh interpreter that imports this tree's ``genesis``.

    HOME is a throwaway directory, so nothing the child resolves under
    ``~/.genesis`` reaches a real install.
    """
    with tempfile.TemporaryDirectory() as home:
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            capture_output=True,
            text=True,
            cwd=REPO,
            env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin", "HOME": home},
            timeout=300,
        )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


_ASSERT_THIS_TREE = """
import genesis
assert genesis.__file__.startswith({src!r}), genesis.__file__
"""


def _with_tree_check(code: str) -> str:
    return _ASSERT_THIS_TREE.format(src=str(REPO / "src")) + textwrap.dedent(code)


def test_importing_the_routing_package_does_not_import_litellm():
    out = _run(
        _with_tree_check("""
        import sys
        import genesis.routing
        import genesis.routing.config
        import genesis.routing.litellm_delegate
        from genesis.routing import LiteLLMDelegate, Router
        print("litellm" in sys.modules)
    """)
    )
    assert out == "False"


def test_the_onboarding_floor_check_does_not_import_litellm():
    # The SessionStart charter hook runs this on every session start and resume.
    out = _run(
        _with_tree_check("""
        import sys
        from genesis.onboarding.floor import compute_floor
        compute_floor({})
        print("litellm" in sys.modules)
    """)
    )
    assert out == "False"


def test_building_the_standalone_router_does_not_import_litellm():
    # Every standalone MCP server builds this before answering `initialize`.
    out = _run(
        _with_tree_check("""
        import sys
        from pathlib import Path
        import genesis.env
        genesis.env.secrets_path = lambda: Path("/nonexistent/secrets.env")
        from genesis.routing.standalone import _build_standalone_router
        router = _build_standalone_router()
        print(router is not None and "litellm" not in sys.modules)
    """)
    )
    assert out == "True"


def test_first_use_loads_the_real_module_and_registers_custom_costs():
    out = _run(
        _with_tree_check("""
        import asyncio, sys
        import genesis.routing.litellm_delegate as d
        assert "litellm" not in sys.modules
        mod = asyncio.run(d._litellm_async())
        assert mod is sys.modules["litellm"]
        assert d.litellm is mod
        assert all(name in mod.model_cost for name in d._CUSTOM_MODEL_COSTS)
        d.warm()
        assert d._litellm() is mod
        print("ok")
    """)
    )
    assert out == "ok"


def test_an_unknown_module_attribute_still_raises_attribute_error():
    import genesis.routing.litellm_delegate as d

    with pytest.raises(AttributeError):
        d.no_such_attribute  # noqa: B018 - the access is the test


def test_a_patched_litellm_is_used_as_is_and_restored(monkeypatch):
    import genesis.routing.litellm_delegate as d

    sentinel = object()
    monkeypatch.setattr(d, "litellm", sentinel, raising=False)
    assert d._litellm() is sentinel


async def test_a_litellm_that_cannot_import_fails_the_call_instead_of_raising(monkeypatch):
    """Before the deferral, a broken litellm failed at import and callers degraded.

    It now fails at the first call, so ``call()`` must turn it into a failed
    CallResult the router can route around. A 400 is classified BAD_REQUEST:
    fail fast, and no provider's breaker is tripped for a local install defect.
    """
    from genesis.routing import litellm_delegate as d
    from genesis.routing.retry import classify_error
    from genesis.routing.types import ErrorCategory, ProviderConfig, RoutingConfig

    monkeypatch.delattr(d, "litellm", raising=False)
    monkeypatch.setitem(sys.modules, "litellm", None)  # makes `import litellm` raise
    cfg = ProviderConfig(
        name="p",
        provider_type="openai",
        model_id="m",
        is_free=True,
        rpm_limit=None,
        open_duration_s=60,
    )
    delegate = d.LiteLLMDelegate(
        RoutingConfig(providers={"p": cfg}, call_sites={}, retry_profiles={})
    )

    result = await delegate.call("p", "m", [{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.status_code == 400
    assert result.reached_provider is False
    assert "litellm" in result.error
    assert classify_error(result.status_code, result.error) is ErrorCategory.BAD_REQUEST


def test_the_package_exports_are_the_submodule_objects():
    import genesis.routing as routing
    from genesis.routing import litellm_delegate, router

    assert routing.LiteLLMDelegate is litellm_delegate.LiteLLMDelegate
    assert routing.Router is router.Router


def test_the_c_loader_reads_the_shipped_routing_config_identically():
    from genesis.routing import config

    text = (REPO / "config" / "model_routing.yaml").read_text()
    assert config._yaml_load(text) == yaml.load(text, Loader=yaml.SafeLoader)  # noqa: S506


def test_a_yaml_error_keeps_the_pure_python_loaders_message():
    # The dashboard save path shows this text; the C loader's omits the snippet.
    from genesis.routing import config

    bad = "a: b: c\n"
    with pytest.raises(yaml.YAMLError) as expected:
        yaml.load(bad, Loader=yaml.SafeLoader)  # noqa: S506
    with pytest.raises(yaml.YAMLError) as got:
        config._yaml_load(bad)
    assert str(got.value) == str(expected.value)


def test_router_init_warms_litellm_once(monkeypatch):
    """The server keeps importing litellm at startup, so its first call is not slow."""
    from unittest.mock import MagicMock

    import genesis.routing.litellm_delegate as d
    from genesis.runtime.init import router as router_init

    monkeypatch.setenv("GENESIS_NETWORK_SENTINEL_DISABLED", "1")
    warm = MagicMock()
    monkeypatch.setattr(d, "warm", warm)
    rt = MagicMock()
    rt._awareness_loop = None
    rt._activity_tracker = None

    router_init.init(rt)

    warm.assert_called_once_with()
    assert rt._router is not None
