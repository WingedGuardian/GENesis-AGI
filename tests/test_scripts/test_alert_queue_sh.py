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


def test_an_identity_already_waiting_is_not_queued_twice(tmp_path):
    """A caller restarting in a loop while delivery is down re-sends the same
    key; the queue keeps one copy and reports it durable."""
    root = tmp_path / "queue"
    r = _run_bash(
        'queue_alert_try critical wg "T1" "B1" "wg:k1" && echo first\n'
        'queue_alert_try critical wg "T2" "B2" "wg:k1" && echo second\n'
        'queue_alert_try critical wg "T3" "B3" "wg:k2" && echo third\n'
        'queue_alert_try critical wg "T4" "B4" && echo nokey1\n'
        'queue_alert_try critical wg "T5" "B5" && echo nokey2',
        {"GENESIS_ALERT_QUEUE_ROOT": str(root)},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["first", "second", "third", "nokey1", "nokey2"]
    titles = sorted(e["title"] for e in _entries(root))
    assert titles == ["T1", "T3", "T4", "T5"], "same key once; no key never deduped"


def test_bytes_that_are_not_utf8_still_queue(tmp_path):
    """A file or process name may hold any bytes; the alert must still queue
    (with replacement characters), never fail and retry forever."""
    root = tmp_path / "queue"
    r = _run_bash(
        "queue_alert_try critical wg \"T $(printf 'a\\377b')\" \"B $(printf '\\376')\" \"k:$(printf '\\375')\" && echo QUEUED",
        {"GENESIS_ALERT_QUEUE_ROOT": str(root)},
    )
    assert r.returncode == 0, r.stderr
    assert "QUEUED" in r.stdout
    (e,) = _entries(root)
    assert e["title"] == "T a\ufffdb" and e["body"] == "B \ufffd"
    assert e["dedupe_key"] == "k:\ufffd"


# --- only a deliverable entry suppresses a same-key write (#3025) ------------
# The shell's dedupe scan mirrors genesis.guardian.alert.queue.v1_entry_problem
# (it runs isolated and cannot import it). A same-key file the drainer would
# quarantine is not a delivery; skipping the write on its account loses the page.


def _v1(**overrides) -> dict:
    entry = {
        "schema": 1, "ts": 1000.0, "severity": "warning", "source": "s",
        "title": "t", "body": "b", "dedupe_key": "k", "meta": {},
    }
    entry.update(overrides)
    return entry


def _drop(**keys) -> str:
    return json.dumps({k: v for k, v in _v1().items() if k not in keys})


_PARITY_CORPUS = [
    ("valid", json.dumps(_v1())),
    ("valid-int-ts", json.dumps(_v1(ts=1000))),
    ("valid-body-only", json.dumps(_v1(title=""))),
    ("valid-meta-null", json.dumps(_v1(meta=None))),
    ("valid-meta-absent", _drop(meta=1)),
    ("valid-other-key", json.dumps(_v1(dedupe_key="other"))),
    ("ts-string", json.dumps(_v1(ts="soon"))),
    ("ts-null", json.dumps(_v1(ts=None))),
    ("ts-bool", json.dumps(_v1(ts=True))),
    ("ts-nan", json.dumps(_v1()).replace('"ts": 1000.0', '"ts": NaN')),
    ("ts-infinity", json.dumps(_v1()).replace('"ts": 1000.0', '"ts": Infinity')),
    ("ts-missing", _drop(ts=1)),
    ("schema-2", json.dumps(_v1(schema=2))),
    ("schema-true", json.dumps(_v1(schema=True))),
    ("schema-missing", _drop(schema=1)),
    ("title-int", json.dumps(_v1(title=5))),
    ("severity-int", json.dumps(_v1(severity=3))),
    ("both-empty", json.dumps(_v1(title="", body=""))),
    ("dedupe-key-int", json.dumps(_v1(dedupe_key=7))),
    ("meta-list", json.dumps(_v1(meta=[1]))),
    ("meta-cause-int", json.dumps(_v1(meta={"likely_cause": 1}))),
    ("not-an-object", json.dumps([1])),
    ("bad-json", "{nope"),
    ("ts-huge-int", json.dumps(_v1()).replace('"ts": 1000.0', '"ts": 1' + "0" * 400)),
    ("deeply-nested", "[" * 200000 + "]" * 200000),
]


def _generated_corpus():
    # Generated from the Python check's own tuples, so a field dropped from
    # EITHER copy fails parity.
    from genesis.guardian.alert.queue import _HOST_META_KEYS, _TEXT_FIELDS

    out = [(f"gen-{f}-int", json.dumps(_v1(**{f: 5}))) for f in _TEXT_FIELDS]
    out += [(f"gen-meta-{f}-int", json.dumps(_v1(meta={f: 5}))) for f in _HOST_META_KEYS]
    return out


def test_shell_dedupe_matches_python_predicate_on_one_corpus(tmp_path):
    from genesis.guardian.alert.queue import v1_entry_problem

    mismatches = []
    for name, raw in _PARITY_CORPUS + _generated_corpus():
        root = tmp_path / name
        root.mkdir()
        (root / "queued.json").write_text(raw)
        r = _run_bash(
            'queue_alert warning s "t" "b" "k"', {"GENESIS_ALERT_QUEUE_ROOT": str(root)}
        )
        assert r.returncode == 0, r.stderr
        written = len(list(root.glob("*.json"))) == 2
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            parsed = None
        suppress = (
            parsed is not None
            and v1_entry_problem(parsed) is None
            and parsed.get("dedupe_key") == "k"
        )
        if written == suppress:
            mismatches.append(name)
    assert mismatches == []


def test_invalid_same_key_entry_does_not_suppress_the_shell_write(tmp_path):
    root = tmp_path / "queue"
    root.mkdir()
    (root / "queued.json").write_text(json.dumps(_v1(ts=None)))
    r = _run_bash('queue_alert warning s "t" "b" "k"', {"GENESIS_ALERT_QUEUE_ROOT": str(root)})
    assert r.returncode == 0, r.stderr
    assert len(list(root.glob("*.json"))) == 2


def test_valid_same_key_entry_still_suppresses_the_shell_write(tmp_path):
    root = tmp_path / "queue"
    root.mkdir()
    (root / "queued.json").write_text(json.dumps(_v1()))
    r = _run_bash('queue_alert warning s "t" "b" "k"', {"GENESIS_ALERT_QUEUE_ROOT": str(root)})
    assert r.returncode == 0, r.stderr
    assert len(list(root.glob("*.json"))) == 1


# --- alert_stable_key: a dedupe key that ignores per-run lines (R3) ---------

_FAILURE = (
    "🚨 *Backup failed*\n\nReason: {reason}\nTime: {time}\n"
    "SQLite lines: {lines}\nDuration: {dur}s"
)


def _stable_key(message: str, prefix: str = "backup") -> str:
    r = _run_bash('alert_stable_key "$PREFIX" "$MSG"', {"PREFIX": prefix, "MSG": message})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_stable_key_ignores_time_duration_and_sqlite_lines():
    a = _FAILURE.format(reason="push rejected", time="2026-09-20T06:10:01-04:00", lines=101, dur=42)
    b = _FAILURE.format(reason="push rejected", time="2026-09-20T12:10:07-04:00", lines=207, dur=97)
    ka, kb = _stable_key(a), _stable_key(b)
    assert ka == kb
    assert ka.startswith("backup:") and len(ka) == len("backup:") + 12


def test_stable_key_differs_for_a_different_reason():
    a = _FAILURE.format(reason="push rejected", time="t1", lines=1, dur=1)
    b = _FAILURE.format(reason="disk full", time="t1", lines=1, dur=1)
    assert _stable_key(a) != _stable_key(b)


def test_whole_message_md5_control_gives_two_keys():
    """Negative control: the previous key (md5 of the whole message) changed on
    every run, so a repeated failure was never collapsed by its key."""
    import hashlib

    a = _FAILURE.format(reason="push rejected", time="t1", lines=1, dur=1)
    b = _FAILURE.format(reason="push rejected", time="t2", lines=1, dur=2)
    old = [hashlib.md5(m.encode()).hexdigest()[:12] for m in (a, b)]
    assert old[0] != old[1]
    assert _stable_key(a) == _stable_key(b)


def test_stable_key_survives_pipefail_when_every_line_is_filtered():
    r = _run_bash(
        'k=$(alert_stable_key backup "$MSG"); echo "k=$k"',
        {"MSG": "Time: now\nDuration: 3s"},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith("k=backup:")
    assert len(r.stdout.strip()) == len("k=backup:") + 12


def test_stable_key_keeps_lines_that_merely_mention_time():
    # Only lines STARTING with the per-run labels are dropped.
    a = "Reason: Time: budget exceeded\nTime: t1"
    b = "Reason: Time: something else\nTime: t1"
    assert _stable_key(a) != _stable_key(b)
