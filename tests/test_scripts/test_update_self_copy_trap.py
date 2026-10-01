"""update.sh's EXIT trap must delete ITS OWN temp copy, and nothing else.

update.sh re-execs itself from a temp copy and arms an EXIT trap that removes
that copy. Two ways it removed the wrong file, both pinned here:

1. The trap named the copy as ``${BASH_SOURCE[0]}``, which bash expands when
   the trap FIRES. If the exit happens inside a function defined in a sourced
   file (an explicit ``exit``, or an unbound variable under ``set -u``),
   ``BASH_SOURCE[0]`` is that file: the trap deleted the LIBRARY and leaked the
   copy (MEASURED, bash 5.2). The path is now bound once at startup.
2. "We are the copy" was decided by an inherited flag alone. A server started
   by update.sh's nohup fallback inherits it, and a dashboard update launched
   from that server passes it on, so that update skipped the copy, ran in
   place, and its trap deleted the repository's own ``scripts/update.sh``.

The behavioural tests run update.sh's REAL lines (read out of the script, not
retyped) under the same ``set -Eeuo pipefail`` the script sets.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
SHARED_LIBS = [
    REPO_ROOT / "scripts" / "lib" / "guardian_pause.sh",
    REPO_ROOT / "scripts" / "lib" / "deploy_marker.sh",
    REPO_ROOT / "scripts" / "lib" / "deploy_checkout.sh",
]
_PRELUDE = "#!/usr/bin/env bash\nset -Eeuo pipefail\n"


def _code_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _self_copy_lines(text: str) -> list[str]:
    """update.sh's lines from the end of the copy-to-temp guard through the
    first EXIT trap after it: what binds and arms the self-copy removal."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == 'exec "$TEMP_COPY" "$@"')
    fi = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "fi")
    trap = next(i for i in range(fi + 1, len(lines)) if _is_exit_trap(lines[i]))
    return _code_lines(lines[fi + 1 : trap + 1])


def _copy_guard_lines(text: str) -> list[str]:
    """update.sh's whole copy-to-temp guard, through the handshake `unset`."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("# ── Copy-to-temp guard"))
    end = next(
        i for i in range(start, len(lines)) if lines[i].startswith("unset GENESIS_UPDATE_FROM_TEMP")
    )
    return _code_lines(lines[start : end + 1])


def _trap_signals(line: str) -> list[str] | None:
    """The signal names a `trap ACTION SIG...` statement arms, or None if the
    line is not one (including `trap - SIG`, which disarms)."""
    stripped = line.strip()
    if not stripped.startswith("trap "):
        return None
    try:
        words = shlex.split(stripped, comments=True)
    except ValueError:
        return None
    if len(words) < 3 or words[1] == "-":
        return None
    return words[2:]


def _is_exit_trap(line: str) -> bool:
    signals = _trap_signals(line)
    return bool(signals) and any(s in {"EXIT", "SIGEXIT", "0"} for s in signals)


def _run_lib_exit(tmp_path: Path, lib_body: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    arm = _self_copy_lines(UPDATE_SH.read_text())
    assert any("EXIT" in ln for ln in arm), "could not find update.sh's self-copy EXIT trap"
    lib = tmp_path / "lib.sh"
    lib.write_text(lib_body)
    harness = tmp_path / "genesis-update-XXXXXX.sh"  # plays the temp copy
    harness.write_text(_PRELUDE + "\n".join([*arm, f'. "{lib}"', "lib_exit", ""]))
    result = subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=30)
    return result, lib, harness


def test_a_lib_that_exits_survives_the_self_copy_trap(tmp_path: Path) -> None:
    result, lib, harness = _run_lib_exit(tmp_path, "lib_exit() { exit 0; }\n")
    assert result.returncode == 0, result.stderr
    assert lib.exists(), "the EXIT trap deleted a SOURCED LIB instead of update.sh's temp copy"
    assert not harness.exists(), "the EXIT trap left update.sh's temp copy behind"


def test_an_unbound_variable_inside_a_lib_does_not_delete_it(tmp_path: Path) -> None:
    """The realistic trigger: `set -u` aborts inside a lib function. That exit
    bypasses the ERR trap and `|| true`, so it reaches the EXIT trap directly."""
    result, lib, harness = _run_lib_exit(tmp_path, 'lib_exit() { echo "$NO_SUCH_VARIABLE_XYZ"; }\n')
    assert result.returncode != 0, "the unbound variable should have aborted the harness"
    assert lib.exists(), "a set -u abort inside a lib deleted the LIB"
    assert not harness.exists(), "a set -u abort inside a lib leaked the temp copy"


def test_an_inherited_flag_does_not_make_update_sh_run_in_place(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = tmp_path / "repo" / "scripts" / "update.sh"
    script.parent.mkdir(parents=True)
    guard = _copy_guard_lines(UPDATE_SH.read_text())
    script.write_text(
        _PRELUDE
        + "\n".join(
            [*guard, 'echo "RAN=${BASH_SOURCE[0]} FLAG=${GENESIS_UPDATE_FROM_TEMP:-unset}"', ""]
        )
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GENESIS_UPDATE_")}
    env.update(HOME=str(home), GENESIS_UPDATE_FROM_TEMP="1")  # inherited, no copy behind it

    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, timeout=30
    )

    assert result.returncode == 0, result.stderr
    ran = re.search(r"RAN=(\S+) FLAG=(\S+)", result.stdout)
    assert ran, result.stdout
    assert ran.group(1) != str(script), "update.sh ran IN PLACE on an inherited flag"
    assert Path(ran.group(1)).parent == home / "tmp", ran.group(1)
    assert ran.group(2) == "unset", "the copy handshake leaked into what the copy runs"
    assert script.exists(), "the EXIT trap deleted the repository's own update.sh"
    assert not list((home / "tmp").glob("genesis-update-*.sh")), "the temp copy was left behind"


def test_no_exit_trap_names_the_copy_through_bash_source() -> None:
    """Every EXIT trap in update.sh names the copy through the startup binding.

    Covers the composed guardian trap as well, and any spelling of EXIT a trap
    can use (``EXIT``, ``SIGEXIT``, ``0``, or EXIT among several signals).
    """
    text = UPDATE_SH.read_text()
    exit_traps = [ln for ln in text.splitlines() if _is_exit_trap(ln)]
    assert len(exit_traps) >= 3, (
        exit_traps
    )  # the mktemp cleanup, the self-copy, the guardian compose
    for ln in exit_traps:
        assert "BASH_SOURCE" not in ln, f"late-bound BASH_SOURCE in an EXIT trap: {ln.strip()}"
    self_copy = [ln for ln in exit_traps if "_SELF_COPY" in ln]
    assert len(self_copy) >= 2, self_copy
    assert 'readonly _SELF_COPY="${BASH_SOURCE[0]}"' in text


def test_the_shared_libs_arm_no_traps() -> None:
    """A trap is one global slot per signal. A lib that arms one silently
    replaces whatever its caller composed there (the self-copy removal)."""
    for lib in SHARED_LIBS:
        armed = [ln for ln in lib.read_text().splitlines() if _trap_signals(ln)]
        assert not armed, f"{lib.name} arms a trap: {armed}"


def test_the_trap_parser_sees_every_spelling() -> None:
    """Guard the guard: the static checks are only as good as this parser."""
    for spelling in (
        "trap 'x' EXIT",
        "trap 'x' 0",
        "trap 'x' SIGEXIT",
        "trap 'x' EXIT INT TERM",
        '    trap "x" INT EXIT  # comment',
    ):
        assert _is_exit_trap(spelling), spelling
    for not_exit in ("trap - EXIT", "trap 'x' INT", "# trap 'x' EXIT", "echo trap 'x' EXIT"):
        assert not _is_exit_trap(not_exit), not_exit
