"""`timeout` is GNU's wherever a script relies on `-k` reaching the whole group.

uutils' `timeout -k` kills the direct child only, and it is the system's `timeout`
on Ubuntu 26.04. `scripts/lib/gnu_timeout.sh` swaps in GNU's as a shell function, so
what has to hold is: every script that passes a kill-after loads it before running
one, it changes nothing where it does not apply, and a function behaves like the
binary in the one call shape where that is not obvious.

`test_backup_backends.py::test_smb_run_kills_client_that_ignores_term` is the end to
end proof, against whichever `timeout` the runner has.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from genesis.guardian import guard_layer_watch as glw

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_HELPER = _SCRIPTS / "lib" / "gnu_timeout.sh"
# Options may come first, and the upstream defect covers a KILL sent as the main signal too.
_KILL_AFTER = re.compile(
    r"(?<![\w-])timeout\s+(?:-\S*\s+)*?(-k\b|--kill-after\b|-s\s*(?:SIG)?KILL\b|--signal[= ](?:SIG)?KILL\b)"
)
#: Scripts with no sourcing of their own, and the script that gives them the helper.
_LOADED_THROUGH = {
    "backup.sh": "lib/backup_backends.sh",  # restore.sh passes no kill-after itself
    "lib/update_rollback.sh": "update.sh",  # read by update.sh, the only one to call it
}
_UUTILS = "timeout (uutils coreutils) 0.8.0"
_GNU = "timeout (GNU coreutils) 9.4"


def _code_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]


def _shell_scripts() -> list[Path]:
    return sorted(
        p
        for p in _SCRIPTS.rglob("*")
        if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts and p != _HELPER
    )


def _users() -> list[str]:
    found = []
    for path in _shell_scripts():
        try:
            lines = _code_lines(path)
        except UnicodeDecodeError:
            continue
        if any(_KILL_AFTER.search(ln) for ln in lines):
            found.append(str(path.relative_to(_SCRIPTS)))
    return found


def test_the_sweep_finds_the_known_users():
    """Guard the guard: an empty or shrunken sweep would pass every case below."""
    assert {
        "backup.sh",
        "cc-slot.sh",
        "deploy_code_only.sh",
        "host-setup.sh",
        "update.sh",
        "lib/backup_backends.sh",
        "lib/update_rollback.sh",
        "lib/watchgod_act.sh",
        "lib/watchgod_runaway.sh",
    } <= set(_users())


#: Files whose kill-after picks GNU's `timeout` inline instead of loading the
#: helper: a unit's one /bin/sh line, run at boot, where sourcing the bash helper
#: from a checkout the line exists to distrust is not an option (#4501).
_SELECTS_INLINE = (
    "systemd/genesis-server.service.template",
    "systemd/genesis-bridge.service.template",
)


@pytest.mark.parametrize("rel", _SELECTS_INLINE)
def test_a_unit_line_selects_gnu_timeout_inline(rel):
    """The sweep above cannot see these (the call is on a variable), so this holds
    them: each selects `gnutimeout` when present, runs its kill-after on that
    choice, and has no bare `timeout -k` left."""
    text = (_SCRIPTS / rel).read_text()
    assert "command -v gnutimeout >/dev/null 2>&1 && t=gnutimeout" in text, rel
    assert '"$$t" -k ' in text, rel
    assert not any(_KILL_AFTER.search(ln) for ln in _code_lines(_SCRIPTS / rel)), rel


@pytest.mark.parametrize("rel", _users())
def test_every_script_passing_a_kill_after_loads_the_helper_first(rel):
    owner = _LOADED_THROUGH.get(rel, rel)
    lines = _code_lines(_SCRIPTS / owner)
    loads = [
        i for i, ln in enumerate(lines) if re.search(r"(^|\s)(\.|source)\s.*gnu_timeout\.sh", ln)
    ]
    assert loads, f"{rel} passes a kill-after, and {owner} never loads gnu_timeout.sh"
    uses = [i for i, ln in enumerate(lines) if _KILL_AFTER.search(ln)]
    assert not uses or loads[0] < uses[0], f"{owner} runs a kill-after before loading the helper"


def _sandbox(tmp_path: Path, version: str | None, gnutimeout: bool = True) -> Path:
    """A PATH holding only what the case names, so the real tools cannot answer."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if version is not None:
        (bindir / "timeout").write_text(
            f'#!/bin/sh\n[ "$1" = --version ] && echo "{version}"\necho "system $*"\n'
        )
        (bindir / "timeout").chmod(0o755)
    if gnutimeout:
        (bindir / "gnutimeout").write_text('#!/bin/sh\necho "gnu $*"\n')
        (bindir / "gnutimeout").chmod(0o755)
    return bindir


def _bash(script: str, bindir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\n" + script],
        env={"PATH": str(bindir)},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("version", "gnutimeout", "predefine", "expected"),
    [
        pytest.param(_UUTILS, True, "", "gnu -k 1 5 x", id="uutils-with-gnu-beside-it"),
        pytest.param(_GNU, True, "", "system -k 1 5 x", id="already-gnu"),
        pytest.param(_UUTILS, False, "", "system -k 1 5 x", id="uutils-and-no-gnu"),
        pytest.param(_UUTILS, True, 'timeout() { echo "own $*"; }\n', "own -k 1 5 x", id="a-test-defined-its-own"),
        # Passing through to the system's, it answers --version as uutils, so only the
        # check for a function already defined keeps the helper from replacing it.
        pytest.param(
            _UUTILS, True, 'timeout() { command timeout "$@"; }\n', "system -k 1 5 x", id="a-pass-through-function"
        ),
    ],
)
def test_the_helper_applies_only_where_it_should(
    tmp_path, version, gnutimeout, predefine, expected
):
    proc = _bash(
        f'{predefine}. "{_HELPER}"\ntimeout -k 1 5 x\n', _sandbox(tmp_path, version, gnutimeout)
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_a_system_with_no_timeout_at_all_loads_it_cleanly(tmp_path):
    proc = _bash(f'. "{_HELPER}"\ndeclare -F timeout || echo none\n', _sandbox(tmp_path, None))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "none"


def test_the_exit_status_comes_through(tmp_path):
    bindir = _sandbox(tmp_path, _UUTILS)
    (bindir / "gnutimeout").write_text("#!/bin/sh\nexit 124\n")
    proc = _bash(f'. "{_HELPER}"\nrc=0; timeout 5 x || rc=$?\necho "$rc"\n', bindir)
    assert proc.stdout.strip() == "124", proc.stderr


def test_a_kept_lock_fd_stays_open_in_the_shell_and_closed_in_the_command(tmp_path):
    """deploy_code_only.sh holds its update lock on an fd and bounds a fetch with
    `timeout ... {fd}>&-`, closing it for the fetch alone. A redirect on a FUNCTION
    applies in the shell itself, so if the close outlived the call, the shell would
    drop its lock with nothing to say so. Foreground on purpose: inside `$(...)` the
    question answers itself."""
    bindir = _sandbox(tmp_path, _UUTILS)
    seen = tmp_path / "seen"
    (bindir / "gnutimeout").write_text(
        f'#!/bin/bash\n[ -e "/proc/$$/fd/$FD" ] && echo open > "{seen}" || echo closed > "{seen}"\n'
    )
    lock = tmp_path / "lock"
    proc = _bash(
        f'. "{_HELPER}"\n'
        f'exec {{LOCK}}>"{lock}"\n'
        "export FD=$LOCK\n"
        "timeout 5 x {LOCK}>&-\n"
        '[ -e "/proc/$$/fd/$FD" ] && echo kept || echo dropped\n',
        bindir,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "kept"
    assert seen.read_text().strip() == "closed"


@pytest.mark.parametrize(
    ("version", "expected"),
    [(_UUTILS, "gnutimeout -k 1 3"), (_GNU, "timeout -k 1 3")],
    ids=["uutils", "gnu"],
)
def test_the_guard_layer_probe_makes_the_same_choice_inline(tmp_path, version, expected):
    """The probe runs as `bash -s` in the container and loads no lib, so it chooses inline."""
    script = glw._PROBE_SCRIPT.decode()
    start = script.index('T="timeout -k')
    end = script.index("\nfi\n", start) + len("\nfi\n")
    proc = _bash(script[start:end] + 'echo "$T"\n', _sandbox(tmp_path, version))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected
