"""Retention for the reaper's archives, and the order that makes it lossless.

The archive LOCK is a GC anchor: a locked, directoryless registration keeps the
commits the archive points at reachable. Expiry therefore has exactly one safe
order — delete the archive, then its commit pin, then unlock, then prune — and
these tests pin the ORDER, not only the end state. They use real git repos and
the real reaper, so the lock reason the expiry parses is the one it writes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import private_module

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "worktree_lifecycle.py"
_HYGIENE = _ROOT / "scripts" / "disk_hygiene.sh"

wl = private_module("worktree_lifecycle_retention", _SCRIPT)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never touch the operator's real ~/.genesis."""
    for name in ("TRASH_DIR", "LOG_DIR", "TOMBSTONE_INDEX", "BOARD_CACHE"):
        monkeypatch.setattr(wl, name, tmp_path / f"isolated-{name.lower()}")
    monkeypatch.delenv(wl.RETENTION_ENV, raising=False)
    monkeypatch.delenv(wl.STALE_CLAIM_ENV, raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "T")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-qm", "seed")
    return r


def _backdate(entry_name: str, days: float) -> None:
    """Age an archive by rewriting `trashed_at` in its sidecar meta."""
    meta = wl.TRASH_DIR / f"{entry_name}.meta.json"
    data = json.loads(meta.read_text())
    data["trashed_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    meta.write_text(json.dumps(data))


def _archive(
    repo: Path, tmp_path: Path, name: str, *, detached_commit: bool = False, lane: str = "merged"
) -> dict:
    """Archive a worktree through the REAL reaper. With ``detached_commit`` the
    worktree holds a commit reachable from nothing but its own HEAD. ``lane`` is
    what the reaper records from its own merged/unmerged verdict; only
    ``"merged"`` archives are eligible to expire."""
    wt = tmp_path / name
    if detached_commit:
        _git(repo, "worktree", "add", "-q", "--detach", str(wt))
        (wt / f"{name}.txt").write_text("only here\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", f"only-{name}")
    else:
        _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    rec = next(x for x in wl._list_worktrees(repo) if x["path"] == str(wt))
    assert wl._trash_worktree(rec, repo, lane=lane) is True
    entry = next(p for p in wl.TRASH_DIR.glob(f"{name}-*.tar.gz"))
    return {"path": wt, "head": head, "entry": entry.name[: -len(".tar.gz")], "archive": entry}


def _registered(repo: Path) -> dict[str, str | None]:
    out = _git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0")
    regs: dict[str, str | None] = {}
    cur = None
    for line in out:
        if line.startswith("worktree "):
            cur = line[len("worktree ") :]
            regs[cur] = None
        elif line.startswith("locked") and cur is not None:
            regs[cur] = line[len("locked ") :] if line != "locked" else ""
    return regs


def _commit_exists(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _gc_now(repo: Path) -> None:
    _git(
        repo,
        "-c",
        "gc.reflogExpire=now",
        "-c",
        "gc.reflogExpireUnreachable=now",
        "-c",
        "gc.worktreePruneExpire=now",
        "reflog",
        "expire",
        "--expire=now",
        "--expire-unreachable=now",
        "--all",
    )
    _git(repo, "-c", "gc.worktreePruneExpire=now", "gc", "--prune=now", "-q")


def _pin_ref(entry: str) -> str:
    """A ref in the refs/archived/<sanitised>-<sha256[:10]> shape."""
    digest = hashlib.sha256(entry.encode()).hexdigest()[:10]
    return f"refs/archived/{entry}-{digest}"


# ─── the lever ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "cli", "expected"),
    [
        (None, None, None),  # retention is OFF by default (#2504)
        (None, 45, 45),
        (None, 0, None),
        ("60", None, 60),
        ("60", 10, 60),  # the env lever wins: it is the kill switch
        ("off", 10, None),
        ("0", None, None),
        ("-3", None, None),
        ("thirty", 10, None),  # unreadable lever -> LESS authority, never a guess
        ("", 12, 12),
    ],
)
def test_retention_lever_resolution(monkeypatch, env, cli, expected):
    if env is not None:
        monkeypatch.setenv(wl.RETENTION_ENV, env)
    days, _source = wl._resolve_retention_days(cli)
    assert days == expected


def test_the_reason_is_matched_exactly_not_by_prefix():
    make = wl._archive_lock_reason
    assert wl._archive_entry_from_reason(make("a-20260101")) == "a-20260101"
    assert wl._archive_entry_from_reason(make("a-1", recovery_failed=True)) == "a-1"
    # An entry that itself contains `;` parses whole, and never as its prefix.
    assert wl._archive_entry_from_reason(make("a;b-2")) == "a;b-2"
    for bad in ("../x", "x/y", "..", ".", ""):
        assert wl._archive_entry_from_reason(make(bad)) is None
    assert wl._archive_entry_from_reason("archived by the reaper -> a") is None
    assert wl._archive_entry_from_reason("do not touch") is None


# ─── the acceptance replay ───────────────────────────────────────────────────


def test_expiry_deletes_archive_then_pin_then_unlocks_then_prunes(repo, tmp_path):
    """The real case, end to end: an archived detached worktree whose commit is
    reachable from nothing but its anchor. Before expiry `gc` must keep it;
    after expiry the archive, pin, lock and registration are all gone and `gc`
    collects it. A merged archive's commit survives because main reaches it."""
    old = _archive(repo, tmp_path, "old", detached_commit=True)
    merged = _archive(repo, tmp_path, "merged")
    young = _archive(repo, tmp_path, "young", detached_commit=True)
    _backdate(old["entry"], 40)
    _backdate(merged["entry"], 40)
    _backdate(young["entry"], 5)

    # Control: the anchor holds before expiry.
    _gc_now(repo)
    assert _commit_exists(repo, old["head"]), "the anchor did not keep the commit alive"

    # A sibling whose directory is merely MOVED ASIDE. A repo-wide `worktree
    # prune` would drop it; the targeted removal must not.
    aside = tmp_path / "aside"
    _git(repo, "worktree", "add", "-q", "-b", "aside", str(aside))
    os.rename(aside, tmp_path / "aside.moved")

    # The commit pin a sibling change adds per archive, plus one for a
    # DIFFERENT entry that must survive. Created after the control gc, so that
    # control proves the LOCK alone anchors the commit.
    _git(repo, "update-ref", _pin_ref(old["entry"]), old["head"])
    unrelated = "refs/archived/unrelated-0123456789"
    _git(repo, "update-ref", unrelated, merged["head"])

    counts = wl._expire_trash(repo, days=30)
    assert counts["expired"] == 2
    assert counts["anchors_removed"] == 2

    regs = _registered(repo)
    assert str(old["path"]) not in regs and str(merged["path"]) not in regs
    assert regs.get(str(young["path"])), "a young archive must keep its anchor"
    assert str(aside) in regs, "a moved-aside sibling must stay registered"
    assert not old["archive"].exists() and not merged["archive"].exists()
    assert young["archive"].exists()
    assert not (wl.TRASH_DIR / f"{old['entry']}.meta.json").exists()

    refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/archived/").stdout.split()
    assert _pin_ref(old["entry"]) not in refs
    assert unrelated in refs, "a pin for a different entry must survive"
    _git(repo, "update-ref", "-d", unrelated)

    rows = [json.loads(x) for x in wl.TOMBSTONE_INDEX.read_text().splitlines()]
    events = [(r.get("event"), r.get("name")) for r in rows if r.get("event")]
    for ev in ("expiring", "deleted", "expired"):
        assert (ev, old["entry"]) in events, ev
    expiring = next(r for r in rows if r.get("event") == "expiring" and r["name"] == old["entry"])
    assert expiring["lane"] == "merged"

    _gc_now(repo)
    assert not _commit_exists(repo, old["head"]), "an expired archive's commit outlived it"
    assert _commit_exists(repo, merged["head"]), "a commit main reaches must survive"
    assert _commit_exists(repo, young["head"]), "a young archive's commit must survive"


def test_ORDER_is_archive_then_ref_then_unlock_then_prune(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "ordered")
    _backdate(a["entry"], 40)
    calls: list[str] = []
    for fn in ("_delete_stored", "_delete_archive_refs", "_unlock_anchor", "_prune_anchor"):
        original = getattr(wl, fn)

        def spy(*args, _fn=fn, _orig=original, **kwargs):
            calls.append(_fn)
            return _orig(*args, **kwargs)

        monkeypatch.setattr(wl, fn, spy)
    wl._expire_trash(repo, days=30)
    assert calls == ["_delete_stored", "_delete_archive_refs", "_unlock_anchor", "_prune_anchor"]


def test_a_failed_archive_deletion_leaves_the_anchor_locked(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "stuck")
    _backdate(a["entry"], 40)
    touched: list[str] = []
    monkeypatch.setattr(wl, "_delete_stored", lambda *_a, **_k: False)
    for fn in ("_delete_archive_refs", "_unlock_anchor", "_prune_anchor"):
        monkeypatch.setattr(wl, fn, lambda *_a, _fn=fn, **_k: touched.append(_fn) or True)
    counts = wl._expire_trash(repo, days=30)
    assert touched == []
    assert counts["expired"] == 0
    assert _registered(repo)[str(a["path"])], "the anchor must stay locked"


def test_an_entry_whose_directory_is_back_is_skipped_whole(repo, tmp_path):
    """A recovery that stops part way re-locks a PRESENT tree. Deleting its
    archive would leave a lock nothing can ever clear."""
    a = _archive(repo, tmp_path, "back")
    _backdate(a["entry"], 40)
    a["path"].mkdir()
    counts = wl._expire_trash(repo, days=30)
    assert counts["expired"] == 0 and counts["skipped"] == 1
    assert a["archive"].exists()
    assert _registered(repo)[str(a["path"])]


def test_the_intent_row_is_written_before_the_archive_is_deleted(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "intent")
    _backdate(a["entry"], 40)
    monkeypatch.setattr(wl, "_append_record", lambda _r: False)
    counts = wl._expire_trash(repo, days=30)
    assert counts["expired"] == 0
    assert a["archive"].exists(), "no recorded intent -> no deletion"


def test_RETRY_finishes_an_expiry_that_died_after_deleting_the_archive(repo, tmp_path):
    a = _archive(repo, tmp_path, "crashed")
    wl._append_record({"event": "expiring", "name": a["entry"]})
    wl._append_record({"event": "deleted", "name": a["entry"]})
    a["archive"].unlink()
    wl._expire_trash(repo, days=30)
    assert str(a["path"]) not in _registered(repo)


def test_a_MISSING_archive_with_no_recorded_expiry_keeps_its_anchor(repo, tmp_path):
    """Missing is not expired: an unmounted trash, a hand deletion. Only our
    own recorded intent licenses removing the anchor."""
    a = _archive(repo, tmp_path, "orphan")
    a["archive"].unlink()
    wl._expire_trash(repo, days=30)
    assert _registered(repo)[str(a["path"])]


def test_expiry_and_recovery_exclude_each_other(repo, tmp_path):
    a = _archive(repo, tmp_path, "busy")
    _backdate(a["entry"], 40)
    fd = os.open(str(wl.TRASH_DIR / wl.LIFECYCLE_LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert wl._expire_trash(repo, days=30)["expired"] == 0
        assert a["archive"].exists()
        assert wl._recover(a["entry"], repo) is False
    finally:
        os.close(fd)


def test_dry_run_changes_nothing(repo, tmp_path):
    a = _archive(repo, tmp_path, "dry")
    _backdate(a["entry"], 40)
    before = _registered(repo)
    wl._expire_trash(repo, days=30, dry_run=True)
    assert a["archive"].exists()
    assert _registered(repo) == before
    assert not wl.TOMBSTONE_INDEX.exists() or "expiring" not in wl.TOMBSTONE_INDEX.read_text()


def test_retention_is_off_by_default_and_the_cli_deletes_nothing(repo, tmp_path, monkeypatch):
    """The split of #2458: with no window named, `--expire-trash` deletes no
    archive, releases no anchor, prunes no registration and journals nothing —
    even for an archive that the metadata predicate would expire."""
    a = _archive(repo, tmp_path, "offbydefault")
    _backdate(a["entry"], 400)
    before = _registered(repo)
    assert before.get(str(a["path"])), "fixture: the archive must be anchored"
    monkeypatch.setattr(wl, "_repo_root", lambda: repo)
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py", "--expire-trash"])
    assert wl.main() == 0
    assert a["archive"].exists(), "retention must be off by default"
    assert _registered(repo) == before
    assert not wl.TOMBSTONE_INDEX.exists() or "expiring" not in wl.TOMBSTONE_INDEX.read_text()
    # Guard-the-guard: with an explicit window this very archive DOES expire, so
    # the assertions above are not vacuously true of an archive nothing touches.
    monkeypatch.setattr(
        sys, "argv", ["worktree_lifecycle.py", "--expire-trash", "--retention-days", "30"]
    )
    assert wl.main() == 0
    assert not a["archive"].exists(), "fixture: an explicit window must expire it"


def test_an_unreadable_anchor_path_is_never_read_as_absent(repo, tmp_path, monkeypatch):
    """`os.path.lexists` is False on EACCES/EIO, so an unreadable-but-present
    tree read as gone and expiry deleted its archive, then released its anchor.
    Only ENOENT/ENOTDIR is absence; any other lstat error keeps everything."""
    a = _archive(repo, tmp_path, "eacces")
    _backdate(a["entry"], 40)
    before = _registered(repo)
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if os.fspath(path) == str(a["path"]):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(wl.os, "lstat", lstat)
    assert os.path.lexists(a["path"]) is False, "fixture: lexists must be fooled here"
    counts = wl._expire_trash(repo, days=30)
    assert a["archive"].exists(), "an unreadable anchor path was read as absent"
    assert counts["expired"] == 0
    assert _registered(repo) == before
    assert wl._path_absent(a["path"]) is False
    monkeypatch.setattr(wl.os, "lstat", real_lstat)
    # Control: once the path reads as genuinely absent, the same archive expires.
    assert wl._expire_trash(repo, days=30)["expired"] == 1


def test_the_cli_honours_the_kill_switch(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "killed")
    _backdate(a["entry"], 40)
    monkeypatch.setenv(wl.RETENTION_ENV, "off")
    monkeypatch.setattr(wl, "_repo_root", lambda: repo)
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py", "--expire-trash"])
    assert wl.main() == 0
    assert a["archive"].exists()


# ─── stale session claims ────────────────────────────────────────────────────


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_only_a_DEAD_sessions_claim_is_released(repo, tmp_path):
    claim = private_module(
        "worktree_claim_retention", _ROOT / "scripts" / "hooks" / "worktree_claim.py"
    )
    dead, live, foreign = tmp_path / "dead", tmp_path / "live", tmp_path / "foreign"
    for p in (dead, live, foreign):
        _git(repo, "worktree", "add", "-q", "--detach", str(p))

    def _payload(pid, start):
        return {
            "ns": claim.PAYLOAD_NAMESPACE,
            "v": claim.PAYLOAD_VERSION,
            "rule": claim.RULE_CLAIM,
            "pid": pid,
            "start": start,
        }

    pid = _dead_pid()
    assert claim.proc_is_gone(pid), "fixture: the pid must really be gone"
    _git(repo, "worktree", "lock", "--reason", claim.format_reason(_payload(pid, 12345)), str(dead))
    me = os.getpid()
    _git(
        repo,
        "worktree",
        "lock",
        "--reason",
        claim.format_reason(_payload(me, claim.proc_starttime(me))),
        str(live),
    )
    _git(repo, "worktree", "lock", "--reason", "do not touch", str(foreign))

    assert wl._release_stale_claims(repo) == 1
    regs = _registered(repo)
    assert regs[str(dead)] is None
    assert regs[str(live)] and regs[str(foreign)] == "do not touch"


def test_an_archive_anchor_is_never_released_as_a_claim(repo, tmp_path):
    a = _archive(repo, tmp_path, "anchor")
    assert wl._release_stale_claims(repo) == 0
    assert _registered(repo)[str(a["path"])]


# ─── wiring ──────────────────────────────────────────────────────────────────


def test_disk_hygiene_releases_claims_before_reaping_and_never_expires(tmp_path):
    """Retention is off (#2504): the hygiene run releases stale claims, then
    reaps, and never schedules `--expire-trash`."""
    log = tmp_path / "argv.log"
    fake_python = tmp_path / "python"
    fake_python.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\nexit 0\n')
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    command = (
        f'source "{_HYGIENE}"; VENV_PY="{fake_python}"; REPO_DIR="{repo}"; HOME="{home}"; main'
    )
    subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=60)
    lifecycle = [line for line in log.read_text().splitlines() if "worktree_lifecycle.py" in line]
    assert [line.split("worktree_lifecycle.py", 1)[1].strip() for line in lifecycle] == [
        "--release-stale-claims",
        "",
    ]


def test_entry_age_prefers_trashed_at_and_falls_back_to_mtime(tmp_path):
    f = tmp_path / "x.tar.gz"
    f.write_bytes(b"x")
    old = time.time() - 50 * 86400
    os.utime(f, (old, old))
    now = time.time()
    assert round(wl._entry_age_days(f, {}, now)) == 50
    ts = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    assert round(wl._entry_age_days(f, {"trashed_at": ts}, now)) == 3
    assert round(wl._entry_age_days(f, {"trashed_at": "garbage"}, now)) == 50


def test_the_anchor_stays_while_ANY_stored_form_of_the_entry_remains(repo, tmp_path):
    """A leftover directory beside the archive (a partial cleanup) is still a
    copy the anchor protects. If only the archive expires, the lock stays."""
    a = _archive(repo, tmp_path, "twoforms")
    _backdate(a["entry"], 40)
    leftover = wl.TRASH_DIR / a["entry"]
    leftover.mkdir()
    (leftover / "partial.txt").write_text("x\n")  # fresh mtime, no meta: young
    counts = wl._expire_trash(repo, days=30)
    assert counts["expired"] == 1
    assert not a["archive"].exists() and leftover.exists()
    assert _registered(repo)[str(a["path"])], "the anchor must outlive the last stored copy"


def test_an_INTENT_row_alone_never_licenses_the_retry(repo, tmp_path):
    """"expiring" is written BEFORE the deletion, which can fail. If a later run
    then sees the archive missing (an unmounted trash), intent is not evidence."""
    a = _archive(repo, tmp_path, "intentonly")
    wl._append_record({"event": "expiring", "name": a["entry"]})
    a["archive"].unlink()
    wl._expire_trash(repo, days=30)
    assert _registered(repo)[str(a["path"])]


def test_a_recovery_scratch_dir_is_never_expired_and_keeps_its_archive(repo, tmp_path):
    a = _archive(repo, tmp_path, "midrecovery")
    _backdate(a["entry"], 40)
    scratch = wl._scratch_dir_for(a["archive"])
    scratch.mkdir()
    old = time.time() - 40 * 86400
    os.utime(scratch, (old, old))
    counts = wl._expire_trash(repo, days=30)
    assert counts["expired"] == 0
    assert scratch.exists() and a["archive"].exists()
    assert _registered(repo)[str(a["path"])]


@pytest.mark.parametrize(
    ("value", "disabled"),
    [(None, False), ("", False), ("1", False), ("on", False), ("0", True),
     ("off", True), ("flase", True)],
)
def test_the_stale_claim_kill_switch_fails_closed_on_a_typo(monkeypatch, value, disabled):
    if value is not None:
        monkeypatch.setenv(wl.STALE_CLAIM_ENV, value)
    assert wl._env_disabled(wl.STALE_CLAIM_ENV) is disabled


def test_UNMERGED_archives_never_expire_and_MERGED_ones_do(repo, tmp_path):
    """Owner ruling: unmerged archives are kept for ever. The lane is the
    reaper's own recorded verdict, and anything but an explicit "merged" —
    unmerged, absent (pre-lane entries), unreadable meta — is kept."""
    unmerged = _archive(repo, tmp_path, "unm", detached_commit=True, lane="unmerged")
    merged = _archive(repo, tmp_path, "mrg", detached_commit=True, lane="merged")
    nolane = _archive(repo, tmp_path, "nolane", detached_commit=True, lane="merged")
    for a in (unmerged, merged, nolane):
        _backdate(a["entry"], 400)
    meta = wl.TRASH_DIR / f"{nolane['entry']}.meta.json"
    data = json.loads(meta.read_text())
    del data["lane"]
    meta.write_text(json.dumps(data))

    counts = wl._expire_trash(repo, days=30)

    assert counts["expired"] == 1
    assert counts["kept"] == 2
    assert not merged["archive"].exists()
    assert str(merged["path"]) not in _registered(repo)
    for kept in (unmerged, nolane):
        assert kept["archive"].exists(), kept["entry"]
        assert _registered(repo)[str(kept["path"])], "a kept archive keeps its anchor"
    _gc_now(repo)
    assert _commit_exists(repo, unmerged["head"]), "an unmerged archive's commit was collected"
    assert _commit_exists(repo, nolane["head"])


def test_an_unreadable_meta_is_kept_not_expired(repo, tmp_path):
    a = _archive(repo, tmp_path, "badmeta")
    meta = wl.TRASH_DIR / f"{a['entry']}.meta.json"
    meta.write_text("{not json")
    old = time.time() - 400 * 86400
    os.utime(a["archive"], (old, old))
    counts = wl._expire_trash(repo, days=30)
    assert counts["expired"] == 0 and counts["kept"] == 1
    assert a["archive"].exists()


@pytest.mark.parametrize(
    ("flag", "expires"),
    [(False, True), (True, False), ("<absent>", False), ("false", False), (0, False)],
)
def test_a_MERGED_archive_that_held_uncommitted_changes_is_kept(repo, tmp_path, flag, expires):
    """Owner ruling: uncommitted changes exist only in the archive, so a merged
    archive that recorded any is kept. Only an explicit boolean False expires;
    absent or non-boolean fails toward keeping."""
    a = _archive(repo, tmp_path, "dirtymrg", detached_commit=True, lane="merged")
    _backdate(a["entry"], 400)
    meta = wl.TRASH_DIR / f"{a['entry']}.meta.json"
    data = json.loads(meta.read_text())
    if flag == "<absent>":
        data.pop("had_uncommitted_changes", None)
    else:
        data["had_uncommitted_changes"] = flag
    meta.write_text(json.dumps(data))

    counts = wl._expire_trash(repo, days=30)

    assert counts["expired"] == (1 if expires else 0)
    assert a["archive"].exists() is (not expires)
    assert (str(a["path"]) in _registered(repo)) is (not expires)


def test_the_REAL_reaper_records_uncommitted_changes_and_that_archive_is_kept(
    repo, tmp_path, monkeypatch
):
    """The flag comes from the reaper itself, not a hand-written fixture.

    A dirty worktree is classified onto the unmerged lane, and the archive step
    re-checks dirtiness for the merged lane, so a MERGED archive records
    uncommitted changes only when a write lands after that re-check, or when the
    archive predates the rule. The edit is made in exactly that window, as a real
    write, so the flag is still the reaper's own reading.
    """
    wt = tmp_path / "realdirty"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    rec = next(x for x in wl._list_worktrees(repo) if x["path"] == str(wt))
    real_nested = wl._nested_worktrees_under

    def write_after_the_recheck(wt_path, repo_root):
        (wt / "seed.txt").write_text("edited, never committed\n")
        return real_nested(wt_path, repo_root)

    monkeypatch.setattr(wl, "_nested_worktrees_under", write_after_the_recheck)
    assert wl._trash_worktree(rec, repo, lane="merged") is True
    entry = next(p for p in wl.TRASH_DIR.glob("realdirty-*.tar.gz"))
    name = entry.name[: -len(".tar.gz")]
    assert json.loads((wl.TRASH_DIR / f"{name}.meta.json").read_text())[
        "had_uncommitted_changes"
    ] is True
    _backdate(name, 400)
    assert wl._expire_trash(repo, days=30)["expired"] == 0
    assert entry.exists()
