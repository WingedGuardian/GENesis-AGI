"""Exercise the real bounded supervisor with owned harmless worker processes."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from pathlib import Path

import pytest

from tests.conftest import private_module

SOURCE = Path(__file__).resolve().parents[2]


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    module = private_module("privacy_lifetime", SOURCE / "scripts/hooks/push_privacy_scan.py")
    worker = tmp_path / "worker.py"
    monkeypatch.setattr(module, "__file__", str(worker))
    monkeypatch.setattr(module, "SCAN_SECONDS", 3.0)
    common = str(tmp_path / "common")
    payload = dict(verdict="CLEAN", identity="a" * 64, findings=[], reasons=[], degraded=[], common_dir=common)

    def run(body, value=None):
        worker.write_text("import json, os, sys, time, subprocess\n" + body)
        if value is not None:
            worker.write_text(worker.read_text().replace("PAYLOAD", repr(json.dumps(value))))
        return asyncio.run(module.scan(SOURCE, ["public", "synthetic-destination"], b"", common_dir=common))

    return module, worker, payload, run


@pytest.mark.parametrize("tail, expected", [("", "CLEAN"), ("sys.exit(7)", "COULD_NOT_SCAN"),
                                           ("print('unexpected')", "COULD_NOT_SCAN"),
                                           ("time.sleep(30)", "COULD_NOT_SCAN")])
def test_success_requires_complete_worker_exit(supervisor, tail, expected):
    module, worker, payload, run = supervisor
    start = time.monotonic()
    result = run("print(PAYLOAD, flush=True)\n" + tail, payload)
    assert result.verdict == expected
    assert time.monotonic() - start < module.SCAN_SECONDS + 1


@pytest.mark.parametrize("tail", ["sys.exit(7)", "print('unexpected')", "time.sleep(30)"])
def test_already_detected_known_value_survives_worker_failure(supervisor, tail):
    module, worker, payload, run = supervisor
    payload.update(verdict="TIER_A", findings=[["known-value", "b" * 64]])
    result = run("print(PAYLOAD, flush=True)\n" + tail, payload)
    assert result.verdict == "TIER_A" and result.findings == (("known-value", "b" * 64),)


@pytest.mark.parametrize("field, value", [("reasons", "raw-private-value"), ("degraded", ["unsafe text"]),
                                         ("common_dir", "/wrong/namespace"), ("findings", "bad"),
                                         ("identity", None)])
def test_malformed_worker_result_never_qualifies(supervisor, field, value):
    module, worker, payload, run = supervisor
    payload[field] = value
    result = run("print(PAYLOAD, flush=True)", payload)
    assert result.verdict == "COULD_NOT_SCAN"


def _running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_stop_owned_worker_descendant(supervisor, tmp_path, cancel):
    module, worker, payload, run = supervisor
    receipt = tmp_path / "pids.json"
    worker.write_text("import json, os, subprocess, sys, time\n"
                      "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                      f"open({str(receipt)!r}, 'w').write(json.dumps([os.getpid(), child.pid]))\n"
                      "time.sleep(30)\n")

    async def exercise():
        task = asyncio.create_task(module.scan(SOURCE, ["public", "fixture"], b"", common_dir=payload["common_dir"]))
        if cancel:
            for _ in range(100):
                if receipt.exists():
                    break
                await asyncio.sleep(0.01)
            assert receipt.exists()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).verdict == "COULD_NOT_SCAN"

    try:
        asyncio.run(exercise())
        pids = json.loads(receipt.read_text())
        for _ in range(100):
            if not any(_running(pid) for pid in pids):
                break
            time.sleep(0.01)
        assert not any(_running(pid) for pid in pids)
    finally:
        if receipt.exists():
            leader = json.loads(receipt.read_text())[0]
            with contextlib.suppress(ProcessLookupError):
                os.killpg(leader, signal.SIGKILL)


def test_oversized_worker_output_is_unknown(supervisor):
    module, worker, payload, run = supervisor
    assert run("print('x' * 70000, flush=True)").verdict == "COULD_NOT_SCAN"
