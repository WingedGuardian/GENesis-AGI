"""Decoded JSON credentials must obey the same scrub policy as labeled text."""

import json

import pytest

from genesis.transcript_analytics import extract, scrub


@pytest.mark.parametrize(
    "label", ["password", "DB_PASSWORD", "PIN", "api_key", "AWS_SECRET_ACCESS_KEY", "refresh_token"]
)
@pytest.mark.parametrize(
    "value",
    ["synthetic multiword value", 123456, {"nested": "synthetic-value"}, ["synthetic-value"]],
)
def test_credential_keys_redact_whole_decoded_value(label, value):
    text, failed = scrub.scrub_text(json.dumps({"diagnostic": "keep", "nested": [{label: value}]}))
    assert not failed
    result = json.loads(text)
    assert result == {"diagnostic": "keep", "nested": [{label: "[REDACTED]"}]}


def test_nested_string_json_and_escaped_secret_are_scrubbed():
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret
    raw = json.dumps(
        {"content": json.dumps({"api_key": "opaquecredential123456"}), "diagnostic": token}
    )
    raw = raw.replace("ghp_", "\\u0067hp_")
    text, failed = scrub.scrub_text(raw)
    assert not failed and token not in text and "opaquecredential123456" not in text
    assert json.loads(json.loads(text)["content"])["api_key"] == "[REDACTED]"


def test_structured_failures_withhold_everything(monkeypatch):
    value = {"password": "synthetic-value"}
    monkeypatch.setattr(scrub, "_scrub", None)
    assert scrub.scrub_json(value) == (None, True)
    monkeypatch.setattr(scrub, "_scrub", lambda _: scrub._PLACEHOLDER)
    assert scrub.scrub_json(value) == (None, True)
    cyclic = []
    cyclic.append(cyclic)
    monkeypatch.setattr(scrub, "_scrub", lambda text: text)
    assert scrub.scrub_json(cyclic) == (None, True)


def test_event_detail_and_raw_timestamp_fields_are_scrubbed(tmp_path):
    path = tmp_path / "source.jsonl"
    records = [
        {
            "type": "system",
            "subtype": "api_error",
            "timestamp": "password: synthetic-value",
            "error": {"api_key": "opaquecredential123456"},
        },
        {
            "type": "assistant",
            "timestamp": "password: synthetic-value",
            "message": {
                "id": "m",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t",
                        "name": "Bash",
                        "input": {"command": json.dumps({"password": "synthetic-value"})},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "password: synthetic-value",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t",
                        "is_error": True,
                        "content": json.dumps({"password": "synthetic-value"}),
                    }
                ]
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    result = extract.extract_source(path, path.name)
    assert "synthetic-value" not in json.dumps(result.tables)
    assert "opaquecredential123456" not in json.dumps(result.tables)
    assert json.loads(result.tables["events"][0]["detail"])["error"]["api_key"] == "[REDACTED]"
    assert result.tables["tool_calls"][0]["tool_use_id"] == "t"


@pytest.mark.parametrize(
    "text",
    [
        'prefix {"password": "synthetic-value"} suffix',
        '{"password": "synthetic-value"',
        'prefix {"api_key": "opaquecredential123456"',
    ],
)
def test_embedded_and_malformed_structured_text_never_leaks(text):
    clean, failed = scrub.scrub_text(text)
    assert "synthetic-value" not in (clean or "")
    assert "opaquecredential123456" not in (clean or "")
    if text.endswith("suffix"):
        assert not failed and clean.startswith("prefix ") and clean.endswith(" suffix")
    else:
        assert clean is None and failed


def test_identity_objects_do_not_bypass_structured_scrub():
    text, failed = scrub.scrub_json(
        {"sessionId": {"password": "synthetic-value"}}, preserve_identity=True
    )
    assert not failed
    assert json.loads(text)["sessionId"] == {"password": "[REDACTED]"}
    assert scrub.scrub_text("[REDACTED]") == ("[REDACTED]", False)


@pytest.mark.parametrize(
    "text", ['if [[ -z "$X" ]]; then echo ok; fi', "[1-4]", "[true condition]", "echo [ordinary]"]
)
def test_shell_brackets_remain_diagnostic_text(text):
    assert scrub.scrub_text(text) == (text, False)


def test_json_scalar_string_decodes_escaped_secret():
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret
    text, failed = scrub.scrub_text(json.dumps(token).replace("ghp_", "\\u0067hp_"))
    assert not failed and token not in text and "A1b2C3d4E5" not in text


@pytest.mark.parametrize("first", [0, True, False, None, {"diagnostic": "keep"}, [], "keep"])
@pytest.mark.parametrize("prefix", ["", "error: ", 'if [[ -z "$X" ]]; then echo '])
def test_all_array_first_types_decode_after_plain_brackets(first, prefix):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret
    raw = prefix + json.dumps([first, token]).replace("ghp_", "\\u0067hp_")
    text, failed = scrub.scrub_text(raw)
    assert not failed and token not in text and "A1b2C3d4E5" not in text
    assert text.startswith(prefix)
    assert json.loads(text[len(prefix) :])[0] == first


@pytest.mark.parametrize("kind", ["text", "thinking", "tool_use", "tool_result"])
def test_only_semantic_tool_reference_ids_are_preserved(kind):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret
    text, failed = scrub.scrub_json(
        {"message": {"content": [{"type": kind, "id": token, "tool_use_id": token}]}},
        preserve_identity=True,
    )
    assert not failed
    block = json.loads(text)["message"]["content"][0]
    assert (block["id"] == token) is (kind == "tool_use")
    assert (block["tool_use_id"] == token) is (kind == "tool_result")


def test_nonsecret_json_keeps_original_diagnostic_bytes():
    for text in [
        '{"error":"Invalid status"}',
        'prefix [1,true,{"diagnostic":"keep"}] suffix',
        '"\\u0061"',
    ]:
        assert scrub.scrub_text(text) == (text, False)


@pytest.mark.parametrize("wrap", ["{}", "prefix {} suffix", "[0,{}]", '{{"nested":{}}}'])
@pytest.mark.parametrize("string_encoded", [False, True])
def test_duplicate_key_population_cannot_restore_shadowed_secret(wrap, string_encoded):
    token = "sk-" + "A" * 40  # pragma: allowlist secret
    duplicate = '{"content":' + json.dumps(token) + ',"content":"harmless"}'
    raw = wrap.format(duplicate)
    if string_encoded:
        raw = json.dumps({"content": raw})
    text, failed = scrub.scrub_text(raw)
    assert failed and text is None


def test_duplicate_identity_and_credential_keys_have_same_preservation_rule():
    token = "sk-" + "A" * 40  # pragma: allowlist secret
    text, failed = scrub.scrub_text(
        '{"id":'
        + json.dumps(token)
        + ',"id":"harmless","password":"synthetic-value","password":"other"}'
    )
    assert failed and text is None
