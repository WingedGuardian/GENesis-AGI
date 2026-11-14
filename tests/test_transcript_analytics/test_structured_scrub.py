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


@pytest.mark.parametrize(
    "raw, secret",
    [
        # A quoted span the scrubber changes, ending in a credential label whose
        # separator and value follow the span. Hiding the span behind a
        # placeholder hid the label from the whole-text pass.
        ('"password=abcd cookie"=SESSIONSECRET9', "SESSIONSECRET9"),
        ("\"--password 'hunter2x' cookie\"=abcSESSION9", "abcSESSION9"),
        ('"curl -u bob:pw4567 cookie"=abcSESSION9', "abcSESSION9"),
        ('"--token \\"api_key"=SECRETVALUE1234', "SECRETVALUE1234"),
    ],
)
def test_label_ending_a_scrubbed_span_still_redacts_the_value_after_it(raw, secret):
    text, failed = scrub.scrub_text(raw)
    assert not failed
    assert secret not in text


def test_boundary_probe_runs_after_the_prose_pass_so_it_cannot_eat_a_label():
    # The protected span (it holds a token) ends in 'cookie"'; the text after it
    # carries a second label whose value is on the next line. Probing before the
    # prose pass redacted 'cookie's value, swallowing the second label, and the
    # next-line value leaked; main's own prose pass redacts it.
    token = "sk-" + "a" * 24
    raw = f'"{token} cookie"=--http-password authorization"=\r\nSECRETNEXTLINE9'
    text, failed = scrub.scrub_text(raw)
    assert not failed
    assert token not in text and "SECRETNEXTLINE9" not in text


@pytest.mark.parametrize(
    "raw, secret",
    [
        # A JSON string only the credential-flag arms change ('-u user:pass'),
        # followed by a label whose value the base arms redact. Protecting that
        # string hid the label's edge from the prose pass; the base arms decide
        # protection now, so the string stays visible to them.
        (' authorization"=-uSECRETUSER1:,"', "SECRETUSER1"),
        (
            ' authorization"=SECRETVAL22://u:\';--api-key is sudocookie"\\"{"command": "mysql  @h',
            "SECRETVAL22",
        ),
    ],
)
def test_flag_arms_never_widen_what_is_protected_from_the_prose_pass(raw, secret):
    text, failed = scrub.scrub_text(raw)
    assert not failed
    assert secret not in text


def test_base_arms_decide_protection_and_full_arms_redact(monkeypatch):
    # A span only the full arms change is NOT protected: the base arms decide,
    # so the prose pass sees it raw and the full arms still redact it there.
    seen = []
    monkeypatch.setattr(scrub, "_scrub_base", lambda text: seen.append(text) or text)
    monkeypatch.setattr(scrub, "_scrub", lambda text: text.replace("FLAGSECRET", "[REDACTED]"))
    text, failed = scrub.scrub_text('pre "x FLAGSECRET" post')
    assert not failed and text == 'pre "x [REDACTED]" post'
    assert '"x FLAGSECRET"' in seen  # the protection decision ran on the base arms


def _base_only(text):
    """Main's adapter, in-tree: with both scrubbers the base arms, the adapter
    runs the base pipeline once and adds no full-arm pass."""
    return scrub._scrub_text(text, 0, [64], scrub._scrub_base, scrub._scrub_base)


_TOKEN = "sk-" + "a" * 28


@pytest.mark.parametrize(
    "raw",
    [
        # A full arm that consumed a protected marker made the boundary probe
        # skip the value after it; the probe runs on the base arms' output now.
        f'echo "{_TOKEN}" ok --password "{_TOKEN} session_cookie" = SECRETAFTER1',
        f'{{"a":"{_TOKEN}"}} --token "{_TOKEN} authorization" = SECRETAFTER1',
        f'; mysql --password "{_TOKEN} cookie" =SECRETAFTER1',
        # The prose pass damaged a marker: main withholds, and a full arm that
        # turned the damage into [REDACTED] made the text pass the check.
        '--passwd is ["password: hunter token"]=SEC1Xmysql --user=al: '
        '{"a":"password: hunter"}\\"echo -pass:""ghp_' + "B" * 36 + ' cookie"\nnext SECRETAFTER1',
        # Every marker consumed: the whole text was withheld, not stored redacted.
        'gh auth login --token "ghp_' + "A" * 36 + '"',
        f"mysql --password \"{_TOKEN}\" -e 'select 1'",
    ],
)
def test_full_arms_only_add_to_the_base_adapter_output(raw):
    base, base_failed = _base_only(raw)
    text, failed = scrub.scrub_text(raw)
    if base_failed:  # main withholds it, so must the full adapter
        assert failed and text is None
        return
    assert not failed
    assert "SECRETAFTER1" not in text and _TOKEN not in text


def test_full_arms_never_show_a_value_the_base_adapter_hides():
    # Seeded differential: whatever the base-only adapter hides, the full one
    # hides too, and it never withholds text the base-only one stored.
    import random

    rng = random.Random(4619)
    words = [
        "--password", "--token", "-u", "bob:", "password=", "Cookie=", " cookie\"",
        " authorization\"=", '"', "'", '\\"', " ", "=", ":", "\n", "{", "}", ",",
        '{"cmd": "', '"}', _TOKEN, "curl", "mysql", ";", "x",
    ]  # fmt: skip
    for _ in range(1500):
        secrets, parts = [], []
        for _ in range(rng.randint(2, 12)):
            if rng.random() < 0.25:
                secrets.append(f"PLANT{len(secrets)}Q{rng.randint(0, 999)}")
                parts.append(secrets[-1])
            else:
                parts.append(rng.choice(words))
        raw = "".join(parts)
        base, _ = _base_only(raw)
        text, _ = scrub.scrub_text(raw)
        assert (base is None) == (text is None), raw
        for secret in secrets:
            assert not (secret not in (base or "") and secret in (text or "")), raw
