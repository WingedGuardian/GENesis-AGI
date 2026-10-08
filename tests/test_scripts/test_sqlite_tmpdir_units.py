"""Units that run Genesis Python and keep TMPDIR on cc-tmp send SQLite's temp
files to ~/tmp.

genesis-server, the bridge and the watchdog set TMPDIR to the small cc-tmp
volume on purpose: the Claude Code sessions they launch inherit it. SQLite
inherits it too, so a large sort or index build spills a 100-200 MB `etilqs_*`
file onto cc-tmp, which the runaway-file pager reports as a disk filling (five
pages in one night, all from genesis-server). SQLite reads SQLITE_TMPDIR before
TMPDIR (MEASURED 2026-10-08, SQLite 3.45.1; a missing directory falls back to
TMPDIR with no error), so one line moves only SQLite's spills.

ALLOWLIST POLARITY: every template that points TMPDIR at cc-tmp must carry the
line and create ~/tmp, so a new such unit fails here until it does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SYSTEMD = Path(__file__).resolve().parents[2] / "scripts" / "systemd"
SQLITE_LINE = "Environment=SQLITE_TMPDIR=__HOME__/tmp"


# Any spelling of TMPDIR pointing at cc-tmp selects the unit, however it starts
# its process: SQLite runs wherever Genesis Python runs, including behind a shell
# wrapper, and the extra line costs nothing in a unit that never opens SQLite.
_CC_TMP_RE = re.compile(r'^Environment="?TMPDIR=(__HOME__|%h)/\.genesis/cc-tmp"?\s*$', re.M)


def _python_units_on_cc_tmp() -> list[Path]:
    return [
        path
        for path in sorted(SYSTEMD.glob("*.service.template"))
        if _CC_TMP_RE.search(path.read_text())
    ]


def test_the_known_units_are_in_scope():
    names = {p.name for p in _python_units_on_cc_tmp()}
    assert {
        "genesis-server.service.template",
        "genesis-bridge.service.template",
        "genesis-watchdog.service.template",
    } <= names, names


@pytest.mark.parametrize("path", _python_units_on_cc_tmp(), ids=lambda p: p.name)
def test_sqlite_temp_files_go_to_the_big_disk(path):
    lines = path.read_text().splitlines()
    assert lines.count(SQLITE_LINE) == 1, path.name
    # The directory must exist or SQLite silently falls back to TMPDIR.
    assert "ExecStartPre=-/bin/mkdir -p __HOME__/tmp" in lines, path.name
