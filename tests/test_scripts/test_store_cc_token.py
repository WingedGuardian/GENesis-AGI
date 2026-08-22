"""Tests for scripts/store_cc_token.sh — CC OAuth setup-token intake.

Focus: the token is read from stdin, written 0600 with a creation epoch, and —
the regression this fixes — the script no longer aborts with "HOME: unbound
variable" under `set -u` when the invoking shell has HOME unset (a recurring
stripped-env condition on this install). Runs the REAL script against a
throwaway HOME in tmp_path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "store_cc_token.sh"
_TOKEN = "sk-ant-oat01-faketesttoken-not-a-real-secret"
# Minimal PATH holding every external the script needs (getent, id, date, cut,
# tr, mkdir, chmod, mv). Covers both merged-/usr and split layouts.
_BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _run(env: dict, stdin: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_SCRIPT), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
    )


def test_stores_token_with_home_set(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": _BASE_PATH, "HOME": str(home)}
    r = _run(env, _TOKEN + "\n")
    assert r.returncode == 0, r.stderr
    token_file = home / ".genesis" / "cc_oauth_token.env"
    assert token_file.exists()
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"
    body = token_file.read_text()
    assert f"CLAUDE_CODE_OAUTH_TOKEN={_TOKEN}" in body
    assert "GENESIS_CC_TOKEN_CREATED_AT=" in body
    # The token value must never appear in the script's own stdout.
    assert _TOKEN not in r.stdout


def test_unset_home_falls_back_via_getent(tmp_path):
    """Regression: HOME unset must not abort under `set -u`.

    A getent shim on PATH redirects the passwd lookup to a tmp home, proving the
    script resolves HOME from passwd instead of crashing (and without touching
    the real ~/.genesis).
    """
    fake_home = tmp_path / "pwhome"
    fake_home.mkdir()
    shim_bin = tmp_path / "bin"
    shim_bin.mkdir()
    getent = shim_bin / "getent"
    # 7-field passwd line; field 6 (home) is the fake home the script must use.
    getent.write_text('#!/bin/sh\necho "u:x:$(id -u):$(id -g)::${FAKE_HOME}:/bin/sh"\n')
    getent.chmod(0o755)

    # env WITHOUT HOME; shim dir first on PATH so our getent wins.
    env = {"PATH": f"{shim_bin}:{_BASE_PATH}", "FAKE_HOME": str(fake_home)}
    assert "HOME" not in env

    # SAFETY: prove the guard resolves HOME *into* tmp_path BEFORE running the
    # real store — mirrors the script's resolution (passwd, fail-closed) so a
    # broken shim can't let the store write the fake token to a real path.
    probe = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'if [ -z "${HOME:-}" ]; then '
            'HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""; '
            'fi; printf %s "$HOME"',
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert probe.stdout == str(fake_home), f"shim did not sandbox HOME: {probe.stdout!r}"

    r = _run(env, _TOKEN + "\n")
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"
    token_file = fake_home / ".genesis" / "cc_oauth_token.env"
    assert token_file.exists(), "token not written to the passwd-resolved home"
    assert f"CLAUDE_CODE_OAUTH_TOKEN={_TOKEN}" in token_file.read_text()


def test_unset_home_unresolvable_fails_closed(tmp_path):
    """HOME unset AND passwd yields no home → fail closed, write nothing.

    Guessing /home/<user> could disagree with credential_bridge.py's
    passwd-based expanduser (root=/root, custom homes), silently storing the
    token where Guardian never reads it. The script must error, not guess.
    """
    shim_bin = tmp_path / "bin"
    shim_bin.mkdir()
    getent = shim_bin / "getent"
    getent.write_text("#!/bin/sh\nexit 0\n")  # succeeds but prints NO home field
    getent.chmod(0o755)

    env = {"PATH": f"{shim_bin}:{_BASE_PATH}"}  # no HOME
    r = _run(env, _TOKEN + "\n")
    assert r.returncode == 1, f"expected fail-closed exit 1; stderr={r.stderr!r}"
    assert "could not be resolved" in r.stderr.lower()
    # Nothing written anywhere (it errored before mkdir).
    assert _TOKEN not in (r.stdout + r.stderr)


def test_argv_token_is_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": _BASE_PATH, "HOME": str(home)}
    r = _run(env, "", _TOKEN)  # token as argv, not stdin
    assert r.returncode != 0
    assert "stdin" in r.stderr.lower()
    assert not (home / ".genesis" / "cc_oauth_token.env").exists()


def test_empty_stdin_errors(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": _BASE_PATH, "HOME": str(home)}
    r = _run(env, "\n   \n")  # whitespace only
    assert r.returncode != 0
    assert "no token" in r.stderr.lower()


def test_remove_is_clean_when_absent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": _BASE_PATH, "HOME": str(home)}
    r = _run(env, "", "--remove")
    assert r.returncode == 0, r.stderr
    assert "nothing to remove" in r.stdout.lower()


def test_file_intake_stores_token(tmp_path):
    """--file PATH reads the token from a file (the fix for the broken interactive
    `claude setup-token | store_cc_token.sh` pipe flow)."""
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "intoken"
    src.write_text(_TOKEN + "\n")
    env = {"PATH": _BASE_PATH, "HOME": str(home)}
    r = _run(env, "", "--file", str(src))
    assert r.returncode == 0, r.stderr
    token_file = home / ".genesis" / "cc_oauth_token.env"
    assert token_file.exists()
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"
    assert f"CLAUDE_CODE_OAUTH_TOKEN={_TOKEN}" in token_file.read_text()
    assert _TOKEN not in r.stdout  # the token value never appears in stdout


def test_file_intake_missing_file_errors(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": _BASE_PATH, "HOME": str(home)}
    r = _run(env, "", "--file", str(tmp_path / "does_not_exist"))
    assert r.returncode == 1
    assert "readable regular file" in r.stderr.lower()
    assert not (home / ".genesis" / "cc_oauth_token.env").exists()


def test_file_intake_requires_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": _BASE_PATH, "HOME": str(home)}
    r = _run(env, "", "--file")
    assert r.returncode == 1
    assert "requires a path" in r.stderr.lower()
    assert not (home / ".genesis" / "cc_oauth_token.env").exists()


def test_file_intake_warns_on_group_or_other_readable(tmp_path):
    """The token sits in the source file as plaintext; warn (non-fatal) if that file
    is readable by group/other (e.g. created under the default umask 022)."""
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "intoken"
    src.write_text(_TOKEN + "\n")
    src.chmod(0o644)
    env = {"PATH": _BASE_PATH, "HOME": str(home)}
    r = _run(env, "", "--file", str(src))
    assert r.returncode == 0, r.stderr
    assert "group/other" in r.stderr.lower()
    assert (
        home / ".genesis" / "cc_oauth_token.env"
    ).exists()  # still stored (warning is non-fatal)
    assert _TOKEN not in r.stdout


def test_file_intake_quiet_on_0600_source(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "intoken"
    src.write_text(_TOKEN + "\n")
    src.chmod(0o600)
    env = {"PATH": _BASE_PATH, "HOME": str(home)}
    r = _run(env, "", "--file", str(src))
    assert r.returncode == 0, r.stderr
    assert "group/other" not in r.stderr.lower()


def test_help_lists_file_option_without_leaking_code(tmp_path):
    """--help prints only the header block (robust to its length), not script code."""
    env = {"PATH": _BASE_PATH, "HOME": str(tmp_path)}
    r = _run(env, "", "--help")
    assert r.returncode == 0
    assert "--file" in r.stdout
    assert "set -euo pipefail" not in r.stdout
