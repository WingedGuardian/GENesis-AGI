import io
import json

from genesis.transcript_analytics import evidence, scrub


def test_window_scrubs_and_bounds(tmp_path):
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret — synthetic fixture
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
