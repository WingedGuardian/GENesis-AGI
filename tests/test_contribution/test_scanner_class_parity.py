"""Cross-surface parity for the address-class vocabulary.

The RFC1918 / CGNAT / IPv6-ULA class regexes are encoded independently on three
surfaces — ``sanitize.py`` (`_PORTABILITY_PATTERNS`, Python ``re``),
``scripts/hooks/commit-msg`` (``grep -E``), and ``scripts/check_portability.sh``
(``rg``). They MUST stay in sync; this test asserts a representative in-class
value is caught by every available surface and that RFC 5737 documentation
values are caught by none. Guards against the surfaces drifting when the class
vocabulary changes (architect finding E-1).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from genesis.contribution import sanitize
from genesis.contribution.findings import FindingKind

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMIT_MSG = REPO_ROOT / "scripts" / "hooks" / "commit-msg"
CHECK_PORTABILITY = REPO_ROOT / "scripts" / "check_portability.sh"

# Representative in-class values — NOT this install's literals. One per class.
IN_CLASS = ["10.0.0.1", "172.16.0.1", "192.168.0.1", "100.64.0.1", "fdff::1"]
# RFC 5737 doc ranges — the sanctioned way to write example IPs; never flagged.
DOC_SAFE = ["192.0.2.1", "198.51.100.7", "203.0.113.9"]


def _sanitize_blocks(value: str, tmp_path: Path) -> bool:
    diff = f"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+V = '{value}'\n"
    r = sanitize.scan_diff(diff, fingerprint_file=tmp_path / "no-fp.txt")
    return any(f.kind == FindingKind.PORTABILITY for f in r.findings)


def _commit_msg_blocks(value: str, tmp_path: Path) -> bool:
    """Run the real commit-msg hook with the fingerprint layer disabled, so only
    the class layer decides. Non-zero exit == blocked."""
    msg = tmp_path / "MSG"
    msg.write_text(f"work touching {value} here\n")
    env = dict(os.environ)
    env["GENESIS_RELEASE_FINGERPRINTS"] = str(tmp_path / "no-fp.txt")
    proc = subprocess.run(
        ["bash", str(COMMIT_MSG), str(msg)],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode != 0


def _check_portability_blocks(value: str, tmp_path: Path) -> bool:
    """Run check_portability.sh (default gating mode) against a temp repo whose
    src/ contains `value`. Exit 1 == blocked."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "x.py").write_text(f"V = '{value}'\n")
    proc = subprocess.run(
        ["bash", str(CHECK_PORTABILITY), str(repo)],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    return proc.returncode == 1


@pytest.mark.parametrize("value", IN_CLASS)
def test_sanitize_and_commit_msg_agree_in_class(value, tmp_path):
    """sanitize.py and the commit-msg hook must both catch every in-class value."""
    assert _sanitize_blocks(value, tmp_path), f"sanitize.py missed {value}"
    assert _commit_msg_blocks(value, tmp_path), f"commit-msg missed {value}"


@pytest.mark.parametrize("value", DOC_SAFE)
def test_sanitize_and_commit_msg_agree_doc_safe(value, tmp_path):
    """Neither surface may flag an RFC 5737 documentation address."""
    assert not _sanitize_blocks(value, tmp_path), f"sanitize.py false-positived {value}"
    assert not _commit_msg_blocks(value, tmp_path), f"commit-msg false-positived {value}"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
@pytest.mark.parametrize("value", IN_CLASS)
def test_check_portability_agrees_in_class(value, tmp_path):
    """check_portability.sh must catch every in-class value the others catch."""
    assert _check_portability_blocks(value, tmp_path), f"check_portability missed {value}"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
@pytest.mark.parametrize("value", DOC_SAFE)
def test_check_portability_agrees_doc_safe(value, tmp_path):
    """check_portability.sh must not flag RFC 5737 documentation addresses."""
    assert not _check_portability_blocks(value, tmp_path), f"check_portability FP {value}"
