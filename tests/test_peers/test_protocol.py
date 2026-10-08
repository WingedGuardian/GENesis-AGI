"""Pinned SDK wire validation without raw untrusted values in error details."""

import json

import pytest
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError, VersionNotSupportedError

from genesis.peers.protocol import parse_cancel, parse_send, validate_version


def request():
    return {
        "message": {"messageId": "one", "role": "ROLE_USER", "parts": [{"text": "Hello"}]},
        "configuration": {"returnImmediately": True},
    }


def test_valid_sdk_send_and_cancel_shapes():
    message, immediate = parse_send(json.dumps(request()).encode())
    assert message["messageId"] == "one" and immediate
    for body in (b"", b"{}", b'{"id":"one"}', b'{"id":"one","metadata":{"ignored":true}}'):
        parse_cancel(body, "one")


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{}",
        b'{"message":{},"message":{}}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b'{"profile":"foreground"}',
    ],
)
def test_malformed_or_unknown_send_has_constant_error(body):
    with pytest.raises(InvalidParamsError) as error:
        parse_send(body)
    assert str(error.value) == "Invalid peer message"


@pytest.mark.parametrize(
    "part",
    [
        {"url": "https://example.com"},
        {"raw": "YQ=="},
        {"text": " "},
        {"text": "Hi", "filename": "secret"},
        {"text": "Hi", "metadata": {"profile": "foreground"}},
    ],
)
def test_content_cannot_request_files_or_execution_authority(part):
    value = request()
    value["message"]["parts"] = [part]
    with pytest.raises(InvalidParamsError):
        parse_send(json.dumps(value).encode())


@pytest.mark.parametrize(
    "body", [b"[]", b'{"id":"other"}', b'{"id":"one","id":"one"}', b'{"action":"approve"}']
)
def test_cancel_requires_matching_path_identifier_and_known_fields(body):
    with pytest.raises(InvalidParamsError):
        parse_cancel(body, "one")


def test_tenant_and_unsupported_output_are_explicitly_refused():
    with pytest.raises(UnsupportedOperationError):
        parse_cancel(b'{"tenant":"other"}', "one")
    value = request()
    value["configuration"]["acceptedOutputModes"] = ["application/octet-stream"]
    with pytest.raises(UnsupportedOperationError):
        parse_send(json.dumps(value).encode())


@pytest.mark.parametrize("version", [None, "0.3", "2.0", "invalid"])
def test_missing_and_wrong_versions_fail(version):
    with pytest.raises(VersionNotSupportedError):
        validate_version(version)


@pytest.mark.parametrize("version", ["1.0", "1.0.1", "1.1"])
def test_pinned_sdk_major_version_contract(version):
    validate_version(version)
