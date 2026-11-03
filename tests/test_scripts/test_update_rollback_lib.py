"""scripts/lib/update_rollback.sh holds definitions, so sourcing it again is safe.

A later change re-sources a merged copy at rollback (#3844). A top-level assignment
there would reset run state a rollback depends on, such as the DB restore it owes;
a top-level `declare` would turn local when sourced from a function.
"""

import re
import subprocess

from tests.test_scripts._update_source import ROLLBACK_LIB, UPDATE_SH, rollback_body_text

_FUNCTION = re.compile(r"^([A-Za-z_]\w*)\(\)\s*\{")
# Safe to run again: a constant, and a flag only the restore changes.
_ALLOWED_TOP_LEVEL = {
    'SERVER_RESTART_OWED="$HOME/.genesis/server_restart_owed.json"',
    "_DB_RESTORE_LANDED=0",
}


def _parses(text: str) -> bool:
    return (
        subprocess.run(["bash", "-n"], input=text, text=True, capture_output=True).returncode == 0
    )


def _top_level_code(lines: list[str]) -> list[str]:
    """Non-comment lines outside any function, using bash's parser to find each
    function's end: an embedded python program can hold a column-0 `}`."""
    out, i = [], 0
    while i < len(lines):
        if _FUNCTION.match(lines[i]):
            j = i
            # A one-line function ends on its own line; the next column-0 `}` is another's.
            while not _parses(lines[i]) and not (
                lines[j].startswith("}") and _parses("\n".join(lines[i : j + 1]))
            ):
                j += 1
            i = j + 1
            continue
        if lines[i].strip() and not lines[i].lstrip().startswith("#"):
            out.append(lines[i])
        i += 1
    return out


def test_only_the_two_safe_lines_run_when_the_lib_is_sourced():
    assert set(_top_level_code(ROLLBACK_LIB.read_text().splitlines())) == _ALLOWED_TOP_LEVEL


def test_the_check_above_catches_top_level_state():
    """Control: the scan is not blind to an assignment after a python-holding function."""
    lines = rollback_body_text().splitlines() + ["DB_SNAPSHOT_TAKEN=0", "declare -a x"]
    assert _top_level_code(lines) == ["DB_SNAPSHOT_TAKEN=0", "declare -a x"]


def test_the_scan_sees_past_a_one_line_function():
    """Control: a one-liner must not hide what follows it up to the next `}`."""
    lines = ["foo() { :; }", "DB_SNAPSHOT_TAKEN=0", "bar() {", "    :", "}"]
    assert _top_level_code(lines) == ["DB_SNAPSHOT_TAKEN=0"]


def _moved_functions() -> set[str]:
    return {
        m.group(1) for ln in ROLLBACK_LIB.read_text().splitlines() if (m := _FUNCTION.match(ln))
    }


def test_nothing_uses_the_lib_before_it_is_sourced():
    """Every moved definition used to sit below this source line, so nothing above it
    could have called one. This keeps a later edit from adding such a call."""
    lines = UPDATE_SH.read_text().splitlines()
    at = lines.index('. "$SCRIPT_DIR/lib/update_rollback.sh"')
    names = "|".join(sorted(_moved_functions()))
    used = re.compile(rf"(?<![\w-])({names}|SERVER_RESTART_OWED|_DB_RESTORE_LANDED)(?![\w-])")
    early = [ln for ln in _top_level_code(lines[:at]) if used.search(ln)]
    assert not early, early
    # And no trap armed that early may reach the rollback.
    assert "\n".join(lines[:at]).count("trap _on_err ERR") == 0


def test_update_sh_keeps_no_copy_of_what_moved():
    lib = _moved_functions()
    assert "_do_rollback" in lib
    kept = {m.group(1) for ln in UPDATE_SH.read_text().splitlines() if (m := _FUNCTION.match(ln))}
    assert not lib & kept


def test_the_handlers_stay_with_the_traps_they_serve():
    text = UPDATE_SH.read_text()
    assert "_on_err() {" in text
    assert "_on_signal() {" in text
