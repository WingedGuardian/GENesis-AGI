"""Strict issued-position parsing; a cursor is never authorization."""

import base64
import json
from uuid import uuid4

import pytest

from genesis.peers.conversation import ConversationRefusal
from genesis.peers.task_cursor import decode, encode, valid_stamp

PEER = {"peer_id": "alpha", "epoch": "a" * 32}
STAMP = "2026-01-02T03:04:05.006Z"
SID = str(uuid4())


def packed(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_cursor_roundtrip_is_an_issued_tuple():
    row = {"id": SID, "a2a_status_at": STAMP}
    token = encode(row, PEER, None)
    assert decode(token, PEER, None) == (STAMP, SID)
    # No database or anchor lookup is needed to decode an issued position.
    row["a2a_status_at"] = "2030-01-01T00:00:00.000Z"
    assert decode(token, PEER, None) == (STAMP, SID)


@pytest.mark.parametrize("stamp", [None, "", "2026-02-30T00:00:00.000Z", "0000-01-01T00:00:00.000Z", "2026-01-02T03:04:05Z", "2026-01-02T03:04:05.006+00:00", "2026-01-02T25:04:05.006Z"])
def test_stamp_rejects_noncanonical_or_invalid_calendar(stamp):
    assert not valid_stamp(stamp)


@pytest.mark.parametrize("field,value", [
    ("v", True), ("v", 2), ("id", "unowned"), ("id", None),
    ("at", "2026-02-30T00:00:00.000Z"), ("peer", "beta"),
    ("epoch", "b" * 32),
    pytest.param("context", str(uuid4()), id="context-uuid"),
    ("extra", "unexpected"),
])
def test_cursor_refuses_type_version_and_scope_changes(field, value):
    body = {"v": 1, "id": SID, "at": STAMP, "peer": PEER["peer_id"], "epoch": PEER["epoch"], "context": None}
    body[field] = value
    with pytest.raises(ConversationRefusal) as refused:
        decode(packed(json.dumps(body)), PEER, None)
    assert refused.value.code == "validation_error" and refused.value.status == 400


@pytest.mark.parametrize("token", [
    pytest.param(SID, id="legacy-uuid"),
    "", "A" * 1025, "++//", packed("[]"), packed("{}"), packed('{"v":1,"v":1}'), packed("null"), "a",
])
def test_cursor_refuses_legacy_malformed_and_duplicate_fields(token):
    with pytest.raises(ConversationRefusal) as refused:
        decode(token, PEER, None)
    assert refused.value.status == 400
