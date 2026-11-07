"""tests/_own_processes.py: /proc lists only this test's descendants."""

from __future__ import annotations

import os
import subprocess
import sys

from tests._own_processes import own_processes_only


def test_proc_lists_this_tests_children_and_hides_other_processes(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert "1" in os.listdir("/proc")  # control: the real listing has init
        own_processes_only(monkeypatch)
        pids = {int(n) for n in os.listdir("/proc") if n.isdigit()}
        assert child.pid in pids
        assert 1 not in pids
        assert os.getppid() not in pids  # pytest's parent is not this test's
        # Only the /proc listing changes; other paths and per-pid reads do not.
        assert os.listdir("/proc/self/fd")
        assert os.path.exists("/proc/1")
    finally:
        child.kill()
        child.wait()
