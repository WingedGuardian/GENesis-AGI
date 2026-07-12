"""Content hashing for profile fact sections.

The hash covers a section's ``facts`` dict ONLY — never ``metrics``. It is
computed over canonical JSON (sorted keys, compact separators) so dict key
order can never flap the hash.

List-order contract: canonicalization does NOT sort lists — element order can
be semantically meaningful, and silently sorting would hide real changes.
Collectors are responsible for emitting lists in a deterministic order
(e.g. mounts sorted by mountpoint, interfaces by name). A collector that emits
nondeterministic list order will churn its section hash; that is a collector
bug, fixed at the collector.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def section_hash(facts: dict[str, Any]) -> str:
    """Return the sha256 hex digest of the canonical JSON form of ``facts``."""
    canonical = json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
