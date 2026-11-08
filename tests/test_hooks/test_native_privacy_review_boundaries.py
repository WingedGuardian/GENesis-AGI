"""Owned controls for the two confirmed architecture findings."""
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_hooks.test_native_push_privacy import SOURCE, _record, _worker
from tests.test_hooks.test_native_push_privacy import native_world as _native_world


@pytest.fixture
def native_world(tmp_path, request):
    return _native_world.__wrapped__(tmp_path, request)


@pytest.mark.parametrize("surface", ["commit", "tag"])
def test_anchored_fingerprint_matches_message_body_lines(native_world, surface):
    repo, env, git, commit, invoke = native_world
    canary = "SYNTHETIC_ANCHORED_BODY"
    Path(env["GENESIS_RELEASE_FINGERPRINTS"]).write_text("^" + canary + "$\n")
    message = "safe subject\n\n" + canary + "\nsafe tail"
    oid = commit(message if surface == "commit" else "safe")
    if surface == "tag":
        raw = f"object {oid}\ntype commit\ntag fixture\ntagger Fixture <fixture@example.invalid> 1 +0000\n\n{message}\n".encode()
        oid = git("hash-object", "-w", "-t", "tag", "--stdin", data=raw).decode().strip()
    result = _worker(native_world, _record(oid))
    assert result["verdict"] == "TIER_A"


def test_native_signal_reaps_worker_and_git_descendants(native_world, tmp_path):
    repo, env, git, commit, invoke = native_world
    tools = tmp_path / "owned-tools"
    tools.mkdir()
    receipt = tmp_path / "owned-pids.json"
    shim = tools / "git"
    shim.write_text(f"#!{sys.executable}\nimport json, os, subprocess, sys, time\n"
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                    f"open({str(receipt)!r}, 'w').write(json.dumps([os.getppid(), os.getpid(), child.pid]))\n"
                    "time.sleep(30)\n")
    shim.chmod(0o755)
    env = dict(env, PATH=str(tools) + os.pathsep + env.get("PATH", ""))
    proc = subprocess.Popen([sys.executable, str(SOURCE / "scripts/hooks/push_privacy_scan.py"),
                             str(SOURCE), str(repo / ".git"), "--", "public", "fixture"],
                            cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        for _ in range(300):
            if receipt.exists():
                break
            time.sleep(0.01)
        assert receipt.exists()
        pids = json.loads(receipt.read_text())
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=5)
        for _ in range(100):
            living = []
            for pid in pids:
                with contextlib.suppress(FileNotFoundError):
                    if Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z":
                        living.append(pid)
            if not living:
                break
            time.sleep(0.01)
        assert not living, "native cancellation must not orphan scan workers or Git children"
        assert proc.returncode != 0
    finally:
        if receipt.exists():
            worker = json.loads(receipt.read_text())[0]
            with contextlib.suppress(ProcessLookupError):
                os.killpg(worker, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=3)


@pytest.mark.parametrize("verdict", ["TIER_A", "TIER_B", "CLEAN", "COULD_NOT_SCAN"])
def test_native_signal_preserves_only_already_accepted_tier_a(tmp_path, verdict):
    receipt, accepted = tmp_path / "worker.pid", tmp_path / "accepted"
    scanner = tmp_path / "observed-scanner.py"
    source = (SOURCE / "scripts/hooks/push_privacy_scan.py").read_text()
    worker = "        if worker:\n            return await _Scan(source, args[3:], data, common).evaluate()"
    fixture = ("        if worker:\n"
               f"            Path({str(receipt)!r}).write_text(str(os.getpid()))\n"
               f"            print(json.dumps(asdict(ScanResult({verdict!r}, 'a' * 64, ((\"known-value\", 'b' * 64),), common_dir=common))), flush=True)\n"
               "            await asyncio.sleep(30)\n"
               "            return ScanResult('CLEAN', 'a' * 64, common_dir=common)")
    observed = "        if await proc.stdout.read(1) or await proc.wait():"
    assert source.count(worker) == source.count(observed) == 1
    source = source.replace(worker, fixture).replace(observed,
        f"        Path({str(accepted)!r}).write_text('accepted')\n" + observed)
    scanner.write_text(source)
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "GENESIS_RELEASE_FINGERPRINTS": str(tmp_path / "absent-fingerprints")}
    proc = subprocess.Popen([sys.executable, str(scanner), str(SOURCE), str(tmp_path / "common"), "--", "public", "fixture"],
                            cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        for _ in range(300):
            if accepted.exists():
                break
            time.sleep(0.01)
        assert accepted.exists()
        os.killpg(proc.pid, signal.SIGTERM)
        output, error = proc.communicate(timeout=5)
        assert proc.returncode == 2
        assert (b"TIER_A" if verdict == "TIER_A" else b"COULD_NOT_SCAN") in error
        with pytest.raises(ProcessLookupError):
            os.killpg(int(receipt.read_text()), 0)
    finally:
        if receipt.exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(receipt.read_text()), signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=3)
