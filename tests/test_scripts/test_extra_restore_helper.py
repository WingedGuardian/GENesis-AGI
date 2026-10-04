"""Unit tests for scripts/lib/extra_restore.py (restore.sh §4c's tar helper)."""

import importlib.util
import io
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
