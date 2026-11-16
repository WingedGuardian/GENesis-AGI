"""Every prepared queue-busy phase retains the exact original status contract."""

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_consumer import _fault
from tests.test_scripts.test_code_intel_consumer import consumer as consumer
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_legs import queue as queue


def health(consumer):
    root = Path(consumer.repo)
    helper = root / "scripts/lib/code_intel_busy.py"
    helper.write_text(helper.read_text() + "\ntime.monotonic_ns = lambda: 3 * 3600 * 10**9\n")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    path = consumer.directory / ".genesis/code-intel-runner-busy.json"
    path.write_text(
        json.dumps(
            dict(
                version=1,
                active=dict(boot_id=boot, first_busy_ns=0, last_busy_ns=0, due_registered=False),
                pending={},
            )
        )
    )
    return path


def notice(consumer):
    paths = list((consumer.directory / ".genesis/alerts/queue").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


@pytest.mark.parametrize(
    "endpoint,phase",
    [
        ("reconcile-inflight", "inflight"),
        ("reconcile-native", "native"),
        ("list-legs", "pending-list"),
        ("claim-native", "claim"),
        ("should-escalate", "escalation"),
    ],
)
def test_busy_boundary_records_exact_phase_without_launching(consumer, endpoint, phase):
    path = health(consumer)
    consumer.queue.write_marker(consumer.repo, "cbm", "fast")
    result = _fault(consumer, endpoint, b"", 75)
    assert result.returncode == 0 and not consumer.events(), result.stderr
    assert json.loads(path.read_text())["active"]["due_registered"] is True
    assert f"phase {phase}." in notice(consumer)["body"]


@pytest.mark.parametrize("boundary", ["disablement", "escalation"])
def test_disabled_and_escalated_claim_reconciliation_remain_busy(consumer, boundary):
    path = health(consumer)
    consumer.queue.write_marker(consumer.repo, "cbm", "fast")
    output = consumer.directory / "busy-wire"
    output.write_bytes(b"")
    env = dict(
        TEST_BUSY_CLAIM_RECONCILIATION="1",
        TEST_RECONCILE_WITNESS=str(consumer.directory / "reconcile-witness"),
    )
    if boundary == "disablement":
        env["TEST_DISABLE_BEFORE_CLAIM"] = str(
            consumer.directory / ".genesis/codebase-memory-mcp.disabled"
        )
    else:
        env.update(
            TEST_FAULT_COMMAND="should-escalate", TEST_WIRE_PATH=str(output), TEST_FAULT_STATUS="75"
        )
    result = consumer.run(**env)
    phase = "disabled-claim" if boundary == "disablement" else "escalation"
    assert result.returncode == 0 and not consumer.events(), result.stderr
    assert json.loads(path.read_text())["active"]["due_registered"] is True
    assert f"phase {phase}." in notice(consumer)["body"]


@pytest.mark.parametrize("escalation_status,reconciliation_status", [(75, 2), (2, 75)])
def test_real_error_wins_over_busy_reconciliation(
    consumer, escalation_status, reconciliation_status
):
    path = health(consumer)
    consumer.queue.write_marker(consumer.repo, "cbm", "fast")
    output = consumer.directory / "error-wire"
    output.write_bytes(b"invalid")
    result = consumer.run(
        TEST_BUSY_CLAIM_RECONCILIATION="1",
        TEST_CLAIM_RECONCILIATION_STATUS=str(reconciliation_status),
        TEST_RECONCILE_WITNESS=str(consumer.directory / "reconcile-witness"),
        TEST_FAULT_COMMAND="should-escalate",
        TEST_WIRE_PATH=str(output),
        TEST_FAULT_STATUS=str(escalation_status),
    )
    assert result.returncode == 76 and not consumer.events(), result.stderr
    assert json.loads(path.read_text())["active"] is None
    assert not list((consumer.directory / ".genesis/alerts/queue").glob("*.json"))


def test_opaque_owner_apply_error_ends_clock_without_inventing_busy(consumer):
    path = health(consumer)
    consumer.queue.write_marker(consumer.repo, "gitnexus", "fast")
    result = consumer.run(TEST_DEFER_NATIVE_APPLY="1")
    assert result.returncode == 76 and len(consumer.events()) == 1, result.stderr
    assert json.loads(path.read_text())["active"] is None
    assert not list((consumer.directory / ".genesis/alerts/queue").glob("*.json"))


def test_normal_empty_completion_ends_clock(consumer):
    path = health(consumer)
    result = consumer.run()
    assert result.returncode == 0 and not consumer.events(), result.stderr
    assert json.loads(path.read_text()) == dict(version=1, active=None, pending={})


def test_raw_waiter_does_not_reset_its_captured_child_busy_clock(consumer):
    path = health(consumer)
    consumer.queue.write_marker(consumer.repo, "cbm", "fast")
    assert _fault(consumer, "list-legs", b"", 75).returncode == 0
    before = path.read_bytes()
    result = subprocess.run(
        ["/bin/bash", str(consumer.runner)],
        capture_output=True,
        text=True,
        timeout=15,
        env=consumer.environment(
            dict(
                TEST_FAULT_COMMAND="list-legs",
                TEST_WIRE_PATH=str(consumer.directory / "wire-fault.bin"),
                TEST_FAULT_STATUS="75",
            )
        ),
    )
    assert result.returncode == 0 and not consumer.events(), result.stderr
    assert path.read_bytes() == before
    assert notice(consumer)["dedupe_key"]
    log = (consumer.directory / ".genesis/code-intelligence-runner.log").read_text()
    assert "entering captured runner before typed queue dispatch" in log
    assert "tick health observation incomplete" not in log
