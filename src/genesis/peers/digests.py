"""Canonical broker intent fingerprints, shared with trusted result disclosure."""

import hashlib
import json


def operation_digest(name, arguments, resource_digest=None):
    return hashlib.sha256(
        json.dumps(
            {"operation": name, "arguments": arguments, "resource_digest": resource_digest},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
