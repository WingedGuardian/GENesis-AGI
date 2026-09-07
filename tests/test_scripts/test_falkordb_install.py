"""Behavioral tests for scripts/lib/falkordb_install.sh.

Driven against the REAL lib with stubbed ``sudo``/``dpkg``/``apt-get``/``gpg``/
``systemctl`` on PATH plus FALKORDB_* overrides, mirroring
test_memory_resilience.py. The artifact download is pointed at a local
``file://`` tree, so no test reaches the network.

The properties under test are the ones that would damage someone's machine if
wrong: never touching an operator's existing redis, never installing an
artifact whose digest does not match, and never aborting the caller.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "falkordb_install.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
UNIT_TEMPLATE = REPO_ROOT / "scripts" / "systemd" / "genesis-falkordb.service.template"

# The digest the lib pins for 4.20.4/x64 — the artifact load-tested on the
# reference install. Duplicated here on purpose: if someone edits the pin, this
# test should fail and make them say why.
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
_SYSTEMCTL_STUB = """#!/bin/bash
echo "systemctl $*" >> "$APT_LOG"
exit 0
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
        "FALKORDB_PROVISION_MARKER": str(tmp_path / "provisioned.marker"),
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


# --- the operator's machine is not ours to change -------------------------


def test_pre_existing_redis_blocks_repo_add_and_install(tmp_path):
    """An existing redis-server means hands off — including the apt repo.

    Adding the upstream repo would silently promote the operator's 7.x to 8.x
    on their next unrelated `apt upgrade`. That is a worse thing to do to
    someone's box than declining to provision, so the skip is total.
    """
    env = _stage(tmp_path)
    env["DPKG_QUERY_STATUS"] = "installed"  # redis-server already installed
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "already installed" in result.stdout
    # The remediation must point somewhere that exists — SETUP.md, at the repo
    # root, with a section of that name.
    assert "SETUP.md" in result.stdout
    setup = (REPO_ROOT / "SETUP.md").read_text()
    assert "Graph engine (FalkorDB)" in setup
    assert not Path(env["FALKORDB_APT_LIST"]).exists(), "repo was added anyway"
    assert "apt-get" not in _apt_log(env), "apt was invoked anyway"


def test_redis_we_provisioned_does_not_replay_the_operator_decision(tmp_path):
    """Re-running on a box WE provisioned must not re-ask a settled question.

    Our apt list file marks the boxes we set up. Without the split, every
    bootstrap re-run there would print an operator-decision message about a
    decision already made — the kind of noise that trains people to skim past
    output that sometimes matters.
    """
    env = _stage(tmp_path)
    env["DPKG_QUERY_STATUS"] = "installed"
    # OUR marker, not the apt list file: SETUP.md tells operators to create that
    # list themselves, so branching on it would claim credit for their work.
    Path(env["FALKORDB_PROVISION_MARKER"]).write_text("2026-09-06T00:00:00Z\n")

    result = _run("falkordb_redis_install", env)
    assert result.returncode == 0, result.stderr
    assert "already provisioned" in result.stdout
    assert "your call" not in result.stdout
    assert "apt-get" not in _apt_log(env)


def test_removed_but_not_purged_redis_does_not_block_provisioning(tmp_path):
    """`dpkg -s` exits 0 for a package in "deinstall ok config-files".

    An operator who ran `apt remove redis-server` has NO redis, but the naive
    check would report one and decline to provision forever — failing closed
    and printing a false statement. The status word is what distinguishes them.
    """
    env = _stage(tmp_path)
    env["DPKG_QUERY_STATUS"] = "config-files"  # removed, not purged
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "already installed" not in result.stdout
    assert Path(env["FALKORDB_APT_LIST"]).exists(), "should have provisioned"


def test_a_non_dpkg_redis_still_blocks_provisioning(tmp_path):
    """A source-built redis or valkey is invisible to dpkg but still theirs."""
    env = _stage(tmp_path)
    env["FALKORDB_REDIS_BINARIES"] = "sh"  # stand-in for a redis on PATH
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "already installed" in result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists(), "repo added anyway"


def test_a_pinned_version_refuses_to_install_when_it_cannot_be_verified(tmp_path):
    """Having a pin and no way to check it must fail CLOSED.

    Different from the unpinned case, which is a deliberate operator override:
    here we know what the bytes should be and cannot confirm it.
    """
    env = _stage(tmp_path)
    no_sha = Path(env["PATH"].split(":")[0]) / "sha256sum"
    no_sha.write_text("#!/bin/bash\nexit 127\n")
    no_sha.chmod(0o755)

    result = _run("falkordb_module_install", env)
    assert result.returncode == 0, result.stderr
    assert "cannot verify" in result.stdout
    assert not (Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so").exists()


def test_no_passwordless_sudo_skips_with_remediation(tmp_path):
    env = _stage(tmp_path)
    env["SUDO_N_RC"] = "1"
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "sudo unavailable" in result.stdout
    # The skip must TEACH the manual path, per the lib contract.
    assert "falkordb_redis_install" in result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists()


def test_fresh_box_adds_repo_installs_and_stands_down_system_redis(tmp_path):
    env = _stage(tmp_path)
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert Path(env["FALKORDB_APT_LIST"]).read_text().strip() == (
        f"deb [signed-by={env['FALKORDB_APT_KEYRING']}] "
        "https://packages.redis.io/deb noble main"
    )
    log = _apt_log(env)
    assert "apt-get update" in log
    assert "apt-get install" in log
    # The deb auto-enables a system redis on 6379; Genesis is socket-only.
    assert "systemctl disable --now redis-server" in log


def test_unknown_codename_skips_before_touching_apt(tmp_path):
    env = _stage(tmp_path)
    Path(env["FALKORDB_OS_RELEASE"]).write_text("ID=weird\n")  # no VERSION_CODENAME
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "VERSION_CODENAME" in result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists()


# --- the artifact must be what we tested ----------------------------------


def test_checksum_mismatch_refuses_and_leaves_nothing_behind(tmp_path):
    """The release ships no checksums, so the pin is the only integrity check."""
    env = _stage(tmp_path)  # fake artifact => digest cannot match the pin
    result = _run("falkordb_module_install", env)

    assert result.returncode == 0, result.stderr
    assert "checksum MISMATCH" in result.stdout
    dest = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4"
    assert not (dest / "falkordb.so").exists(), "installed despite a bad digest"
    assert not (dest / "falkordb.so.partial").exists(), "left a partial download"


def test_matching_checksum_installs_with_the_execute_bit(tmp_path):
    """Redis refuses a module without +x; the release asset arrives 644."""
    env = _stage(tmp_path)
    # Make the fake artifact hash to the pinned value by pinning the fake.
    artifact = tmp_path / "release" / "v4.20.4" / "falkordb-x64.so"
    real_sha = subprocess.run(
        ["sha256sum", str(artifact)], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    env["FALKORDB_VERSION"] = "4.20.4"

    result = _run(
        f'_falkordb_expected_sha() {{ printf "{real_sha}"; }}; falkordb_module_install',
        env,
    )
    assert result.returncode == 0, result.stderr
    installed = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so"
    assert installed.is_file(), result.stdout
    assert installed.stat().st_mode & 0o111, "module installed without +x"


def test_unpinned_version_warns_loudly_rather_than_silently_trusting(tmp_path):
    env = _stage(tmp_path)
    env["FALKORDB_VERSION"] = "9.9.9"
    (tmp_path / "release" / "v9.9.9").mkdir()
    (tmp_path / "release" / "v9.9.9" / "falkordb-x64.so").write_bytes(b"x")

    result = _run("falkordb_module_install", env)
    assert result.returncode == 0, result.stderr
    assert "no pinned checksum" in result.stdout
    assert "UNVERIFIED" in result.stdout


def test_second_run_is_idempotent(tmp_path):
    env = _stage(tmp_path)
    env["FALKORDB_VERSION"] = "9.9.9"
    (tmp_path / "release" / "v9.9.9").mkdir()
    (tmp_path / "release" / "v9.9.9" / "falkordb-x64.so").write_bytes(b"x")

    assert _run("falkordb_module_install", env).returncode == 0
    second = _run("falkordb_module_install", env)
    assert second.returncode == 0
    assert "already present" in second.stdout


def test_download_failure_leaves_no_half_file(tmp_path):
    env = _stage(tmp_path)
    env["FALKORDB_RELEASE_BASE"] = f"file://{tmp_path / 'nonexistent'}"
    result = _run("falkordb_module_install", env)

    assert result.returncode == 0, result.stderr
    assert "download failed" in result.stdout
    dest = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4"
    assert not (dest / "falkordb.so").exists()
    assert not (dest / "falkordb.so.partial").exists()


# --- wiring ----------------------------------------------------------------


def test_bootstrap_sources_the_lib_and_substitutes_the_version():
    """The lib must reach existing installs, and the unit must get a version.

    update.sh re-runs bootstrap.sh, never install.sh — so provisioning placed
    only in install.sh would never reach a box that already exists. This test
    pins the lib to bootstrap.
    """
    text = BOOTSTRAP.read_text()
    assert "lib/falkordb_install.sh" in text
    assert "falkordb_provision" in text
    assert "__FALKORDB_VERSION__" in text


def _exec_argv(unit: str) -> str:
    """The ExecStart line's arguments only.

    Asserting against the whole file is how the socket-only property went
    untested: `--port 0` also appears in the comment that explains it, so
    deleting the actual argument left the assertion green (verified by
    mutation). Match the argv, not the prose about the argv.
    """
    body = unit.split("ExecStart=", 1)[1]
    return body.split("\n[", 1)[0]


def test_unit_is_socket_only_and_bounded():
    """Properties that keep the engine off the network and inside its budget."""
    unit = UNIT_TEMPLATE.read_text()
    argv = _exec_argv(unit)
    assert "--port 0" in argv, "unit would open a TCP listener"
    assert "--unixsocket " in argv
    assert "--maxmemory 512mb" in argv
    # Eviction would silently corrupt a projection; it must fail loudly instead.
    assert "--maxmemory-policy noeviction" in argv
    assert "MemoryMax=1G" in unit


def test_start_limit_directives_are_in_the_unit_section():
    """StartLimit* in [Service] is HALF-silently ignored by systemd.

    It accepts StartLimitBurst there as a legacy alias but rejects
    StartLimitIntervalSec, so burst counts against the manager's 10s default.
    Measured with RestartSec=5: burst=4 becomes unreachable and a unit whose
    module fails to load restarts forever instead of entering `failed` — which
    the posture rule assumes cannot happen.
    """
    unit = UNIT_TEMPLATE.read_text()
    # Line-anchored: "[Service]" also appears inside the comment explaining
    # this very rule, so a bare split would cut in the wrong place.
    unit_section, service_section = unit.split("\n[Service]\n", 1)
    for key in ("StartLimitBurst", "StartLimitIntervalSec"):
        assert key in unit_section, f"{key} must be in [Unit]"
        assert key not in service_section, f"{key} in [Service] is ignored"


def test_unit_write_scope_is_narrow():
    """ReadWritePaths=%h would grant the engine the whole home."""
    unit = UNIT_TEMPLATE.read_text()
    rw = [ln for ln in unit.splitlines() if ln.startswith("ReadWritePaths=")]
    assert rw, "no ReadWritePaths — ProtectSystem=strict would block the socket"
    for line in rw:
        assert line != "ReadWritePaths=%h", (
            "grants write to the repo, secrets.env, ~/.ssh and ~/.claude"
        )
        assert line.startswith("ReadWritePaths=%h/.genesis")


def test_bootstrap_does_not_arm_the_engine():
    """PR-F1's central promise: the unit is rendered, never enabled.

    Bootstrap's enable loop covers `*.timer` plus explicitly-named services, so
    arming this one is a one-line change away. The old assertion here checked
    for `WantedBy=default.target`, which is present whether or not anything
    enables it — it stated the invariant without testing it.
    """
    text = BOOTSTRAP.read_text()
    enabling = [ln for ln in text.splitlines() if "systemctl --user enable" in ln]
    assert not any("falkordb" in ln for ln in enabling), enabling


def test_the_pinned_digest_is_the_one_we_load_tested():
    assert PINNED_SHA in LIB.read_text()
