"""Apply existing disclosure patterns to original JSON strings, before escaping."""

import json

from genesis.security.output_scanner import scan_outbound


def json_strings_safe(value):
    # Reject cycles, nonfinite values and unsupported objects before traversal.
    # Callers retain their own encoded-size and closed-schema constraints.
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    for scalar, keys in _json_leaves(value):
        if not scan_outbound(scalar).safe:
            return False
        if any(not scan_outbound("".join((key, "=", scalar))).safe for key in keys):
            return False
    return True


def _json_leaves(value):
    """Yield original scalar contents with their enclosing JSON string keys."""
    # Keep key associations through containers: separating a credential name
    # from its value must not defeat the existing assignment patterns.
    pending = [(value, ())]
    while pending:
        item, keys = pending.pop()
        if isinstance(item, str):
            yield item, keys
        elif type(item) in (int, float):
            yield json.dumps(item, allow_nan=False), keys
        elif isinstance(item, dict):
            for key, child in item.items():
                pending.append((key, ()))
                pending.append((child, keys + (key,) if isinstance(key, str) else keys))
        elif isinstance(item, (list, tuple)):
            pending.extend((child, keys) for child in item)
        # Null and booleans carry no credential value. Admissibility is checked
        # before traversal, and string joining never invokes formatting hooks.
