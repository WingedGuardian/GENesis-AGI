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


def _hooked_repo(tmp_path):
    """A repo whose post-checkout hook leaves a process running in the background,
    the way an install's own hook might (a re-indexer, a notifier)."""
    root = repo(tmp_path)
    git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "c1",
    )
    hook = root / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nsleep 5 >/dev/null 2>&1 &\n")
    hook.chmod(0o755)
    lock_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return root, lock_path / "genesis-checkout.lock"


@pytest.mark.parametrize("wrapped", [True, False], ids=["wrapper", "control"])
def test_a_hooks_background_child_does_not_keep_the_lock(tmp_path, wrapped):
    """A git call made while the lock is held runs hooks as its children. Through
    genesis_without_checkout_lock, a hook's background child gets no copy of the
    lock descriptor, so releasing the lock frees it at once. The unwrapped control
    shows the leak the wrapper exists to prevent."""
    root, lock_path = _hooked_repo(tmp_path)
    call = "genesis_without_checkout_lock git" if wrapped else "git"
    body = (
        'source "$1"; genesis_checkout_lock "$2"; '
        f'{call} -C "$2" checkout -q -b side; genesis_checkout_unlock'
    )
    result = subprocess.run(
        ["bash", "-c", body, "bash", str(HELPER), str(root)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert can_lock(lock_path, fcntl.LOCK_EX) is wrapped


def _held_regions(text):
    """(start, end) line spans between each genesis_checkout_lock call and the
    next genesis_checkout_unlock."""
    lines = text.splitlines()
    spans, start = [], None
    for i, line in enumerate(lines):
        code = line.split("#", 1)[0]
        if 'genesis_checkout_lock "$GENESIS_ROOT"' in code and start is None:
            start = i
        elif "genesis_checkout_unlock" in code and start is not None:
            spans.append((start, i))
            start = None
    return lines, spans


@pytest.mark.parametrize("script", ["update.sh", "deploy_code_only.sh"])
def test_every_hook_running_git_call_in_a_held_region_is_wrapped(script):
    """checkout and merge run git hooks (post-checkout, post-merge). Inside the
    held regions each must go through genesis_without_checkout_lock, or run with
    hooks disabled, so a hook's child cannot hold the lock."""
    import re

    lines, spans = _held_regions((REPO_ROOT / "scripts" / script).read_text())
    assert spans, f"{script}: no held region found"
    # The subcommand must end at whitespace: `merge-base` reads and runs no hooks.
    hooky = re.compile(r"\bgit (?:-c \S+ )*-C \S+ (?:-c \S+ )*(?:checkout|merge|switch)(?=\s|$)")
    unwrapped = [
        f"{script}:{n + 1}: {lines[n].strip()}"
        for a, b in spans
        for n in range(a, b + 1)
        if hooky.search(lines[n].split("#", 1)[0])
        and "genesis_without_checkout_lock" not in lines[n]
        and "core.hooksPath=/dev/null" not in lines[n]
    ]
    assert unwrapped == []
