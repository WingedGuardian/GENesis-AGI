"""REAL-binary integration tests for the contribution secret scanners.

These deliberately do NOT mock detect-secrets / gitleaks (unlike test_sanitize.py's
``no_external_scanners`` fixture). They guard against the 2026-08 regression where
BOTH secret layers were silently dead while the mocked tests stayed green:

- ``_run_detect_secrets`` matched ``val == "true"`` but real detect-secrets output
  is ``True  (unverified)`` / ``True  (4.872)`` — the entropy/status suffix meant it
  parsed ZERO findings (the "required floor" caught nothing).
- ``_run_gitleaks`` passed ``--no-git`` alongside ``--pipe``; that combination makes
  gitleaks scan nothing from stdin (verified: even the default ``github-pat`` rule
  never fires). Removing ``--no-git`` restores the scan; ``-c .gitleaks.toml`` adds
  the genesis rules (the config extends defaults via ``useDefault = true``).

detect-secrets is a declared core dependency, so its test runs on CI. gitleaks is an
optional external binary → skipif-guarded.
"""

from __future__ import annotations

import shutil

import pytest

from genesis.contribution import sanitize
from genesis.contribution.findings import FindingKind, Severity

# A github PAT (default-rule secret for both scanners) + a 12-digit account id near
# a keyword (the genesis-aws-account-id custom rule, gitleaks -c only). Synthetic.
_SECRET_DIFF = (
    "diff --git a/config.py b/config.py\n"
    "--- a/config.py\n"
    "+++ b/config.py\n"
    "@@ -0,0 +1,2 @@\n"
    '+github_token = "ghp_1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7q8R"\n'
    '+account_id = "123456789012"\n'
)


@pytest.mark.skipif(shutil.which("detect-secrets") is None, reason="detect-secrets not installed")
def test_detect_secrets_real_binary_flags_suffixed_true():
    """The REAL detect-secrets '<plugin> : True  (suffix)' output must parse as a hit.

    RED against the ``== "true"`` parser (the suffix makes it miss every finding).
    """
    parsed = sanitize.parse_diff(_SECRET_DIFF)
    ran, hits = sanitize._run_detect_secrets(parsed)
    assert ran
    assert any(h.kind == FindingKind.SECRET and h.severity == Severity.BLOCK for h in hits), (
        "detect-secrets must BLOCK on the github token (real output is 'True  (unverified)')"
    )


@pytest.mark.skipif(
    shutil.which("gitleaks") is None and shutil.which("betterleaks") is None,
    reason="gitleaks not installed",
)
def test_gitleaks_real_binary_scans_stdin():
    """REAL gitleaks must actually scan the piped diff and flag the token.

    RED against ``--no-git --pipe`` (that combo scans nothing from stdin).
    """
    ran, hits = sanitize._run_gitleaks(_SECRET_DIFF)
    assert ran
    assert any(h.kind == FindingKind.SECRET and h.severity == Severity.BLOCK for h in hits), (
        "gitleaks must flag the github token via --pipe (default rule)"
    )


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_gitleaks_loads_repo_config_for_genesis_rules():
    """With -c <repo>/.gitleaks.toml the custom genesis-aws-account-id rule fires.

    Proves the config is actually loaded (extends defaults), not just default rules.
    """
    ran, hits = sanitize._run_gitleaks(_SECRET_DIFF)
    assert ran
    assert any(
        "account" in (h.detail or "").lower() or "123456789012" in (h.detail or "") for h in hits
    ), "the genesis-aws-account-id rule (only in .gitleaks.toml) must fire with -c"


@pytest.mark.skipif(shutil.which("detect-secrets") is None, reason="detect-secrets not installed")
def test_scan_diff_end_to_end_blocks_secret():
    """Full scan_diff over a secret-bearing diff must NOT be ok (the floor blocks it)."""
    result = sanitize.scan_diff(_SECRET_DIFF)
    assert result.ok is False
    assert any(f.kind == FindingKind.SECRET for f in result.blocking())
