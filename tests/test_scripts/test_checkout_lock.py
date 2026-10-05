from __future__ import annotations

import fcntl
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts._checkout_lock_helpers import can_lock, git, held, repo

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "lib" / "checkout_lock.sh"
pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="bash-only")


def test_shell_helper_holds_exclusive_lock_and_unlock_is_idempotent(tmp_path):
    root = repo(tmp_path)
    lock_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    lock_path /= "genesis-checkout.lock"
    body = (
        'source "$1"; genesis_checkout_lock "$2"; echo held; read -r _; '
        "genesis_checkout_unlock; genesis_checkout_unlock"
    )
    proc = subprocess.Popen(
        ["bash", "-c", body, "bash", str(HELPER), str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    assert not can_lock(lock_path, fcntl.LOCK_SH)
    proc.stdin.write("\n")
    proc.stdin.flush()
    assert proc.wait(timeout=5) == 0
    assert can_lock(lock_path, fcntl.LOCK_EX)


def test_shell_helper_times_out_behind_shared_holder(tmp_path):
    root = repo(tmp_path)
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    lock_path = common / "genesis-checkout.lock"
    with held(lock_path, fcntl.LOCK_SH):
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; genesis_checkout_lock "$2"',
                "bash",
                str(HELPER),
                str(root),
            ],
            env={"PATH": "/usr/bin:/bin", "GENESIS_CHECKOUT_LOCK_WAIT_S": "1"},
            capture_output=True,
            text=True,
            timeout=5,
        )
    assert result.returncode == 1
    assert "checkout busy (a Claude launch holds genesis-checkout.lock)" in result.stderr


def test_shell_helper_warns_and_fails_open_for_unresolvable_checkout(tmp_path):
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; genesis_checkout_lock "$2"',
            "bash",
            str(HELPER),
            str(tmp_path / "missing"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "cannot resolve checkout lock path" in result.stderr
