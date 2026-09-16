"""The host Guardian's venv is pyyaml and nothing else — guard that contract.

`scripts/install_guardian.sh` creates the Guardian venv and installs exactly
`pyyaml`. So every module the host-side Guardian imports must resolve using the
standard library plus yaml. Nothing else is available, and there is no other
`pip install` anywhere in the install or gateway path.

This is easy to break invisibly: `genesis.observability` looks like a natural
home for a `/proc` helper, but importing ANY of its submodules executes that
package's `__init__`, which pulls `aiohttp` and `aiosqlite`. The failure does not
appear in CI (the container venv has both), in review (the import line looks
ordinary), or in a file-presence check of the deploy path (the file IS shipped).
It appears on the host, as a ModuleNotFoundError before the tick's `try:`, which
means no heartbeat and no `save_state` — so the container watchdog sees a stale
heartbeat and restart-loops a Guardian that fails identically every time. The
recovery brain stops watching, which is worse than the bug being fixed.

Each import runs in a SUBPROCESS with the absent packages blocked at the meta
path, so one module's success cannot mask another's failure through a warm
`sys.modules`, and nothing leaks into the rest of the suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# The host venv is the standard library plus pyyaml. Expressed as an ALLOWLIST,
# not a list of known-absent packages: a denylist can only ever catch the
# dependencies somebody already thought of, and the whole point of this guard is
# the one nobody thought of. MEASURED in the container venv, importable and
# invisible to a denylist of the obvious names: requests, psutil, pydantic,
# dotenv, jinja2, bs4, tiktoken, websockets, rich, tenacity — any of which would
# reproduce the original failure on the host.
_HOST_ALLOWED = "sys.stdlib_module_names | {'yaml', '_yaml', 'genesis'}"

# Modules the host actually executes. `check` is the tick, but `__main__` is the
# entry point and dispatches the gateway verbs, several of which import modules
# that are NOT in check's transitive closure — measured, not assumed.
_HOST_MODULES = (
    "genesis.guardian.__main__",
    "genesis.guardian.check",
    "genesis.guardian.state_machine",
    "genesis.guardian.recovery",
    "genesis.guardian.diagnosis",
    "genesis.guardian.collector",
    "genesis.guardian.host_profile",       # --host-profile
    "genesis.guardian.bundle_watch",       # --bundle-status
    "genesis.guardian.memory_watch",       # --ram-status
    "genesis.guardian.grow_capacity",      # provisioning verbs
    "genesis.guardian.provisioning.flow",
    "genesis.guardian.provisioning.ledger",
    "genesis.guardian.provisioning.expand",
    "genesis.guardian.repo_bundle",
    "genesis.guardian.credential_bridge",
    "genesis.env",
    "genesis.util.host_boot",
)

_PROBE = """
import importlib.abc, sys
# Pin THIS worktree's src ahead of any installed genesis, so the probe tests the
# code under review rather than whatever is on site-packages.
sys.path.insert(0, {src!r})
ALLOWED = {allowed}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] not in ALLOWED:
            raise ModuleNotFoundError(
                "No module named %r (host venv = stdlib + pyyaml only)" % name
            )
        return None

sys.meta_path.insert(0, Blocker())
mod = __import__({module!r})
# Prove the worktree copy was the one imported. A future switch to a
# finder-based editable install would beat sys.path and silently make every
# assertion here vacuous.
resolved = sys.modules[{module!r}].__file__ or ""
assert resolved.startswith({src!r}), "imported %s, not the worktree copy" % resolved
print("OK")
"""


def _import_under_minimal_venv(module: str, repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, "-c",
            _PROBE.format(
                src=str(repo_root / "src"),
                allowed=_HOST_ALLOWED,
                module=module,
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=repo_root,
    )


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("module", _HOST_MODULES)
def test_host_module_imports_without_container_only_dependencies(
    module: str,
    repo_root: Path,
) -> None:
    result = _import_under_minimal_venv(module, repo_root)
    assert result.returncode == 0, (
        f"{module} does not import on the host Guardian's venv:\n{result.stderr}\n"
        "Something in its import chain needs a package install_guardian.sh does "
        "not install. Move the dependency-free part into genesis/util/ (stdlib-only, "
        "already imported host-side) rather than importing genesis.observability."
    )


def test_the_probe_actually_blocks(repo_root: Path) -> None:
    """Control — without this, a probe that silently imports nothing would report
    every module clean and the guard above would be inert."""
    result = _import_under_minimal_venv("genesis.observability.cc_slots", repo_root)
    assert result.returncode != 0, (
        "the blocker did not fire: genesis.observability pulls aiohttp via its "
        "package __init__, so this import MUST fail under a minimal venv"
    )
    assert "aiohttp" in result.stderr
