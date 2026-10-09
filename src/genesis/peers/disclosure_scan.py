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
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if not scan_outbound(item).safe:
                return False
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return True
