"""Unit tests for scripts/lib/extra_restore.py (restore.sh §4c's tar helper)."""

import importlib.util
import io
import os
import tarfile
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "extra_restore.py"
_spec = importlib.util.spec_from_file_location("extra_restore", _HELPER)
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)


def _tar(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, kind, extra in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = extra
                tf.addfile(info)
            else:
                info.size = len(extra)
                tf.addfile(info, io.BytesIO(extra))
    buf.seek(0)
    return tarfile.open(fileobj=buf)


@pytest.mark.parametrize(
    "entries,root",
    [
        ([(".genesis/a", "dir", None), (".genesis/a/f", "file", b"x")], ".genesis/a"),
        ([("w/s", "dir", None), ("w/s/x", "file", b"x"), ("w/s/y", "file", b"y")], "w/s"),
        ([("/abs", "dir", None), ("/abs/f", "file", b"x")], "abs"),  # leading '/' stripped
        ([("w", "dir", None), ("../esc", "file", b"x"), ("w/ok", "file", b"x")], "w"),
        # no directory member at the common path: refused, never trimmed to a parent
        ([("work/f.txt", "file", b"x")], None),
        ([("p/link", "sym", "target")], None),
        ([("w/s/x", "file", b"x"), ("w/s/y", "file", b"y")], None),
        ([("a", "dir", None), ("a/x", "file", b"x"), ("b/y", "file", b"y")], None),
        ([("../only", "file", b"x")], None),
    ],
)
def test_archive_root(entries, root):
    assert er.archive_root(_tar(entries)) == root


@pytest.mark.parametrize(
    "name,link,ok",
    [
        ("w/s/rel", "f", True),
        ("w/s/sub/rel", "../f", True),
        ("w/s/up", "../sibling", False),  # leaves the restored tree
        ("w/s/up", "../../.ssh", False),
    ],
)
def test_filter_confines_symlinks_to_the_tree(tmp_path, name, link, ok):
    keep = er.make_filter("w/s")
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = link
    out = keep(info, str(tmp_path))
    assert (out is not None) is ok, keep.refused


def test_filter_keeps_directory_modes_minus_group_other_write(tmp_path):
    keep = er.make_filter("w")
    info = tarfile.TarInfo("w/d")
    info.type = tarfile.DIRTYPE
    info.mode = 0o4775
    assert keep(info, str(tmp_path)).mode == 0o755


def test_cli_root_keeps_stdout_for_the_value_only(tmp_path, capsys):
    t = tmp_path / "a.tar"
    with tarfile.open(t, "w") as tf:
        d = tarfile.TarInfo("w")
        d.type = tarfile.DIRTYPE
        tf.addfile(d)
        info = tarfile.TarInfo("w/f")
        tf.addfile(info, io.BytesIO(b""))
    assert er.main(["x", "root", str(t)]) == 0
    out = capsys.readouterr()
    assert out.out == "w\n"


def test_swap_reports_an_unproven_rename_as_status_6(tmp_path, monkeypatch):
    """Codex round 2: a failed fsync of the destination's parent is reported (6),
    not silently accepted as a durable restore."""
    t = tmp_path / "a.tar"
    with tarfile.open(t, "w") as tf:
        d = tarfile.TarInfo("w")
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tf.addfile(d)
        tf.addfile(tarfile.TarInfo("w/f"), io.BytesIO(b""))
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(er, "_fsync_tree", lambda top: None)

    def boom(fd):
        raise OSError(5, "EIO")

    monkeypatch.setattr(er.os, "fsync", boom)
    rc = er.cmd_swap(str(t), str(stage), "w", str(tmp_path / "w"), "")
    assert rc == 6
    assert (tmp_path / "w" / "f").exists()


def test_no_member_is_written_through_an_earlier_symlink(tmp_path):
    """Verification review: `app/b -> .` then `app/b/x -> ..` used to plant a link
    out of the tree, and the post-rename chmod of `app/x/victim` then changed a
    directory OUTSIDE the restored tree. A member whose path runs through a link
    member is refused, and modes are only ever applied inside the tree."""
    base = tmp_path / "h"
    base.mkdir()
    victim = base / "victim"
    victim.mkdir(mode=0o700)
    t = tmp_path / "chain.tar"
    with tarfile.open(t, "w") as tf:
        for name, kind, extra in [
            ("app", "dir", 0o755),
            ("app/b", "sym", "."),
            ("app/b/x", "sym", ".."),
            ("app/x/victim", "dir", 0o555),
        ]:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = extra
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = extra
            tf.addfile(info)
    stage = base / ".stage"
    stage.mkdir()
    try:
        rc = er.cmd_swap(str(t), str(stage), "app", str(base / "app"), "")
        assert rc == 4, rc  # restored, with the chained member refused
        assert not (base / "app" / "x").is_symlink()
        assert oct(victim.stat().st_mode & 0o777) == oct(0o700), "a dir outside the tree changed"
    finally:
        for p in base.rglob("*"):
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o755)


def test_root_reads_names_on_any_python(tmp_path, monkeypatch):
    """`root` extracts nothing, so the restore-safety gate does not apply to it
    (backup uses it to check every archive)."""
    t = tmp_path / "a.tar"
    with tarfile.open(t, "w") as tf:
        d = tarfile.TarInfo("w")
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tf.addfile(d)
    monkeypatch.setattr(er, "_python_is_safe", lambda: False)
    assert er.main(["x", "root", str(t)]) == 0
    assert er.main(["x", "swap", str(t), str(tmp_path), "w", str(tmp_path / "w"), ""]) == 3


def test_an_empty_destination_survives_a_failed_rename(tmp_path, monkeypatch):
    """Codex round 3: an existing empty destination is removed only when the restored
    tree is ready, and recreated if the rename then fails."""
    t = tmp_path / "a.tar"
    with tarfile.open(t, "w") as tf:
        d = tarfile.TarInfo("w")
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tf.addfile(d)
        tf.addfile(tarfile.TarInfo("w/f"), io.BytesIO(b""))
    target = tmp_path / "w"
    target.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    real_rename = er.os.rename

    def failing_rename(src, dst):
        if str(dst) == str(target):
            raise OSError(28, "ENOSPC")
        return real_rename(src, dst)

    monkeypatch.setattr(er.os, "rename", failing_rename)
    assert er.cmd_swap(str(t), str(stage), "w", str(target), "") == 5
    assert target.is_dir() and not any(target.iterdir())


def test_an_empty_destination_is_replaced(tmp_path):
    t = tmp_path / "a.tar"
    with tarfile.open(t, "w") as tf:
        d = tarfile.TarInfo("w")
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tf.addfile(d)
        f = tarfile.TarInfo("w/f")
        f.size = 1
        tf.addfile(f, io.BytesIO(b"x"))
    (tmp_path / "w").mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    assert er.cmd_swap(str(t), str(stage), "w", str(tmp_path / "w"), "") == 0
    assert (tmp_path / "w" / "f").read_bytes() == b"x"


def test_a_long_multibyte_name_gets_a_valid_short_aside(tmp_path, capsysbinary):
    """Class audit: the aside name keeps at most 100 bytes of the directory's name, cut
    on a character boundary, and is written to stdout as raw bytes."""
    name = "目" * 40  # 120 bytes in UTF-8
    t = tmp_path / "a.tar"
    with tarfile.open(t, "w") as tf:
        d = tarfile.TarInfo(name)
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tf.addfile(d)
    target = tmp_path / name
    target.mkdir()
    (target / "old").write_text("old")
    stage = tmp_path / "stage"
    stage.mkdir()
    assert er.cmd_swap(str(t), str(stage), name, str(target), "auto") == 0
    out = capsysbinary.readouterr().out
    assert out.startswith(b"aside ")
    aside = out[len(b"aside ") :].rstrip(b"\n")
    os.fsdecode(aside).encode("utf-8")  # valid UTF-8: no character was split
    assert (Path(os.fsdecode(aside)) / "old").read_text() == "old"
    assert len(os.path.basename(aside)) < 255


def test_root_prints_a_non_utf8_name_under_a_strict_locale(tmp_path):
    """Class audit: `root` writes raw bytes, so a non-UTF-8 directory name works under
    en_US.UTF-8 (a strict stdout) as well as under C.UTF-8."""
    import subprocess

    t = tmp_path / "a.tar"
    with tarfile.open(t, "w", encoding="utf-8", errors="surrogateescape") as tf:
        d = tarfile.TarInfo(os.fsdecode(b"w\xff"))
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tf.addfile(d)
    for locale in ("en_US.UTF-8", "C.UTF-8"):
        proc = subprocess.run(
            ["python3", str(_HELPER), "root", str(t)],
            capture_output=True,
            env={**os.environ, "LC_ALL": locale},
        )
        assert proc.returncode == 0, (locale, proc.stderr)
        assert proc.stdout == b"w\xff\n", (locale, proc.stdout)
