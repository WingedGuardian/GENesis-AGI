"""A failed observer must let the owned launcher clean its separate child."""

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]


def test_observer_failure_uses_graceful_owned_launcher_cleanup(tmp_path, monkeypatch):
    harness = private_module("validator_native_harness_under_test", ROOT / "tests/native_codex_validator_terminal.py")
    marker = tmp_path / "ready.json"
    program = f'''import json,os,pathlib,signal,subprocess,sys,time
child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True)
def stop(*args):
 os.killpg(child.pid,signal.SIGTERM); child.wait(timeout=5); sys.exit(2)
signal.signal(signal.SIGINT,stop)
p=pathlib.Path({str(marker)!r}); t=p.with_suffix('.pending')
t.write_text(json.dumps({{"child":child.pid}})); t.rename(p)
time.sleep(60)
'''
    owned = {}

    def failed_observer(process, *args):
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        owned["pid"] = json.loads(marker.read_text())["child"]
        owned["before"] = harness.process_state(owned["pid"])
        raise RuntimeError("Fixture observer failed before its stop assertion")

    monkeypatch.setattr(harness, "cancel_native_probe", failed_observer)
    try:
        with pytest.raises(RuntimeError, match="Fixture observer failed"):
            harness.run_launcher([sys.executable, "-c", program], dict(os.environ), 15,
                                 cancellation=(tmp_path, {}))
        after = harness.process_state(owned["pid"])
        assert after is None or after["state"] == "Z"
    finally:
        if owned:
            current = harness.process_state(owned["pid"])
            if current and current["state"] != "Z" and current["start"] == owned["before"]["start"]:
                os.killpg(owned["pid"], signal.SIGTERM)
