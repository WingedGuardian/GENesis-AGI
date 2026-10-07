import io
import json

from genesis.transcript_analytics import evidence, scrub


def test_window_scrubs_and_bounds(tmp_path):
    secret = (
        "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret — synthetic fixture
    )  # pragma: allowlist secret — synthetic fixture
    result = evidence._window(io.BytesIO((secret + "\n" + "x" * 5000 + "\n").encode()), 1, 5, 100)
    assert secret not in json.dumps(result)
    assert result["truncated"]
    assert result["target_available"]


def test_scrub_failure_withholds_raw(monkeypatch):
    monkeypatch.setattr(scrub, "_scrub", None)
    out = evidence._window(io.BytesIO(b"sensitive\n"), 1, 5, 100)
    assert "sensitive" not in json.dumps(out)
    assert "withheld" in json.dumps(out)


def test_missing_invalid_and_symlink_reference_are_explicit(tmp_path):
    assert "unavailable" in evidence.read_reference(tmp_path, "../private", 1)
    assert "unavailable" in evidence.read_reference(tmp_path, "missing.jsonl", 1)
    private = tmp_path.parent / "outside.jsonl"
    private.write_text("secret\n")
    (tmp_path / "link.jsonl").symlink_to(private)
    assert "unavailable" in evidence.read_reference(tmp_path, "link.jsonl", 1)


def test_call_and_result_references_follow_distinct_sources(tmp_path, monkeypatch):
    call = json.dumps({"message": {"content": [{"type": "tool_use", "id": "example"}]}}) + "\n"
    result = (
        json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "example"}]}})
        + "\n"
    )
    (tmp_path / "call.jsonl").write_text(call)
    (tmp_path / "result.jsonl").write_text(result)
    monkeypatch.setattr(
        evidence, "run_query", lambda *args: ([], [("call.jsonl", 1, "result.jsonl", 1)])
    )
    out = evidence.evidence(tmp_path, tmp_path, "example", budget=1024)
    assert [r["source"] for r in out["references"]] == ["call.jsonl", "result.jsonl"]
    assert [r["records"][0]["text"] for r in out["references"]] == [call, result]
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 1024


def test_changed_live_source_is_not_presented_as_original_evidence(tmp_path):
    (tmp_path / "call.jsonl").write_text(
        '{"message":{"content":[{"type":"tool_use","id":"other"}]}}\n'
    )
    out = evidence.read_reference(
        tmp_path, "call.jsonl", 1, expected_id="original", archive_dir=tmp_path
    )
    assert "unavailable" in out
    assert not out.get("target_available")


def test_encrypted_local_archive_fallback_and_authentication(tmp_path, monkeypatch):
    import hashlib
    import shutil
    import subprocess
    import tarfile

    import pytest

    if not shutil.which("gpg"):
        pytest.skip("gpg unavailable")
    relative = "project/agent-example.jsonl"
    raw = b'{"message":{"content":[{"type":"tool_use","id":"example"}]}}\n'
    plain = tmp_path / "one.tar"
    with tarfile.open(plain, "w") as tar:
        member = tarfile.TarInfo(relative)
        member.size = len(raw)
        tar.addfile(member, io.BytesIO(raw))
    archive = tmp_path / f"v2-{hashlib.sha256(relative.encode()).hexdigest()}.tar.gpg"
    password = "synthetic-evidence-password"  # pragma: allowlist secret — synthetic fixture
    monkeypatch.setenv("GENESIS_BACKUP_PASSPHRASE", password)
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase-fd",
            "0",
            "--output",
            str(archive),
            "--symmetric",
            str(plain),
        ],
        input=password.encode(),
        check=True,
        capture_output=True,
    )
    out = evidence.read_reference(
        tmp_path / "gone", relative, 1, archive_dir=tmp_path, expected_id="example"
    )
    assert out["origin"] == "local encrypted archive"
    assert out["target_available"]
    assert out["records"][0]["text"] == raw.decode()
    content = bytearray(archive.read_bytes())
    content[-1] ^= 1
    archive.write_bytes(content)
    assert "unavailable" in evidence.read_reference(
        tmp_path / "gone", relative, 1, archive_dir=tmp_path, expected_id="example"
    )


def test_context_cannot_consume_target_identity_budget():
    target = b'{"message":{"content":[{"type":"tool_use","id":"example"}]}}\n'
    result = evidence._window(
        io.BytesIO(b"x" * 5000 + b"\n" + target + b"after\n"), 2, 1, 100, expected_id="example"
    )
    assert result["target_available"]
    assert "example" in next(r["text"] for r in result["records"] if r["line"] == 2)
    assert result["truncated"]
    assert [r["line"] for r in result["records"]] == sorted(r["line"] for r in result["records"])


def test_output_trimming_preserves_both_targets(tmp_path, monkeypatch):
    for name, block in (
        ("call", {"type": "tool_use", "id": "example"}),
        ("result", {"type": "tool_result", "tool_use_id": "example"}),
    ):
        block["content"] = '\\"' * 1000
        target = json.dumps({"message": {"content": [block]}}) + "\n"
        (tmp_path / f"{name}.jsonl").write_text("before" * 1000 + "\n" + target + "after" * 1000)
    monkeypatch.setattr(
        evidence, "run_query", lambda *args: ([], [("call.jsonl", 2, "result.jsonl", 2)])
    )
    out = evidence.evidence(tmp_path, tmp_path, "example", budget=1024)
    assert all(ref["target_available"] for ref in out["references"])
    assert all(any(row["line"] == 2 for row in ref["records"]) for ref in out["references"])
    assert out["truncated"]
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 1024


def test_encoded_source_reference_uses_original_filesystem_path(tmp_path, monkeypatch):
    import os

    from genesis.transcript_analytics import store

    relative = os.fsdecode(b"project-\xff.jsonl")
    identity = "fsbytes:" + os.fsencode(relative).hex()
    monkeypatch.setattr(
        store, "source_path", lambda value: os.fsdecode(bytes.fromhex(value[8:])), raising=False
    )
    (tmp_path / relative).write_bytes(
        b'{"message":{"content":[{"type":"tool_use","id":"example"}]}}\n'
    )
    out = evidence.read_reference(tmp_path, identity, 1, expected_id="example")
    assert out["origin"] == "live"
    assert out["target_available"]


def test_metadata_overflow_and_unknown_huge_id_stay_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "run_query", lambda *args: ([], []))
    out = evidence.evidence(tmp_path, tmp_path, "tool" * 1000, budget=1024)
    assert "unavailable" in out
    assert len(json.dumps(out).encode()) <= 1024
    monkeypatch.setattr(evidence, "run_query", lambda *args: ([], [("long" * 1000, 1, None, None)]))
    monkeypatch.setattr(
        evidence,
        "read_reference",
        lambda *args, **kwargs: {
            "records": [{"line": 1, "text": "x", "truncated": False}],
            "target_available": True,
        },
    )
    out = evidence.evidence(tmp_path, tmp_path, "example", budget=1024)
    assert "unavailable" in out
    assert len(json.dumps(out).encode()) <= 1024


def test_escaped_targets_share_remaining_json_byte_budget():
    out = {
        "references": [
            {
                "line": 1,
                "records": [{"line": 1, "text": '\\"' * 1000, "truncated": False}],
                "target_available": True,
            }
            for _ in range(2)
        ],
        "truncated": False,
    }
    result = evidence._fit_output(out, 1024)
    texts = [ref["records"][0]["text"] for ref in result["references"]]
    assert all(texts)
    assert abs(len(texts[0]) - len(texts[1])) <= 1
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 1024
