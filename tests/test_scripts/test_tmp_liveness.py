"""scripts/lib/tmp_liveness.sh — "is anything still writing here?"

These helpers moved out of the watchgod when it stopped deleting anything; the
~/tmp prune in disk_hygiene.sh is now their consumer. Each case below pinned a
real fail-OPEN in the watchgod's sweeps (a writer found, then reaped anyway),
so they travel with the code rather than being lost with the v1 sweep tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "tmp_liveness.sh"


def _has_writer(dirpath: str, snapshot: str) -> bool:
    # The snapshot goes on stdin: a single argv string is capped at 128 KB
    # (MAX_ARG_STRLEN), and the large-snapshot case is ~1 MB.
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail\nsource '{_LIB}'\nsnap=\"$(cat)\"\n"
            'if dir_has_live_writer "$1" "$snap"; then echo yes; else echo no; fi',
            "_",
            dirpath,
        ],
        input=snapshot,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip() == "yes"


def test_a_file_under_the_dir_is_a_writer():
    assert _has_writer("/t/job", "/x/other\n/t/job/part.bin\n")


def test_a_sibling_sharing_a_name_prefix_is_not():
    # pip-unpack-a must not be kept alive by a writer in pip-unpack-abc.
    assert not _has_writer("/t/pip-unpack-a", "/t/pip-unpack-abc/f\n")


def test_a_cwd_at_the_dir_itself_is_a_writer():
    assert _has_writer("/t/job", "/t/job\n")


def test_a_deleted_inode_is_not_a_writer():
    # Its directory entry is gone; sparing the dir for it reclaims nothing.
    assert not _has_writer("/t/job", "/t/job/old.log (deleted)\n")


def test_a_backslash_in_the_name_matches_literally():
    # awk -v would expand \t; the needle travels through the environment.
    assert _has_writer("/t/ta\\tb", "/t/ta\\tb/f\n")


def test_a_large_snapshot_with_the_writer_first_still_matches():
    # An early awk exit used to SIGPIPE the printf and, under pipefail, turn a
    # FOUND writer into "no writer" once the snapshot exceeded awk's buffer.
    snap = "/t/job/f\n" + "".join(f"/elsewhere/{i:06d}/file\n" for i in range(40_000))
    assert len(snap) > 800_000
    assert _has_writer("/t/job", snap)


def test_an_empty_snapshot_has_no_writer():
    assert not _has_writer("/t/job", "")


def test_live_open_paths_sees_this_process_cwd(tmp_path):
    proc = subprocess.run(
        ["bash", "-c", f"source '{_LIB}'\nlive_open_paths"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert str(tmp_path) in proc.stdout.splitlines()


def _held(path: str, snapshot: str) -> bool:
    proc = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nsource '{_LIB}'\nsnap=\"$(cat)\"\n"
         'if path_is_held "$1" "$snap"; then echo yes; else echo no; fi', "_", path],
        input=snapshot, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip() == "yes"


def test_path_is_held_is_exact():
    assert _held("/t/a.bin", "/x\n/t/a.bin\n")
    assert not _held("/t/a.bin", "/t/a.bin.part\n/t/a.bi\n")


def test_path_is_held_survives_a_large_snapshot_with_the_path_first():
    """MEASURED: `printf | grep -q` returned 141 under pipefail once the
    snapshot outgrew the pipe buffer, and a held file was deleted."""
    snap = "/t/a.bin\n" + "".join(f"/elsewhere/{i:06d}/file\n" for i in range(40_000))
    assert _held("/t/a.bin", snap)
