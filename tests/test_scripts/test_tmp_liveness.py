"""scripts/lib/tmp_liveness.sh — "is anything still writing here?"

These helpers moved out of the watchgod when it stopped deleting anything; the
~/tmp prune in disk_hygiene.sh is now their consumer. Each case below pinned a
real fail-OPEN in the watchgod's sweeps (a writer found, then reaped anyway),
so they travel with the code rather than being lost with the v1 sweep tests.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

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


_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "tmp_liveness.sh"


def _esc(p) -> str:
    """Field 5 of /proc/self/mountinfo: the kernel escapes these four as octal."""
    return (
        str(p)
        .replace("\\", "\\134")
        .replace(" ", "\\040")
        .replace("\t", "\\011")
        .replace("\n", "\\012")
    )


def _crosses(path: str, table: list[str]) -> bool:
    """path_crosses_mount against a table given in mount_targets' (escaped) form."""
    r = subprocess.run(
        [
            "bash",
            "-c",
            f'source \'{_LIB}\'\nif path_crosses_mount "$1" "$2"; then echo Y; else echo N; fi',
            "_",
            path,
            "\n".join(table),
        ],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() == "Y"


def test_path_crosses_mount_reads_the_table_exactly():
    """Review of #2521 item 6: the table must catch a mount AT the path and
    below it, never a sibling sharing a prefix."""
    assert _crosses("/h/tmp/job", ["/h/tmp/job"])
    assert _crosses("/h/tmp/job", ["/h/tmp/job/data"])
    assert not _crosses("/h/tmp/job", ["/h/tmp/job2"])
    assert not _crosses("/h/tmp/job", ["/h/tmp/job2/data"])
    assert _crosses("/h/tmp/a b", [_esc("/h/tmp/a b/m")])


def test_path_crosses_mount_decodes_every_mountinfo_escape():
    """Review of #2570: entries are DECODED, not matched against an encoded
    path, so a name with a newline, tab, space or backslash, holding a bind
    mount, is still recognised -- and a near-miss sibling is not."""
    # "job 1" / "a\t9" / "a\n1": an escape followed by a DIGIT, which a naive
    # %b decode misreads as a four-digit octal escape (found in round-3 review).
    for name in ("a\nb", "a\tb", "a b", "a\\b", "a\\134b", "job 1", "a\t9", "a\n1", "a\\1234"):
        assert _crosses(f"/h/tmp/{name}", [_esc(f"/h/tmp/{name}/m")]), repr(name)
    assert not _crosses("/h/tmp/a\nb", [_esc("/h/tmp/a\nbc/m")])


def _mountinfo(tmp_path, mounts, readable=True) -> dict:
    f = tmp_path / "mountinfo"
    if readable:
        f.write_text(
            "".join(f"{i} 1 0:{i} / {_esc(m)} rw - x x rw\n" for i, m in enumerate(mounts, 20))
        )
    return dict(os.environ, TL_MOUNTINFO=str(f))


def _targets(tmp_path, root: str, mounts, readable=True) -> tuple[list[str], int]:
    r = subprocess.run(
        ["bash", "-c", f'source \'{_LIB}\'\nmount_targets "$1"; echo "rc=$?"', "_", root],
        env=_mountinfo(tmp_path, mounts, readable),
        capture_output=True,
        text=True,
    )
    *out, rc = r.stdout.split("\n")[:-1]
    return out, int(rc.removeprefix("rc="))


def test_mount_targets_reads_mountinfo_and_keeps_only_mounts_below_the_root(tmp_path):
    """Review of #2570: filtered once per deleter root, entries kept escaped
    (a newline in a name cannot split a record), read straight from the
    kernel's table so no findmnt is needed."""
    out, rc = _targets(
        tmp_path, "/h/tmp", ["/", "/h", "/h/tmp", "/h/tmp/a\nb", "/h/tmpx/c", "/h/tmp/d"]
    )
    assert rc == 0
    assert out == ["/h/tmp/a\\012b", "/h/tmp/d"]
    out, rc = _targets(tmp_path, "/", ["/", "/a", "/b/c"])
    assert rc == 0 and out == ["/a", "/b/c"], "a root of / must still see every mount"


def test_mount_targets_reports_an_unreadable_table(tmp_path):
    """#2570 premise check: 'unreadable' must never read as 'no mounts'."""
    assert _targets(tmp_path, "/h/tmp", [], readable=False)[1] == 1
    assert _targets(tmp_path, "/h/tmp", ["/h/tmp/d"])[1] == 1, "no '/' entry: not a real table"


def _remove(tmp_path, target: Path, mounts: list[Path], table_ok: int = 1) -> int:
    r = subprocess.run(
        [
            "bash",
            "-c",
            f'source \'{_LIB}\'\nremove_tree_one_fs "$1" "$2" "$3"; echo $?',
            "_",
            str(target),
            "\n".join(_esc(m) for m in mounts),
            str(table_ok),
        ],
        capture_output=True,
        text=True,
    )
    return int(r.stdout.strip())


def test_remove_tree_one_fs_reports_what_happened(tmp_path):
    """Review of #2570: every shell recursive delete goes through this helper,
    and callers count a unit as reclaimed only on 0."""
    root = tmp_path / "root"
    gone = root / "gone"
    (gone / "d").mkdir(parents=True)
    assert _remove(tmp_path, gone, []) == 0 and not gone.exists()

    held = root / "held"
    (held / "vol").mkdir(parents=True)
    (held / "vol" / "keep").write_text("x")
    assert _remove(tmp_path, held, [held / "vol"]) == 2
    assert (held / "vol" / "keep").exists(), "a mount below is never recursed into"

    blind = root / "blind"
    (blind / "keep").mkdir(parents=True)
    assert _remove(tmp_path, blind, [], table_ok=0) == 2, "no table: fail closed"
    assert blind.exists()

    stuck = root / "stuck"
    (stuck / "ro").mkdir(parents=True)
    (stuck / "ro" / "f").write_text("x")
    (stuck / "ro").chmod(0o555)
    try:
        if os.geteuid() != 0:
            assert _remove(tmp_path, stuck, []) == 1 and stuck.exists()
    finally:
        (stuck / "ro").chmod(0o755)


# Every relation a mount under ROOT can have to a candidate under ROOT, in one
# table (#2570 premise check: the review rounds found these one at a time
# because no single test enumerated them). ROOT = /r, candidate = /r/c/s.
_RELATIONS = [
    ("equal", "/r/c/s", True),
    ("below", "/r/c/s/vol", True),
    ("deep below", "/r/c/s/a/b/vol", True),
    ("above (between root and candidate)", "/r/c", True),
    ("sibling", "/r/c/t", False),
    ("sibling sharing a prefix", "/r/c/s2", False),
    ("prefix of the candidate's name", "/r/c/s2/x", False),
    ("disjoint", "/r/other", False),
]


@pytest.mark.parametrize(("relation", "mount", "crosses"), _RELATIONS, ids=[r[0] for r in _RELATIONS])
def test_path_crosses_mount_covers_every_relation(relation, mount, crosses):
    assert _crosses("/r/c/s", [_esc(mount)]) is crosses, relation


@pytest.mark.parametrize(("relation", "mount", "crosses"), _RELATIONS, ids=[r[0] for r in _RELATIONS])
def test_every_relation_survives_escaping(relation, mount, crosses):
    """The same table with a space, a tab, a newline and a digit after each
    escape in every name: decoding must not change any answer."""
    def odd(p: str) -> str:
        return p.replace("/c", "/c 1").replace("/s", "/s\t2").replace("/vol", "/v\n3")
    assert _crosses(odd("/r/c/s"), [_esc(odd(mount))]) is crosses, relation
