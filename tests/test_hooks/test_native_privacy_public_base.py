"""Actual adapter proof uses a local-only native Git transport and owned refs."""
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_hooks._privacy_public_world import LocalPublicBaseWorld
from tests.test_hooks.test_native_push_privacy import SOURCE, _record, _worker


@pytest.fixture
def public_world(tmp_path):
    world = LocalPublicBaseWorld(tmp_path)
    config = world.root / ".genesis/config"
    config.mkdir(parents=True)
    (config / "genesis.yaml").write_text("github:\n  user: fixture\n  public_repo: public\n")
    fingerprints = world.root / "fingerprints.txt"
    fingerprints.write_text("public main\n")
    world.env["GENESIS_RELEASE_FINGERPRINTS"] = str(fingerprints)
    return world


def evaluate(world, destination="git@github.com:fixture/public.git"):
    before = world.snapshot()
    result = _worker((world.client, world.env, None, None, None), _record(world.outgoing), destination=destination)
    assert world.snapshot() == before
    return result


@pytest.mark.parametrize("destination", ["git@github.com:fixture/public.git", "ssh://git@github.com/fixture/public.git"])
def test_fresh_actual_destination_base_excludes_only_proved_public_history(public_world, destination):
    result = evaluate(public_world, destination)
    assert result["verdict"] == "CLEAN" and result["degraded"] == []
    assert public_world.transport()[-1]["allowed"] is True


def test_master_fallback_only_when_main_is_absent(public_world):
    first = evaluate(public_world)
    public_world.advertise(None, public_world.main)
    fallback = evaluate(public_world)
    assert fallback["verdict"] == "CLEAN" and fallback["degraded"] == []
    assert fallback["identity"] != first["identity"]


def test_unavailable_advertised_main_cannot_use_available_master(public_world):
    remote_only = public_world.commit(public_world.root / "public.git", "remote-only", public_world.main)
    public_world.advertise(remote_only, public_world.master)
    result = evaluate(public_world)
    assert result["verdict"] == "TIER_A"
    assert result["degraded"] == ["public-base-unproved"]


@pytest.mark.parametrize("failure", ["absent", "transport"])
def test_missing_proof_scans_full_history_explicitly(public_world, failure):
    if failure == "absent":
        public_world.advertise(None, None)
    else:
        public_world.env["R8_SHIM_FAIL"] = "1"
    result = evaluate(public_world)
    assert result["verdict"] == "TIER_A"
    assert result["degraded"] == ["public-base-unproved"]


def test_private_origin_cannot_certify_public_ancestry(public_world):
    Path(public_world.env["GENESIS_RELEASE_FINGERPRINTS"]).write_text("synthetic private origin content\n")
    result = evaluate(public_world)
    assert result["verdict"] == "TIER_A" and result["degraded"] == []


def test_changed_advertised_base_changes_identity_without_fetch(public_world):
    first = evaluate(public_world)
    public_world.advertise(public_world.master, public_world.main)
    second = evaluate(public_world)
    assert first["verdict"] == second["verdict"] == "CLEAN"
    assert first["identity"] != second["identity"]


@pytest.mark.parametrize("known_value", [False, True])
def test_stalled_public_proof_leaves_time_for_complete_fallback(public_world, known_value):
    world = public_world
    if not known_value:
        Path(world.env["GENESIS_RELEASE_FINGERPRINTS"]).write_text("unmatched-synthetic-fixture\n")
    receipt = world.root / "sleeping-transport.json"
    world.shim.write_text(f"#!{sys.executable}\nimport json, os, time\n"
                          f"open({str(receipt)!r}, 'w').write(json.dumps([os.getpgrp(), os.getpid()]))\n"
                          "time.sleep(30)\n")
    before = world.snapshot()
    proc = subprocess.Popen([sys.executable, str(SOURCE / "scripts/hooks/push_privacy_scan.py"),
                             str(SOURCE), str(world.client / ".git"), "--", "public", "git@github.com:fixture/public.git"],
                            cwd=world.client, env=world.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    started = time.monotonic()
    try:
        output, error = proc.communicate(_record(world.outgoing), timeout=12)
        assert proc.returncode == (2 if known_value else 0), error
        assert (b"TIER_A" if known_value else b"degraded public-base proof") in error
        assert time.monotonic() - started < 12
        assert world.snapshot() == before
        group, transport = json.loads(receipt.read_text())
        with pytest.raises(ProcessLookupError):
            os.killpg(group, 0)
        assert not Path(f"/proc/{transport}").exists()
    finally:
        if receipt.exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(json.loads(receipt.read_text())[0], signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=3)
