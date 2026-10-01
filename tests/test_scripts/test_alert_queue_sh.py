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


def test_watchgod_pages_through_the_queue_with_per_tier_dedupe_keys():
    """The watchgod's pages go through this queue, one dedupe key per
    (filesystem, tier): a key shared across tiers would let the ORANGE
    WARNING swallow the RED EMERGENCY that follows it. Behaviour (once per
    episode, the EMERGENCY still arriving after a WARNING) is pinned against the
    real script in test_watchgod_disk_guardian.py; this only checks the script
    still routes both pages here with distinct keys.

    v1 of the watchgod paged RED only and kept ORANGE dashboard-only (design
    D2). v2 pages a WARNING at ORANGE by design: ORANGE now means the whole
    disk is filling, not that one directory crossed a budget."""
    text = _WATCHGOD.read_text()
    # _wg_page routes both through queue_alert_try with source watchgod:disk.
    assert 'queue_alert_try "$1" "watchgod:disk"' in text
    assert '_wg_page warning "Disk filling' in text
    assert '_wg_page emergency "Disk nearly full' in text
    assert '"watchgod:disk:${dev}:orange"' in text
    assert '"watchgod:disk:${dev}:red"' in text


def test_queue_alert_try_reports_failure_and_queue_alert_still_never_does(tmp_path):
    """The watchgod marks a page sent only when queue_alert_try returned 0; a
    queue it cannot write (a file where the directory should be) must say so,
    while plain queue_alert keeps its never-break-the-caller contract."""
    blocker = tmp_path / "file"
    blocker.write_text("")
    env = {"GENESIS_ALERT_QUEUE_ROOT": str(blocker / "queue")}
    r = _run_bash('if queue_alert_try emergency x t b; then echo OK; else echo FAILED; fi', env)
    assert r.stdout.strip() == "FAILED"
    r = _run_bash("queue_alert emergency x t b; echo survived", env)
    assert r.returncode == 0 and r.stdout.strip() == "survived"
    good = tmp_path / "q"
    r = _run_bash('queue_alert_try emergency x t b && echo OK', {"GENESIS_ALERT_QUEUE_ROOT": str(good)})
    assert r.stdout.strip() == "OK" and len(_entries(good)) == 1


def test_queue_alert_try_fails_when_the_write_itself_fails(tmp_path):
    """The directory exists but cannot take a new file (a full or read-only
    disk): the Python write fails, the status says so, and no partial temp is
    left for the drainer."""
    root = tmp_path / "queue"
    root.mkdir()
    root.chmod(0o555)
    try:
        r = _run_bash('if queue_alert_try emergency x t b; then echo OK; else echo FAILED; fi',
                      {"GENESIS_ALERT_QUEUE_ROOT": str(root)})
    finally:
        root.chmod(0o755)
    assert r.stdout.strip() == "FAILED"
    assert list(root.iterdir()) == []
