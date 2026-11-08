"""Confine machine-wide /proc scans in a test to the test's own processes.

Some scripts decide by scanning every same-user process in /proc: is a git
working in this checkout, does anyone hold this lock open. A test that spawns its
own holder and asks that question gets the right answer only while no unrelated
process matches. Under pytest-xdist the other workers run git subprocesses of
their own at the same moment, and a git whose fd table is unreadable (exiting,
or in state D) counts as a holder, so the verdict flips: three such tests failed
one of six parallel CI runs (2026-11-06). MEASURED: with one foreign
non-dumpable process named git alive, test_stale_index_lock.py failed 18 of 45
without this view and 0 of 45 with it.

`own_processes_only(monkeypatch)` makes `os.listdir("/proc")` list only
processes descended from this test process (or from `ancestor`). Everything
else, including every other path and every direct /proc/<pid> read, is
untouched. `CLI_STUB` applies the same view inside a script run as a separate
process.
"""

from __future__ import annotations

import os

_PROC = "/proc"


def _parent(pid: int) -> int | None:
    try:
        with open(f"{_PROC}/{pid}/stat") as fh:
            # The command name is in parentheses and may hold spaces or ")",
            # so the fields after it are split from the last ")".
            return int(fh.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def _descends_from(pid: int, ancestor: int) -> bool:
    seen = set()
    while pid and pid not in seen:
        if pid == ancestor:
            return True
        seen.add(pid)
        parent = _parent(pid)
        if parent is None:
            return False
        pid = parent
    return False


def own_processes_only(monkeypatch, ancestor: int | None = None) -> None:
    real_listdir = os.listdir
    root = os.getpid() if ancestor is None else ancestor

    def listdir(path="."):
        names = real_listdir(path)
        if isinstance(path, int) or os.path.normpath(os.fsdecode(path)) != _PROC:
            return names
        return [n for n in names if not n.isdigit() or _descends_from(int(n), root)]

    monkeypatch.setattr(os, "listdir", listdir)


# Run a script as its own process with the same view, rooted at the process that
# started it (the test): `python -I -S -c CLI_STUB <this file> <script> <args>...`.
# It execs this file's source, so it needs no sys.path.
CLI_STUB = """
import os, runpy, sys
helper, script, *args = sys.argv[1:]
exec(compile(open(helper).read(), helper, "exec"))
class _Patch:
    def setattr(self, obj, name, value):
        setattr(obj, name, value)
own_processes_only(_Patch(), ancestor=os.getppid())
sys.argv = [script, *args]
runpy.run_path(script, run_name="__main__")
"""
