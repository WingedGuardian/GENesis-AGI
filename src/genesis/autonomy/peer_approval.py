"""Peer operation consent is individual and channel-bound, never a batch grant."""

import re

PEER_OPERATION_ACTION_TYPE = "peer_operation"


def named_human_resolver(value: str | None) -> bool:
    return value == "dashboard" or (
        isinstance(value, str) and re.fullmatch(r"telegram:button:[1-9][0-9]*", value) is not None
    )
