"""Shared scaffolding for the falkordb_install.sh behavioral suite.

The suite drives the REAL lib with FALKORDB_* overrides and a stub bin dir at
the front of PATH that individual tests populate (sha256sum, chmod, ...),
mirroring test_memory_resilience.py. The artifact download is pointed at a
local ``file://`` tree, so no test reaches the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "falkordb_install.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
UNIT_TEMPLATE = REPO_ROOT / "scripts" / "systemd" / "genesis-falkordb.service.template"

# The digest the lib pins for 4.20.4/x64 — the artifact load-tested on the
# reference install. Duplicated here on purpose: if someone edits the pin, the
# test that asserts it should fail and make them say why.
PINNED_SHA = (
    "81ea6b989dc2fd4c9ad905e246018b220b02f0e40c406255f9da4768c1684555"  # pragma: allowlist secret
)


def _stage(tmp_path: Path) -> dict:
    """Stub bin dir, a fake release tree, and the env overlay."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    release = tmp_path / "release" / "v4.20.4"
    release.mkdir(parents=True)
    (release / "falkordb-x64.so").write_bytes(b"pretend-module")

    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "FALKORDB_DEPS_DIR": str(tmp_path / "deps"),
        "FALKORDB_DATA_DIR": str(tmp_path / "data"),
        "FALKORDB_RELEASE_BASE": f"file://{tmp_path / 'release'}",
        # `command -v` searches the real PATH, so without this seam the result
        # would depend on whether the machine running the suite has redis.
        "FALKORDB_REDIS_BINARIES": "falkordb-test-absent-binary",
    }


def _run(fn: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; {fn}'],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
