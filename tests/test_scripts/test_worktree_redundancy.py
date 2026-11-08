"""The content-verified expiry predicate (#2504), on real repos and real archives.

Each arm of #2504 is an archive that must be KEPT, built through the real reaper
or by rewriting a real archive, and judged through the real `_merge_verdict`;
only `gh` is stubbed, with a script on PATH. The expiring cases are the
other half: without them every keep would pass vacuously.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from tests.test_scripts.test_worktree_trash_retention import (  # noqa: F401 — fixtures
    _archive,
    _backdate,
    _git,
    _isolate,
    _registered,
    repo,
    wl,
)

_IGNORE = "__pycache__/\n*.pyc\nconfig/.generated/\n*.egg-info/\ndata/\n*.log\n.abide\n"


@pytest.fixture
def ignoring(repo: Path) -> Path:  # noqa: F811 — the imported fixture
    """The repo with a committed .gitignore, so regenerable files leave a
    worktree clean, as they do in a real checkout."""
    (repo / ".gitignore").write_text(_IGNORE)
    (repo / "link").symlink_to("seed.txt")
    _git(repo, "add", ".gitignore", "link")
    _git(repo, "commit", "-qm", "ignore")
    return repo


def _gh(tmp_path: Path, monkeypatch, stdout: str, code: int = 0) -> None:
    """A `gh` on PATH that prints ``stdout`` and exits ``code``."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "payload.json").write_text(stdout)
    gh = bindir / "gh"
    gh.write_text(f'#!/bin/sh\ncat "{bindir / "payload.json"}"\nexit {code}\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _meta(a: dict) -> dict:
    return json.loads((wl.TRASH_DIR / f"{a['entry']}.meta.json").read_text())


def _judge(a: dict, root: Path) -> tuple[bool, str]:
    """The real predicate with the real verdict, as `_expire_trash` calls it."""
    return wl._archive_redundant(wl._load_redundancy_module(), a["archive"], _meta(a), root)


def _members(archive: Path) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    with tarfile.open(archive, "r:gz") as tf:
        return [(m, tf.extractfile(m).read() if m.isreg() else None) for m in tf]


def _write(archive: Path, items) -> None:
    with tarfile.open(archive, "w:gz") as tf:
        for m, data in items:
            tf.addfile(m, io.BytesIO(data) if data is not None else None)


def _reg(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _special(name: str, kind: bytes, linkname: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


def _add(a: dict, *extra) -> None:
    _write(a["archive"], [*_members(a["archive"]), *extra])


# ─── the expiring half ───────────────────────────────────────────────────────


def test_a_clean_ancestor_archive_expires(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "clean")
    assert _judge(a, ignoring) == (True, f"content matches head {a['head'][:12]} (ancestor)")
    _backdate(a["entry"], 40)
    assert wl._expire_trash(ignoring, days=30)["expired"] == 1
    assert not a["archive"].exists()


def test_regenerable_files_alone_do_not_keep_an_archive(ignoring, tmp_path):
    """Owner allowlist: caches, the .abide link, config/.generated, *.egg-info."""
    wt = tmp_path / "regen"
    _git(ignoring, "worktree", "add", "-q", "--detach", str(wt))
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\x00pyc")
    (wt / ".abide").symlink_to("/nonexistent/.abide")
    (wt / "config" / ".generated").mkdir(parents=True)
    (wt / "config" / ".generated" / "x.json").write_text("{}\n")
    (wt / "pkg.egg-info").mkdir()
    (wt / "pkg.egg-info" / "PKG-INFO").write_text("x\n")
    rec = next(x for x in wl._list_worktrees(ignoring) if x["path"] == str(wt))
    assert wl._trash_worktree(rec, ignoring, lane="merged") is True
    entry = next(wl.TRASH_DIR.glob("regen-*.tar.gz"))
    a = {"archive": entry, "entry": entry.name[: -len(".tar.gz")]}
    names = {m.name.split("/", 1)[-1] for m, _ in _members(entry)}
    assert {".abide", "config/.generated/x.json", "pkg.egg-info/PKG-INFO"} <= names
    assert _judge(a, ignoring)[0] is True


def test_a_squash_merged_pr_expires_after_its_branch_is_deleted(ignoring, tmp_path, monkeypatch):
    """`pr`, judged by the RECORDED head: the branch ref is gone, so only
    `tip` lets GitHub's merged head be compared."""
    wt = tmp_path / "feat"
    _git(ignoring, "worktree", "add", "-q", "-b", "feat", str(wt))
    (wt / "new.txt").write_text("feature\n")
    _git(wt, "add", "new.txt")
    _git(wt, "commit", "-qm", "feature")
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    _git(ignoring, "merge", "--squash", "-q", "feat")
    _git(ignoring, "commit", "-qm", "squash feat")
    rec = next(x for x in wl._list_worktrees(ignoring) if x["path"] == str(wt))
    assert wl._trash_worktree(rec, ignoring, lane="merged") is True
    # `branch -D` refuses while the anchor registration names the branch.
    _git(ignoring, "update-ref", "-d", "refs/heads/feat")
    assert _git(ignoring, "rev-parse", "--verify", "-q", "feat", check=False).returncode != 0
    entry = next(wl.TRASH_DIR.glob("feat-*.tar.gz"))
    a = {"archive": entry, "entry": entry.name[: -len(".tar.gz")]}
    _gh(tmp_path, monkeypatch, json.dumps([{"number": 1, "headRefOid": head}]))
    assert _judge(a, ignoring) == (True, f"content matches head {head[:12]} (pr)")
    # The same archive when GitHub merged a DIFFERENT head: `cherry` would call
    # it patch-equivalent, and patch-id is refused.
    _gh(tmp_path, monkeypatch, json.dumps([{"number": 1, "headRefOid": "0" * 40}]))
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "verdict: patch-id" in why
    _gh(tmp_path, monkeypatch, "", code=1)  # gh down: also patch-id, also kept
    assert _judge(a, ignoring)[0] is False


# ─── #2504's arms: each one keeps ────────────────────────────────────────────


def test_an_ignored_file_keeps_the_archive(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "ignored")
    _add(a, _reg(f"{a['entry']}/build.log", b"only here\n"))
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "build.log" in why


def test_data_genesis_db_keeps_the_archive(ignoring, tmp_path):
    wt = tmp_path / "withdb"
    _git(ignoring, "worktree", "add", "-q", "--detach", str(wt))
    (wt / "data").mkdir()
    (wt / "data" / "genesis.db").write_bytes(b"SQLite format 3\x00")
    rec = next(x for x in wl._list_worktrees(ignoring) if x["path"] == str(wt))
    assert wl._trash_worktree(rec, ignoring, lane="merged") is True
    entry = next(wl.TRASH_DIR.glob("withdb-*.tar.gz"))
    name = entry.name[: -len(".tar.gz")]
    _backdate(name, 40)
    counts = wl._expire_trash(ignoring, days=30)
    assert counts["expired"] == 0 and counts["kept"] == 1
    assert entry.exists()


def test_a_tracked_file_that_differs_from_the_head_keeps(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "edited")
    items = [
        _reg(m.name, b"edited\n") if m.name.endswith("/seed.txt") else (m, d)
        for m, d in _members(a["archive"])
    ]
    _write(a["archive"], items)
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "seed.txt" in why


def test_a_TRACKED_file_under_an_allowlisted_path_is_still_compared(ignoring, tmp_path):
    """The allowlist is for paths the head does not track; a tracked file in
    `node_modules/` that differs from the head is a loss like any other."""
    (ignoring / "node_modules").mkdir()
    (ignoring / "node_modules" / "x.js").write_text("tracked\n")
    _git(ignoring, "add", "node_modules/x.js")
    _git(ignoring, "commit", "-qm", "vendored")
    a = _archive(ignoring, tmp_path, "vendored")
    items = [
        _reg(m.name, b"edited\n") if m.name.endswith("/node_modules/x.js") else (m, d)
        for m, d in _members(a["archive"])
    ]
    _write(a["archive"], items)
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "node_modules/x.js" in why


def test_a_dirty_patch_keeps_the_archive(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "patched")
    _add(a, _reg(f"{a['entry']}/.dirty.patch", b"diff --git a/x b/x\n"))
    assert _judge(a, ignoring)[0] is False


def test_a_symlink_whose_target_differs_keeps(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "relinked")
    items = [
        _special(m.name, tarfile.SYMTYPE, "elsewhere") if m.name.endswith("/link") else (m, d)
        for m, d in _members(a["archive"])
    ]
    _write(a["archive"], items)
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "link" in why


def test_an_unresolvable_head_keeps(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "nohead")
    meta = _meta(a) | {"head": "f" * 40, "commit": "f" * 40}
    redundant, why = wl._archive_redundant(
        wl._load_redundancy_module(), a["archive"], meta, ignoring
    )
    assert redundant is False and "does not resolve" in why


def test_an_unmerged_head_keeps(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "unmerged", detached_commit=True)
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "not proven in main" in why


def test_the_archived_head_is_judged_not_where_the_branch_ref_moved(ignoring, tmp_path):
    """The archived head is unmerged; the branch NAME was later reset onto main.
    Judging the name would read `ancestor` and expire unmerged work."""
    wt = tmp_path / "moved"
    _git(ignoring, "worktree", "add", "-q", "-b", "moved", str(wt))
    (wt / "y.txt").write_text("unmerged\n")
    _git(wt, "add", "y.txt")
    _git(wt, "commit", "-qm", "unmerged")
    rec = next(x for x in wl._list_worktrees(ignoring) if x["path"] == str(wt))
    assert wl._trash_worktree(rec, ignoring, lane="merged") is True
    # `branch -f` refuses while the anchor registration still names the branch.
    _git(ignoring, "update-ref", "refs/heads/moved", "main")
    entry = next(wl.TRASH_DIR.glob("moved-*.tar.gz"))
    a = {"archive": entry, "entry": entry.name[: -len(".tar.gz")]}
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "not proven in main" in why


def test_a_legacy_directory_entry_keeps(ignoring, tmp_path):
    legacy = wl.TRASH_DIR / "legacy-20250101"
    legacy.mkdir(parents=True)
    head = _git(ignoring, "rev-parse", "HEAD").stdout.strip()
    meta = {"head": head, "lane": "merged", "had_uncommitted_changes": False}
    assert wl._archive_redundant(wl._load_redundancy_module(), legacy, meta, ignoring) == (
        False,
        "format not content-checked",
    )


# ─── fail closed ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "{entry}/../escape",
        "other-entry/seed.txt",
        "{entry}/./seed.txt",
        "{entry}//seed.txt",
    ],
)
def test_a_member_name_it_cannot_place_keeps(ignoring, tmp_path, name):
    a = _archive(ignoring, tmp_path, "names")
    _add(a, _reg(name.format(entry=a["entry"]), b"seed\n"))
    redundant, why = _judge(a, ignoring)
    # Refused by the name check itself, not by a later lookup that happens to miss.
    assert redundant is False and ("outside the archive root" in why or "unsafe member" in why)


@pytest.mark.parametrize(
    ("kind", "linkname"),
    [(tarfile.FIFOTYPE, ""), (tarfile.LNKTYPE, "seed.txt"), (tarfile.CHRTYPE, "")],
)
def test_a_special_member_keeps(ignoring, tmp_path, kind, linkname):
    a = _archive(ignoring, tmp_path, "special")
    _add(a, _special(f"{a['entry']}/odd", kind, linkname))
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "special member" in why


def test_a_git_DIRECTORY_is_not_the_allowlisted_pointer(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "gitdir")
    items = [(m, d) for m, d in _members(a["archive"]) if not m.name.endswith("/.git")]
    gitdir = tarfile.TarInfo(f"{a['entry']}/.git")
    gitdir.type = tarfile.DIRTYPE
    items += [(gitdir, None), _reg(f"{a['entry']}/.git/config", b"[core]\n")]
    _write(a["archive"], items)
    assert _judge(a, ignoring)[0] is False


def test_a_git_SYMLINK_is_not_the_allowlisted_pointer(ignoring, tmp_path):
    """Only a regular `.git` file is the worktree's pointer; a link there could
    stand in for anything."""
    a = _archive(ignoring, tmp_path, "gitlink")
    items = [
        _special(m.name, tarfile.SYMTYPE, "/elsewhere") if m.name.endswith("/.git") else (m, d)
        for m, d in _members(a["archive"])
    ]
    _write(a["archive"], items)
    redundant, why = _judge(a, ignoring)
    assert redundant is False and ".git" in why


def _tar_bytes(archive: Path) -> bytes:
    with gzip.open(archive, "rb") as f:
        return f.read()


def test_a_truncated_gzip_trailer_keeps(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "trunc")
    a["archive"].write_bytes(a["archive"].read_bytes()[:-8])  # CRC32 + ISIZE gone
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "unreadable" in why


def test_a_bad_header_after_the_first_member_keeps(ignoring, tmp_path):
    """tarfile ends iteration SILENTLY at a bad header past offset 0, so the
    members after it would go unjudged."""
    a = _archive(ignoring, tmp_path, "badhdr")
    _add(a, _reg(f"{a['entry']}/after.txt", b"never judged\n"))
    raw = bytearray(_tar_bytes(a["archive"]))
    at = raw.index(f"{a['entry']}/after.txt".encode())
    raw[at + 148 : at + 156] = b"garbage!"  # its header checksum field
    with gzip.open(a["archive"], "wb") as f:
        f.write(bytes(raw))
    with tarfile.open(a["archive"], "r:gz") as tf:
        assert not any(m.name.endswith("after.txt") for m in tf), "fixture: iteration must stop"
    assert _judge(a, ignoring)[0] is False


@pytest.mark.parametrize("data", [b"", b"\0" * 1024])
def test_a_bad_header_hiding_an_EMPTY_or_NUL_member_keeps(ignoring, tmp_path, data):
    """The bad block itself is never read past: draining from after it would
    see only NULs for an empty or all-NUL member, and pass."""
    a = _archive(ignoring, tmp_path, "nulhdr")
    _add(a, _reg(f"{a['entry']}/lost.bin", data))
    raw = bytearray(_tar_bytes(a["archive"]))
    at = raw.index(f"{a['entry']}/lost.bin".encode())
    raw[at + 148 : at + 156] = b"garbage!"
    with gzip.open(a["archive"], "wb") as f:
        f.write(bytes(raw))
    assert _judge(a, ignoring)[0] is False


def test_data_after_the_end_of_archive_keeps(ignoring, tmp_path):
    a = _archive(ignoring, tmp_path, "trailing")
    raw = _tar_bytes(a["archive"])  # read BEFORE gzip.open truncates the file
    with gzip.open(a["archive"], "wb") as f:
        f.write(raw + b"x" * 512)
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "after the end" in why


def test_a_module_that_fails_to_load_expires_nothing(ignoring, tmp_path, monkeypatch):
    a = _archive(ignoring, tmp_path, "noload")
    _backdate(a["entry"], 40)
    monkeypatch.setattr(wl, "__file__", str(tmp_path / "elsewhere" / "worktree_lifecycle.py"))
    assert wl._load_redundancy_module() is None
    counts = wl._expire_trash(ignoring, days=30)
    assert counts["expired"] == 0 and counts["kept"] == 1
    assert a["archive"].exists()


def test_a_verdict_that_raises_keeps(ignoring, tmp_path, monkeypatch):
    a = _archive(ignoring, tmp_path, "raises")

    def boom(*_a, **_k):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(wl, "_merge_verdict", boom)
    redundant, why = _judge(a, ignoring)
    assert redundant is False and "RuntimeError" in why


def test_gh_json_that_is_not_a_list_keeps(ignoring, tmp_path, monkeypatch):
    """A dict from `gh` made `_merge_verdict` raise AttributeError; the
    predicate turns that into a keep, never an expiry."""
    wt = tmp_path / "odd"
    _git(ignoring, "worktree", "add", "-q", "-b", "odd", str(wt))
    (wt / "x.txt").write_text("x\n")
    _git(wt, "add", "x.txt")
    _git(wt, "commit", "-qm", "x")
    rec = next(x for x in wl._list_worktrees(ignoring) if x["path"] == str(wt))
    assert wl._trash_worktree(rec, ignoring, lane="merged") is True
    entry = next(wl.TRASH_DIR.glob("odd-*.tar.gz"))
    a = {"archive": entry, "entry": entry.name[: -len(".tar.gz")]}
    _gh(tmp_path, monkeypatch, json.dumps({"number": 1}))
    assert _judge(a, ignoring)[0] is False


@pytest.mark.parametrize("value", ["abc", 3, {"x": 1}])
def test_a_non_list_unique_commits_does_not_abort_the_run(ignoring, tmp_path, value):
    a = _archive(ignoring, tmp_path, "oddmeta")
    _backdate(a["entry"], 40)
    meta_path = wl.TRASH_DIR / f"{a['entry']}.meta.json"
    meta_path.write_text(json.dumps(_meta(a) | {"unique_commits": value}))
    counts = wl._expire_trash(ignoring, days=30, dry_run=True)
    assert counts["would_expire"] == 1


def test_dry_run_counts_what_would_expire_and_logs_each_keep(ignoring, tmp_path, capsys):
    clean = _archive(ignoring, tmp_path, "dryclean")
    held = _archive(ignoring, tmp_path, "dryheld", detached_commit=True)
    for a in (clean, held):
        _backdate(a["entry"], 40)
    counts = wl._expire_trash(ignoring, days=30, dry_run=True)
    assert counts["would_expire"] == 1 and counts["kept"] == 1 and counts["expired"] == 0
    out = capsys.readouterr().out
    assert f"KEEP {held['entry']}: head not proven in main" in out
    assert f"WOULD EXPIRE {clean['entry']}" in out
    assert clean["archive"].exists() and _registered(ignoring)[str(clean["path"])]
