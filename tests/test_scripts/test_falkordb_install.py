"""Behavioral tests for falkordb_redis_install / consent / provisioning.

See falkordb_stubs.py for the harness. The properties under test are the ones
that would damage someone's machine if wrong: never touching an operator's
existing redis, and never aborting the caller. Artifact integrity and unit
wiring live in test_falkordb_module.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts.falkordb_stubs import REPO_ROOT, _apt_log, _run, _stage

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
    # OUR marker AND a live stamp for THIS package — the state a completed run
    # leaves. The list alone is not enough: SETUP.md tells operators to create
    # it themselves, so branching on it would claim credit for their work.
    _mark_ours(env)
    Path(env["FALKORDB_PROVISION_MARKER"]).write_text("2026-09-06T00:00:00Z\n")

    result = _run("falkordb_redis_install", env)
    assert result.returncode == 0, result.stderr
    assert "already provisioned" in result.stdout
    assert "your call" not in result.stdout
    assert "apt-get" not in _apt_log(env)


def test_a_completion_marker_never_vouches_for_a_replaced_redis(tmp_path):
    """The marker records that OUR install finished; it says nothing about the
    redis on the box now. Without a live stamp the package is not ours, and
    the operator gets the >= 8.0.0 warning instead of a false OK."""
    env = _stage(tmp_path)
    env["DPKG_QUERY_STATUS"] = "installed"  # a redis we have no stamp for
    Path(env["FALKORDB_PROVISION_MARKER"]).write_text("2026-09-06T00:00:00Z\n")

    result = _run("falkordb_redis_install", env)
    assert result.returncode == 0, result.stderr
    assert "already provisioned" not in result.stdout, "marker trusted alone"
    assert "your call" in result.stdout


def test_system_provisioning_is_opt_in(tmp_path):
    """Merging this must not add an apt repo to anyone's machine.

    update.sh re-runs bootstrap.sh on every update, so without a consent gate an
    operator who merely pulled Genesis would silently acquire a third-party apt
    trust anchor and a database daemon — for a feature that stays inert until a
    later release wires a consumer. Every OTHER gate here asks "can we?"; this
    one asks "may we?".
    """
    env = _stage(tmp_path)
    del env["GENESIS_FALKORDB_PROVISION"]
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "opt-in" in result.stdout
    assert "GENESIS_FALKORDB_PROVISION=1" in result.stdout, "must teach the opt-in"
    assert not Path(env["FALKORDB_APT_LIST"]).exists(), "repo added without consent"
    assert "apt-get" not in _apt_log(env), "apt invoked without consent"


# Consent is read the way Genesis reads its own config: yaml.safe_load, then a
# real boolean true at graph_engine.provision. Each row is (config body,
# consents). YAML 1.1 (what safe_load implements) spells true as true/yes/on
# in three cases each, so `yes` IS consent -- the gate means exactly what the
# loader means, never a second dialect of it.
_CONSENT_CASES = (
    ("memory:\n  wing: x\ngraph_engine:\n  provision: true\n", True),
    ("graph_engine:\n  provision: True\n", True),
    ("graph_engine:\n  provision: TRUE\n", True),
    ("graph_engine:\n  provision: yes\n", True),
    ("graph_engine:\n  provision: true  # opted in\n", True),
    # Not the first key, and after a nested block.
    ("graph_engine:\n  backends:\n    experimental: true\n  provision: true\n", True),
    # Duplicate keys and blocks are legal; the LAST one wins.
    ("graph_engine:\n  provision: false\n  provision: true\n", True),
    ("graph_engine:\n  provision: true\n  provision: false\n", False),
    ("graph_engine:\n  provision: true\ngraph_engine:\n  backend: x\n", False),
    # Tabs cannot indent YAML: the loader rejects the whole file.
    ("graph_engine:\n\tprovision: true\n", False),
    ("graph_engine:\n  provision: true\n: [unclosed\n", False),
    # Not a boolean.
    ("graph_engine:\n  provision: 'true'\n", False),
    ("graph_engine:\n  provision: true#x\n", False),
    ("graph_engine:\n  provision: 1\n", False),
    ("graph_engine:\n  provision: false\n", False),
    # Not the documented key path.
    ("graph_engine:\n  other: true\n", False),
    ("other_block:\n  provision: true\n", False),
    ("graph_engine:\n  backends:\n    experimental:\n      provision: true\n", False),
    ("unrelated:\n  graph_engine:\n    provision: true\n", False),
    ("graph_engine:\n  # provision: true\n", False),
    # Not a mapping at the root, or at the section.
    ("- graph_engine:\n    provision: true\n", False),
    ("graph_engine: true\n", False),
    ("", False),
)


@pytest.mark.parametrize(("body", "consents"), _CONSENT_CASES)
def test_config_consent_is_what_the_yaml_loader_reads(tmp_path, body, consents):
    """An operator should not have to export an env var on every update, and
    a config Genesis itself would read differently must never authorise a
    third-party apt repo."""
    env = _stage(tmp_path)
    del env["GENESIS_FALKORDB_PROVISION"]
    Path(env["FALKORDB_LOCAL_CONFIG"]).write_text(body)
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert Path(env["FALKORDB_APT_LIST"]).exists() == consents, (body, result.stdout)
    assert ("opt-in" in result.stdout) != consents, result.stdout


def test_an_unreadable_config_says_why_it_is_not_consent(tmp_path):
    """A parse failure or a missing venv must be SAID, not read as a quiet no."""
    env = _stage(tmp_path)
    del env["GENESIS_FALKORDB_PROVISION"]
    config = Path(env["FALKORDB_LOCAL_CONFIG"])
    config.write_text("graph_engine:\n\tprovision: true\n")
    result = _run("falkordb_redis_install", env)
    assert "could not be parsed" in result.stdout, result.stdout

    config.write_text("graph_engine:\n  provision: true\n")
    env["FALKORDB_PYTHON"] = str(tmp_path / "no-venv" / "python")
    result = _run("falkordb_redis_install", env)
    assert result.returncode == 0, result.stderr
    assert "no venv Python" in result.stdout, result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists()


def test_kill_switch_stops_everything_including_the_module(tmp_path):
    env = _stage(tmp_path)
    env["GENESIS_FALKORDB_PROVISION_DISABLED"] = "1"
    result = _run("falkordb_provision", env)

    assert result.returncode == 0, result.stderr
    assert "DISABLED" in result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists()
    assert not (Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so").exists()


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


def test_no_passwordless_sudo_skips_with_remediation(tmp_path):
    env = _stage(tmp_path)
    env["SUDO_N_RC"] = "1"
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "sudo unavailable" in result.stdout
    # The skip must TEACH the manual path, per the lib contract.
    assert "falkordb_redis_install" in result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists()


def test_the_sudo_remediation_carries_its_own_consent(tmp_path):
    """The printed command must WORK when run, not merely be printed.

    sudo resets the environment and HOME, so neither the caller's
    GENESIS_FALKORDB_PROVISION=1 nor their genesis.yaml reaches the elevated
    shell. A command that re-sources the lib without stating consent inside it
    lands on the opt-in skip and provisions nothing.
    """
    env = _stage(tmp_path)
    env["SUDO_N_RC"] = "1"
    result = _run("falkordb_redis_install", env)
    line = next(ln for ln in result.stdout.splitlines() if "sudo bash -c '" in ln)
    body = line.split("sudo bash -c '", 1)[1].rsplit("'", 1)[0]

    # The elevated shell: no inherited consent, passwordless sudo available.
    elevated = {k: v for k, v in env.items() if k != "GENESIS_FALKORDB_PROVISION"}
    elevated["SUDO_N_RC"] = "0"
    rerun = subprocess.run(
        ["bash", "-c", body],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=elevated,
    )

    assert rerun.returncode == 0, rerun.stderr
    assert "opt-in" not in rerun.stdout, rerun.stdout
    assert "apt-get install" in _apt_log(env)


def test_fresh_box_adds_repo_installs_and_stands_down_system_redis(tmp_path):
    env = _stage(tmp_path)
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    apt_list = Path(env["FALKORDB_APT_LIST"]).read_text()
    assert apt_list.startswith(
        f"deb [signed-by={env['FALKORDB_APT_KEYRING']}] "
        "https://packages.redis.io/deb noble main"
    )
    assert "# genesis-install-stamp: 2:8.0.4-1rl1~noble:" in apt_list
    log = _apt_log(env)
    assert "apt-get update" in log
    assert "apt-get install" in log
    # The deb auto-enables a system redis on 6379; Genesis is socket-only.
    assert "systemctl disable --now redis-server" in log
    # Standing it down is verified before success is claimed — is-enabled
    # reporting disabled (stub rc 1) is what lets this line print.
    assert "systemctl is-enabled" in log
    assert "system unit disabled" in result.stdout
    # The marker is written only AFTER the verified stand-down.
    assert Path(env["FALKORDB_PROVISION_MARKER"]).exists()


def test_a_failed_disable_reports_the_daemon_it_left_running(tmp_path):
    """Standing the package's system unit down is part of provisioning.

    If `disable --now` fails, a second redis is left on :6379 across reboots —
    that must surface as an incomplete-provisioning warning with remediation,
    never as the "system unit disabled" success line.
    """
    env = _stage(tmp_path)
    env["SYSTEMCTL_RC"] = "1"
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "could not be" in result.stdout
    assert "still enabled on" in result.stdout
    assert "system unit disabled" not in result.stdout, "claimed the posture anyway"
    # The install stamp IS recorded in the apt list (the package is ours)
    # but the completion marker is not — that split is what lets a later run
    # retry exactly the unfinished stand-down instead of replaying
    # 'already provisioned'.
    assert "# genesis-install-stamp: " in Path(env["FALKORDB_APT_LIST"]).read_text()
    assert not Path(env["FALKORDB_PROVISION_MARKER"]).exists()


def test_a_unit_still_enabled_after_disable_leaves_no_marker(tmp_path):
    """The verify-failure path must leave the marker unwritten too."""
    env = _stage(tmp_path)
    env["SYSTEMCTL_IS_ENABLED_RC"] = "0"
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "still enabled" in result.stdout
    assert not Path(env["FALKORDB_PROVISION_MARKER"]).exists()


def _mark_ours(env: dict) -> Path:
    """Record our install the way the lib does: the CURRENT package's
    fingerprint (version + mtime of its dpkg info file) as a stamp comment
    inside the apt list, so a retry sees the claim as live, not stale."""
    info = Path(env["FALKORDB_DPKG_INFO"]) / "redis-server.list"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("pkg-files\n")
    stamp = f"2:8.0.4-1rl1~noble:{int(info.stat().st_mtime)}"
    Path(env["FALKORDB_APT_LIST"]).write_text(
        "deb [signed-by=x] https://packages.redis.io/deb noble main\n"
        f"# genesis-install-stamp: {stamp}\n"
    )
    return info


def test_an_incomplete_provisioning_retries_only_the_stand_down(tmp_path):
    """We installed the package but the stand-down failed: the next run must
    finish OUR step, not read our redis as the operator's and stop.

    Without the install/completion marker split, a redis that is ours lands in
    the same 'leave it alone' branch as one that is theirs — and the daemon we
    created stays enabled on :6379 forever.
    """
    env = _stage(tmp_path)
    env["DPKG_QUERY_STATUS"] = "installed"  # package on the box already
    _mark_ours(env)
    # No FALKORDB_PROVISION_MARKER: this run must retry, not declare victory.

    result = _run("falkordb_redis_install", env)
    assert result.returncode == 0, result.stderr
    assert "completed pending stand-down" in result.stdout
    assert "your call" not in result.stdout, "read our own redis as the operator's"
    assert "apt-get install" not in _apt_log(env), "re-installed instead of retrying"
    assert Path(env["FALKORDB_PROVISION_MARKER"]).exists()


def test_a_unit_still_enabled_after_disable_reports_it(tmp_path):
    """The disable can succeed while the unit stays enabled — verify, don't trust."""
    env = _stage(tmp_path)
    env["SYSTEMCTL_IS_ENABLED_RC"] = "0"  # unit still enabled after the disable
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "still enabled" in result.stdout
    assert "system unit disabled" not in result.stdout, "claimed the posture anyway"


def test_an_incomplete_retry_that_still_fails_keeps_retrying(tmp_path):
    """A retry that fails must not write the completion marker either — the
    NEXT retry must still know the stand-down is outstanding."""
    env = _stage(tmp_path)
    env["DPKG_QUERY_STATUS"] = "installed"
    _mark_ours(env)
    env["SYSTEMCTL_RC"] = "1"

    result = _run("falkordb_redis_install", env)
    assert result.returncode == 0, result.stderr
    assert "enabled on :6379" in result.stdout
    assert not Path(env["FALKORDB_PROVISION_MARKER"]).exists()


def test_a_stale_install_marker_never_disables_a_replacement_redis(tmp_path):
    """The operator purges our package and installs their own: the fingerprint
    in the install marker no longer matches the package on the box, so the
    retry must NOT stand it down — and the stale claim is discarded.
    """
    env = _stage(tmp_path)
    env["DPKG_QUERY_STATUS"] = "installed"
    info = _mark_ours(env)
    # The replacement package: dpkg rewrites its info file, changing the
    # fingerprint. Force a different second so the stamp provably differs.
    info.write_text("pkg-files\n")
    new_mtime = int(info.stat().st_mtime) + 4000
    os.utime(info, (new_mtime, new_mtime))

    result = _run("falkordb_redis_install", env)
    assert result.returncode == 0, result.stderr
    assert "your call" in result.stdout, "claimed a redis that is not ours"
    assert "systemctl disable" not in _apt_log(env), "disabled an operator's service"
    assert not Path(env["FALKORDB_PROVISION_MARKER"]).exists()


def test_an_unstampable_install_warns_instead_of_silent_orphaning(tmp_path):
    """If the stamp cannot be appended to the apt list, the run must SAY that
    # provenance is missing — a failed stand-down would otherwise silently
    # become an unretryable operator-owned redis."""
    env = _stage(tmp_path)
    tee = Path(env["PATH"].split(":")[0]) / "tee"
    tee.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-a" ]; then exit 1; fi\n'
        'exec /usr/bin/tee "$@"\n'
    )
    tee.chmod(0o755)

    result = _run("falkordb_redis_install", env)
    assert result.returncode == 0, result.stderr
    assert "could not record provisioning provenance" in result.stdout


def test_an_unpinned_architecture_changes_nothing_on_the_system(tmp_path):
    """No pinned digest for this arch means the module can never verify —
    so provisioning must stop BEFORE apt, not after redis is already on.

    On arm64v8 the module install would deterministically refuse; without the
    preflight an opted-in ARM box gained a third-party apt repo and a redis
    daemon for an engine that could never load.
    """
    env = _stage(tmp_path)
    uname = Path(env["PATH"].split(":")[0]) / "uname"
    uname.write_text("#!/bin/bash\nprintf 'aarch64\\n'\n")
    uname.chmod(0o755)

    result = _run("falkordb_provision", env)
    assert result.returncode == 0, result.stderr
    assert "no pinned checksum" in result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists(), "repo added before preflight"
    assert "apt-get" not in _apt_log(env), "apt ran on an unsupportable arch"


def test_a_derivative_maps_to_its_ubuntu_base_suite(tmp_path):
    """Mint's VERSION_CODENAME is 'wilma' — no such redis suite. UBUNTU_CODENAME
    is the base it actually tracks, so the source must be written for that."""
    env = _stage(tmp_path)
    Path(env["FALKORDB_OS_RELEASE"]).write_text(
        "ID=linuxmint\nVERSION_CODENAME=wilma\nUBUNTU_CODENAME=noble\n"
    )
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "noble main" in Path(env["FALKORDB_APT_LIST"]).read_text(), (
        "wrote a suite the repo does not serve"
    )


def test_an_unserved_distro_writes_no_apt_source(tmp_path):
    """Neither UBUNTU_CODENAME nor a served ID — any suite would be a guess."""
    env = _stage(tmp_path)
    Path(env["FALKORDB_OS_RELEASE"]).write_text(
        "ID=arch\nVERSION_CODENAME=rolling\n"
    )
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "no published redis apt suite" in result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists()


def test_a_failed_apt_update_stops_and_leaves_the_box_as_it_was(tmp_path):
    """A source that cannot update is removed, and the run STOPS there.

    Even with a usable candidate still offered from a cached index, going on to
    install would re-create the removed list through the provenance stamp and
    leave the package with no update source. apt consults every list on every
    operation, so a broken suite left behind would also fail unrelated runs.
    """
    env = _stage(tmp_path)
    env["APT_RC"] = "1"  # every apt-get call fails; the candidate is 8.x
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "update failed" in result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists(), "source left or re-created"
    assert not Path(env["FALKORDB_APT_KEYRING"]).exists(), "keyring this run added left"
    assert "apt-get install" not in _apt_log(env), "installed after a failed update"


def test_a_keyring_this_run_did_not_create_is_never_touched(tmp_path):
    """An operator-managed keyring at our path is theirs: not overwritten, not
    removed, and no source of ours is pointed at it."""
    env = _stage(tmp_path)
    keyring = Path(env["FALKORDB_APT_KEYRING"])
    keyring.write_text("operator keyring")
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert keyring.read_text() == "operator keyring"
    assert not Path(env["FALKORDB_APT_LIST"]).exists()
    assert "apt-get" not in _apt_log(env)


def test_every_failure_after_the_repo_add_removes_what_this_run_added(tmp_path):
    """Leave the machine as it was found: list, keyring, and a keyring
    directory this run had to create all go when the run cannot finish."""
    for name, extra in (
        ("list write fails", {"tee": "#!/bin/bash\nexit 1\n"}),
        ("update fails", {"APT_RC": "1"}),
        ("candidate below floor", {"APT_CANDIDATE": "8.0~rc1-1rl1"}),
    ):
        case = tmp_path / name.replace(" ", "-")
        case.mkdir()
        env = _stage(case)
        env["FALKORDB_APT_KEYRING"] = str(case / "keyrings" / "redis.gpg")
        for key, value in extra.items():
            if key == "tee":
                stub = Path(env["PATH"].split(":")[0]) / "tee"
                stub.write_text(value)
                stub.chmod(0o755)
            else:
                env[key] = value
        result = _run("falkordb_redis_install", env)
        assert result.returncode == 0, (name, result.stderr)
        assert not Path(env["FALKORDB_APT_LIST"]).exists(), name
        assert not (case / "keyrings").exists(), (name, result.stdout)
        assert "apt-get install" not in _apt_log(env), name


def test_a_failed_install_keeps_the_source_it_may_have_installed_from(tmp_path):
    """Once install has run, a package from the source may be on the box in
    some state; removing the source would cut it off from updates."""
    env = _stage(tmp_path)
    env["APT_INSTALL_RC"] = "100"
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "install failed" in result.stdout
    assert Path(env["FALKORDB_APT_LIST"]).exists()


@pytest.mark.parametrize(
    ("candidate", "installs"),
    (
        ("6:8.0.4-1rl1~noble1", True),
        ("8.0.0", True),
        ("10.0.0-1", True),
        ("8.0~rc1-1rl1", False),  # a prerelease sorts BELOW 8.0.0
        ("6:8.0~rc1-1rl1", False),
        ("5:7.0.15-1build2", False),  # an epoch must not lift 7.x over the floor
        ("(none)", False),  # no candidate at all
    ),
)
def test_the_floor_compares_the_whole_version(tmp_path, candidate, installs):
    env = _stage(tmp_path)
    Path(env["FALKORDB_APT_LIST"]).write_text(
        "deb [signed-by=x] https://packages.redis.io/deb noble main\n"
    )
    env["APT_CANDIDATE"] = candidate
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert ("apt-get install" in _apt_log(env)) == installs, result.stdout


def test_a_later_run_with_a_stale_index_still_refuses_the_install(tmp_path):
    """The candidate gate must hold on the SECOND run, not only the first.

    A run that leaves `redis.list` behind when its update fails makes the
    next bootstrap skip the whole repo block — update included — and go
    straight to `apt-get install` with the same unusable index. The check
    therefore lives immediately before the install, unconditionally.
    """
    env = _stage(tmp_path)
    Path(env["FALKORDB_APT_LIST"]).write_text(
        "deb [signed-by=x] https://packages.redis.io/deb noble main\n"
    )
    env["APT_CANDIDATE"] = "6:7.0.15-1"  # stale index: only the distro's 7.x
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "apt-get update" not in _apt_log(env), "re-ran update on existing list"
    assert "apt-get install" not in _apt_log(env), "installed a below-floor redis"
    assert "below" in result.stdout and "floor" in result.stdout


def test_a_failed_apt_cache_probe_skips_without_aborting_bootstrap(tmp_path):
    """The probe runs unconditionally now, so its failure must degrade, not abort.

    A bare `candidate="$(apt-cache ... )"` assignment carries the pipeline's
    status into a caller running `set -euo pipefail` — a transient cache error
    would kill the entire bootstrap over an optional provisioning step.
    """
    env = _stage(tmp_path)
    # apt-cache exits nonzero: unreadable cache.
    apt_cache = Path(env["PATH"].split(":")[0]) / "apt-cache"
    apt_cache.write_text("#!/bin/bash\nexit 100\n")
    apt_cache.chmod(0o755)

    result = _run("falkordb_redis_install", env)  # _run sets -euo pipefail
    assert result.returncode == 0, result.stderr
    assert "could not ask apt" in result.stdout
    assert "apt-get install" not in _apt_log(env)


def test_unknown_codename_skips_before_touching_apt(tmp_path):
    env = _stage(tmp_path)
    Path(env["FALKORDB_OS_RELEASE"]).write_text("ID=weird\n")  # no codenames at all
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "no published redis apt suite" in result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists()
