"""Signal and partial-startup regressions run in their own disposable process."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["normal", "SIGTERM", "SIGINT", "bind-cancelled", "bind-error",
    "lifespan-cancelled", "lifespan-failed", "shutdown-cancelled", "force-exit"])
def test_owner_retirement_precedes_signal_replay_and_loop_teardown(tmp_path, case):
    receipt = tmp_path / "receipt.json"
    helper = Path(__file__).with_name("shared_runner_lifecycle_child.py")
    source = Path(__file__).resolve().parents[2] / "src"
    env = dict(os.environ, PYTHONPATH=str(source))
    run = subprocess.run([sys.executable, str(helper), case, str(receipt)], capture_output=True,
                         text=True, timeout=15, env=env)
    assert run.returncode in {0, -15}, run.stderr
    result = json.loads(receipt.read_text())
    events = result["events"]
    assert "owner-closed-running=True" not in events, result
    assert events.count("resources-closed") == 1, result
    assert events.count("lifespan-stopped") == 1, result
    assert events.index("lifespan-stopped") < events.index("resources-closed"), result
    if case == "SIGTERM":
        assert run.returncode == -15, result
    if case == "SIGINT":
        assert "KeyboardInterrupt" in events, result
    if case in {"bind-cancelled", "lifespan-cancelled", "shutdown-cancelled"}:
        assert "CancelledError" in events, result
