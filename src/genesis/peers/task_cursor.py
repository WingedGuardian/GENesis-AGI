"""Issued positions, scoped to an authorized peer/epoch/context, never row anchors."""

import base64
import binascii
import json
import re
from datetime import datetime
from uuid import UUID

from genesis.peers.protocol import _unique_object


def valid_stamp(stamp):
    if not isinstance(stamp, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", stamp) is None:
        return False
    try:
        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def encode(row, peer, context):
    value = {"v": 1, "at": row["a2a_status_at"], "id": row["id"],
             "peer": peer["peer_id"], "epoch": peer["epoch"], "context": context}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode(token, peer, context):
    from genesis.peers.conversation import ConversationRefusal

    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 1024 or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
            raise ValueError
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {"v", "at", "id", "peer", "epoch", "context"}:
            raise ValueError
        if type(value["v"]) is not int or value["v"] != 1 or not valid_stamp(value["at"]):
            raise ValueError
        if not isinstance(value["id"], str) or str(UUID(value["id"])) != value["id"]:
            raise ValueError
        if value["peer"] != peer["peer_id"] or value["epoch"] != peer["epoch"] or value["context"] != context:
            raise ValueError
        return value["at"], value["id"]
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise ConversationRefusal("validation_error", 400) from None
