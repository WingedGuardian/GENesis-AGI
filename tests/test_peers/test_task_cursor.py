"""Strict issued-position parsing; a cursor is never authorization."""

import base64
import json
from uuid import uuid4

import pytest

from genesis.peers.conversation import ConversationRefusal
from genesis.peers.task_cursor import decode, encode, valid_stamp

PEER = {"peer_id": "alpha", "epoch": "a" * 32, "same_owner": 1}
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


@pytest.mark.parametrize("same_owner,trust", [(1, "same_owner"), (0, "conversation_shared")])
def test_new_cursor_records_current_authorized_trust(same_owner, trust):
    peer = {**PEER, "same_owner": same_owner}
    token = encode({"id": SID, "a2a_status_at": STAMP}, peer, None)
    value = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert value["v"] == 2 and value["trust"] == trust
    assert decode(token, peer, None) == (STAMP, SID)
    with pytest.raises(ConversationRefusal) as refused:
        decode(token, {**peer, "same_owner": 1 - same_owner}, None)
    assert refused.value.code == "validation_error" and refused.value.status == 400


def test_legacy_v1_cursor_is_valid_only_for_same_owner():
    token = packed(json.dumps({"v": 1, "id": SID, "at": STAMP,
                              "peer": PEER["peer_id"], "epoch": PEER["epoch"], "context": None}))
    assert decode(token, PEER, None) == (STAMP, SID)
    with pytest.raises(ConversationRefusal) as refused:
        decode(token, {**PEER, "same_owner": 0}, None)
    assert refused.value.code == "validation_error" and refused.value.status == 400


@pytest.mark.parametrize("trust", [None, True, "unknown", "conversation_shared"])
def test_v2_cursor_refuses_invalid_or_mismatching_trust(trust):
    token = packed(json.dumps({"v": 2, "id": SID, "at": STAMP,
                              "peer": PEER["peer_id"], "epoch": PEER["epoch"],
                              "context": None, "trust": trust}))
    with pytest.raises(ConversationRefusal) as refused:
        decode(token, PEER, None)
    assert refused.value.code == "validation_error" and refused.value.status == 400


@pytest.mark.parametrize("version,change", [(1, "duplicate"), (2, "duplicate"), (2, "extra")])
def test_complete_cursor_envelope_refuses_duplicate_or_extra_fields(version, change):
    body = {"v": version, "id": SID, "at": STAMP, "peer": PEER["peer_id"],
            "epoch": PEER["epoch"], "context": None}
    if version == 2:
        body["trust"] = "same_owner"
    if change == "extra":
        body["extra"] = "unrecognized"
    text = json.dumps(body)
    if change == "duplicate":
        field = '"trust":"same_owner"' if version == 2 else '"v":1'
        text = text[:-1] + "," + field + "}"
    with pytest.raises(ConversationRefusal) as refused:
        decode(packed(text), PEER, None)
    assert refused.value.code == "validation_error" and refused.value.status == 400
