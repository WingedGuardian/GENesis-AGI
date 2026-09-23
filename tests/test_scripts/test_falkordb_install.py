"""Behavioral tests for falkordb_redis_install / consent / provisioning.

See falkordb_stubs.py for the harness. The properties under test are the ones
that would damage someone's machine if wrong: never touching an operator's
existing redis, and never aborting the caller. Artifact integrity and unit
wiring live in test_falkordb_module.py.
"""

from __future__ import annotations

from pathlib import Path

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
    # OUR marker, not the apt list file: SETUP.md tells operators to create that
    # list themselves, so branching on it would claim credit for their work.
    Path(env["FALKORDB_PROVISION_MARKER"]).write_text("2026-09-06T00:00:00Z\n")

    result = _run("falkordb_redis_install", env)
    assert result.returncode == 0, result.stderr
    assert "already provisioned" in result.stdout
    assert "your call" not in result.stdout
    assert "apt-get" not in _apt_log(env)


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


def test_local_config_can_grant_consent(tmp_path):
    """An operator should not have to export an env var on every update."""
    env = _stage(tmp_path)
    del env["GENESIS_FALKORDB_PROVISION"]
    Path(env["FALKORDB_LOCAL_CONFIG"]).write_text(
        "memory:\n  wing: x\ngraph_engine:\n  provision: true\n"
    )
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert Path(env["FALKORDB_APT_LIST"]).exists(), result.stdout


def test_a_false_config_value_is_not_consent(tmp_path):
    """The fail direction: anything not plainly true reads as no."""
    env = _stage(tmp_path)
    del env["GENESIS_FALKORDB_PROVISION"]
    for body in (
        "graph_engine:\n  provision: false\n",
        "graph_engine:\n  other: true\n",
        "other_block:\n  provision: true\n",   # provision, but not ours
        "",
    ):
        Path(env["FALKORDB_LOCAL_CONFIG"]).write_text(body)
        result = _run("falkordb_redis_install", env)
        assert result.returncode == 0, result.stderr
        assert "opt-in" in result.stdout, f"treated as consent: {body!r}"
        assert not Path(env["FALKORDB_APT_LIST"]).exists(), f"provisioned on: {body!r}"


def test_consent_is_read_only_at_the_documented_key_path(tmp_path):
    """`provision: true` consents only as a DIRECT child of a TOP-LEVEL `graph_engine:`.

    That is the one key path SETUP.md and the skip message name. This gate
    authorises putting a third-party apt repo on someone's machine, so it must
    read consent only where consent was written -- a matcher that accepts any
    nested `provision: true` turns an unrelated sub-block into permission for a
    system change the operator never agreed to.
    """
    env = _stage(tmp_path)
    del env["GENESIS_FALKORDB_PROVISION"]
    not_consent = (
        # Nested UNDER graph_engine -- `graph_engine.provision` is itself unset.
        "graph_engine:\n  backends:\n    experimental:\n      provision: true\n",
        # `graph_engine` nested under something else is a different key path.
        "unrelated:\n  graph_engine:\n    provision: true\n",
        # A commented-out value is a decision NOT taken.
        "graph_engine:\n  # provision: true\n",
    )
    for body in not_consent:
        Path(env["FALKORDB_LOCAL_CONFIG"]).write_text(body)
        result = _run("falkordb_redis_install", env)
        assert result.returncode == 0, result.stderr
        assert "opt-in" in result.stdout, f"treated as consent: {body!r}"
        assert not Path(env["FALKORDB_APT_LIST"]).exists(), f"provisioned on: {body!r}"


def test_consent_still_reads_when_it_is_not_the_first_key(tmp_path):
    """The narrowing must not become an under-read.

    An operator who writes other graph_engine settings -- including a nested
    block -- before `provision:` has still consented. Without this the fix for
    the over-read above could silently blind the gate instead of scoping it.
    """
    env = _stage(tmp_path)
    del env["GENESIS_FALKORDB_PROVISION"]
    Path(env["FALKORDB_LOCAL_CONFIG"]).write_text(
        "graph_engine:\n"
        "  backends:\n"
        "    experimental: true\n"
        "  provision: true\n"
        "memory:\n"
        "  wing: x\n"
    )
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert Path(env["FALKORDB_APT_LIST"]).exists(), result.stdout


def test_a_later_false_overrides_an_earlier_true(tmp_path):
    """Duplicate keys are legal YAML and PyYAML keeps the LAST one.

    `provision: true` followed by `provision: false` is a withdrawal, not
    consent — a matcher that OR-accumulates (any true anywhere grants) would
    add an apt repo to a box whose operator changed their mind.
    """
    env = _stage(tmp_path)
    del env["GENESIS_FALKORDB_PROVISION"]
    for body, consents in (
        ("graph_engine:\n  provision: true\n  provision: false\n", False),
        ("graph_engine:\n  provision: false\n  provision: true\n", True),
    ):
        Path(env["FALKORDB_LOCAL_CONFIG"]).write_text(body)
        result = _run("falkordb_redis_install", env)
        assert result.returncode == 0, result.stderr
        assert Path(env["FALKORDB_APT_LIST"]).exists() == consents, (
            f"last-key-wins not honoured: {body!r}"
        )
        Path(env["FALKORDB_APT_LIST"]).unlink(missing_ok=True)


def test_a_repeated_graph_engine_block_restarts_consent(tmp_path):
    """Duplicate TOP-LEVEL mappings are legal YAML; PyYAML keeps the LAST.

    An earlier block's `provision: true` must not survive into a file whose
    effective `graph_engine` mapping never consented — the gate must disagree
    with a withdrawn decision, not with itself.
    """
    env = _stage(tmp_path)
    del env["GENESIS_FALKORDB_PROVISION"]
    for body, consents in (
        # Earlier block consents; the LAST mapping (what PyYAML loads) does not.
        ("graph_engine:\n  provision: true\ngraph_engine:\n  backend: x\n", False),
        # Earlier block silent; the effective mapping consents.
        ("graph_engine:\n  backend: x\ngraph_engine:\n  provision: true\n", True),
    ):
        Path(env["FALKORDB_LOCAL_CONFIG"]).write_text(body)
        result = _run("falkordb_redis_install", env)
        assert result.returncode == 0, result.stderr
        assert Path(env["FALKORDB_APT_LIST"]).exists() == consents, (
            f"last-block-wins not honoured: {body!r}"
        )
        Path(env["FALKORDB_APT_LIST"]).unlink(missing_ok=True)


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
    assert "disable --now' failed" in result.stdout
    assert "still enabled on :6379" in result.stdout
    assert "system unit disabled" not in result.stdout, "claimed the posture anyway"
    # And no 'completed provisioning' marker — a later run must retry the
    # stand-down, not print 'already provisioned' over a live system daemon.
    assert not Path(env["FALKORDB_PROVISION_MARKER"]).exists()


def test_a_unit_still_enabled_after_disable_leaves_no_marker(tmp_path):
    """The verify-failure path must leave the marker unwritten too."""
    env = _stage(tmp_path)
    env["SYSTEMCTL_IS_ENABLED_RC"] = "0"
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "still enabled" in result.stdout
    assert not Path(env["FALKORDB_PROVISION_MARKER"]).exists()


def test_a_unit_still_enabled_after_disable_reports_it(tmp_path):
    """The disable can succeed while the unit stays enabled — verify, don't trust."""
    env = _stage(tmp_path)
    env["SYSTEMCTL_IS_ENABLED_RC"] = "0"  # unit still enabled after the disable
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "still enabled" in result.stdout
    assert "system unit disabled" not in result.stdout, "claimed the posture anyway"


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


def test_a_failed_apt_update_refuses_a_below_floor_candidate(tmp_path):
    """A stale index can only offer the distro's 7.x, which the module refuses.

    Installing it anyway would put a database daemon on the box that cannot
    run the engine it was installed for — so a failed `apt-get update` gates
    the install on a verified candidate >= the floor.
    """
    env = _stage(tmp_path)
    env["APT_RC"] = "1"           # every apt-get call fails
    env["APT_CANDIDATE"] = "6:7.0.15-1"  # what a stale index offers on noble
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "update failed" in result.stdout
    # A source that cannot update must not be LEFT: apt consults every list on
    # every operation, so a broken suite would fail unrelated apt runs too.
    assert not Path(env["FALKORDB_APT_LIST"]).exists(), "broken source left enabled"
    assert "below" in result.stdout and "floor" in result.stdout
    assert "apt-get update" in _apt_log(env)
    assert "apt-get install" not in _apt_log(env), "installed an unusable redis"


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
