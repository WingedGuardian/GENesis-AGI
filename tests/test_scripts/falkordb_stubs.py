"""Shared scaffolding for the falkordb_install.sh behavioral suites.

Split out of test_falkordb_install.py when that file reached the repo's
line cap. Both suites drive the REAL lib with stubbed
``sudo``/``dpkg``/``apt-get``/``gpg``/``systemctl`` on PATH plus FALKORDB_*
overrides, mirroring test_memory_resilience.py. The artifact download is
pointed at a local ``file://`` tree, so no test reaches the network.
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
PINNED_SHA = "81ea6b989dc2fd4c9ad905e246018b220b02f0e40c406255f9da4768c1684555"

_SUDO_STUB = """#!/bin/bash
if [ "$1" = "-n" ] && [ "$2" = "true" ]; then exit "${SUDO_N_RC:-0}"; fi
exec "$@"
"""
# dpkg -s <pkg> rc: 0 = installed, 1 = not.
_DPKG_STUB = """#!/bin/bash
if [ "$1" = "-s" ]; then exit "${DPKG_S_RC:-1}"; fi
exit 0
"""
# dpkg-query -W -f='${db:Status-Status}' redis-server — prints the status word.
# The lib asks for this rather than `dpkg -s` because `dpkg -s` also exits 0 for
# a removed-but-not-purged package ("deinstall ok config-files"), which would
# make a box with NO redis look like a box that has one.
_DPKG_QUERY_STUB = """#!/bin/bash
printf '%s' "${DPKG_QUERY_STATUS-}"
[ -n "${DPKG_QUERY_STATUS-}" ]
"""
_APT_STUB = """#!/bin/bash
echo "apt-get $*" >> "$APT_LOG"
exit "${APT_RC:-0}"
"""
# apt-cache policy <pkg> — reports a configurable Candidate version. The lib
# verifies it against the module's 8.0.0 floor immediately before EVERY
# install, so the default is what the upstream repo actually serves today;
# below-floor tests override APT_CANDIDATE.
_APT_CACHE_STUB = """#!/bin/bash
if [ "$1" = "policy" ]; then
    printf ' %s:\\n  Installed: (none)\\n  Candidate: %s\\n' "$2" "${APT_CANDIDATE:-2:8.0.4-1rl1~noble}"
fi
exit 0
"""
_GPG_STUB = """#!/bin/bash
echo "gpg $*" >> "$APT_LOG"
# --dearmor -o <path> <infile>: create the keyring so the caller's checks pass.
prev=""
for a in "$@"; do
    if [ "$prev" = "-o" ]; then printf 'keyring' > "$a"; fi
    prev="$a"
done
exit 0
"""
# `is-enabled` answers DISABLED (rc 1) by default — the posture the lib verifies
# after `disable --now`. SYSTEMCTL_RC fails the disable outright; a test that
# wants the unit to report still-enabled overrides the stub on PATH instead.
_SYSTEMCTL_STUB = """#!/bin/bash
echo "systemctl $*" >> "$APT_LOG"
if [ "$1" = "is-enabled" ]; then exit "${SYSTEMCTL_IS_ENABLED_RC:-1}"; fi
exit "${SYSTEMCTL_RC:-0}"
"""


def _stage(tmp_path: Path) -> dict:
    """Stub bin dir, a fake release tree, and the env overlay."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("sudo", _SUDO_STUB),
        ("dpkg", _DPKG_STUB),
        ("dpkg-query", _DPKG_QUERY_STUB),
        ("apt-get", _APT_STUB),
        ("apt-cache", _APT_CACHE_STUB),
        ("gpg", _GPG_STUB),
        ("systemctl", _SYSTEMCTL_STUB),
    ):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)

    release = tmp_path / "release" / "v4.20.4"
    release.mkdir(parents=True)
    (release / "falkordb-x64.so").write_bytes(b"pretend-module")

    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_CODENAME=noble\n')

    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "APT_LOG": str(tmp_path / "apt.log"),
        "FALKORDB_DEPS_DIR": str(tmp_path / "deps"),
        "FALKORDB_DATA_DIR": str(tmp_path / "data"),
        "FALKORDB_APT_KEYRING": str(tmp_path / "keyring.gpg"),
        "FALKORDB_APT_LIST": str(tmp_path / "redis.list"),
        "FALKORDB_KEY_URL": f"file://{tmp_path / 'os-release'}",
        "FALKORDB_OS_RELEASE": str(os_release),
        "FALKORDB_RELEASE_BASE": f"file://{tmp_path / 'release'}",
        # `command -v` searches the real PATH, so without this seam the result
        # would depend on whether the machine running the suite has redis
        # installed — which it does, on any box that has run this provisioning.
        "FALKORDB_REDIS_BINARIES": "falkordb-test-absent-binary",
        "FALKORDB_INSTALL_MARKER": str(tmp_path / "installed.marker"),
        "FALKORDB_PROVISION_MARKER": str(tmp_path / "provisioned.marker"),
        # The system half is opt-in; tests that exercise it must say so, the
        # same way an operator has to. The consent tests below override this.
        "GENESIS_FALKORDB_PROVISION": "1",
        "FALKORDB_LOCAL_CONFIG": str(tmp_path / "genesis.yaml"),
    }


def _run(fn: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; {fn}'],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _apt_log(env: dict) -> str:
    log = Path(env["APT_LOG"])
    return log.read_text() if log.exists() else ""
