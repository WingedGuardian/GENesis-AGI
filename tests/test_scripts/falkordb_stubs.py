"""Shared scaffolding for the falkordb_install.sh behavioral suite.

The suite drives the REAL lib and the REAL engine check with FALKORDB_*
overrides and a stub bin dir at the front of PATH (getconf, uname, systemctl),
mirroring test_memory_resilience.py. The digests the check trusts come from a
fake digest file written for the fake wheel (``fake_digests``), never from the
fake wheel's RECORD. The venv is a fake whose ``bin/python``:

* answers ``-m pip ...`` by logging its arguments and exiting ``$PIP_RC``
  (default 0), so no test reaches the network or installs anything;
* runs anything else with the real interpreter, appending
  ``--site-packages <fake site>`` when it is the engine check, so the check
  verifies a fake falkordblite tree built here instead of a real wheel.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "falkordb_install.sh"
CHECK = REPO_ROOT / "scripts" / "lib" / "falkordb_engine_check.py"
REQUIREMENTS = REPO_ROOT / "scripts" / "lib" / "falkordb-requirements.txt"
DIGESTS = REPO_ROOT / "scripts" / "lib" / "falkordb-engine-digests.txt"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
INSTALL = REPO_ROOT / "scripts" / "install.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UNIT_TEMPLATE = REPO_ROOT / "scripts" / "systemd" / "genesis-falkordb.service.template"

#: The pinned version, duplicated on purpose: moving the pin should fail a test
#: and make whoever moved it say why.
PINNED_VERSION = "0.11.0"

#: The fake wheel's tag, as its dist-info WHEEL file states it.
FAKE_TAG = "cp312-cp312-manylinux_2_38_x86_64"

#: The engine files a falkordblite wheel installs, relative to site-packages,
#: and whether each is a binary (installed 0755) or a library (0644).
ENGINE_FILES = {
    "redislite/bin/redis-server": True,
    "redislite/bin/redis-cli": True,
    "redislite/bin/falkordb.so": True,
    "falkordblite.libs/libssl-test.so.3": False,
    "falkordblite.libs/libgomp-test.so.1": False,
}


def _record_digest(data: bytes) -> str:
    """RECORD's form: urlsafe base64 of the raw sha256, padding stripped."""
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def fake_wheel(site: Path, version: str = PINNED_VERSION, tag: str = FAKE_TAG) -> Path:
    """A fake installed falkordblite: dist-info (METADATA, WHEEL, RECORD) plus
    every engine file, each with distinct bytes. Returns the site-packages dir."""
    dist_info = site / f"falkordblite-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: falkordblite\nVersion: {version}\n"
    )
    (dist_info / "WHEEL").write_text(
        f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: false\nTag: {tag}\n"
    )
    lines = []
    for rel, is_binary in ENGINE_FILES.items():
        path = site / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"fake {rel}\n".encode()
        path.write_bytes(data)
        path.chmod(0o755 if is_binary else 0o644)
        lines.append(f"{rel},sha256={_record_digest(data)},{len(data)}")
    (site / "redislite" / "__init__.py").write_text("")
    lines.append(f"falkordblite-{version}.dist-info/METADATA,,")
    lines.append(f"falkordblite-{version}.dist-info/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(lines) + "\n")
    return site


def fake_digests(
    path: Path,
    site: Path,
    version: str = PINNED_VERSION,
    tag: str = FAKE_TAG,
    extra: str = "",
) -> Path:
    """A committed-digests file for the fake wheel at ``site``, computed from
    its files as they are NOW (so a test tampers after calling this)."""
    lines = [extra] if extra else []
    for rel in ENGINE_FILES:
        sha = hashlib.sha256((site / rel).read_bytes()).hexdigest()
        lines.append(f"{version} {tag} {sha} {rel}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# test digests\n" + "\n".join(lines) + "\n")
    return path


def copy_dir(deps: Path, version: str = PINNED_VERSION, tag: str = FAKE_TAG) -> Path:
    """Where the check places the copy of a wheel."""
    return deps / f"{version}-{tag}"


def tree_state(root: Path) -> dict:
    """Every entry under ``root`` with its bytes and stat identity: compare two
    of these to prove a directory was left untouched."""
    state = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            state[str(path.relative_to(root))] = (info.st_ino, info.st_mtime_ns, info.st_mode, data)
    return state


def _stage(tmp_path: Path, *, wheel: bool = True) -> dict:
    """Stub bin dir, a fake venv + site-packages, and the env overlay."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    getconf = bin_dir / "getconf"
    getconf.write_text(
        "#!/bin/bash\n"
        '[ "$1" = GNU_LIBC_VERSION ] || exit 1\n'
        # `-`, not `:-`: an EMPTY value is how a test plays a non-glibc libc.
        'printf "%s\\n" "${FAKE_GLIBC-glibc 2.39}"\n'
    )
    getconf.chmod(0o755)
    uname = bin_dir / "uname"
    uname.write_text(
        "#!/bin/bash\n"
        '[ "$1" = -m ] || exec /bin/uname "$@"\n'
        # `-`, not `:-`: an EMPTY value plays a uname that prints nothing.
        'printf "%s\\n" "${FAKE_ARCH-x86_64}"\n'
    )
    uname.chmod(0o755)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n'
        # No user manager to ask: what systemd 255 does (MEASURED), nothing on
        # stdout, the reason on stderr, rc 1.
        'if [ -n "${ENGINE_NOBUS:-}" ]; then\n'
        '    echo "Failed to connect to bus: No medium found" >&2; exit 1\n'
        "fi\n"
        'state="${ENGINE_STATE:-inactive}"\n'
        'printf "%s\\n" "$state"\n'
        '[ "$state" = active ]\n'
    )
    systemctl.chmod(0o755)

    site = tmp_path / "site"
    digests = tmp_path / "digests.txt"
    if wheel:
        fake_wheel(site)
        fake_digests(digests, site)
    else:
        site.mkdir()
        digests.write_text("# no wheel staged\n")

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text(
        "#!/bin/bash\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then\n'
        '    printf "%s\\n" "$*" >> "$PIP_LOG"\n'
        '    exit "${PIP_RC:-0}"\n'
        "fi\n"
        f'for arg in "$@"; do\n'
        f'    if [ "$arg" = "{CHECK}" ]; then\n'
        f'        exec "{sys.executable}" "$@" --site-packages "$FAKE_SITE"\n'
        "    fi\n"
        "done\n"
        f'exec "{sys.executable}" "$@"\n'
    )
    python.chmod(0o755)

    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "FALKORDB_DEPS_DIR": str(tmp_path / "home" / ".genesis" / "deps" / "falkordb"),
        "FALKORDB_DIGESTS_FILE": str(digests),
        "FALKORDB_SYSTEMCTL": str(systemctl),
        "FAKE_SITE": str(site),
        "PIP_LOG": str(tmp_path / "pip.log"),
        "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "VENV": str(venv),
    }


def _run(fn: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; {fn}'],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _provisioned(fn: str = "") -> str:
    """A shell prefix: provision against the staged venv, then ``fn``."""
    return f'falkordb_provision "$VENV"; {fn}'


def _report() -> str:
    """Shell that prints the provision's exported results, one per line."""
    return (
        'printf "OK=%s\\nDIR=%s\\nSERVER=%s\\nCLI=%s\\nMODULE=%s\\nDIGESTS=%s\\n" '
        '"$FALKORDB_ENGINE_OK" "$FALKORDB_ENGINE_DIR" "$FALKORDB_REDIS_SERVER" '
        '"$FALKORDB_REDIS_CLI" "$FALKORDB_MODULE" "$FALKORDB_DIGESTS"'
    )


def _results(stdout: str) -> dict:
    out = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"OK", "DIR", "SERVER", "CLI", "MODULE", "DIGESTS"}:
            out[key] = value
    return out


def site_file(env: dict, rel: str) -> Path:
    return Path(env["FAKE_SITE"]) / rel


def engine_dir(env: dict) -> Path:
    """The copy a staged provision places."""
    return copy_dir(Path(env["FALKORDB_DEPS_DIR"]))


def copy_file(env: dict, rel: str) -> Path:
    return engine_dir(env) / rel


def pip_calls(env: dict) -> list[str]:
    log = Path(env["PIP_LOG"])
    return log.read_text().splitlines() if log.exists() else []
