"""Original sensitive strings remain visible across JSON escaping and nesting."""

import json
import secrets

import pytest

from genesis.peers.broker import BrokerRefusal, EmptyArguments, PeerBroker
from genesis.peers.disclosure_scan import json_strings_safe
from genesis.security.output_scanner import scan_outbound


@pytest.mark.parametrize("placement", ["value", "key", "nested", "tuple"])
@pytest.mark.parametrize("shape", ["quoted_assignment", "unicode_path"])
def test_escaping_cannot_hide_original_sensitive_string(placement, shape):
    text = (
        'token: "' + secrets.token_hex(16) + '"'
        if shape == "quoted_assignment"
        else "/home/用户/public"
    )
    assert not scan_outbound(text).safe, "original-string control must detect"
    assert scan_outbound(json.dumps(text)).safe, "serialized-string control must miss"
    choices = {
        "value": {"answer": text},
        "key": {text: "public"},
        "nested": [{"answer": [text]}],
        "tuple": (text,),
    }
    assert not json_strings_safe(choices[placement])


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
