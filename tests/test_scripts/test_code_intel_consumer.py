"""Owned future-capability simulations use the real queue and harmless providers.

Only the private runner copy's readiness response, and its handling of a ready
answer, change. The shipped source must continue refusing. A separate CLI proxy injects explicit transport faults;
it delegates normal commands to unchanged copied facade/helper sources.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.managed_code_intel_fixture import configured_sentinel, install_manager
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def consumer(queue, tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    scripts = tmp_path / "future/scripts"
    lib = scripts / "lib"
    lib.mkdir(parents=True)
    original = (ROOT / "scripts/code_intel_runner.sh").read_text()
    old = '_marker() { python3 "$MARKER_PY" "$@"; }'
    new = '_marker() { if [ "${1:-}" = automatic-ready ]; then return 0; fi; python3 "$MARKER_PY" "$@"; }'
    assert original.count(old) == 1
    # The shipped binary stops on a ready answer (exit 76); only the strict
    # cutover gives "ready" a meaning, so the private copy proceeds instead.
    stop = '    0) _log "readiness helper reported ready, but this protocol has no ready state — stopping tick"; exit 76 ;;'
    assert original.count(stop) == 1
    runner = scripts / "code_intel_runner.sh"
    runner.write_text(original.replace(old, new).replace(stop, "    0) ;;"))
    hashes = {}
    for name in (
        "index_marker.py",
        "index_marker_cli.py",
        "index_marker_legacy.py",
        "index_marker_legs.py",
        "proc_pressure.sh",
    ):
        source = ROOT / "scripts/lib" / name
        target = lib / ("real_index_marker.py" if name == "index_marker.py" else name)
        shutil.copyfile(source, target)
        assert source.read_bytes() == target.read_bytes()
        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    (lib / "index_marker.py").write_text(
        "import os,sys\nfrom pathlib import Path\n"
        "command=sys.argv[1]\n"
        "if command=='claim':\n"
        "    if os.environ.get('TEST_WRITE_BEFORE_CLAIM'):\n"
        "        import subprocess\n"
        "        subprocess.run([sys.executable,str(Path(__file__).with_name('real_index_marker.py')),'write','--repo',os.environ['TEST_QUEUE_REPO'],'--tools',os.environ['TEST_WRITE_BEFORE_CLAIM'],'--mode','full'],check=True,capture_output=True)\n"
        "    if os.environ.get('TEST_DISABLE_BEFORE_CLAIM'):\n"
        "        Path(os.environ['TEST_DISABLE_BEFORE_CLAIM']).touch()\n"
        "if command==os.environ.get('TEST_FAULT_COMMAND'):\n"
        "    if os.environ.get('TEST_FAULT_AFTER_REAL')=='1':\n"
        "        import subprocess\n"
        "        r=subprocess.run([sys.executable,str(Path(__file__).with_name('real_index_marker.py')),*sys.argv[1:]],capture_output=True)\n"
        "        sys.stderr.buffer.write(r.stderr)\n"
        "    sys.stdout.buffer.write(Path(os.environ['TEST_WIRE_PATH']).read_bytes())\n"
        "    sys.exit(int(os.environ.get('TEST_FAULT_STATUS','0')))\n"
        "os.execv(sys.executable,[sys.executable,str(Path(__file__).with_name('real_index_marker.py')),*sys.argv[1:]])\n"
    )
    provider = tmp_path / "harmless_provider.sh"
    program = tmp_path / "harmless_provider.sh.py"
    calls = tmp_path / "provider.jsonl"
    program.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        f"with Path(os.environ.get('TEST_PROVIDER_LOG',{str(calls)!r})).open('a') as stream:\n"
        "    stream.write(json.dumps({'repo':sys.argv[1],'provider':sys.argv[2],'mode':sys.argv[3]})+'\\n')\n"
        "if os.environ.get('TEST_PROVIDER_READY'):\n"
        "    import time\n"
        "    Path(os.environ['TEST_PROVIDER_READY']).write_text('provider reached')\n"
        "    deadline=time.monotonic()+15\n"
        "    while not Path(os.environ['TEST_PROVIDER_ALLOW']).exists():\n"
        "        if time.monotonic()>deadline: raise RuntimeError('owned provider barrier expired')\n"
        "        time.sleep(.01)\n"
        "if os.environ.get('TEST_ENQUEUE_AT'):\n"
        "    import subprocess\n"
        f"    step=len(Path({str(calls)!r}).read_text().splitlines())\n"
        "    if str(step)==os.environ['TEST_ENQUEUE_AT']:\n"
        "        subprocess.run([sys.executable,os.environ['TEST_QUEUE_CLI'],'write','--repo',sys.argv[1],'--tools',sys.argv[2],'--mode','fast'],check=True,capture_output=True)\n"
        "sys.exit(int(os.environ.get('TEST_'+sys.argv[2].upper()+'_RC','0')))\n"
    )
    provider.write_text('#!/usr/bin/env bash\nexec /usr/bin/python3 "$0.py" "$@"\n')
    provider.chmod(0o700)
    control_log = tmp_path / "instrument.jsonl"
    control = subprocess.run(
        ["bash", str(provider), "fixture", "gitnexus", "fast"],
        env={**os.environ, "TEST_PROVIDER_LOG": str(control_log), "TEST_GITNEXUS_RC": "0"},
        capture_output=True,
        timeout=10,
    )
    assert control.returncode == 0 and not calls.exists(), control.stderr
    assert [json.loads(line) for line in control_log.read_text().splitlines()] == [
        {"repo": "fixture", "provider": "gitnexus", "mode": "fast"}
    ]
    install_manager(scripts / "codebase_managed.py", tmp_path, repo, provider)

    def environment(extra):
        return {
            **os.environ,
            "HOME": str(tmp_path),
            "GENESIS_HOME": str(tmp_path / ".genesis"),
            "CODE_INTEL_ENTRYPOINT": str(provider),
            "CODE_INTEL_FAKE_LOADAVG": "0",
            "CODE_INTEL_FAKE_IOWAIT": "0",
            "CODE_INTEL_FAKE_CLAUDE_CPU": "0",
            "TEST_QUEUE_CLI": str(lib / "real_index_marker.py"),
            "TEST_QUEUE_REPO": str(repo),
            **extra,
        }

    def run(**extra):
        return subprocess.run(
            ["bash", str(runner)],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment(extra),
        )

    def events():
        return (
            [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        )

    return SimpleNamespace(
        queue=queue,
        repo=str(repo),
        hash=queue.marker_hash(str(repo)),
        run=run,
        events=events,
        directory=tmp_path,
        source_hashes=hashes,
        runner=runner,
        environment=environment,
    )


def _history(consumer):
    with sqlite3.connect(consumer.queue.database_path()) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM executions ORDER BY execution_seq")]


def test_two_provider_successes_settle_independent_execution_once(consumer):
    q = consumer.queue
    q.write_marker(consumer.repo, "both", "fast")
    q.stamp_full(consumer.hash)
    result = consumer.run()
    assert result.returncode == 0, result.stderr
    assert consumer.events() == [
        {"repo": consumer.repo, "provider": "cbm", "mode": "fast"},
        {"repo": consumer.repo, "provider": "gitnexus", "mode": "fast"},
    ]
    assert q.list_markers() == [] and q.get_inflight(consumer.hash) is None
    assert [row["applied_state"] for row in _history(consumer)] == ["consumed", "consumed"]
    assert consumer.run().returncode == 0 and len(consumer.events()) == 2


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("rc", ["1", "4", "5"])
def test_single_provider_failure_charges_only_its_leg(consumer, rc, provider):
    q = consumer.queue
    q.write_marker(consumer.repo, "both", "fast")
    q.stamp_full(consumer.hash)
    result = consumer.run(**{"TEST_" + provider.upper() + "_RC": rc})
    assert result.returncode == 0, result.stderr
    rows = q.list_markers()
    assert len(consumer.events()) == 2
    assert len(rows) == 1 and rows[0]["provider"] == provider and rows[0]["attempts"] == 1
    history = _history(consumer)
    assert [row["charged_attempts"] for row in history] == ([1, 0] if provider == "cbm" else [0, 1])
    assert [row["scalar_action"] for row in history] == (
        ["restore_failure", "consume"] if provider == "cbm" else ["consume", "restore_failure"]
    )


def test_list_error_stops_tick_before_any_claim_or_provider(consumer):
    q = consumer.queue
    q.write_marker(consumer.repo, "both", "fast")

    def ageless():
        return [{k: v for k, v in row.items() if k != "age_s"} for row in q.list_markers()]

    before = ageless()
    output = consumer.directory / "failed-list.bin"
    output.write_bytes(b"complete-looking-but-untrusted\0")
    result = consumer.run(
        TEST_FAULT_COMMAND="list-legs", TEST_WIRE_PATH=str(output), TEST_FAULT_STATUS="2"
    )
    assert result.returncode == 76 and not consumer.events(), result.stderr
    assert ageless() == before and _history(consumer) == []


def _wire(*fields):
    return (
        b"\0".join(field.encode() if isinstance(field, str) else field for field in fields) + b"\0"
    )


def _fault(consumer, command, raw, status, *, after=False):
    output = consumer.directory / "wire-fault.bin"
    output.write_bytes(raw)
    return consumer.run(
        TEST_FAULT_COMMAND=command,
        TEST_WIRE_PATH=str(output),
        TEST_FAULT_STATUS=str(status),
        TEST_FAULT_AFTER_REAL="1" if after else "0",
    )


def test_empty_list_is_quiet_only_in_the_owned_capability_fixture(consumer):
    consumer.queue._prepare()
    result = consumer.run()
    assert result.returncode == 0 and not consumer.events(), result.stderr
    assert _history(consumer) == []
    assert not list((consumer.directory / ".genesis").glob(".code-intel-output.*.tmp"))


@pytest.mark.parametrize("status", [1, 2, 143])
@pytest.mark.parametrize("payload", ["empty", "complete", "partial"])
def test_list_process_error_cannot_become_an_empty_or_partial_success(consumer, status, payload):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    raw = _wire("1", "pending", consumer.hash, consumer.repo, "gitnexus", "0")
    raw = b"" if payload == "empty" else raw[:-1] if payload == "partial" else raw
    result = _fault(consumer, "list-legs", raw, status)
    assert result.returncode == 76 and not consumer.events(), result.stderr
    assert len(q.list_markers()) == 1 and _history(consumer) == []


@pytest.mark.parametrize(
    "kind",
    ["unterminated", "extra", "missing", "version", "type", "hash", "repo", "provider", "age"],
)
def test_invalid_later_list_record_prevents_every_claim(consumer, kind):
    q = consumer.queue
    q.write_marker(consumer.repo, "both", "fast")
    first = ["1", "pending", consumer.hash, consumer.repo, "cbm", "0"]
    second = ["1", "pending", consumer.hash, consumer.repo, "gitnexus", "0"]
    if kind in ("version", "type", "hash", "repo", "provider", "age"):
        index = {"version": 0, "type": 1, "hash": 2, "repo": 3, "provider": 4, "age": 5}[kind]
        second[index] = "" if kind == "repo" else "bad"
    raw = _wire(*first, *second)
    if kind == "unterminated":
        raw = raw[:-1]
    if kind == "extra":
        raw += b"\0"
    if kind == "missing":
        raw = _wire(*first, *second[:-1])
    result = _fault(consumer, "list-legs", raw, 0)
    assert result.returncode == 76 and not consumer.events(), result.stderr
    assert len(q.list_markers()) == 2 and _history(consumer) == []


@pytest.mark.parametrize(
    "age",
    [
        "",
        "-1",
        "+1",
        "00",
        "01",
        " 1",
        "1\n",
        "0x10",
        "1e2",
        "١",
        "9223372036854775808",
        "18446744073709551615",
        "$(printf injected)",
    ],
)
def test_noncanonical_or_unrepresentable_age_is_never_arithmetic(consumer, age):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    raw = _wire("1", "pending", consumer.hash, consumer.repo, "gitnexus", age)
    result = _fault(consumer, "list-legs", raw, 0)
    assert result.returncode == 76 and not consumer.events(), result.stderr
    assert _history(consumer) == []


@pytest.mark.parametrize("age", ["0", "1", "9223372036854775807"])
def test_canonical_boundary_age_roundtrips_before_idle_use(consumer, age):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    raw = _wire("1", "pending", consumer.hash, consumer.repo, "gitnexus", age)
    result = _fault(consumer, "list-legs", raw, 0)
    assert result.returncode == 0 and len(consumer.events()) == 1, result.stderr
    assert q.list_markers() == []


@pytest.mark.parametrize("kind", ["empty", "unterminated", "wrong-hash", "wrong-provider", "extra"])
def test_predicate_one_requires_complete_matching_unclaimed_ack(consumer, kind):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    fields = ["1", "unclaimed", consumer.hash, "gitnexus"]
    if kind == "wrong-hash":
        fields[2] = ("1" if fields[2][0] == "0" else "0") + fields[2][1:]
    if kind == "wrong-provider":
        fields[3] = "cbm"
    raw = _wire(*fields)
    if kind == "empty":
        raw = b""
    if kind == "unterminated":
        raw = raw[:-1]
    if kind == "extra":
        raw += b"\0"
    result = _fault(consumer, "claim", raw, 1)
    assert result.returncode == 76 and not consumer.events(), result.stderr
    assert len(q.list_markers()) == 1 and _history(consumer) == []


def test_complete_matching_unclaimed_ack_is_a_predicate(consumer):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    result = _fault(consumer, "claim", _wire("1", "unclaimed", consumer.hash, "gitnexus"), 1)
    assert result.returncode == 0 and not consumer.events(), result.stderr
    assert len(q.list_markers()) == 1 and _history(consumer) == []


@pytest.mark.parametrize(
    "kind", ["empty", "unterminated", "extra", "mode", "context", "repo", "provider", "hash"]
)
def test_committed_claim_with_invalid_response_never_launches(consumer, kind):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    fields = ["1", "claim", consumer.hash, consumer.repo, "gitnexus", "fast", "opaque"]
    if kind in ("mode", "context", "repo", "provider", "hash"):
        index = {"mode": 5, "context": 6, "repo": 3, "provider": 4, "hash": 2}[kind]
        fields[index] = "" if kind in ("context", "repo") else "bad"
    raw = _wire(*fields)
    if kind == "empty":
        raw = b""
    if kind == "unterminated":
        raw = raw[:-1]
    if kind == "extra":
        raw += b"\0"
    result = _fault(consumer, "claim", raw, 0, after=True)
    assert result.returncode == 76 and not consumer.events(), result.stderr
    assert q.get_inflight(consumer.hash)["protocol"] == 2
    assert len(_history(consumer)) == 1 and _history(consumer)[0]["scalar_action"] is None


@pytest.mark.parametrize(
    "status,payload",
    [
        (1, "empty"),
        (1, "false"),
        (2, "false"),
        (143, "true"),
        (0, "partial"),
        (0, "wrong-hash"),
        (0, "extra"),
        (0, "bad-bit"),
    ],
)
def test_escalation_error_restores_owned_claim_without_launch(consumer, status, payload):
    q = consumer.queue
    q.write_marker(consumer.repo, "cbm", "fast")
    fields = ["1", "escalation", consumer.hash, "1" if payload == "true" else "0"]
    if payload == "wrong-hash":
        fields[2] = "bad"
    if payload == "bad-bit":
        fields[3] = "01"
    raw = _wire(*fields)
    if payload == "empty":
        raw = b""
    if payload == "partial":
        raw = raw[:-1]
    if payload == "extra":
        raw += b"\0"
    result = _fault(consumer, "should-escalate", raw, status)
    assert result.returncode == 76 and not consumer.events(), result.stderr
    rows = q.list_markers()
    assert len(rows) == 1 and rows[0]["attempts"] == 0 and rows[0]["mode"] == "fast"
    assert (
        q.get_inflight(consumer.hash) is None
        and _history(consumer)[0]["scalar_action"] == "restore"
    )
    assert q.get_repo_state(consumer.hash)["last_full"] is None


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("mode", ["fast", "moderate", "full"])
@pytest.mark.parametrize("rc", ["0", "3", "75", "1"])
def test_provider_mode_result_matrix_preserves_debt_and_provider_clocks(
    consumer, provider, mode, rc
):
    q = consumer.queue
    q.write_marker(consumer.repo, provider, mode)
    q.stamp_full(consumer.hash)
    before = q.get_repo_state(consumer.hash)["last_full"]
    result = consumer.run(**{"TEST_" + provider.upper() + "_RC": rc})
    assert result.returncode == 0 and len(consumer.events()) == 1, result.stderr
    assert consumer.events()[0] == {"repo": consumer.repo, "provider": provider, "mode": mode}
    rows, state = q.list_markers(), q.get_repo_state(consumer.hash)
    if rc == "0":
        assert rows == []
        assert (state["last_full"] != before) == (provider == "cbm" and mode == "full")
    else:
        assert (
            len(rows) == 1
            and rows[0]["provider"] == provider
            and rows[0]["attempts"] == (1 if rc == "1" else 0)
        )
        assert state["last_full"] == before
    assert (state["full_backoff"] is not None) == (
        provider == "cbm" and mode == "full" and rc == "3"
    )


@pytest.mark.parametrize("mode", ["fast", "moderate"])
@pytest.mark.parametrize("rc", ["0", "1", "3", "75"])
def test_escalated_full_preserves_original_generation_and_backoff_rules(consumer, mode, rc):
    q = consumer.queue
    q.write_marker(consumer.repo, "cbm", mode)
    result = consumer.run(TEST_CBM_RC=rc)
    assert result.returncode == 0 and consumer.events()[0]["mode"] == "full", result.stderr
    rows, state = q.list_markers(), q.get_repo_state(consumer.hash)
    if rc == "0":
        assert rows == [] and state["last_full"] is not None
    else:
        assert len(rows) == 1 and rows[0]["mode"] == mode and rows[0]["attempts"] == 0
        assert state["last_full"] is None
    assert (state["full_backoff"] is not None) == (rc in ("1", "3"))


@pytest.mark.parametrize(
    "extra",
    [
        {"CODE_INTEL_FAKE_LOADAVG": "3"},
        {"CODE_INTEL_FAKE_IOWAIT": "80"},
        {"CODE_INTEL_FAKE_CLAUDE_CPU": "95"},
    ],
)
def test_actual_consumer_idle_gate_defers_before_claim(consumer, extra):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    result = consumer.run(**extra)
    assert result.returncode == 0 and not consumer.events(), result.stderr
    assert _history(consumer) == [] and q.list_markers()[0]["attempts"] == 0


@pytest.mark.parametrize("load,expected", [("3", 1), ("7", 0)])
def test_starved_actual_consumer_uses_relaxed_window(consumer, load, expected):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    with q._write_db() as db:
        db.execute("UPDATE pending SET requested_at=?", (q.time.time() - 86410,))
    result = consumer.run(CODE_INTEL_FAKE_LOADAVG=load, CODE_INTEL_FAKE_CLAUDE_CPU="95")
    assert result.returncode == 0 and len(consumer.events()) == expected, result.stderr
    assert len(q.list_markers()) == 1 - expected


@pytest.mark.parametrize("sentinel_form", ["absolute", "tilde", "relative-invalid"])
def test_disabled_cbm_preserves_generation_while_gitnexus_completes_once(consumer, sentinel_form):
    q = consumer.queue
    q.write_marker(consumer.repo, "both", "fast")
    q.stamp_full(consumer.hash)
    before = {k: v for k, v in q.list_markers()[0].items() if k != "age_s"}
    home = consumer.directory
    sentinel = home / "off"
    if sentinel_form == "absolute":
        configured_sentinel(home, str(sentinel))
    elif sentinel_form == "tilde":
        configured_sentinel(home, "~/off")
    else:
        configured_sentinel(home, "relative-off")
    sentinel.touch()
    for _ in range(3):
        result = consumer.run()
        assert result.returncode == 0, result.stderr
    rows = q.list_markers()
    assert len(rows) == 1 and {k: v for k, v in rows[0].items() if k != "age_s"} == before
    assert [event["provider"] for event in consumer.events()] == ["gitnexus"]
    assert len(_history(consumer)) == 1 and _history(consumer)[0]["provider"] == "gitnexus"


def test_disablement_after_idle_before_claim_restores_exact_generation(consumer):
    q = consumer.queue
    q.write_marker(consumer.repo, "cbm", "fast")
    sentinel = consumer.directory / ".genesis/codebase-memory-mcp.disabled"
    result = consumer.run(TEST_DISABLE_BEFORE_CLAIM=str(sentinel))
    assert result.returncode == 0 and sentinel.exists() and not consumer.events(), result.stderr
    rows = q.list_markers()
    assert len(rows) == 1 and rows[0]["mode"] == "fast" and rows[0]["attempts"] == 0
    history = _history(consumer)
    assert (
        len(history) == 1
        and history[0]["scalar_action"] == "restore"
        and history[0]["charged_attempts"] == 0
    )


def test_claim_uses_new_mode_without_widening_to_new_sibling(consumer):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    result = consumer.run(TEST_WRITE_BEFORE_CLAIM="both")
    assert result.returncode == 0, result.stderr
    assert consumer.events() == [{"repo": consumer.repo, "provider": "gitnexus", "mode": "full"}]
    rows = q.list_markers()
    assert len(rows) == 1 and rows[0]["provider"] == "cbm" and rows[0]["mode"] == "full"
    assert q.get_repo_state(consumer.hash)["last_full"] is None


@pytest.mark.parametrize("new_generation", [False, True])
def test_cap_retirement_preserves_a_new_same_provider_generation(consumer, new_generation):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    for attempt in range(1, q.MAX_ATTEMPTS + 1):
        extra = {"TEST_GITNEXUS_RC": "1"}
        if new_generation and attempt == q.MAX_ATTEMPTS:
            extra["TEST_ENQUEUE_AT"] = str(q.MAX_ATTEMPTS)
        result = consumer.run(**extra)
        assert result.returncode == 0, result.stderr
    assert len(consumer.events()) == q.MAX_ATTEMPTS
    rows = q.list_markers()
    assert len(rows) == int(new_generation)
    if new_generation:
        assert rows[0]["provider"] == "gitnexus" and rows[0]["attempts"] == 0
    assert q.get_failed(consumer.hash, provider="gitnexus")["attempts"] == q.MAX_ATTEMPTS
    assert [row["charged_attempts"] for row in _history(consumer)] == list(
        range(1, q.MAX_ATTEMPTS + 1)
    )


@pytest.mark.parametrize("remembered", [False, True])
def test_reconciliation_restores_unstarted_or_settles_retained_success(consumer, remembered):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    context = q.claim(consumer.hash, provider="gitnexus")["context"]
    if remembered:
        q.remember_outcome(context, "consume")
    result = consumer.run()
    assert result.returncode == 0 and len(consumer.events()) == (0 if remembered else 1), (
        result.stderr
    )
    history = _history(consumer)
    assert q.list_markers() == [] and q.get_inflight(consumer.hash) is None
    assert [row["scalar_action"] for row in history] == (
        ["consume"] if remembered else ["restore", "consume"]
    )
    assert all(row["charged_attempts"] == 0 for row in history)


def test_legacy_possible_native_claim_is_not_reconciled_or_dispatched(consumer):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    path = q.marker_dir() / (consumer.hash + ".inflight.json")
    path.write_text(
        json.dumps(
            dict(
                repo_path=consumer.repo,
                tools="both",
                mode="fast",
                requested_at=1,
                attempts=4,
                claim_id="old",
            )
        )
    )
    q.list_markers()
    before = q.get_inflight(consumer.hash)
    result = consumer.run()
    assert result.returncode == 0 and not consumer.events(), result.stderr
    assert q.get_inflight(consumer.hash) == before and before["protocol"] == 1
    assert len(q.list_markers()) == 1 and len(_history(consumer)) == 1


@pytest.mark.parametrize("status,raw", [(0, b"unknown\n"), (0, b""), (1, b"")])
def test_applied_success_with_lost_or_invalid_answer_is_never_resent(consumer, status, raw):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    result = _fault(consumer, "apply-outcome", raw, status, after=True)
    assert result.returncode == 76 and len(consumer.events()) == 1, result.stderr
    assert (
        q.get_inflight(consumer.hash) is None
        and _history(consumer)[0]["applied_state"] == "consumed"
    )
    assert consumer.run().returncode == 0 and len(consumer.events()) == 1
    assert len(_history(consumer)) == 1


def _wait_file(path, *, process=None):
    deadline = time.monotonic() + 15
    while not path.exists():
        if process is not None and process.poll() is not None:
            pytest.fail(f"owned locker exited before readiness: {process.returncode}")
        assert time.monotonic() < deadline, "owned deterministic fixture did not reach its barrier"
        time.sleep(0.01)


def test_real_busy_completion_spools_then_next_tick_settles_without_resend(consumer):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    started = consumer.directory / "provider.started"
    allow = consumer.directory / "provider.allow"
    ready = consumer.directory / "locker.ready"
    release = consumer.directory / "locker.release"
    done = consumer.directory / "locker.done"
    assert not any(path.exists() for path in (started, allow, ready, release, done))
    script = consumer.directory / "owned_locker.py"
    script.write_text(
        "import sqlite3,sys,time\nfrom pathlib import Path\n"
        "db=sqlite3.connect(sys.argv[1],isolation_level=None)\n"
        "try:\n"
        "    db.execute('BEGIN IMMEDIATE')\n"
        "    Path(sys.argv[2]).write_text('write reservation acquired')\n"
        "    deadline=time.monotonic()+20\n"
        "    while not Path(sys.argv[3]).exists():\n"
        "        if time.monotonic()>deadline: raise RuntimeError('owned locker barrier expired')\n"
        "        time.sleep(.01)\n"
        "    db.rollback()\n"
        "finally: db.close()\n"
        "Path(sys.argv[4]).write_text('normal release')\n"
    )
    locker = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        tick = pool.submit(
            consumer.run, TEST_PROVIDER_READY=str(started), TEST_PROVIDER_ALLOW=str(allow)
        )
        try:
            _wait_file(started)
            locker = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(script),
                    str(q.database_path()),
                    str(ready),
                    str(release),
                    str(done),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _wait_file(ready, process=locker)
            allow.write_text("provider may finish after write reservation")
            result = tick.result(timeout=20)
            # The decision is retained (spooled); a busy apply defers the tick.
            assert result.returncode == 0 and len(consumer.events()) == 1, result.stderr
            log = (consumer.directory / ".genesis/code-intelligence-runner.log").read_text()
            assert "apply deferred" in log
            spools = list(q.marker_dir().glob(".outcome-spool-*.spool"))
            assert len(spools) == 1 and locker.poll() is None
            event = json.loads(spools[0].read_bytes())
            assert (
                event["version"] == 2
                and event["kind"] == "outcome"
                and event["action"] == "consume"
            )
            assert event["hash"] == consumer.hash and event["provider"] == "gitnexus"
            release.write_text("ordinary test-owner release")
            assert locker.wait(timeout=10) == 0 and done.read_text() == "normal release"
        finally:
            allow.touch()
            release.touch()
            if locker is not None:
                locker.wait(timeout=10)
        assert tick.done()
    result = consumer.run()
    assert result.returncode == 0 and len(consumer.events()) == 1, result.stderr
    assert q.list_markers() == [] and q.get_inflight(consumer.hash) is None
    assert not list(q.marker_dir().glob(".outcome-spool-*"))
    history = _history(consumer)
    assert (
        len(history) == 1
        and history[0]["applied_state"] == "consumed"
        and history[0]["charged_attempts"] == 0
    )


def test_transport_creation_failure_preserves_work_before_reconciliation(consumer):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    binpath = consumer.directory / "fault-bin"
    binpath.mkdir()
    tool = binpath / "mktemp"
    tool.write_text("#!/usr/bin/env bash\nexit 1\n")
    tool.chmod(0o700)
    result = consumer.run(PATH=str(binpath) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))
    assert result.returncode == 76 and not consumer.events(), result.stderr
    assert len(q.list_markers()) == 1 and _history(consumer) == []


@pytest.mark.parametrize("status", [1, 2, 143])
def test_committed_claim_error_status_cannot_authorize_valid_looking_output(consumer, status):
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    raw = _wire("1", "claim", consumer.hash, consumer.repo, "gitnexus", "fast", "opaque")
    result = _fault(consumer, "claim", raw, status, after=True)
    assert result.returncode == 76 and not consumer.events(), result.stderr
    assert q.get_inflight(consumer.hash)["protocol"] == 2
    assert len(_history(consumer)) == 1 and _history(consumer)[0]["scalar_action"] is None


@pytest.mark.parametrize("command", ["list-legs", "claim", "should-escalate"])
def test_actual_machine_child_signal_is_an_observation_error(consumer, command):
    q = consumer.queue
    q.write_marker(consumer.repo, "cbm", "fast")
    proxy = consumer.runner.parent / "lib/index_marker.py"
    original = proxy.read_text()
    needle = "command=sys.argv[1]\n"
    assert original.count(needle) == 1
    proxy.write_text(
        original.replace(
            needle,
            needle
            + f"if command=={command!r}:\n    import signal\n    os.kill(os.getpid(),signal.SIGTERM)\n",
        )
    )
    result = consumer.run()
    assert result.returncode == 76 and not consumer.events(), result.stderr
    rows = q.list_markers()
    assert len(rows) == 1 and rows[0]["attempts"] == 0
    history = _history(consumer)
    assert len(history) == (1 if command == "should-escalate" else 0)
    if history:
        assert history[0]["scalar_action"] == "restore" and q.get_inflight(consumer.hash) is None


@pytest.mark.parametrize("command", ["reconcile-inflight", "list-legs", "claim", "should-escalate"])
def test_busy_queue_defers_the_tick_instead_of_failing_it(consumer, command):
    """A busy queue (CLI 75) is transient: log, exit 0, no debt, no launch."""
    q = consumer.queue
    q.write_marker(consumer.repo, "cbm", "fast")
    result = _fault(consumer, command, b"", 75)
    assert result.returncode == 0 and not consumer.events(), result.stderr
    log = (consumer.directory / ".genesis/code-intelligence-runner.log").read_text()
    assert "queue busy at" in log
    rows = q.list_markers()
    assert len(rows) == 1 and rows[0]["attempts"] == 0 and rows[0]["mode"] == "fast"
    assert q.get_inflight(consumer.hash) is None
    expected = ["restore"] if command == "should-escalate" else []
    assert [row["scalar_action"] for row in _history(consumer)] == expected


def test_busy_apply_retains_the_decision_and_the_next_tick_replays_it(consumer):
    """A busy apply after a launch defers (exit 0); the provider is never rerun."""
    q = consumer.queue
    q.write_marker(consumer.repo, "gitnexus", "fast")
    first = _fault(consumer, "apply-outcome", b"", 75)
    assert first.returncode == 0 and len(consumer.events()) == 1, first.stderr
    assert q.get_inflight(consumer.hash) is not None
    second = consumer.run()
    assert second.returncode == 0 and len(consumer.events()) == 1, second.stderr
    assert q.list_markers() == [] and q.get_inflight(consumer.hash) is None
    assert [row["scalar_action"] for row in _history(consumer)] == ["consume"]
