import io
import json

import pytest

from genesis.transcript_analytics import evidence, scrub


def test_window_scrubs_and_bounds(tmp_path):
    secret = (
        "ghp_"
        + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret — synthetic fixture
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
    assert json.loads(out["records"][0]["text"]) == json.loads(raw)
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


@pytest.mark.parametrize("kind,key", [("tool_use", "id"), ("tool_result", "tool_use_id")])
def test_late_matching_block_survives_target_clipping(kind, key):
    block = {"content": "payload" * 1000, "type": kind, key: "example"}
    raw = (
        json.dumps(
            {"message": {"content": [{"type": "thinking", "thinking": "x" * 10000}, block]}}
        ).encode()
        + b"\n"
    )
    out = evidence._window(io.BytesIO(raw), 1, 0, 300, expected_id="example")
    assert out["target_available"] and out["truncated"]
    text = out["records"][0]["text"]
    assert "example" in text and "payload" in text and "thinking" not in text


def test_clipped_target_without_complete_identity_is_unavailable():
    raw = (
        json.dumps({"message": {"content": [{"type": "tool_use", "id": "x" * 1000}]}}).encode()
        + b"\n"
    )
    out = evidence._window(io.BytesIO(raw), 1, 0, 100, expected_id="x" * 1000)
    assert not out["target_available"]
    assert out["truncated"]


@pytest.mark.parametrize("raw_id", ["tool-\ud800", "jsonid:literal"])
def test_transport_identity_decoded_before_raw_evidence_match(tmp_path, monkeypatch, raw_id):
    from genesis.transcript_analytics.identity import encode_identity

    identity = encode_identity(raw_id)
    raw = json.dumps({"message": {"content": [{"type": "tool_use", "id": raw_id}]}})
    (tmp_path / "call.jsonl").write_text(raw + "\n")
    monkeypatch.setattr(evidence, "run_query", lambda *args: ([], [("call.jsonl", 1, None, None)]))
    out = evidence.evidence(tmp_path, tmp_path, identity, budget=1024)
    assert out["references"][0]["target_available"]
    assert (
        json.loads(out["references"][0]["records"][0]["text"])["message"]["content"][0]["id"]
        == raw_id
    )
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 1024


@pytest.mark.parametrize(
    "kind,correct,wrong", [("tool_use", "id", "tool_use_id"), ("tool_result", "tool_use_id", "id")]
)
def test_identity_match_uses_field_for_actual_block_type(kind, correct, wrong):
    raw = json.dumps(
        {"message": {"content": [{"type": kind, correct: "other", wrong: "example"}]}}
    ).encode()
    out = evidence._window(io.BytesIO(raw + b"\n"), 1, 0, 1024, expected_id="example")
    assert not out["target_available"]
    assert out["records"] == []


def test_structured_context_and_target_scrub_preserves_only_reference_ids():
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret
    records = [
        {
            "password": "synthetic-value",
            "nested": [{"api_key": "opaquecredential123456"}],
            "diagnostic": token,
        },
        {
            "sessionId": token,
            "message": {
                "id": token,
                "content": [
                    {
                        "type": "tool_use",
                        "id": token,
                        "name": "Bash",
                        "input": {
                            "password": "synthetic-value",
                            "id": token,
                            "description": json.dumps({"api_key": "opaquecredential123456"}),
                        },
                    }
                ],
            },
        },
    ]
    raw = "".join(
        json.dumps(record).replace("ghp_", "\\u0067hp_") + "\n" for record in records
    ).encode()
    out = evidence._window(io.BytesIO(raw), 2, 1, 8192, expected_id=token)
    assert out["target_available"]
    assert "synthetic-value" not in json.dumps(out)
    assert "opaquecredential123456" not in json.dumps(out)
    context = json.loads(out["records"][0]["text"])
    target = json.loads(out["records"][1]["text"])["message"]["content"][0]
    assert context["diagnostic"] != token
    assert target["id"] == token
    assert target["input"]["id"] != token
    assert target["input"]["password"] == "[REDACTED]"


@pytest.mark.parametrize(
    "raw", [b'{"password": "synthetic-value"', b'{"diagnostic": "\\u0067hp_opaque"']
)
def test_malformed_structured_context_is_withheld(raw):
    out = evidence._window(io.BytesIO(raw + b"\n"), 1, 0, 4096)
    assert "synthetic-value" not in json.dumps(out)
    assert "opaque" not in json.dumps(out)
    assert "withheld" in out["records"][0]["text"]


@pytest.mark.parametrize("kind", ["text", "thinking", "tool_use", "tool_result"])
@pytest.mark.parametrize("key", ["id", "tool_use_id"])
def test_window_preserves_only_actual_tool_reference_ids(kind, key):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret
    raw = json.dumps({"message": {"content": [{"type": kind, key: token}]}}).encode()
    out = evidence._window(io.BytesIO(raw), 1, 0, 4096)
    value = json.loads(out["records"][0]["text"])["message"]["content"][0][key]
    assert (value == token) is ((kind, key) in (("tool_use", "id"), ("tool_result", "tool_use_id")))


_ENCODINGS = (
    "utf-8",
    "utf-8-sig",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
)


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("diagnostic", ["café", "ascii"])
def test_valid_encoding_population_keeps_valid_json_diagnostics(encoding, diagnostic):
    record = {"diagnostic": diagnostic}
    raw = json.dumps(record, ensure_ascii=False).encode(encoding)
    out = evidence._window(io.BytesIO(raw), 1, 0, 4096)
    text = out["records"][0]["text"]
    assert json.loads(text.removeprefix("\ufeff")) == record
    if encoding in ("utf-8", "utf-8-sig"):
        assert text.encode("utf-8") == raw
    else:
        assert "\x00" not in text


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_changed_secret_encoding_population_uses_scrubbed_representation(encoding):
    raw = json.dumps(
        {"diagnostic": "café", "password": "synthetic-value"}, ensure_ascii=False
    ).encode(encoding)
    out = evidence._window(io.BytesIO(raw), 1, 0, 4096)
    text = out["records"][0]["text"]
    assert json.loads(text) == {"diagnostic": "café", "password": "[REDACTED]"}
    assert "synthetic-value" not in text


@pytest.mark.parametrize(
    "raw", [b"\xffsynthetic-value", b'{"password":"synthetic-value"}\xff', b"\xff\xfe{\x00"]
)
def test_malformed_encoding_is_withheld_without_replacement_decoding(raw):
    out = evidence._window(io.BytesIO(raw), 1, 0, 4096)
    assert out["records"][0]["text"] == "[scrub failed: evidence withheld]"


@pytest.mark.parametrize("encoding", _ENCODINGS)
@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_record_keys_never_restore_shadowed_secret(encoding, nested):
    token = "sk-" + "A" * 40  # pragma: allowlist secret
    raw = '{"content":' + json.dumps(token) + ',"content":"harmless"}'
    if nested:
        raw = '{"nested":[' + raw + "]}"
    out = evidence._window(io.BytesIO(raw.encode(encoding)), 1, 0, 4096)
    text = out["records"][0]["text"]
    assert token not in text
    parsed = json.loads(text)
    assert (parsed["nested"][0] if nested else parsed) == {"content": "harmless"}


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_malformed_json_encoding_population_never_falls_through_as_nul_text(encoding):
    raw = '{"password":"synthetic-value"'.encode(encoding)
    out = evidence._window(io.BytesIO(raw), 1, 0, 4096)
    assert out["records"][0]["text"] == "[scrub failed: evidence withheld]"
