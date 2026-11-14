"""Fault attribution preserves native outcomes and incomplete-census evidence."""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests import _live_writes as audit
from tests import _live_writes_report as codec
from tests.test_live_writes import _SUBRUN_CONFTEST
from tests.test_live_writes import home as home
from tests.test_live_writes_reporting import _subrun


def _diagnostics():
    return [row for row in audit._S.hits if row[1] == "observer fault"]


def test_native_write_keeps_fault_attribution_and_known_destination(home, monkeypatch):
    target = home / ".genesis" / "owned"
    original = audit.os.lstat

    def unavailable(path, *args, **kwargs):
        if path == str(target):
            raise PermissionError("private error text")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(audit.os, "lstat", unavailable)
    with target.open("x") as output:
        output.write("actual native write")
    assert target.read_text() == "actual native write"
    assert audit._S.faults == 1
    assert any(row[2:] == ("write", str(target)) for row in audit._S.hits)
    assert [row[1:] for row in _diagnostics()] == [
        ("observer fault", "open/open-no-effect/PermissionError", "<not captured>")
    ]
    assert _diagnostics()[0][0].endswith("test_native_write_keeps_fault_attribution_and_known_destination")


@pytest.mark.parametrize("stage", ["open-no-effect", "ordinary-read", "judge", "name-effects"])
@pytest.mark.parametrize("failure,reason", [
    (FileNotFoundError, "FileNotFoundError"), (NotADirectoryError, "NotADirectoryError"),
    (PermissionError, "PermissionError"), (OSError, "OSError"), (ValueError, "ValueError"),
    (TypeError, "TypeError"), (RuntimeError, "RuntimeError"), (AssertionError, "AssertionError"),
    (Exception, "Exception"), (pytest.fail.Exception, "BaseException"),
])
def test_fault_stage_and_fixed_reason(home, monkeypatch, stage, failure, reason):
    # name_effects only catches OSError/ValueError locally; other failures reach judge.
    expected_stage = ("judge" if stage == "name-effects" and not issubclass(failure, (OSError, ValueError))
                      else stage)
    owner, attribute = {
        "open-no-effect": (audit._live_writes_fs, "open_no_effect"),
        "ordinary-read": (audit, "_ordinary_read"),
        "judge": (audit, "_judge"),
        "name-effects": (audit._live_writes_fs, "name_effects"),
    }[stage]

    def broken(*args):
        raise failure("private error text")

    target = str(home / ".genesis" / "target")
    event, args = ("os.rename", (target, target + "-new", -1, -1)) if stage == "name-effects" else (
        "open", (target, "w", os.O_WRONLY)
    )
    with monkeypatch.context() as patch:
        patch.setattr(owner, attribute, broken)
        audit._hook(event, args)
    assert audit._S.faults == 1
    assert [row[2:] for row in _diagnostics()] == [(f"{event}/{expected_stage}/{reason}", "<not captured>")]
    assert audit._busy.on is False
    audit._hook("os.mkdir", (target + "-after", 0o700, -1))
    assert any(row[2:] == ("os.mkdir", target + "-after") for row in audit._S.hits)


def test_repeated_concurrent_faults_deduplicate_rows_not_counts(home, monkeypatch):
    monkeypatch.setattr(audit, "_node", lambda: "test::same")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: audit._unknown("open", "judge", ValueError("private")), range(32)))
    assert audit._S.faults == 32
    assert _diagnostics() == [("test::same", "observer fault", "open/judge/ValueError", "<not captured>")]


def test_two_judging_failures_in_one_event_keep_both_counts(home, monkeypatch):
    def broken(*args):
        raise ValueError("owned")

    monkeypatch.setattr(audit._live_writes_fs, "open_no_effect", broken)
    monkeypatch.setattr(audit, "_ordinary_read", broken)
    audit._hook("open", (str(home / ".genesis" / "target"), "w", os.O_WRONLY))
    assert audit._S.faults == 2
    assert {row[2] for row in _diagnostics()} == {
        "open/open-no-effect/ValueError", "open/ordinary-read/ValueError",
    }


def test_worker_fault_evidence_merges_once(home, monkeypatch):
    monkeypatch.setattr(audit._S, "merged_workers", set())
    monkeypatch.setattr(audit._S, "worker_status", {})
    monkeypatch.setattr(audit._S, "unreported", [])
    for _ in range(2):
        audit._unknown("open", "judge", ValueError())
    expected = list(audit._S.hits)
    config = type("C", (), {"workeroutput": {}})()
    audit.pytest_sessionfinish(type("S", (), {"config": config})(), 0)
    transported = config.workeroutput["genesis_live_writes"]
    assert transported["hits"] == [[field.encode() for field in row] for row in expected]
    audit._S.hits = {}
    audit._S.faults = 0
    node = type("N", (), {"gateway": type("G", (), {"id": "gw0"})(), "workeroutput": config.workeroutput})()
    audit.pytest_testnodedown(node, None)
    audit.pytest_testnodedown(node, None)
    assert list(audit._S.hits) == expected
    assert audit._S.faults == 2 and audit._S.unreported == []


def test_error_payload_and_custom_type_name_never_reach_diagnostics(home):
    error = type("PRIVATE_CLASS_SENTINEL", (OSError,), {})(13, "PRIVATE_MESSAGE_SENTINEL", "PRIVATE_PATH_SENTINEL")
    audit._unknown("open", "judge", error)
    payload = codec.encode(audit._S.hits)
    assert b"PRIVATE_" not in payload
    assert b"open/judge/OSError" in payload


def test_failed_attribution_uses_fixed_fallback_without_retry(home, monkeypatch):
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("private node error")

    monkeypatch.setattr(audit, "_node", broken)
    audit._unknown("open", "judge", ValueError())
    assert calls == [1]
    assert audit._S.faults == 1
    assert _diagnostics() == [
        ("<attribution unavailable>", "observer fault", "open/judge/ValueError", "<not captured>")
    ]


def test_failed_diagnostic_storage_keeps_fault_and_native_operation(home, monkeypatch):
    class BrokenHits(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("private storage error")

    monkeypatch.setattr(audit._S, "hits", BrokenHits())
    target = home / ".genesis" / "still-created"
    target.mkdir()
    assert target.is_dir()
    assert audit._S.faults == 1
    assert audit._busy.on is False


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("where", ["attribution", "storage"])
def test_diagnostic_capture_preserves_interrupts(home, monkeypatch, signal, where):
    def interrupted(*args):
        raise signal()

    if where == "attribution":
        monkeypatch.setattr(audit, "_node", interrupted)
    else:
        class InterruptedHits(dict):
            __setitem__ = interrupted

        monkeypatch.setattr(audit._S, "hits", InterruptedHits())
    with pytest.raises(signal):
        audit._unknown("open", "judge", ValueError())
    assert audit._S.faults == 1


@pytest.mark.parametrize("workers", [0, 2])
def test_native_fault_report_and_census_stay_incomplete(tmp_path, monkeypatch, workers):
    from scripts.ci.live_writes_census import assess

    run_id = "owned-fault-census"
    monkeypatch.setenv("GENESIS_LIVE_WRITES_RUN_ID", run_id)
    payload, text = _subrun(tmp_path, _SUBRUN_CONFTEST, '''
import pytest
from tests import _live_writes as audit

@pytest.mark.parametrize("case", [0, 1])
def test_owned_fault(tmp_path, monkeypatch, case):
    target = tmp_path / "owned"
    original = audit.os.lstat
    audit._S.real_home = str(tmp_path)
    audit._S.allowed_roots = ()
    before = audit._S.faults
    def unavailable(path, *args, **kwargs):
        if path == str(target):
            raise PermissionError("PRIVATE_MESSAGE_SENTINEL")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(audit.os, "lstat", unavailable)
    with target.open("x") as output:
        output.write("actual native write")
    assert target.read_text() == "actual native write"
    assert audit._S.faults == before + 1
''', workers)
    rows = codec.decode(payload)
    faults = [row for row in rows if row[1] == "observer fault"]
    assert len(faults) == 2, text
    assert len([row for row in rows if row[2] == "write"]) == 2
    assert all(row[2:] == ("open/open-no-effect/PermissionError", "<not captured>") for row in faults)
    assert "PRIVATE_MESSAGE_SENTINEL" not in payload.decode() + text
    receipt = json.loads((tmp_path / "report.tsv.status.json").read_text())
    assert receipt["faults"] == 2 and receipt["row_count"] == 4
    assert receipt["exitstatus"] == 0
    assert sum(record["faults"] for record in receipt["workers"].values()) == (2 if workers else 0)
    assert assess(tmp_path / "report.tsv", run_id=run_id, workers=workers, tests_outcome="success") == {
        "verdict": "incomplete", "rows": 4, "reason": "observation-state",
    }
