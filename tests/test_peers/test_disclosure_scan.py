"""Original sensitive strings remain visible across JSON escaping and nesting."""

import json
import secrets

import pytest

from genesis.peers.broker import BrokerRefusal, EmptyArguments, PeerBroker
from genesis.peers.disclosure_scan import json_strings_safe
from genesis.security.output_scanner import scan_outbound


@pytest.mark.parametrize("compound", [{}, [], {"api_key": None}, ["api_key"]])
@pytest.mark.parametrize("depth", [1, 2, 4])
@pytest.mark.parametrize("placement", ["root", "nested", "list", "tuple", "encoded"])
@pytest.mark.parametrize("sensitive", [False, True])
def test_compound_decoded_mapping_keys_are_unsupported(compound, depth, placement, sensitive):
    key = compound
    for _ in range(depth):
        key = json.dumps(key)
    value = secrets.token_hex(20) if sensitive else "ordinary result"
    payload = {key: value}
    if placement == "nested":
        payload = {"ordinary": payload}
    elif placement == "list":
        payload = [payload]
    elif placement == "tuple":
        payload = (payload,)
    elif placement == "encoded":
        payload = json.dumps(payload)
    verdict = json_strings_safe(payload)
    assert verdict is False


def test_duplicate_decoded_members_cannot_hide_compound_key():
    key = json.dumps(json.dumps({"api_key": None}))
    payload = '{' + key + ':"ordinary result",' + key + ':null}'
    assert not json_strings_safe(payload)


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_scalar_encoded_keys_and_json_values_remain_supported(depth):
    key = "ordinary"
    for _ in range(depth):
        key = json.dumps(key)
    assert json_strings_safe({key: json.dumps({"text": "ordinary result", "items": [1, 2]})})


@pytest.mark.parametrize("key", ["token", "api_key", "GENESIS_PEER_FIXTURE_TOKEN"])
@pytest.mark.parametrize("shape", ["scalar", "list", "nested", "tuple"])
def test_structured_credentials_keep_key_value_association(key, shape):
    # Generate privately; failed assertions display only the boolean verdict.
    credential = secrets.token_hex(16)
    value = {
        "scalar": credential,
        "list": [credential],
        "nested": {"value": [credential]},
        "tuple": (credential,),
    }[shape]
    verdict = json_strings_safe({key: value})
    assert not verdict


@pytest.mark.parametrize("value", [None, True, False, {}, []])
def test_empty_credential_fields_carry_no_value(value):
    assert json_strings_safe({"token": value})


def test_ordinary_structured_values_remain_allowed():
    assert json_strings_safe({"count": 1234567890123456, "text": "Public fixture"})
    credential = int(secrets.token_hex(16), 16)
    verdict = json_strings_safe({"token": credential})
    assert not verdict


@pytest.mark.parametrize("placement", ["value", "key", "nested", "tuple"])
@pytest.mark.parametrize("shape", ["quoted_assignment", "unicode_path"])
def test_escaping_cannot_hide_original_sensitive_string(placement, shape):
    text = (
        'token: "' + secrets.token_hex(16) + '"'
        if shape == "quoted_assignment"
        else "/home/用户/public"
    )
    original_safe = scan_outbound(text).safe
    assert not original_safe
    encoded_safe = scan_outbound(json.dumps(text)).safe
    assert encoded_safe
    choices = {
        "value": {"answer": text},
        "key": {text: "public"},
        "nested": [{"answer": [text]}],
        "tuple": (text,),
    }
    verdict = json_strings_safe(choices[placement])
    assert not verdict


@pytest.mark.parametrize(
    "value", [None, True, 1, 0.5, {"title": "Public prose", "items": ["safe", 0]}]
)
def test_safe_json_population_passes(value):
    assert json_strings_safe(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), {1, 2}])
def test_non_json_population_refused(value):
    assert not json_strings_safe(value)


def test_cyclic_json_refused_before_traversal():
    value = []
    value.append(value)
    assert not json_strings_safe(value)


async def test_task_context_scans_original_message_before_wrapping():
    broker = PeerBroker(None, lambda *_: None)
    unsafe = {"parts": [{"text": 'token: "' + secrets.token_hex(16) + '"'}]}
    with pytest.raises(BrokerRefusal, match="operation_refused"):
        await broker._context(
            {"id": "fixture", "message_json": json.dumps(unsafe)}, {}, EmptyArguments()
        )
    safe = {"parts": [{"text": "Please inspect the public fixture"}]}
    result = await broker._context(
        {"id": "fixture", "message_json": json.dumps(safe)}, {}, EmptyArguments()
    )
    assert result["task_id"] == "fixture" and "public fixture" in result["context"]


@pytest.mark.parametrize("depth", [1, 2, 4])
@pytest.mark.parametrize("placement", ["root", "value", "list"])
def test_json_encoded_credentials_are_refused(depth, placement):
    encoded = {"api_key": secrets.token_hex(20)}
    for _ in range(depth):
        encoded = json.dumps(encoded)
    payload = {"root": encoded, "value": {"result": encoded}, "list": [encoded]}[placement]
    verdict = json_strings_safe(payload)
    assert not verdict


def test_duplicate_json_keys_cannot_hide_earlier_credential():
    encoded = '{"api_key":' + json.dumps(secrets.token_hex(20)) + ',"api_key":null}'
    verdict = json_strings_safe(encoded)
    assert not verdict


def test_json_encoded_keys_keep_child_association():
    encoded_key = json.dumps(json.dumps("api_key"))
    verdict = json_strings_safe({encoded_key: secrets.token_hex(20)})
    assert not verdict


def test_decoded_json_retains_outer_key_association():
    verdict = json_strings_safe({"api_key": json.dumps({"parts": [secrets.token_hex(20)]})})
    assert not verdict


@pytest.mark.parametrize("encoded", ['{"value": NaN}', '{"value": 1e999}', '[' + '9' * 5000 + ']'])
def test_invalid_decoded_numbers_fail_closed(encoded):
    assert not json_strings_safe(encoded)


@pytest.mark.parametrize("payload", ['plain prose with {braces}', '{unfinished', '[ordinary text', '"unfinished'])
def test_non_json_prose_remains_allowed(payload):
    assert json_strings_safe(payload)


def test_large_ordinary_operation_result_remains_allowed():
    payload = {"output": "x" * (2 * 1024 * 1024 - 32)}
    assert json_strings_safe(payload)


def test_deep_encoded_input_refused_without_unbounded_work():
    payload = "[" * 2000 + "0" + "]" * 2000
    assert not json_strings_safe(payload)


def test_list_siblings_do_not_inherit_each_others_key_ancestry():
    assert json_strings_safe([{"api_key": {"parts": []}}, "ordinary public output" * 4])


def test_shared_acyclic_container_is_not_mistaken_for_cycle():
    shared = {"parts": ["ordinary output"]}
    assert json_strings_safe([shared, shared])


def test_aggregate_ancestor_scanning_work_is_bounded(monkeypatch):
    import genesis.peers.disclosure_scan as disclosure

    monkeypatch.setattr(disclosure, "scan_outbound", lambda _: type("Verdict", (), {"safe": True})())
    value = "ordinary output" * 30000
    for _ in range(200):
        value = {"parts": value}
    assert not json_strings_safe(value)


def test_native_excessive_nesting_refused_before_json_encoding():
    value = "ordinary output"
    for _ in range(2000):
        value = [value]
    assert not json_strings_safe(value)
