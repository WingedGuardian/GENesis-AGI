"""Pin the repository signing key: falkordb_redis_install installs the upstream
apt signing key only when the downloaded file holds exactly one key whose
fingerprint is the pinned one.

Harness: falkordb_stubs.py. The gpg stub answers ``--show-keys --with-colons``
with the colon listing real gpg prints for the served key (including the
subkey's own ``fpr`` record); GPG_KEY_FPRS and GPG_SHOW_RC vary it. Nothing
here touches the real system or the network.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.test_scripts.falkordb_stubs import _apt_log, _run, _stage

PIN = "54318FA4052D1E61A6B6F7BB5F4349D6BF53AA0C"
OTHER = "0123456789ABCDEF0123456789ABCDEF01234567"


def _stage_pin(tmp_path: Path) -> dict:
    env = _stage(tmp_path)
    # A keyring directory this run would have to create: a refusal must leave
    # not even the directory behind.
    env["FALKORDB_APT_KEYRING"] = str(tmp_path / "keyrings" / "redis.gpg")
    # The lib's temp files (downloaded key, throwaway GNUPGHOME) land here, so
    # a test can see that a refusal cleans them up.
    tmpdir = tmp_path / "lib-tmp"
    tmpdir.mkdir()
    env["TMPDIR"] = str(tmpdir)
    return env


def _assert_refused_without_change(env: dict, result, tmp_path: Path) -> None:
    assert result.returncode == 0, result.stderr
    assert "repo NOT added" in result.stdout, result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists(), "apt list written"
    assert not Path(env["FALKORDB_APT_KEYRING"]).exists(), "keyring written"
    assert not (tmp_path / "keyrings").exists(), "keyring directory created"
    log = _apt_log(env)
    assert "--dearmor" not in log, "key was installed anyway"
    assert "apt-get" not in log, "apt was invoked anyway"
    assert list((tmp_path / "lib-tmp").iterdir()) == [], "temp files left behind"


def test_the_pinned_key_is_checked_before_it_is_installed(tmp_path):
    env = _stage_pin(tmp_path)
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert Path(env["FALKORDB_APT_LIST"]).exists(), result.stdout
    assert Path(env["FALKORDB_APT_KEYRING"]).exists(), result.stdout
    gpg_calls = [line for line in _apt_log(env).splitlines() if line.startswith("gpg ")]
    show = [i for i, line in enumerate(gpg_calls) if "--show-keys" in line]
    dearmor = [i for i, line in enumerate(gpg_calls) if "--dearmor" in line]
    assert show, f"the fingerprint was never checked: {gpg_calls}"
    assert dearmor and show[0] < dearmor[0], gpg_calls
    assert "--with-colons" in gpg_calls[show[0]], "must read the colon listing"
    assert "apt-get install" in _apt_log(env)


def test_a_different_fingerprint_is_refused_with_no_system_change(tmp_path):
    env = _stage_pin(tmp_path)
    env["GPG_KEY_FPRS"] = OTHER
    result = _run("falkordb_redis_install", env)

    _assert_refused_without_change(env, result, tmp_path)
    assert OTHER in result.stdout and PIN in result.stdout, result.stdout


def test_a_missing_gpg_is_refused_with_no_system_change(tmp_path):
    env = _stage_pin(tmp_path)
    stub_bin = Path(env["PATH"].split(":")[0])
    (stub_bin / "gpg").unlink()
    # The real gpg lives in /usr/bin, so rebuild a system PATH without it.
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    for entry in os.scandir("/usr/bin"):
        if entry.name != "gpg":
            (sysbin / entry.name).symlink_to(entry.path)
    env["PATH"] = f"{stub_bin}:{sysbin}"
    result = _run("falkordb_redis_install", env)

    assert result.returncode == 0, result.stderr
    assert "gpg" in result.stdout and "required" in result.stdout, result.stdout
    assert not Path(env["FALKORDB_APT_LIST"]).exists()
    assert not Path(env["FALKORDB_APT_KEYRING"]).exists()
    assert not (tmp_path / "keyrings").exists()
    assert "apt-get" not in _apt_log(env)


def test_two_keys_in_the_file_are_refused_even_when_the_first_matches(tmp_path):
    env = _stage_pin(tmp_path)
    env["GPG_KEY_FPRS"] = f"{PIN} {OTHER}"
    result = _run("falkordb_redis_install", env)

    _assert_refused_without_change(env, result, tmp_path)
    assert "holds 2 keys" in result.stdout, result.stdout


def test_a_file_with_no_key_is_refused(tmp_path):
    env = _stage_pin(tmp_path)
    env["GPG_KEY_FPRS"] = ""
    result = _run("falkordb_redis_install", env)

    _assert_refused_without_change(env, result, tmp_path)
    assert "holds 0 keys" in result.stdout, result.stdout


def test_a_key_gpg_cannot_list_is_refused(tmp_path):
    """Unparseable bytes, or a gpg too old for --show-keys, fail closed."""
    env = _stage_pin(tmp_path)
    env["GPG_SHOW_RC"] = "2"
    result = _run("falkordb_redis_install", env)

    _assert_refused_without_change(env, result, tmp_path)
    assert "could not read the downloaded signing key" in result.stdout, result.stdout


def test_the_pin_is_not_an_environment_override(tmp_path):
    """A pin the environment could replace would be advice, not a pin."""
    env = _stage_pin(tmp_path)
    env["FALKORDB_REDIS_KEY_FPR"] = OTHER
    result = _run('printf "%s" "$FALKORDB_REDIS_KEY_FPR"', env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == PIN
