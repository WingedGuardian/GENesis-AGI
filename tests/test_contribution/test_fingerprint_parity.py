"""LOCAL parity proof for the fingerprint layer.

Skips on CI / fresh installs (no materialized fingerprint file). On an install
that HAS run the generator, it proves every generated pattern actually BLOCKs
its source value through the real ``scan_diff`` path — the coverage-parity
requirement for moving install-private detection off the hardcoded lists.

No private value appears in this file: every probe is derived at runtime from
the live-harvested patterns / the local file, never hard-coded.
"""

from __future__ import annotations

import re

import pytest

from genesis.contribution import fingerprints as fp
from genesis.contribution import sanitize
from genesis.contribution.findings import FindingKind

_FP_FILE = fp._default_path()

pytestmark = pytest.mark.skipif(
    not _FP_FILE.is_file(),
    reason="local-only: needs a materialized ~/.genesis/release-fingerprints.txt (run scripts/bootstrap.sh)",
)


def _make_diff(added: str) -> str:
    return (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n existing\n"
        + f"+{added}\n"
    )


def _sample_from_pattern(pat: str) -> str | None:
    """Derive a string that matches ``pat`` for escaped-literal patterns.

    Generated patterns are ``re.escape``'d literals optionally wrapped in ``\\b``
    (verified by the ERE-contract unit test), so un-escaping is deterministic.
    Returns None for a pattern the un-escape can't reproduce (an exotic
    hand-edited regex with char-classes/quantifiers) — the caller skips it.
    """
    core = pat
    if core.startswith(r"\b"):
        core = core[2:]
    if core.endswith(r"\b"):
        core = core[:-2]
    sample = re.sub(r"\\(.)", r"\1", core)  # drop the escaping backslashes
    if sample.endswith("."):  # IP /24 prefix → append an octet
        sample += "7"
    # Only usable if the pattern actually matches the reconstructed sample.
    return sample if re.search(pat, sample) else None


def test_fingerprint_file_has_active_patterns():
    active = fp._active_patterns(_FP_FILE)
    assert active, "materialized fingerprint file has zero active patterns"


def test_live_harvest_is_nonempty():
    # Guard against a vacuous parity pass (architect finding F).
    assert fp.harvest(), "live harvest produced nothing — parity would prove nothing"


def test_every_active_pattern_blocks_its_value():
    active = fp._active_patterns(_FP_FILE)
    assert active, "no active patterns to prove"
    proven = 0
    exotic: list[str] = []
    for pat in active:
        sample = _sample_from_pattern(pat)
        if sample is None:
            exotic.append(pat)
            continue
        r = sanitize.scan_diff(_make_diff(f"leaked value {sample} here"), fingerprint_file=_FP_FILE)
        assert r.ok is False, f"pattern {pat!r} did not block its own value"
        assert any(f.kind == FindingKind.FINGERPRINT for f in r.blocking()), (
            f"pattern {pat!r} value blocked, but not by the fingerprint layer"
        )
        proven += 1
    # At least the config-derivable (auto-generated) patterns must be probe-proven.
    assert proven > 0, f"proved zero patterns (all {len(exotic)} were exotic/unreconstructable)"
