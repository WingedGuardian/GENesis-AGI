"""Tests for scripts/lib/load_secrets.sh — reads secrets as DATA.

The loader replaced `set -a; source secrets.env` in backup.sh
(2026-07-10 safety-gate remediation): sourcing executes the file, so a
value containing $(...) runs code. The loader must (1) never evaluate,
and (2) stay behavior-identical to `source` for every value shape the
file legitimately holds.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "load_secrets.sh"

_FIXTURE = """\
# full-line comment
PLAIN=abc
EMPTY=

DQ="a b"
SQ='c d'
TRAIL=xval  # trailing comment
HASH=a#b
export EXPORTED=ok
1BAD=nope
BAD-KEY=nope
   INDENTED=fine
"""


def _load_and_dump(tmp_path: Path, content: str, keys: list[str]) -> list[str]:
    """Run the loader on a fixture file, return values NUL-separated."""
    secrets = tmp_path / "secrets.env"
    secrets.write_text(content)
    dump = " ".join(f'printf "%s\\0" "${{{k}}}";' for k in keys)
    proc = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; load_secrets_file "{secrets}"; {dump}'],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split("\0")[:-1]


class TestLoadSecrets:
    def test_value_shapes_match_source_semantics(self, tmp_path):
        values = _load_and_dump(
            tmp_path, _FIXTURE,
            ["PLAIN", "EMPTY", "DQ", "SQ", "TRAIL", "HASH",
             "EXPORTED", "INDENTED"],
        )
        assert values == [
            "abc", "", "a b", "c d", "xval", "a#b", "ok", "fine",
        ]

    def test_invalid_keys_skipped(self, tmp_path):
        secrets = tmp_path / "secrets.env"
        secrets.write_text(_FIXTURE)
        proc = subprocess.run(
            ["bash", "-c",
             f'source "{LIB}"; load_secrets_file "{secrets}"; env'],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert "nope" not in proc.stdout

    def test_never_executes_values(self, tmp_path):
        evil = (
            "EVIL=$(touch pwned-subst)\n"
            "EVIL2=`touch pwned-backtick`\n"
            "EVIL3=x; touch pwned-chain\n"
        )
        values = _load_and_dump(tmp_path, evil, ["EVIL", "EVIL2", "EVIL3"])
        # Values are the LITERAL text — nothing ran.
        assert values == [
            "$(touch pwned-subst)", "`touch pwned-backtick`",
            "x; touch pwned-chain",
        ]
        assert not list(tmp_path.glob("pwned*"))

    def test_equivalent_to_source_for_shell_safe_file(self, tmp_path):
        """For the shapes secrets.env legitimately holds, the loader and
        `source` must export IDENTICAL values (regression: silent value
        drift would corrupt the backup passphrase)."""
        keys = ["PLAIN", "EMPTY", "DQ", "SQ", "TRAIL", "HASH",
                "EXPORTED", "INDENTED"]
        loader_vals = _load_and_dump(tmp_path, _FIXTURE, keys)
        secrets = tmp_path / "secrets.env"
        dump = " ".join(f'printf "%s\\0" "${{{k}}}";' for k in keys)
        sourced = subprocess.run(
            ["bash", "-c", f'set -a; source "{secrets}"; set +a; {dump}'],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert sourced.returncode == 0, sourced.stderr
        assert loader_vals == sourced.stdout.split("\0")[:-1]

    def test_quoted_value_with_inline_comment(self, tmp_path):
        """`KEY="a b" # c` -> `a b` (quotes AND comment stripped), matching
        `source`. Leaving the quotes would corrupt the backup passphrase
        (2026-07-10 review P2)."""
        content = (
            'PP="abc def" # backup\n'
            "SQ='x y' # note\n"
            'DQEMBED="a#b" # c\n'
        )
        vals = _load_and_dump(tmp_path, content, ["PP", "SQ", "DQEMBED"])
        assert vals == ["abc def", "x y", "a#b"]

    def test_missing_file_is_noop(self, tmp_path):
        proc = subprocess.run(
            ["bash", "-c",
             f'source "{LIB}"; load_secrets_file "{tmp_path}/absent.env"; '
             f'echo alive'],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0
        assert "alive" in proc.stdout


@pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
@pytest.mark.parametrize("value", ["\u00a0edge\u00a0", "\u2003edge\u2003", "edge\u0085inside", "edge\u2028inside"])
def test_unicode_value_bytes_are_not_syntax_whitespace(tmp_path, locale, value):
    path = tmp_path / "secrets.env"
    path.write_text("GENESIS_BACKUP_PASSPHRASE=" + value + "\n")
    proc = subprocess.run(
        ["bash", "-c", 'source "$1"; load_secrets_file "$2"; printf "%s\\0%s" "$GENESIS_BACKUP_PASSPHRASE" "$LC_ALL"',
         "test", str(LIB), str(path)],
        env={**os.environ, "LC_ALL": locale}, capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    actual, restored_locale = proc.stdout.split(b"\0")
    assert actual == value.encode()
    assert restored_locale.decode() == locale


@pytest.mark.parametrize("incoming_locale", [None, "C", "C.UTF-8"])
@pytest.mark.parametrize("file_locale", ["C", "C.UTF-8"])
@pytest.mark.parametrize("locale_first", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
def test_file_locale_exports_without_changing_ascii_syntax(
    tmp_path, incoming_locale, file_locale, locale_first, quoted
):
    value = "\u2003edge\u2003"
    locale_line = "LC_ALL=" + file_locale + "\n"
    value_line = "GENESIS_BACKUP_PASSPHRASE=" + ('"' + value + '"' if quoted else value) + "\n"
    path = tmp_path / "secrets.env"
    path.write_text(locale_line + value_line if locale_first else value_line + locale_line)
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    if incoming_locale is not None:
        env["LC_ALL"] = incoming_locale
    proc = subprocess.run(
        ["bash", "-c", 'source "$1"; load_secrets_file "$2"; printf "%s\\0%s" "$GENESIS_BACKUP_PASSPHRASE" "$LC_ALL"',
         "test", str(LIB), str(path)], env=env, capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    actual, exported_locale = proc.stdout.split(b"\0")
    assert actual == value.encode()
    assert exported_locale.decode() == file_locale
