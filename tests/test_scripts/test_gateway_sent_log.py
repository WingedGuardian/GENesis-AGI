"""Tests for the guardian-gateway.sh ``sent-log --since <epoch>`` read-only verb.

Runs the REAL gateway script against a throwaway ``$HOME`` install. The verb
takes an argument from SSH_ORIGINAL_COMMAND, which is untrusted, so the strict
shape check is the security surface: every rejected value must fail BEFORE the
guardian CLI runs. Also covers the venv-missing guard, the src-skew guard (an old
src must never fall through into a full ``run_check`` cycle) and correct
forwarding of a valid value. The CLI's output is covered by
``tests/test_guardian/test_alert_sent_log.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_GATEWAY = Path(__file__).resolve().parents[2] / "scripts" / "guardian-gateway.sh"


def _run(home: Path, verb: str):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["SSH_ORIGINAL_COMMAND"] = verb
    return subprocess.run(
        ["bash", str(_GATEWAY)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _install(home: Path, *, with_src: bool = True) -> Path:
    d = home / ".local" / "share" / "genesis-guardian"
    (d / "config").mkdir(parents=True)
    (d / "config" / "guardian.yaml").write_text("container_name: genesis\n")
    py = d / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\necho "ARGS: $*"\n')
    py.chmod(0o755)
    if with_src:
        mod = d / "src" / "genesis" / "guardian" / "alert"
        mod.mkdir(parents=True)
        (mod / "sent_log.py").write_text("# stub for the skew guard\n")
    return d


@pytest.mark.parametrize("since", ["0", "1761600000", "1761600000.5", "1761600000.123456"])
def test_valid_since_is_forwarded(tmp_path, since):
    _install(tmp_path)
    proc = _run(tmp_path, f"sent-log --since {since}")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"ARGS: -m genesis.guardian --sent-log {since}"


@pytest.mark.parametrize(
    "since",
    [
        "",
        "-1",
        "+1",
        "1e9",
        "12345678901",  # 11 integer digits
        "1.1234567",  # 7 fraction digits
        "1.",
        ".5",
        "1 2",
        "1;id",
        "$(id)",
        "`id`",
        "1\n2",
        "1\n",
        "0x10",
        "nan",
    ],
)
def test_invalid_since_is_refused_before_the_cli(tmp_path, since):
    _install(tmp_path)
    proc = _run(tmp_path, f"sent-log --since {since}")
    assert proc.returncode == 1
    assert "invalid since" in proc.stderr
    assert "ARGS:" not in proc.stdout


@pytest.mark.parametrize("verb", ["sent-log", "sent-log 5", "sent-log --since"])
def test_malformed_verb_is_denied(tmp_path, verb):
    _install(tmp_path)
    proc = _run(tmp_path, verb)
    assert proc.returncode == 1
    assert "ARGS:" not in proc.stdout


def test_no_venv(tmp_path):
    install = _install(tmp_path)
    (install / ".venv" / "bin" / "python").unlink()
    proc = _run(tmp_path, "sent-log --since 0")
    assert proc.returncode == 1
    assert "guardian venv not found" in proc.stderr


def test_skew_guard_blocks_fallthrough(tmp_path):
    """An install whose src predates sent_log.py must return a clean error and
    never invoke the CLI (whose old main() would run a full guardian cycle)."""
    _install(tmp_path, with_src=False)
    proc = _run(tmp_path, "sent-log --since 0")
    assert proc.returncode == 1
    assert "predates sent-log" in proc.stderr
    assert "ARGS:" not in proc.stdout


@pytest.mark.parametrize("since", ["٣", "1٣", "１"])  # Arabic-Indic, fullwidth
@pytest.mark.parametrize("locale", ["en_US.UTF-8", "C.UTF-8"])
def test_non_ascii_digits_are_refused_in_a_utf8_locale(tmp_path, since, locale):
    """A [0-9] range follows the locale's collation and, under en_US.UTF-8,
    admitted a non-ASCII digit; the gateway lists the ASCII digits instead.
    (Where the locale is not installed bash falls back to C, which passes.)"""
    _install(tmp_path)
    env = dict(os.environ, HOME=str(tmp_path), LANG=locale, LC_ALL=locale,
               SSH_ORIGINAL_COMMAND=f"sent-log --since {since}")
    proc = subprocess.run(
        ["bash", str(_GATEWAY)], env=env, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 1
    assert "invalid since" in proc.stderr
    assert "ARGS:" not in proc.stdout
