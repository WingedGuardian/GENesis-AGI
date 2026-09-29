"""backup.sh backs up the content-boundary key only when it is a real key.

The runtime ignores anything at that path that is not a regular file holding 64
hex characters, so the backup applies the same test before replacing its
encrypted copy: a corrupt or oversized file must not overwrite a good backup.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_BACKUP = Path(__file__).resolve().parents[2] / "scripts" / "backup.sh"
_KEY = "ab" * 32


def _function_source() -> str:
    text = _BACKUP.read_text()
    match = re.search(r"^_is_boundary_key\(\) \{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL)
    assert match, "backup.sh no longer defines _is_boundary_key"
    return match.group(0)


def _accepted(path: Path) -> bool:
    script = _function_source() + '_is_boundary_key "$1"\n'
    return subprocess.run(["bash", "-c", script, "_", str(path)], check=False).returncode == 0


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("good", _KEY + "\n", True),
        ("bad", "zz\n", False),
        ("oversized", _KEY + " " * 400, False),
        ("empty", "", False),
    ],
)
def test_only_a_real_key_is_accepted(tmp_path, name, content, expected):
    path = tmp_path / name
    path.write_text(content)
    assert _accepted(path) is expected


def test_a_symlink_or_missing_file_is_refused(tmp_path):
    good = tmp_path / "good"
    good.write_text(_KEY)
    link = tmp_path / "link"
    link.symlink_to(good)
    assert _accepted(link) is False
    assert _accepted(tmp_path / "missing") is False


def test_the_backup_checks_the_key_before_encrypting_it():
    text = _BACKUP.read_text()
    check = text.index('if [ "$_dstn" = "boundary_key" ] && ! _is_boundary_key "$_srcf"; then')
    encrypt = text.index('if encrypt_file "$_srcf" "creds/${_dstn}.gpg"; then')
    assert check < encrypt
