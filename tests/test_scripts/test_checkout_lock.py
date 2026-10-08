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


def test_a_trap_during_the_wait_does_not_take_the_lock_as_held(tmp_path):
    """A signal while the lock is still awaited runs update.sh's trap, which
    re-enters genesis_checkout_lock for the rollback. The re-entry check trusts
    GENESIS_CHECKOUT_LOCK_FD, so that variable must not exist until flock has
    succeeded: here a shared holder keeps the lock, and the trap's re-entry has
    to wait for it and refuse rather than proceed as if it held it."""
    import os
    import signal
    import time

    root = repo(tmp_path)
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    lock_path = common / "genesis-checkout.lock"
    body = (
        'source "$1"; R="$2"; '
        "trap 'if GENESIS_CHECKOUT_LOCK_WAIT_S=1 genesis_checkout_lock \"$R\"; "
        "then echo TRAP-HELD; else echo TRAP-REFUSED; fi; exit 0' TERM; "
        'echo waiting; GENESIS_CHECKOUT_LOCK_WAIT_S=30 genesis_checkout_lock "$2"; '
        "echo NOT-REACHED"
    )
    with held(lock_path, fcntl.LOCK_SH):
        proc = subprocess.Popen(
            ["bash", "-c", body, "bash", str(HELPER), str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            assert proc.stdout.readline().strip() == "waiting"
            # The whole group, as a service stop or ^C delivers it: flock dies too.
            assert proc.pid > 1
            children = Path(f"/proc/{proc.pid}/task/{proc.pid}/children")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not any(
                Path(f"/proc/{c}/comm").read_text().strip() == "flock"
                for c in children.read_text().split()
                if Path(f"/proc/{c}/comm").exists()
            ):
                time.sleep(0.05)
            os.killpg(proc.pid, signal.SIGTERM)
            out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    assert "TRAP-REFUSED" in out, out + err
    assert "TRAP-HELD" not in out
    assert "NOT-REACHED" not in out


def test_unlock_unpublishes_the_lock_before_closing_it():
    """A trap between the two steps must never see the lock as held after its
    descriptor is closed, so unlock unsets GENESIS_CHECKOUT_LOCK_FD first."""
    import re

    text = HELPER.read_text()
    body = text[text.index("genesis_checkout_unlock() {") :]
    body = body[: body.index("\n}\n")]
    unset_at = body.index("unset GENESIS_CHECKOUT_LOCK_FD")
    close_at = re.search(r"exec \{\w+\}>&-", body).start()
    assert unset_at < close_at


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


@pytest.mark.parametrize("script", ["update.sh", "deploy_code_only.sh", "lib/deploy_recovery.sh"])
def test_every_hook_running_git_call_is_wrapped(script):
    """checkout and merge run git hooks (post-checkout, post-merge). Each must go
    through genesis_without_checkout_lock, or run with hooks disabled, so a hook's
    child cannot hold the lock. The whole file, not the text between lock and
    unlock: helpers defined elsewhere run inside the held regions (the rollback's
    _ephemeral_clear_before_reset did), and the wrapper is a plain call when no
    lock is held."""
    import re

    text = (REPO_ROOT / "scripts" / script).read_text()
    lines, spans = _held_regions(text)
    # The rollback helpers moved into lib/deploy_recovery.sh run inside the
    # scripts' held regions; the lib has none of its own.
    assert spans or script.startswith("lib/"), f"{script}: no held region found"
    # The subcommand must end at whitespace: `merge-base` reads and runs no hooks.
    hooky = re.compile(r"\bgit (?:-c \S+ )*-C \S+ (?:-c \S+ )*(?:checkout|merge|switch)(?=\s|$)")
    calls = [n for n, line in enumerate(lines) if hooky.search(line.split("#", 1)[0])]
    assert calls, f"{script}: the pattern found no git calls at all"
    unwrapped = [
        f"{script}:{n + 1}: {lines[n].strip()}"
        for n in calls
        if "genesis_without_checkout_lock" not in lines[n]
        and "core.hooksPath=/dev/null" not in lines[n]
    ]
    assert unwrapped == []
