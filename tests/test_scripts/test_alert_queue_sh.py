"""Tests for ``scripts/lib/alert_queue.sh`` + the watchgod transition-only guard.

The lib writes schema-v1 JSON that ``genesis.guardian.alert.queue`` reads (the
cross-language boundary), never breaks its caller, and — as wired into
``tmp_watchgod.sh`` — pages EMERGENCY once per red episode, never for warnings.
"""

import json
import os
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_LIB = _ROOT / "scripts" / "lib" / "alert_queue.sh"
_WATCHGOD = _ROOT / "scripts" / "tmp_watchgod.sh"


def _run_bash(body: str, env: dict) -> subprocess.CompletedProcess:
    script = f'set -euo pipefail\nsource "{_LIB}"\n{body}\n'
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        env=full_env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _entries(queue_root: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(queue_root.glob("*.json"))]


def test_queue_alert_writes_valid_v1_json(tmp_path):
    root = tmp_path / "queue"
    r = _run_bash(
        'queue_alert emergency backup "My title" "My body" "backup:k"',
        {"GENESIS_ALERT_QUEUE_ROOT": str(root)},
    )
    assert r.returncode == 0, r.stderr
    entries = _entries(root)
    assert len(entries) == 1
    e = entries[0]
    assert e["schema"] == 1
    assert (e["severity"], e["source"], e["title"], e["body"]) == (
        "emergency",
        "backup",
        "My title",
        "My body",
    )
    assert e["dedupe_key"] == "backup:k"
    assert e["meta"] == {}


def test_queue_alert_survives_quotes_and_newlines(tmp_path):
    root = tmp_path / "queue"
    body = "has \"double\" and 'single' quotes\nand a newline"
    # Pass the tricky body via an env var so the test itself is injection-safe.
    r = _run_bash(
        'queue_alert warning src "t" "$BODY"',
        {"GENESIS_ALERT_QUEUE_ROOT": str(root), "BODY": body},
    )
    assert r.returncode == 0, r.stderr
    entries = _entries(root)
    assert len(entries) == 1
    assert entries[0]["body"] == body  # round-trips exactly


def test_queue_alert_0600_permissions(tmp_path):
    root = tmp_path / "queue"
    _run_bash("queue_alert info s t b", {"GENESIS_ALERT_QUEUE_ROOT": str(root)})
    path = next(root.glob("*.json"))
    assert (path.stat().st_mode & 0o777) == 0o600


def test_queue_alert_never_breaks_caller(tmp_path):
    # Unwritable root (a file) must NOT abort the caller under set -e.
    blocker = tmp_path / "blocked"
    blocker.write_text("x")
    r = _run_bash(
        "queue_alert emergency s t b; echo REACHED",
        {"GENESIS_ALERT_QUEUE_ROOT": str(blocker / "queue")},
    )
    assert r.returncode == 0
    assert "REACHED" in r.stdout


def test_watchgod_emergency_transition_only(tmp_path):
    # Replicate the exact guard tmp_watchgod's red paths use, run it TWICE
    # (two polls in one red episode) → exactly ONE queued page.
    root = tmp_path / "queue"
    alert_dir = tmp_path / "alerts"
    alert_dir.mkdir()
    guard = (
        'mkdir -p "$ALERT_DIR"; '
        'if [[ ! -f "$ALERT_DIR/tmp_emergency" ]]; then '
        '  queue_alert emergency watchgod:cc "RED" "body" "watchgod:tmp_emergency"; '
        "fi; "
        'touch "$ALERT_DIR/tmp_emergency"'
    )
    env = {"GENESIS_ALERT_QUEUE_ROOT": str(root), "ALERT_DIR": str(alert_dir)}
    assert _run_bash(guard, env).returncode == 0
    assert _run_bash(guard, env).returncode == 0  # second poll, flag persists
    assert len(_entries(root)) == 1  # only the transition paged


def test_watchgod_warning_tier_never_queues():
    # D2: the warning/orange cleaners must NOT call queue_alert (warnings stay
    # dashboard-only via watchgod_state.json). Only the red cleaners page.
    text = _WATCHGOD.read_text()

    def _body(fn: str) -> str:
        start = text.index(f"{fn}()")
        return text[start : text.index("\n}\n", start)]  # to the closing brace

    for fn in ("clean_cc_orange", "clean_sys_orange", "clean_sys_yellow"):
        assert "queue_alert" not in _body(fn), f"{fn} must not page a warning"
    for fn in ("clean_cc_red", "clean_sys_red"):
        assert "queue_alert emergency" in _body(fn), f"{fn} must page emergency"
