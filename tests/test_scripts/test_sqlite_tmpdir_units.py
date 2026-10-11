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

import shlex
from pathlib import Path

import pytest

SYSTEMD = Path(__file__).resolve().parents[2] / "scripts" / "systemd"
CC_TMP = ("__HOME__/.genesis/cc-tmp", "%h/.genesis/cc-tmp")


def _assignments(text: str) -> list[tuple[str, str]]:
    """Every NAME=value an ``Environment=`` line sets, split the way systemd does:
    several whitespace-separated assignments per line, each optionally single- or
    double-quoted (systemd.exec, Environment=)."""
    found = []
    for line in text.splitlines():
        if not line.startswith("Environment="):
            continue
        for item in shlex.split(line.removeprefix("Environment=")):
            name, sep, value = item.partition("=")
            if sep:
                found.append((name, value))
    return found


def _python_units_on_cc_tmp() -> list[Path]:
    # Any spelling of TMPDIR pointing at cc-tmp selects the unit, however it starts
    # its process: SQLite runs wherever Genesis Python runs, including behind a
    # shell wrapper, and the extra line costs nothing in a unit that never opens it.
    return [
        path
        for path in sorted(SYSTEMD.glob("*.service.template"))
        if any(n == "TMPDIR" and v in CC_TMP for n, v in _assignments(path.read_text()))
    ]


def test_the_selector_reads_every_environment_form():
    """Round-2 review: the selector matched only a whole-line TMPDIR, so a unit
    using another valid form would have shipped without SQLITE_TMPDIR."""
    for line in (
        "Environment=FOO=1 TMPDIR=__HOME__/.genesis/cc-tmp",
        "Environment='TMPDIR=__HOME__/.genesis/cc-tmp'",
        'Environment="TMPDIR=%h/.genesis/cc-tmp" BAR=2',
    ):
        assert ("TMPDIR", CC_TMP[0]) in _assignments(line) or (
            "TMPDIR",
            CC_TMP[1],
        ) in _assignments(line), line


def test_the_known_units_are_in_scope():
    names = {p.name for p in _python_units_on_cc_tmp()}
    assert {
        "genesis-server.service.template",
        "genesis-bridge.service.template",
        "genesis-watchdog.service.template",
    } <= names, names


@pytest.mark.parametrize("path", _python_units_on_cc_tmp(), ids=lambda p: p.name)
def test_sqlite_temp_files_go_to_the_big_disk(path):
    text = path.read_text()
    lines = text.splitlines()
    assert [v for n, v in _assignments(text) if n == "SQLITE_TMPDIR"] == ["__HOME__/tmp"], path.name
    # Quoted whole, so a home directory with a space stays one assignment.
    assert 'Environment="SQLITE_TMPDIR=__HOME__/tmp"' in lines, path.name
    # The directory must exist and be writable, or SQLite falls back to TMPDIR: the
    # pre-start step creates it and warns when it is unusable, never refusing to start.
    pre = [line for line in lines if line.startswith("ExecStartPre=") and "SQLITE_TMPDIR" in line]
    assert len(pre) == 1, path.name
    assert pre[0].startswith("ExecStartPre=-"), path.name  # a failure here never stops the unit
    # It reads the directory systemd resolved for SQLite from the environment; no
    # install value appears in the Exec line, where systemd and the shell would
    # each parse it.
    assert "__HOME__" not in pre[0] and '"$$SQLITE_TMPDIR"' in pre[0], path.name


def test_the_pre_start_step_warns_on_an_unusable_directory(tmp_path):
    """Run the rendered command itself: a writable dir is silent, an unwritable or
    blocked one is reported on stderr, and the command never fails the unit."""
    import os

    def run(home):
        return _run_pre_start(home / "tmp")

    ok = run(tmp_path / "ok")
    assert (tmp_path / "ok" / "tmp").is_dir() and ok.stderr == ""
    if os.geteuid() == 0:
        pytest.skip("root can write a mode-0555 directory")
    ro = tmp_path / "ro"
    (ro / "tmp").mkdir(parents=True)
    (ro / "tmp").chmod(0o555)
    try:
        bad = run(ro)
    finally:
        (ro / "tmp").chmod(0o755)
    assert "not a writable directory" in bad.stderr
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "tmp").write_text("a file where the directory should be")
    assert "not a writable directory" in run(blocked).stderr


def _run_pre_start(sqlite_tmpdir):
    """Run the server unit's pre-start step as systemd would: its Exec line split,
    systemd's `$$`/`%%` escapes turned into the literal `$`/`%` the shell receives,
    and SQLITE_TMPDIR in the environment as the unit's Environment= line sets it."""
    import os
    import subprocess

    template = (SYSTEMD / "genesis-server.service.template").read_text()
    (line,) = [ln for ln in template.splitlines() if ln.startswith("ExecStartPre=-/bin/sh -c ")]
    argv = [
        arg.replace("$$", "$").replace("%%", "%")
        for arg in shlex.split(line.removeprefix("ExecStartPre=-"))
    ]
    env = {**os.environ, "SQLITE_TMPDIR": str(sqlite_tmpdir)}
    return subprocess.run(argv, capture_output=True, text=True, env=env)


def test_a_home_path_is_data_never_shell_code(tmp_path):
    """Round-2 review: the rendered home was spliced into the shell program, so a
    legal path holding `$( )` ran a command. The step now reads the resolved
    directory from the environment, so no path is ever parsed as code; the class
    audit's `${...}` and backslash paths are covered too."""
    marker = tmp_path / "marker"
    for name in (f"odd home $(touch {marker})", "a${PATH}b", "x\\c", "50%off"):
        home = tmp_path / name
        done = _run_pre_start(home / "tmp")
        assert not marker.exists(), "the home path was executed as shell code"
        assert (home / "tmp").is_dir() and done.stderr == "", (name, done.stderr)
