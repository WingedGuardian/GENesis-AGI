"""Compact a clean, unmerged worktree archive to a pinned commit and a stub.

`worktree_lifecycle.py` archives a reaped worktree as ``<entry>.tar.gz`` with a
``<entry>.meta.json`` sidecar and keeps its registration LOCKED as an anchor, so
the commits the archive points at stay reachable. Unmerged archives are kept for
ever (#2504). Most of them hold nothing their recorded head does not: every file
matches that commit byte for byte. For those, the tarball is a second copy of
one commit, and this module replaces it with:

* a pin, ``refs/archived/<entry>-<sha256(entry)[:10]>``, the name
  `_delete_archive_refs` already expects, so the commit stays reachable without
  the anchor; and
* a bundle, ``<entry>.compacted.bundle``: the commits the head adds beyond the
  default branch (measured: 28 MB for 166 archives whose tarballs held about
  2.6 GB). The pin lives in packed-refs, which a refs repair can drop silently;
  the bundle is the copy outside the object store, as the tarball was; and
* a stub, ``<entry>.compacted.json``: the sidecar metadata plus the pin and the
  bundle's digest, kept for ever. `--recover` recreates the worktree DETACHED at
  the pinned commit.

Every run first AUDITS every stub: a pin that no longer names its head is
re-created, from the bundle when gc has collected the commit, and anything it
cannot repair is an ERROR (owner 2026-11-10).

Only a LOSSLESS compaction is done (owner ruling 2026-11-10). Every gate below
fails toward keeping the archive whole. The order is pin, bundle, stub, anchor, tarball:
whatever a crash leaves, the commit stays reachable (by the anchor until the pin
exists, and by the pin after it), and the next run finishes from the stub.
Compaction never writes the journal's ``deleted`` event, because expiry's retry
pass reads that event as licence to delete the entry's pin.

Loaded by path from `worktree_lifecycle.py`, which passes itself as ``lc``:
the lock, the anchor helpers, the journal and the trash location are its own,
so this module cannot drift from them. Stdlib-only, like the rest of the
lifecycle contract.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
# Dropped on top of lifecycle's `_git_env`, as `worktree_redundancy.GIT_LOCATION_VARS`
# does: each makes git read another index, object store or namespace.
_EXTRA_GIT_VARS = (
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


def pin_ref(entry: str) -> str:
    """The pin for ``entry``. The entry is used verbatim; one that is not a valid
    ref component (measured: none of 672 on one install) is kept, not renamed."""
    digest = hashlib.sha256(entry.encode("utf-8", "surrogateescape")).hexdigest()[:10]
    return f"refs/archived/{entry}-{digest}"


def stub_path(lc, entry: str) -> Path:
    return lc.TRASH_DIR / f"{entry}{lc.COMPACTED_SUFFIX}"


def _git_rc(
    lc,
    repo_root: Path,
    args: list[str],
    *,
    stdin: str | None = None,
    timeout: int = 60,
    private: bool = False,
) -> tuple[int | None, str]:
    """``private``: files git creates are 0600 from the start (umask 077)."""
    env = lc._git_env()
    for var in _EXTRA_GIT_VARS:
        env.pop(var, None)
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            cwd=str(repo_root),
            timeout=timeout,
            env=env,
            input=stdin,
            preexec_fn=(lambda: os.umask(0o077)) if private else None,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, ""
    return r.returncode, r.stdout


def _resolve(lc, repo_root: Path, ref: str) -> str | None:
    """The commit ``ref`` names, or None (absent or unreadable alike)."""
    rc, out = _git_rc(lc, repo_root, ["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"])
    return out.strip() if rc == 0 else None


def _identity(path: Path) -> dict | None:
    try:
        st = path.lstat()
    except OSError:
        return None
    return {"ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _admin_has_refs(admin: Path) -> bool:
    """Any per-worktree ref (refs/bisect, refs/worktree, refs/rewritten live in
    the admin dir; git-worktree(1), REFS). Unreadable counts as present."""
    refs = admin / "refs"
    try:
        if not refs.exists():
            return False
        return any(files for _root, _dirs, files in os.walk(refs, onerror=_raise))
    except OSError:
        return True


def _raise(err: OSError) -> None:
    raise err


def _not_a_candidate(lc, stored: Path, meta: dict, *, days: int, now: float) -> str | None:
    """The metadata-only checks, which most of a trash fails: asked without the
    lock and without logging, so a merged or young archive costs a file read."""
    if not meta:
        return "no readable sidecar metadata"
    if meta.get("lane") != "unmerged":
        return f"lane is {meta.get('lane')!r}, not 'unmerged' (merged archives belong to expiry)"
    if meta.get("had_uncommitted_changes") is not False:
        return "held uncommitted changes (or did not record whether it did)"
    age = lc._entry_age_days(stored, meta, now)
    if age is None or age <= days:
        return f"not older than {days}d"
    return None


def _keep_reason(
    lc,
    red,
    stored: Path,
    meta: dict,
    entry: str,
    mine: list[dict],
    repo_root: Path,
    *,
    days: int,
    now: float,
) -> str | None:
    """Why ``stored`` must stay a full archive, or None when compacting it loses
    nothing. Cheap checks first; the content walk (about 1.6 s) last."""
    why = _not_a_candidate(lc, stored, meta, days=days, now=now)
    if why is not None:
        return why
    head = meta.get("head") or meta.get("commit")
    if not isinstance(head, str) or not _SHA.match(head):
        return "no recorded 40-hex head"
    if _resolve(lc, repo_root, head) != head:
        return f"head {head[:12]} does not resolve"
    if lc._scratch_dir_for(stored).exists():
        return "a recovery left its extraction directory behind"
    twin = lc.TRASH_DIR / entry
    if os.path.lexists(twin):
        return "a directory of the same name is beside the archive"
    if len(mine) != 1:
        return f"{len(mine)} archive anchors name it, not exactly one"
    anchor = mine[0]
    if anchor["reason"] != lc._archive_lock_reason(entry):
        return "its anchor's lock reason is not the plain archive reason"
    if not lc._path_absent(anchor["path"]):
        return f"its registration's directory exists ({anchor['path']})"
    admin = anchor["admin"]
    rc, out = _git_rc(lc, repo_root, ["--git-dir", str(admin), "rev-parse", "HEAD"])
    if rc != 0 or out.strip() != head:
        return "its registration's HEAD is not the recorded head"
    rc, _ = _git_rc(
        lc, repo_root, ["--git-dir", str(admin), "diff-index", "--cached", "--quiet", "HEAD"]
    )
    if rc != 0:
        return "its registration's index differs from HEAD (staged state)"
    if _admin_has_refs(admin):
        return "its registration holds per-worktree refs"
    try:
        logged = red._reflog_commits([admin])
    except OSError as e:
        return f"its reflog is unreadable ({type(e).__name__})"
    if logged:
        # `--not` does not reach `--stdin` revisions, which stay positive.
        rc, out = _git_rc(
            lc,
            repo_root,
            ["rev-list", "--stdin", "--not", "--branches", "--remotes", "--tags", head],
            stdin="\n".join(sorted(logged)) + "\n",
        )
        if rc != 0:
            return "its reflog commits could not be checked"
        if out.split():
            return f"its reflog holds commit(s) nothing else reaches: {out.split()[0][:12]}"
    ref = pin_ref(entry)
    rc, _ = _git_rc(lc, repo_root, ["check-ref-format", ref])
    if rc != 0:
        return "its name is not a valid ref name for a pin"
    rc, out = _git_rc(lc, repo_root, ["rev-parse", "--verify", "-q", ref])
    if rc == 0 and out.strip() != head:
        return f"its pin {ref} already names another commit"
    if rc not in (0, 1):
        return "its pin could not be read"
    tree = red._head_tree(repo_root, head)
    if tree is None:
        return "head tree could not be read"
    seen: set[str] = set()
    try:
        unmatched = red._unmatched_members(stored, entry, tree, seen=seen)
    except red._Unsafe as e:
        return str(e)
    except red._READ_ERRORS as e:
        return f"archive unreadable ({type(e).__name__})"
    if unmatched:
        return f"holds files not in head {head[:12]}: {', '.join(unmatched[:3])}"
    # The other direction: a tracked file the archive lacks was deleted and the
    # deletion never committed. A checkout of the head would bring it back.
    missing = sorted(p for p in tree if p not in seen)
    if missing:
        return f"lacks tracked file(s) of head {head[:12]}: {', '.join(missing[:3])}"
    return None


def _pin(lc, repo_root: Path, ref: str, head: str) -> bool:
    """Create the pin (create-only: an empty old value; git-update-ref(1)), or
    accept one already at ``head``."""
    rc, _ = _git_rc(lc, repo_root, ["update-ref", "-m", "worktree compaction", ref, head, ""])
    return rc == 0 or _resolve(lc, repo_root, ref) == head


def bundle_path(lc, entry: str) -> Path:
    return lc.TRASH_DIR / f"{entry}{lc.COMPACTED_BUNDLE_SUFFIX}"


def _sha256(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _bundle_base(lc, repo_root: Path) -> str | None:
    """What a bundle leaves out: the default branch, from its remote-tracking
    ref when there is one (the remote holds it), else the local branch."""
    default = lc._default_branch(repo_root)
    for ref in (f"refs/remotes/origin/{default}", f"refs/heads/{default}"):
        if _resolve(lc, repo_root, ref):
            return ref
    return None


def _write_bundle(lc, repo_root: Path, entry: str, ref: str, head: str) -> dict:
    """Step 1b: ``<entry>.compacted.bundle``, the commits ``head`` adds beyond the
    default branch, verified and fsynced. It is the copy that lives outside the
    object store, as the tarball did (owner 2026-11-10): a pin lost to a refs
    repair can be restored from it after gc has collected the commit. Returns
    the stub fields, or ``{"error": why}``."""
    base = _bundle_base(lc, repo_root)
    if base is None:
        return {"error": "no default branch to bundle against"}
    rc, _ = _git_rc(lc, repo_root, ["merge-base", "--is-ancestor", head, base])
    if rc == 0:
        # Nothing to carry: the default branch already holds the head.
        return {"bundle": "", "bundle_base": base}
    if rc != 1:
        return {"error": "could not tell whether the head is in the default branch"}
    final = bundle_path(lc, entry)
    tmp = lc.TRASH_DIR / f".{final.name}.{os.getpid()}.tmp"
    try:
        tmp.unlink(missing_ok=True)
        rc, _ = _git_rc(
            lc,
            repo_root,
            ["bundle", "create", "-q", str(tmp), ref, "--not", base],
            timeout=600,
            private=True,
        )
        if rc != 0 or not tmp.is_file():
            return {"error": "git bundle create failed"}
        rc, _ = _git_rc(lc, repo_root, ["bundle", "verify", "-q", str(tmp)], timeout=600)
        if rc != 0:
            return {"error": "the bundle did not verify"}
        os.chmod(tmp, lc._PRIVATE_FILE_MODE)
        _fsync_file(tmp)
        digest = _sha256(tmp)
        os.replace(tmp, final)
        lc._fsync_path(lc.TRASH_DIR)
    except OSError as e:
        return {"error": f"the bundle could not be written ({e})"}
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    if digest is None:
        return {"error": "the bundle could not be read back"}
    return {
        "bundle": final.name,
        "bundle_base": base,
        "bundle_sha256": digest,
        "bundle_bytes": final.stat().st_size,
    }


def valid_stub(lc, stub: dict, entry: str) -> bool:
    """A stub this module wrote: every field a reader uses is present and has the
    one shape this module writes, so a corrupted or hand-edited stub cannot steer
    a fetch, a deletion or an argv."""
    bundle = stub.get("bundle")
    admin = stub.get("anchor_admin")
    original = stub.get("original_path")
    return (
        stub.get("format") == lc.COMPACTED_FORMAT
        and stub.get("name") == entry
        and stub.get("pin") == pin_ref(entry)
        and isinstance(stub.get("head"), str)
        and bool(_SHA.match(stub["head"]))
        and (
            bundle == ""
            or (
                bundle == f"{entry}{lc.COMPACTED_BUNDLE_SUFFIX}"
                and isinstance(stub.get("bundle_sha256"), str)
                and bool(_SHA256.match(stub["bundle_sha256"]))
            )
        )
        and isinstance(admin, str)
        and admin not in ("", ".", "..")
        and os.path.basename(admin) == admin
        and isinstance(original, str)
        and os.path.isabs(original)
        and not original.startswith("-")
        and "\0" not in original
    )


def _bundle_intact(lc, stub: dict) -> bool:
    """The recorded bundle is a regular file with the recorded digest. A digest
    that cannot be computed never matches."""
    path = lc.TRASH_DIR / stub["bundle"]
    if path.is_symlink() or not path.is_file():
        return False
    digest = _sha256(path)
    return digest is not None and digest == stub.get("bundle_sha256")


def repair_pin(lc, repo_root: Path, stub: dict) -> str:
    """Make the stub's pin name its head again: ``"ok"``, ``"repinned"``,
    ``"restored"`` (from the bundle) or ``"error: why"``. Never overwrites a pin
    that names another commit."""
    pin, head = stub["pin"], stub["head"]
    if _git_rc(lc, repo_root, ["check-ref-format", pin])[0] != 0:
        return f"error: {pin} is not a valid ref name"
    current = _resolve(lc, repo_root, pin)
    if current == head:
        return "ok"
    if current is not None or _git_rc(lc, repo_root, ["rev-parse", "--verify", "-q", pin])[0] == 0:
        return f"error: {pin} names another object; left as it is"
    if _resolve(lc, repo_root, head) == head:
        return (
            "repinned"
            if _pin(lc, repo_root, pin, head)
            else "error: the pin could not be re-created"
        )
    if not stub["bundle"]:
        return f"error: head {head[:12]} is gone and it was in {stub.get('bundle_base')}"
    bundle = lc.TRASH_DIR / stub["bundle"]
    if not _bundle_intact(lc, stub):
        return "error: its head is gone and its bundle is missing or altered"
    rc, _ = _git_rc(
        lc,
        repo_root,
        ["fetch", "-q", "--no-tags", "--no-write-fetch-head", str(bundle), f"{pin}:{pin}"],
        timeout=600,
    )
    if rc != 0 or _resolve(lc, repo_root, pin) != head:
        return "error: the bundle could not restore the pin"
    return "restored"


def _stubs(lc) -> list[str]:
    return sorted(
        p.name[: -len(lc.COMPACTED_SUFFIX)]
        for p in lc.TRASH_DIR.iterdir()
        if p.name.endswith(lc.COMPACTED_SUFFIX)
    )


def _audit(lc, repo_root: Path, counts: dict, *, dry_run: bool) -> None:
    """Every stub, every run: its pin is the only ref holding the commit, and a
    refs repair can drop it silently. Re-pin (or restore from the bundle) while
    that is still possible, and make anything else an ERROR."""
    try:
        entries = _stubs(lc)
    except OSError as e:
        counts["errors"] += 1
        lc._log(f"ERROR compaction audit: the trash could not be listed ({e})")
        return
    for entry in entries:
        stub = lc._read_meta(stub_path(lc, entry))
        if not valid_stub(lc, stub, entry):
            counts["errors"] += 1
            lc._log(f"ERROR {entry}: its compaction stub is unreadable or incomplete")
            continue
        if _resolve(lc, repo_root, stub["pin"]) == stub["head"]:
            continue
        if dry_run:
            counts["errors"] += 1
            lc._log(
                f"PIN MISSING {entry}: a real run would re-pin it or restore it from its bundle"
            )
            continue
        with lc._lifecycle_lock() as held:
            if not held:
                counts["skipped"] += 1
                lc._log(f"SKIP auditing {entry}: another lifecycle operation holds the lock")
                continue
            outcome = repair_pin(lc, repo_root, stub)
        if outcome in ("repinned", "restored"):
            counts["repinned"] += 1
            lc._log(f"{outcome.upper()} the pin of {entry} ({stub['pin']})")
        elif outcome != "ok":
            counts["errors"] += 1
            lc._log(f"ERROR {entry}: {outcome[len('error: ') :]}")


def _finish(lc, repo_root: Path, stub: dict, anchors: list[dict] | None) -> str:
    """Steps 3-4 from a written stub: ``"done"``, ``"nothing"`` or ``"held: why"``.

    Acts only on the tarball and the anchor the stub recorded, so an entry that
    came back under the same name is never touched.
    """
    entry = stub["name"]
    if anchors is None:
        return "held: registrations could not be enumerated"
    if _resolve(lc, repo_root, stub["pin"]) != stub["head"]:
        return "held: its pin is missing or names another commit"
    if stub["bundle"] and not _bundle_intact(lc, stub):
        return "held: its bundle is missing or altered"
    tarball = lc.TRASH_DIR / f"{entry}{lc.ARCHIVE_SUFFIX}"
    sidecar = lc._sidecar_meta_path(lc.TRASH_DIR / entry)
    # Every check before any action: a tarball or an anchor that is not the one
    # the stub recorded holds the whole entry, its anchor included.
    if os.path.lexists(tarball) and _identity(tarball) != stub.get("archive_identity"):
        return "held: the tarball is not the one it recorded"
    mine = [a for a in anchors if a["entry"] == entry]
    for anchor in mine:
        if anchor["admin"].name != stub.get("anchor_admin"):
            return f"held: an anchor it did not record ({anchor['admin'].name}) names it"
        if anchor["reason"] != lc._archive_lock_reason(entry):
            return "held: its anchor was re-locked by a recovery that did not complete"
    did = False
    for anchor in mine:
        if not (lc._unlock_anchor(anchor) and lc._prune_anchor(anchor)):
            return "held: its anchor could not be removed"
        did = True
    did = _prune_leftover_registration(lc, repo_root, stub) or did
    if os.path.lexists(tarball):
        if not lc._delete_stored(tarball, sidecar):
            return "held: the tarball could not be deleted"
        did = True
    elif os.path.lexists(sidecar):
        try:
            sidecar.unlink()
        except OSError as e:
            return f"held: its old sidecar could not be removed ({e})"
        lc._fsync_path(lc.TRASH_DIR)
        did = True
    if did:
        lc._append_record(
            {
                "event": "compacted",
                "name": entry,
                "pin": stub["pin"],
                "at": datetime.now(UTC).isoformat(),
            }
        )
    return "done" if did else "nothing"


def _prune_leftover_registration(lc, repo_root: Path, stub: dict) -> bool:
    """A run that died between unlocking the recorded anchor and pruning it
    leaves an unlocked, directory-less registration that no longer reads as an
    anchor. Prune it, by the same identity checks `_prune_anchor` applies."""
    admin_root = lc._admin_worktrees_dir(repo_root)
    name = stub.get("anchor_admin")
    if admin_root is None or not isinstance(name, str) or not name or "/" in name:
        return False
    admin = admin_root / name
    if admin.is_symlink() or not admin.is_dir() or (admin / "locked").exists():
        return False
    path = stub["original_path"]
    if lc._registration_path(admin) != path or not lc._path_absent(path):
        return False
    return lc._prune_anchor({"admin": admin, "path": path})


def _resume(lc, repo_root: Path, counts: dict) -> None:
    """Finish every stub whose tarball, sidecar or anchor is still there."""
    try:
        names = {p.name for p in lc.TRASH_DIR.iterdir()}
    except OSError as e:
        counts["errors"] += 1
        lc._log(f"ERROR compaction resume: the trash could not be listed ({e})")
        return
    pending = [
        n[: -len(lc.COMPACTED_SUFFIX)] for n in sorted(names) if n.endswith(lc.COMPACTED_SUFFIX)
    ]
    if not pending:
        return
    anchors = lc._archive_anchors(repo_root)
    admin_root = lc._admin_worktrees_dir(repo_root)
    owned = {a["entry"] for a in anchors or []}
    for entry in pending:
        stub = lc._read_meta(stub_path(lc, entry))
        if not valid_stub(lc, stub, entry):
            continue  # the audit reports it
        leftover_admin = (
            admin_root is not None
            and isinstance(stub.get("anchor_admin"), str)
            and (admin_root / stub["anchor_admin"]).is_dir()
            and lc._registration_path(admin_root / stub["anchor_admin"]) == stub["original_path"]
        )
        leftovers = (
            entry + lc.ARCHIVE_SUFFIX in names
            or entry + ".meta.json" in names
            or entry in owned
            or leftover_admin
            or anchors is None
        )
        if not leftovers:
            continue
        with lc._lifecycle_lock() as held:
            if not held:
                counts["skipped"] += 1
                lc._log(f"SKIP resuming {entry}: another lifecycle operation holds the lock")
                continue
            outcome = _finish(lc, repo_root, stub, lc._archive_anchors(repo_root))
        if outcome == "done":
            counts["resumed"] += 1
            lc._log(f"RESUMED compaction of {entry}")
        elif outcome != "nothing":
            counts["held"] += 1
            lc._log(f"HOLD {entry}: {outcome[len('held: ') :]}")


def compact_trash(
    lc, red, repo_root: Path, *, days: int, dry_run: bool = False, now: float | None = None
) -> dict:
    """Compact every eligible archive; see the module docstring. ``lc`` is
    `worktree_lifecycle`, ``red`` is `worktree_redundancy`. ``errors`` and
    ``held`` in the result are what a caller should treat as a failed run."""
    now = time.time() if now is None else now
    counts = {
        "compacted": 0,
        "would_compact": 0,
        "resumed": 0,
        "repinned": 0,
        "skipped": 0,
        "held": 0,
        "errors": 0,
        "kept": {},
    }
    if not lc.TRASH_DIR.is_dir():
        return counts
    _audit(lc, repo_root, counts, dry_run=dry_run)
    if not dry_run:
        _resume(lc, repo_root, counts)
    anchors = lc._archive_anchors(repo_root)
    if anchors is None:
        counts["errors"] += 1
        lc._log("ERROR compaction: registrations could not be enumerated; nothing compacted")
        return counts
    by_entry: dict[str, list[dict]] = {}
    for a in anchors:
        by_entry.setdefault(a["entry"], []).append(a)
    for stored, meta_path in lc._iter_trash_entries():
        if not stored.name.endswith(lc.ARCHIVE_SUFFIX) or lc._SCRATCH_NAME.match(stored.name):
            continue
        if stored.is_symlink() or not stored.is_file():
            continue
        entry = lc._entry_name(stored)
        if os.path.lexists(stub_path(lc, entry)):
            continue  # the resume pass owns it
        why = _not_a_candidate(lc, stored, lc._read_meta(meta_path), days=days, now=now)
        if why is not None:
            counts["kept"][entry] = why
            continue
        if dry_run:
            why = _keep_reason(
                lc,
                red,
                stored,
                lc._read_meta(meta_path),
                entry,
                by_entry.get(entry, []),
                repo_root,
                days=days,
                now=now,
            )
            if why is None:
                counts["would_compact"] += 1
                lc._log(f"WOULD COMPACT {entry}")
            else:
                counts["kept"][entry] = why
                lc._log(f"KEEP {entry}: {why}")
            continue
        with lc._lifecycle_lock() as held:
            if not held:
                counts["skipped"] += 1
                lc._log(f"SKIP {entry}: another lifecycle operation holds the lock")
                continue
            result = _compact_one(lc, red, repo_root, stored, meta_path, entry, days=days, now=now)
        if result is None:
            counts["compacted"] += 1
        elif result.startswith("held: "):
            counts["held"] += 1
        else:
            counts["kept"][entry] = result
    dry = f"{counts['would_compact']} would compact (dry run), " if dry_run else ""
    lc._log(
        f"Compaction: {dry}{counts['compacted']} compacted, {counts['resumed']} earlier "
        f"compaction(s) finished, {counts['repinned']} pin(s) repaired, {counts['held']} "
        f"held, {counts['errors']} error(s), {counts['skipped']} skipped, "
        f"{len(counts['kept'])} kept"
    )
    return counts


def _compact_one(
    lc, red, repo_root: Path, stored: Path, meta_path: Path, entry: str, *, days: int, now: float
) -> str | None:
    """One entry, under the lifecycle lock. None when compacted; a ``"held: "``
    reason when it stopped part way (the next run finishes it); else why it is
    kept whole."""
    anchors = lc._archive_anchors(repo_root)
    if anchors is None:
        return "registrations could not be enumerated"
    mine = [a for a in anchors if a["entry"] == entry]
    meta = lc._read_meta(meta_path)
    why = _keep_reason(lc, red, stored, meta, entry, mine, repo_root, days=days, now=now)
    if why is not None:
        lc._log(f"KEEP {entry}: {why}")
        return why
    head = meta.get("head") or meta.get("commit")
    ref = pin_ref(entry)
    identity = _identity(stored)
    if identity is None:
        return "the tarball could not be read"
    # 1. Pin. From here the commit is reachable whatever else happens.
    if not _pin(lc, repo_root, ref, head):
        lc._log(f"KEEP {entry}: the pin could not be created")
        return "the pin could not be created"
    # 1b. Bundle: the copy outside the object store.
    bundle = _write_bundle(lc, repo_root, entry, ref, head)
    if "error" in bundle:
        lc._log(f"KEEP {entry}: {bundle['error']}; its pin is harmless")
        return bundle["error"]
    if not lc._append_record(
        {
            "event": "compacting",
            "name": entry,
            "pin": ref,
            "head": head,
            "at": datetime.now(UTC).isoformat(),
        }
    ):
        return "the journal could not be written"
    # 2. Stub, durable before anything is removed.
    stub = {
        **meta,
        "name": entry,
        "format": lc.COMPACTED_FORMAT,
        "pin": ref,
        "head": head,
        **bundle,
        "compacted_at": datetime.now(UTC).isoformat(),
        "archive_bytes": identity["size"],
        "archive_identity": identity,
        "anchor_admin": mine[0]["admin"].name,
    }
    path = stub_path(lc, entry)
    try:
        lc._atomic_write_text(path, json.dumps(stub, indent=2), mode=lc._PRIVATE_FILE_MODE)
        _fsync_file(path)
        lc._fsync_path(lc.TRASH_DIR)
    except OSError as e:
        lc._log(f"KEEP {entry}: the stub could not be written ({e}); its pin is harmless")
        return f"the stub could not be written ({e})"
    # 3-4. Anchor, then tarball and sidecar.
    outcome = _finish(lc, repo_root, stub, lc._archive_anchors(repo_root))
    if outcome != "done":
        lc._log(f"HOLD {entry}: {outcome[len('held: ') :]}; the next run finishes it")
        return outcome
    kb = stub.get("bundle_bytes", 0) / 1024
    lc._log(
        f"COMPACTED {entry}: pinned {head[:12]} at {ref}, {kb:.0f} KB bundle; "
        f"{identity['size'] / 1048576:.1f} MB freed"
    )
    return None
