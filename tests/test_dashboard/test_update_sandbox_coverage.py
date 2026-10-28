"""Every test module that imports the dashboard update routes uses the sandbox.

``genesis.dashboard.routes.updates`` starts update.sh, the update orchestrator
and Claude sessions, and binds its paths to the real home at import. An earlier
test in this area launched real update orchestrators on the machine running the
suite. tests/_update_routes_sandbox.py isolates all of it; this test makes using
it a rule rather than a habit: a module importing the routes either takes the
``updates_sandbox`` fixture, calls ``isolate_update_routes`` itself, or is named
below with the reason it touches nothing (or, for the live fence's own tests,
why it spawns under that fence instead).
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]

# Modules that import the routes and read only constants or source text.
_TEXT_ONLY = {
    "test_hooks/test_main_checkout_guard.py": "reads UPDATE_TIER_ENV and the template text",
    "test_ui/test_dashboard_e2e.py": "served by tests/test_ui/conftest.py, which calls "
    "isolate_update_routes for every dashboard it builds",
}

# Modules that spawn through the routes under the live fence on purpose. The
# sandbox replaces the routes' Popen with a refusal, which would disable the
# spawn branches these modules exist to replay.
_FENCE_SPAWNS = {
    "test_live_fence.py": "replays each spawn branch of the update routes under the "
    "live fence (tests/_live_fence.py), with the routes' state and log paths in tmp_path",
}


def _imports_the_routes(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "genesis.dashboard.routes.updates":
                return True
            if node.module == "genesis.dashboard.routes" and any(
                alias.name == "updates" for alias in node.names
            ):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name == "genesis.dashboard.routes.updates" for alias in node.names
        ):
            return True
    return False


def _importers() -> list[Path]:
    return sorted(
        p
        for p in TESTS.rglob("*.py")
        if p.name != "_update_routes_sandbox.py"
        and _imports_the_routes(ast.parse(p.read_text(encoding="utf-8")))
    )


def test_the_scan_finds_the_known_importers():
    """Guard the guard: a scan that finds nothing would pass every module."""
    found = {p.relative_to(TESTS).as_posix() for p in _importers()}
    assert "test_dashboard/test_update_marker_claim.py" in found
    assert "test_ui/test_dashboard_e2e.py" in found
    assert len(found) >= 10, found


def test_every_importer_uses_the_sandbox():
    missing = []
    for path in _importers():
        rel = path.relative_to(TESTS).as_posix()
        if rel in _TEXT_ONLY or rel in _FENCE_SPAWNS:
            continue
        text = path.read_text(encoding="utf-8")
        if "updates_sandbox" not in text and "isolate_update_routes" not in text:
            missing.append(rel)
    assert not missing, (
        "these modules import genesis.dashboard.routes.updates without the sandbox "
        f"(use the updates_sandbox fixture): {missing}"
    )


def test_the_text_only_list_is_still_true():
    """A name on either list that no longer imports the routes is stale."""
    found = {p.relative_to(TESTS).as_posix() for p in _importers()}
    listed = set(_TEXT_ONLY) | set(_FENCE_SPAWNS)
    assert listed <= found, listed - found
    # The e2e entry rests on its conftest building every dashboard in the sandbox.
    assert "isolate_update_routes(" in (TESTS / "test_ui" / "conftest.py").read_text()


def test_the_sandbox_isolates_and_refuses(updates_sandbox):
    from genesis import env
    from genesis.dashboard.routes import updates

    assert env.genesis_home() == updates._GENESIS_DIR == updates._STATE_FILE.parent
    assert updates._PID_FILE.parent == updates._GENESIS_DIR
    try:
        updates.subprocess.Popen(["true"])
    except AssertionError:
        pass
    else:  # pragma: no cover - the failure this test exists for
        raise AssertionError("the sandbox let a process launch through")
    assert updates_sandbox.launched == [["true"]]
