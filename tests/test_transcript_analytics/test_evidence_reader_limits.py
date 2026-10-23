"""Evidence input admission, line limits and retained source facts."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

from genesis.transcript_analytics import evidence
from genesis.transcript_analytics.extract import _hash

LIMIT = 1048576


def _call():
    return {"message": {"content": [{"type": "tool_use", "id": "unit", "name": "Read"}]}}


def _raw():
    return (json.dumps(_call()) + "\n").encode()


class BoundedStream(io.BytesIO):
    def __iter__(self):
        raise AssertionError("unbounded stream iteration")

    def readline(self, size=-1):
        assert 0 < size <= LIMIT + 1
        return super().readline(size)


def test_oversized_prefix_and_context_keep_physical_line_numbers_without_next_read():
    prefix = b"x" * (LIMIT + 2) + b"\n"
    target = _raw()
    stream = BoundedStream(prefix + target + b"must not read this line\n")
    out = evidence._window(stream, 2, 0, 2048, expected_id="unit")
    assert out["target_available"] and out["records"][0]["line"] == 2
    assert stream.tell() == len(prefix + target)
    out = evidence._window(BoundedStream(prefix + target), 2, 1, 2048, expected_id="unit")
    assert out["target_available"] and out["truncated"]
    assert "oversized" in out["records"][0]["text"]


@pytest.mark.parametrize("size", [LIMIT - 1, LIMIT, LIMIT + 1])
@pytest.mark.parametrize("newline", [False, True])
def test_record_ceiling_including_optional_line_terminator(size, newline):
    target = _raw().rstrip(b"\n")
    body = b" " * (size - len(target) - int(newline)) + target + (b"\n" if newline else b"")
    out = evidence._window(BoundedStream(body), 1, 0, 2048, expected_id="unit")
    assert out["target_available"] == (size <= LIMIT)
    if size > LIMIT:
        assert "limit" in out["unavailable"] and out["records"] == []


def test_frozen_zero_and_appended_suffix_are_not_read():
    stream = BoundedStream(_raw() + b"appended context\n")
    out = evidence._window(stream, 1, 5, 2048, expected_id="unit", source_bytes=0)
    assert not out["target_available"] and stream.tell() == 0
    out = evidence._window(stream, 1, 5, 2048, expected_id="unit", source_bytes=len(_raw()))
    assert out["target_available"] and [r["line"] for r in out["records"]] == [1]
    assert stream.tell() == len(_raw())


def test_empty_read_during_oversized_drain_terminates():
    body = b"x" * (LIMIT + 2)
    out = evidence._window(BoundedStream(body), 2, 0, 2048,
        expected_id="unit", source_bytes=len(body) + 100)
    assert not out["target_available"] and "unavailable" in out


def test_many_empty_context_records_have_bounded_retention():
    body = b"\n" * 49999 + _raw() + b"\n" * 50000
    tracemalloc.start()
    try:
        out = evidence._window(BoundedStream(body), 50000, 50000, 2048, expected_id="unit")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert out["target_available"] and out["truncated"]
    assert peak < 16 * 1024 * 1024


@pytest.mark.parametrize("placement", ["target", "context"])
def test_recursive_json_is_withheld_without_aborting(placement):
    recursive = b"[" * 12000 + b"0" + b"]" * 12000 + b"\n"
    body = recursive if placement == "target" else recursive + _raw()
    out = evidence._window(BoundedStream(body), 1 if placement == "target" else 2,
        0 if placement == "target" else 1, 2048, expected_id="unit")
    assert out["target_available"] == (placement == "context")
    if placement == "context":
        assert out["records"][0]["scrub_failed"]


@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("denial", [None, 7, "synthetic-denial"])
def test_result_projection_uses_the_same_envelope_as_the_original_hash(multiple, denial):
    block = {"type": "tool_result", "tool_use_id": "unit", "content": "synthetic"}
    blocks = [block] + ([{"type": "tool_result", "tool_use_id": "other"}] if multiple else [])
    detail = {"interrupted": True, "agentId": "synthetic-child"}
    source = {"message": {"content": blocks}, "toolUseResult": detail, "toolDenialKind": denial}
    expected_hash = _hash({"block": block, "toolUseResult": None if multiple else detail,
                          "denial_kind": denial if isinstance(denial, str) else None})
    assert evidence._observation_hash(source, blocks, block) == expected_hash
    out = evidence._window(io.BytesIO((json.dumps(source) + "\n").encode()), 1, 0, 4096,
        expected_id="unit", expected_hash=expected_hash, expected_kind="result")
    assert out["target_available"]
    selected = json.loads(out["records"][0]["text"])
    assert selected["message"]["content"] == [block]
    assert selected.get("toolUseResult") == (None if multiple else detail)
    assert selected.get("toolDenialKind") == (denial if isinstance(denial, str) else None)


def test_result_envelope_is_scrubbed_with_the_selected_block():
    block = {"type": "tool_result", "tool_use_id": "unit", "content": "synthetic"}
    secret = "synthetic-envelope-credential"
    source = {"message": {"content": [block]}, "toolUseResult": {"password": secret}}
    out = evidence._window(io.BytesIO((json.dumps(source) + "\n").encode()), 1, 0,
                           4096, expected_id="unit")
    assert out["target_available"] and secret not in json.dumps(out)


@pytest.mark.parametrize("kind", ["regular", "fifo"])
def test_live_descriptor_admission_never_waits_for_a_fifo_writer(tmp_path, kind):
    target = tmp_path / "source.jsonl"
    if kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(_raw())
    script = """import json,sys
from pathlib import Path
from genesis.transcript_analytics import evidence
print(json.dumps(evidence.read_reference(Path(sys.argv[1]),'source.jsonl',1,
    surrounding=0,budget=2048,expected_id='unit')))
"""
    run = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True,
        text=True, timeout=12, env=dict(os.environ, PYTHONPATH=str(Path(evidence.__file__).parents[2]),
                                      GENESIS_REPO=str(Path(evidence.__file__).parents[3])))
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert bool(out.get("target_available")) == (kind == "regular")
    if kind == "fifo":
        assert "unavailable" in out


@pytest.mark.parametrize("archive", ["absent", "invalid", "usable"])
def test_oversized_live_reason_survives_unavailable_archive(tmp_path, monkeypatch, archive):
    (tmp_path / "source.jsonl").write_bytes(b" " * (LIMIT + 1) + _raw())
    if archive != "absent":
        path = tmp_path / ("v2-" + hashlib.sha256(b"source.jsonl").hexdigest() + ".tar.gpg")
        path.write_bytes(b"synthetic routing fixture")
    def archived(*args, **kwargs):
        if archive == "invalid":
            raise ValueError("synthetic invalid archive")
        return {"origin": "local encrypted archive", "target_available": True, "records": []}
    monkeypatch.setattr(evidence, "_archive_window", archived)
    out = evidence.read_reference(tmp_path, "source.jsonl", 1, archive_dir=tmp_path,
                                  expected_id="unit", surrounding=0, budget=2048)
    if archive == "usable":
        assert out["target_available"] and out["origin"] == "local encrypted archive"
    else:
        assert not out["target_available"] and "limit" in out["unavailable"]


@pytest.mark.parametrize("ancestor", [False, True])
def test_symlink_loops_report_unavailable(tmp_path, ancestor):
    loop = tmp_path / "loop"
    loop.symlink_to("loop")
    relative = "loop/source.jsonl" if ancestor else "loop"
    out = evidence.read_reference(tmp_path, relative, 1)
    assert "unavailable" in out and not out.get("target_available")


@pytest.mark.parametrize("archive", ["absent", "invalid", "usable"])
def test_changed_live_identity_keeps_existing_archive_diagnostics(tmp_path, monkeypatch, archive):
    original = _call()
    expected = _hash(original["message"]["content"][0])
    original["message"]["content"][0]["name"] = "Grep"
    (tmp_path / "source.jsonl").write_text(json.dumps(original) + "\n")
    if archive != "absent":
        path = tmp_path / ("v2-" + hashlib.sha256(b"source.jsonl").hexdigest() + ".tar.gpg")
        path.write_bytes(b"synthetic routing fixture")

    def archived(*args, **kwargs):
        if archive == "invalid":
            raise ValueError("synthetic invalid archive")
        return {"origin": "local encrypted archive", "target_available": True, "records": []}

    monkeypatch.setattr(evidence, "_archive_window", archived)
    out = evidence.read_reference(tmp_path, "source.jsonl", 1, archive_dir=tmp_path,
        expected_id="unit", expected_hash=expected, expected_kind="call")
    if archive == "usable":
        assert out["target_available"] and out["origin"] == "local encrypted archive"
    else:
        reason = "original target and local encrypted archive unavailable" if archive == "absent" else "local archive unreadable or invalid"
        assert out == {"unavailable": reason}
