"""The restore's writes (#3492 PR B2b): a kit source beside
live_move_restore.py, which holds the check and the judge. Loaded by
live_move_repair.py for `--restore TS` (without --check); every name of the
check it needs is reached through ``chk``, that module as the repair loaded it.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import time

#: As live_move_restore._DIR (a directory fd, never through a link).
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

#: A test's SIGKILL lands at the named step of the write path (see _step).
KILL_ENV = "GENESIS_LIVE_RESTORE_TEST_KILL"

RENAME_NOREPLACE = 1

_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

_BACK = ("restored", "already-restored")

_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

#: Refusals no re-run can turn into a write: the part goes back by hand.
_PERMANENT = ("other:cannot-make", "other:target-unreadable", "other:too-large", "other:no-hash")


def _step(name: str) -> None:
    """A named point of the write path (stage, intent, aside, put-in, index,
    index-call, flag, post-judge). A test sets KILL_ENV to SIGKILL the run there."""
    if os.environ.get(KILL_ENV) == name:
        os.kill(os.getpid(), signal.SIGKILL)


_LIBC: list = []


def _renameat2(sfd: int, sname: str, dfd: int, dname: str, where: str) -> None:
    """renameat2(..., RENAME_NOREPLACE) through ctypes: the one move the
    restore makes (aside, put-in, put-back), which never replaces anything
    (EEXIST). ``where`` names the move in its error."""
    if not _LIBC:
        try:
            import ctypes

            fn = ctypes.CDLL(None, use_errno=True).renameat2
            fn.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            _LIBC.append((ctypes, fn))
        except (ImportError, OSError, AttributeError):
            raise OSError(errno.ENOSYS, "renameat2 is not available", where) from None
    ctypes, fn = _LIBC[0]
    if fn(sfd, os.fsencode(sname), dfd, os.fsencode(dname), RENAME_NOREPLACE) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), where)


def _dirfd(fd: int, rel: str, make: bool = False, mode: int = 0o777) -> int:
    """An fd of the directory ``rel`` below the directory ``fd``, every
    component opened without following a link (review L-1: no path-based
    call through a parent that may since be a symlink); a missing one made
    when ``make`` (os.mkdir, one at a time, never os.makedirs)."""
    fd = os.dup(fd)
    try:
        for part in (x for x in rel.split("/") if x):
            try:
                nfd = os.open(part, _DIR, dir_fd=fd)
            except FileNotFoundError:
                if not make:
                    raise
                os.mkdir(part, mode, dir_fd=fd)
                nfd = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _rmdir_tree(fd: int, name: str) -> None:
    """rmdir ``name`` below ``fd`` and every directory under it, deepest
    first, never following a link: a directory holding anything stays
    (ENOTEMPTY). It cannot tell whose an empty directory is, so the caller
    runs it only where every directory is the restore's own. Each level is
    listed through a fresh descriptor (a long-held one reads stale on btrfs)."""
    try:
        sub = os.open(name, _DIR, dir_fd=fd)
    except OSError:
        return
    try:
        for entry in list(os.scandir(sub)):
            if entry.is_dir(follow_symlinks=False):
                _rmdir_tree(sub, entry.name)
    finally:
        os.close(sub)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=fd)


def _why(exc: OSError) -> str:
    if exc.errno == errno.EXDEV:
        return "other:cross-device"
    if exc.errno in (errno.EINVAL, errno.ENOSYS):
        return "other:no-noreplace"
    known = (errno.EEXIST, errno.ENOTEMPTY, errno.ENOENT, errno.ENOTDIR, errno.ELOOP)
    return "changed-during-restore" if exc.errno in known else "other:write-failed"


class _Part(Exception):
    """One part refused at write time; ``note`` says where things are."""

    def __init__(self, reason: str, note: str = "") -> None:
        super().__init__(reason)
        self.reason, self.note = reason, note


class _Stop(Exception):
    """The run stops (a record it cannot write, review L-7), naming why."""


def restore(rp, chk, repo: str, home: str, ts: str, say) -> int:
    """``--restore TS``: put back every part the judge calls as-left, under the
    repair's locks (held by the caller), refusing every other part by name.
    0 when nothing was refused, else 1."""
    got = chk._open(rp, repo, home, ts, say, write=True)
    if isinstance(got, int):
        return got
    judge, parts, loaded, ctx = got
    try:
        return _Writer(rp, chk, repo, ctx, judge, parts, loaded, say).run()
    finally:
        os.close(ctx.bfd)


class _Writer:
    """One restore run. Per node part (the in-flight one first, review SF-C):
    STAGE the saved node in <run>/new/<path> (each file's sha256 checked
    against SAVED.json as it is copied, its mode the record's & 0o777); record
    the part in flight; move what is at the path to <run>/aside/<path>;
    re-judge the aside (review SF-1: a session's write in between is put back,
    refused); PUT IN with renameat2(RENAME_NOREPLACE) (a node that appeared
    meanwhile is never replaced); judge the aside again (review L-2; a run
    killed before it leaves that to the judge of the next, Judge._landed); record.
    A refused part whose aside holds something the restore did not move there
    stays in flight, and then nothing else is written this run or the next
    (Judge.unresolved); the repair's own as-left copy there holds nothing back.
    Then the index parts in one update-index call, the flags, a refresh, and a
    second judge that must read already-restored for every part written."""

    def __init__(self, rp, chk, repo, ctx, judge, parts, loaded, say) -> None:
        self.rp, self.repo, self.ctx, self.j, self.say = rp, repo, ctx, judge, say
        self.chk = chk  # live_move_restore, the check and the judge
        self.parts, self.loaded = parts, loaded
        self.verdict = {(p, k): v for p, k, v in parts}
        # self.refused: the rows with a refused part, kept by _refuse (review W1).
        self.out, self.notes, self.refused = {}, dict(judge.notes), set()
        self.prior = chk.restore_block(loaded["report"])
        self.run_name = self.run_fd = self.repo_fd = None
        self.recorded_run = None  # the run REPAIR.json's record names after our last save
        self.kept = set()  # paths this run moved into its aside and did not put back
        self.wrote, self.flight = [], None
        # _kept_aside: this run's moves (run, path), and the ignored files
        # every as-left aside still holds, named.
        self.moved_now, self.kept_aside = [], []
        self.called = set()  # every path whose entries the index call replaced
        # The in-flight record kept for the next run: an aside still holding what
        # a refused in-flight part left there (#3986 review). While it is held
        # nothing else is written, since the record has one slot.
        self.hold = list(judge.inflight) if judge.held else None

    def run(self) -> int:
        j = self.j
        for (p, k), v in self.verdict.items():
            if v == "already-restored":
                self.out[(p, k)] = v
            elif v != "as-left":
                self._refuse(p, k, v)
        nodes = sorted(
            (r for r in j.rows if self.verdict.get((r["path"], "node")) == "as-left"),
            key=lambda r: (r["path"] != j.resumed, r["path"]),
        )
        self.repo_fd = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for row in nodes:
                if self.hold:
                    break
                self._node(row)
            for (p, k), v in self.verdict.items():
                if k == "entry" and v == "as-left":
                    self.out[(p, k)] = self.out[(j.entry_of[p], "node")]
            _step("index")
            if not self.hold:
                self._index()
                self._flags()
            if self.wrote:
                self.rp._git(self.repo, "update-index", "-q", "--refresh", check=False)
            _step("post-judge")
            self._post()
            self._name_kept_aside()
            self._finish()
            self._drop_unused_run()
        except _Stop as exc:
            self._drop_unused_run()  # a stop leaves nothing a later run needs either
            self._report()
            self.say(str(exc))
            return 1
        finally:
            for fd in (self.repo_fd, self.run_fd):
                if fd is not None:
                    os.close(fd)
        return self._report()

    # ── the record in REPAIR.json (review SF-B, SF-C, SF-10) ────────────────

    def _save(self, inflight, refused: int | None = None, restored_at: str | None = None) -> None:
        """Re-read REPAIR.json (under update.lock: --dismiss takes it too) and
        write its `restore` block; a failure stops the run (review L-7)."""
        rp, prior = self.rp, self.prior or {}
        kept = prior.get("refused", 0) if refused is None else refused
        block = {"inflight": inflight, "refused": kept, "restored_at": restored_at}
        try:
            loaded = rp.load_report(self.ctx.backup, self.repo)
            if loaded is None:
                raise rp.BadReport("REPAIR.json is gone")
            report = {**loaded["report"], "restore": block}
            # Under update.lock, as every REPAIR.json writer (Repair._retry_hooks relies on it).
            rp.write_json(os.path.join(self.ctx.backup, "REPAIR.json"), report, private=True)
            self.recorded_run = inflight[0] if inflight else None
        except (OSError, rp.BadReport) as exc:
            part = json.dumps(self.flight) + " node" if self.flight else "no part"
            folder = os.path.join(self.ctx.shown, self.run_name) if self.run_name else "none made"
            raise _Stop(
                f"stopped: REPAIR.json could not be written ({exc}); in flight: {part};"
                f" the run folder: {folder}"
            ) from None

    def _run(self) -> str:
        """The run folder, made at the first write (review NOTE-2), 0700, named
        for this report's ts too (#4011), so every later run finds it."""
        if self.run_name is None:
            name = (
                time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                + f"-{os.getpid()}-{self.ctx.ts}{self.chk.RUN_SUFFIX}"
            )
            try:
                os.mkdir(name, 0o700, dir_fd=self.ctx.bfd)
            except OSError as exc:
                raise _Stop(f"stopped: the run folder {name} cannot be made ({exc})") from None
            try:
                self.run_fd = os.open(name, _DIR, dir_fd=self.ctx.bfd)
            except OSError as exc:
                with contextlib.suppress(OSError):
                    os.rmdir(name, dir_fd=self.ctx.bfd)  # made empty just now
                raise _Stop(f"stopped: the run folder {name} cannot be made ({exc})") from None
            self.run_name = name
        return self.run_name

    # ── one node part ───────────────────────────────────────────────────────

    def _node(self, row: dict) -> None:
        p, j = row["path"], self.j
        flying = p == j.resumed  # REPAIR.json names it in flight
        moved = j.inflight[0] if flying else None  # the run whose aside holds what was here
        staged, held = None, None  # held: the run whose aside a refusal leaves holding it
        self.flight = p
        try:
            staged = self._stage(row)
            if not flying:
                self._save([self._run(), p, "node"])
                flying = True
                _step("intent")
                if j.left_is_node(p):
                    self._move(p, self.run_name, to_aside=True)
                    moved = self.run_name
                    _step("aside")
                    if not self._still_left(p, moved):
                        raise _Part("changed-during-restore", "it changed after it was judged")
            if staged is not None:
                d, name = os.path.split(p)
                dst = _dirfd(self.repo_fd, d, make=True)
                try:
                    _renameat2(staged, name, dst, name, "put-in")
                finally:
                    os.close(dst)
                _step("put-in")
                if moved is not None and not self._still_left(p, moved):
                    # The path is the restore's now; the aside holds the newer write.
                    where, held, moved = self._aside(p, moved), moved, None
                    raise _Part("changed-during-restore", f"written after it was moved to {where}")
            self.out[(p, "node")] = "restored"
            self.wrote.append(p)
            if moved is not None:
                self.moved_now.append((moved, p))
        except (_Part, OSError) as exc:
            reason = exc.reason if isinstance(exc, _Part) else _why(exc)
            note = exc.note if isinstance(exc, _Part) else f"{exc.filename or ''}: {exc.strerror}"
            if moved is not None and not self._move(p, moved, to_aside=False):
                note = f"{note}; what was at the path is kept at {self._aside(p, moved)}"
                # Held only for content the restore did not move there: the
                # repair's own as-left copy is what git gives back (#3986 review).
                held = None if self._still_left(p, moved) else moved
                if held is None:  # as-left there: its ignored files are named this run
                    self.moved_now.append((moved, p))
            self._refuse(p, "node", reason, note)
        finally:
            if staged is not None:
                os.close(staged)
        if held is not None:  # kept in flight, and nothing else written (#3986 review)
            self.hold = [held, p, "node"]
            unresolved = self.chk.unresolved_note(self._aside(p, held))
            for (q, k), v in self.verdict.items():
                # An entry takes its directory's outcome (run()): one whose
                # directory was decided this run is not refused here. Only an
                # as-left entry is in entry_of (a cannot-make or unreadable one
                # is not), so that is tested first.
                if v != "as-left" or (q, k) in self.out:
                    continue
                if k == "entry" and (j.entry_of[q], "node") in self.out:
                    continue
                self._refuse(q, k, self.chk.INFLIGHT_UNRESOLVED, unresolved)
        if flying:
            self._save(self.hold)
        self.flight = None

    def _aside(self, p: str, run: str) -> str:
        return os.path.join(self.ctx.shown, run, "aside", p)

    def _move(self, p: str, run: str, to_aside: bool) -> bool:
        """The node at ``p`` into <run>/aside/<p> (raises), or back (False if it cannot)."""
        d, name = os.path.split(p)
        try:
            here = _dirfd(self.repo_fd, d)
        except OSError:
            if to_aside:
                raise
            return False
        try:
            there = _dirfd(self.ctx.bfd, f"{run}/aside/{d}", make=to_aside, mode=0o700)
            try:
                if to_aside:
                    _renameat2(here, name, there, name, "aside")
                    self.kept.add(p)
                else:
                    _renameat2(there, name, here, name, "put-back")
                    self.kept.discard(p)
            finally:
                os.close(there)
        except OSError:
            if to_aside:
                raise
            return False
        finally:
            os.close(here)
        return True

    def _still_left(self, p: str, run: str) -> bool:
        """Is the aside of ``p`` still the as-left state? Each such read has a
        budget of its own (#3986 review): the judge's was spent judging every
        row, and a large tag directory read again after the move would else
        run it out and read as changed, refusing (or holding) for nothing."""
        j = self.j
        spent, j.budget = j.budget, self.chk._Budget(self.rp)
        try:
            return j._left(p, root=os.path.join(self.ctx.root, run, "aside"), strict=True)
        except (OSError, self.rp.TooLarge):
            return False
        finally:
            j.budget = spent

    # ── ignored files kept aside (#4025 item 4) ─────────────────────────────

    def _name_kept_aside(self) -> None:
        """The files git ignores (a server's __pycache__) that a tag directory
        held when it was moved aside stay in its aside: a tag directory is a
        node part only where the repair saved a file or a link there (one whose
        disk node is a directory, or nothing, is no node part, its files are
        rows: live_move_repair.Repair._row), so they never have a directory to
        go back to. They are named on every
        run, from every as-left aside (this run's, the in-flight one and every
        earlier run's, runs.aside_parts), so a run killed before naming them
        loses nothing; they never withhold restored_at."""
        j = self.j
        cands = [*self.moved_now, *j.asides_left]
        if j.inflight and not j.held:
            cands.append((j.inflight[0], j.inflight[1]))
        for run, p in dict.fromkeys(cands):
            if p in j.tag or p not in j.tag_dirs or not self._still_left(p, run):
                continue
            spent, j.budget = j.budget, self.chk._Budget(self.rp)
            try:
                tagged = j._tag_below(p)
                have = j._walk(os.path.join(self.ctx.root, run, "aside"), p)
            except (OSError, self.rp.TooLarge):
                continue  # the next run reads it again
            finally:
                j.budget = spent
            where = self._aside(p, run)
            self.kept_aside += [json.dumps(f"{where}/{q}") for q in sorted(have) if q not in tagged]

    # ── staging in <run>/new ────────────────────────────────────────────────

    def _stage(self, row: dict) -> int | None:
        """The new node, built at <run>/new/<path>; its parent's fd. None for
        a node that was absent (review SF-9: the restore only moves aside)."""
        p, disk = row["path"], row["disk"]
        if disk == "absent":
            return None
        self._run()
        d, name = os.path.split(p)
        fd = _dirfd(self.run_fd, f"new/{d}", make=True, mode=0o700)
        try:
            if disk == "dir":
                self._stage_dir(row, fd, name)
            elif disk == "other":  # a FIFO (review N-8: its row's mode); sockets: cannot-make
                os.mkfifo(name, 0o600, dir_fd=fd)
                os.chmod(name, int(self.chk._fifo(row["special"]["mode"])[1], 8), dir_fd=fd)
            else:
                self._stage_one(row["saved_as"], fd, name)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _stage_dir(self, row: dict, fd: int, name: str) -> None:
        """A directory row built whole (review SF-2): every entry the judge
        restores, the update's own bytes left out; the directories the
        umask's, as a checkout makes them (git records no directory mode)."""
        os.mkdir(name, 0o777, dir_fd=fd)
        top = os.open(name, _DIR, dir_fd=fd)
        try:
            for q in sorted(self.j.dirs[row["path"]]):
                qd, qn = os.path.split(q)
                sub = _dirfd(top, qd, make=True)
                try:
                    self._stage_one(f"{row['saved_as']}/{q}", sub, qn)
                finally:
                    os.close(sub)
        finally:
            os.close(top)

    def _stage_one(self, rel: str, fd: int, name: str) -> None:
        rec = self.j.records.get(rel)
        if rec is None:
            raise _Part("other:no-hash", f"SAVED.json records no {rel}")
        src = os.path.join(self.ctx.backup, rel)
        try:
            st = os.lstat(src)
        except OSError:
            raise _Part("other:not-saved", f"{rel} is gone") from None
        bad = _Part("other:hash-mismatch", f"{rel} is not what SAVED.json records")
        if rec["type"] == "symlink":
            if not stat.S_ISLNK(st.st_mode) or os.readlink(src) != rec["target"]:
                raise bad
            os.symlink(rec["target"], name, dir_fd=fd)
        elif rec["type"] == "fifo":
            if not stat.S_ISFIFO(st.st_mode):
                raise bad
            os.mkfifo(name, 0o600, dir_fd=fd)
            os.chmod(name, int(self.chk._fifo(rec["mode"])[1], 8), dir_fd=fd)
        elif rec["sha256"] is None:
            raise _Part("other:too-large", f"{rel} was over the hash budget")
        else:
            self._copy(src, rec, fd, name, bad)

    def _copy(self, src: str, rec: dict, fd: int, name: str, bad: _Part) -> None:
        """Copy a saved file, hashing it as it goes; refused unless its sha256
        is the record's. The mode: the record's & 0o777 (owner ruling)."""
        try:
            sfd = os.open(src, _READ)
        except OSError:
            raise bad from None
        try:
            if not stat.S_ISREG(os.fstat(sfd).st_mode):
                raise bad
            out = os.open(name, _NEW, 0o600, dir_fd=fd)
            try:
                h = hashlib.sha256()
                while chunk := os.read(sfd, 1 << 20):
                    h.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(out, view) :]
                _step("stage")
                if h.hexdigest() != rec["sha256"]:
                    raise bad
                self.rp._copy_xattrs(sfd, out)  # the saved copy kept them (#3974 item 7)
                os.fchmod(out, int(rec["mode"], 8) & 0o777)
            finally:
                os.close(out)
        finally:
            os.close(sfd)

    # ── the index, the flags (§5) ───────────────────────────────────────────

    def _refuse(self, p: str, kind: str, why: str, note: str = "") -> None:
        self.out[(p, kind)] = f"refused:{why}"
        self.refused.add(self.j.entry_of.get(p, p))
        if note:
            self.notes[(p, kind)] = note

    # The row's node part (and ``kinds``), and a covering directory row's node
    # part (review N4), each written or already restored.
    def _back(self, p: str, *kinds: str) -> bool:
        keys = [(q, "node") for q in (p, *self.rp._parents(p))] + [(p, k) for k in kinds]
        return all(self.out.get(key, "restored") in _BACK for key in keys)

    def _index(self) -> None:
        """The index parts whose disk part (if any) is back, in ONE update-index
        call. Its real footprint is git's directory/file rule, not a drop list
        (review SF-A, MEASURED there): a stage-0 record drops every entry at
        and under its path and any file entry at a parent. So right before the
        call the whole index is read once; a part whose footprint holds an
        entry that is not one of this report's rows as the judge read it (no
        part of that row refused) is refused, and its parents `child-refused`;
        so is one whose footprint holds a flag the restore does not set again
        after the call (`other:flag-changed`: the call re-creates entries
        unflagged); each footprint is dropped explicitly, and the entries of a
        row already back inside one are written again in the same call. After the call the WHOLE index
        must be the one expected. Residual (owner ruling): a session's git
        that stages between that read and the call, milliseconds; git has no
        compare-and-swap for the index."""
        j, rp = self.j, self.rp
        todo = sorted(p for (p, k), v in self.verdict.items() if k == "index" and v == "as-left")
        if not todo:
            return
        rows = {r["path"]: r for r in j.rows}

        def sets_again(q: str, go: list[str], flags: dict) -> bool:
            # The call re-creates every entry it drops unflagged (#3986 review):
            # a flag there now survives only as one the judge read and _flags
            # then sets, the row's own flag part (one already back is set again
            # only when its entry is re-created, so only when its row is in the call).
            v = self.verdict.get((q, "flag"))
            return flags.get(q) == j.flags.get(q) and (
                v == "as-left" or (v == "already-restored" and q in go)
            )

        feet, pre = self.chk.footprint(
            rows, todo, self._back, self.refused, self._refuse, self._blobs,
            lambda: rp._index_state(self.repo), j.index, sets_again, rp._parents,
        )  # fmt: skip
        if not feet:
            return
        gone = {q for f in feet.values() for q in f}  # each footprint holds its own path
        # A row whose index is already back can sit in another's footprint (an
        # unmerged entry below a stage-0 file, which git keeps beside it,
        # MEASURED on git 2.43): its saved entries are written again in the
        # same call, never dropped while the report is cleared (#3986 review).
        # A flag on one still refuses the call above (`other:flag-changed`).
        back = {q: rows[q]["index"] for q in gone - set(feet)
                if q in rows and self.out.get((q, "index")) == "already-restored"}  # fmt: skip
        kept = self._kept(pre, gone)
        null = "0" * (2 * hashlib.new(j.fmt).digest_size)
        recs = []
        for p in sorted(feet):
            recs += [f"0 {null}\t{q}" for q in feet[p]]
            recs += [f"{m} {o} {s}\t{p}" for m, o, s in rows[p]["index"]]
        for q in sorted(back):
            recs += [f"{m} {o} {s}\t{q}" for m, o, s in back[q]]
        data = b"".join(os.fsencode(r) + b"\0" for r in recs)
        try:
            rp._git(self.repo, "update-index", "-z", "--index-info", data=data)
        except rp.RepairError as exc:  # review NOTE-7: a session's git holds index.lock
            for p in feet:
                self._refuse(p, "index", "index-locked", str(exc)[:300])
            return
        self.wrote += list(feet)
        self.called = gone
        _step("index-call")
        want = {q: v for q, v in pre.items() if q not in gone}
        want.update((p, [tuple(e) for e in rows[p]["index"]]) for p in feet if rows[p]["index"])
        want.update((q, [tuple(e) for e in v]) for q, v in back.items() if v)
        ok = rp._index_state(self.repo)[0] == want
        for p in feet:
            if ok:
                self.out[(p, "index")] = "restored"
            else:
                self._refuse(p, "index", "index-skipped", kept)

    # Review W2: the entries the call replaces, as `ls-files -s -z` records, in
    # <run>/index-before.txt (0600), written before it runs; the note naming it.
    def _kept(self, pre: dict, gone: set) -> str:
        shown = os.path.join(self.ctx.shown, self._run(), "index-before.txt")
        recs = [f"{m} {o} {s}\t{q}" for q in sorted(gone) for m, o, s in pre.get(q, ())]
        try:
            fd = os.open("index-before.txt", _NEW, 0o600, dir_fd=self.run_fd)
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"".join(os.fsencode(r) + b"\0" for r in recs))
        except OSError as exc:
            raise _Stop(f"stopped: {shown}: {exc}; the index is as it was") from None
        return f"the index is not the one written; the one before the call is in {shown}"

    def _blobs(self, row: dict) -> str:
        """Each saved blob the object store lacks, written back; why not, or ''."""
        for mode, oid, _stage in row["index"]:
            if mode == "160000" or oid in self.j.stored:
                continue
            # Read here, through the anchored folder, and given to git on stdin:
            # a /proc/self/fd path means nothing in git's own process.
            try:
                fd = os.open(os.path.join(self.ctx.backup, "index-blobs", oid), _READ)
                try:
                    data = os.read(fd, os.fstat(fd).st_size + 1)
                finally:
                    os.close(fd)
            except OSError:
                return f"index-blobs/{oid} cannot be read"
            args = ("hash-object", "-w", "--no-filters", "--stdin")
            got = self.rp._git(self.repo, *args, check=False, data=data)
            if got.returncode or os.fsdecode(got.stdout.strip()) != oid:
                return f"index-blobs/{oid} does not hash to its name"
        return ""

    def _flags(self) -> None:
        """Flags after the index call (it re-creates entries). Review N-1: S
        skip-worktree, a lowercase tag assume-unchanged, s both. A flag goes
        only on the entry the judge read, or the one this run's index call
        wrote (#3986 review): an entry a session staged since then is refused
        `changed-during-restore`, never flagged. The index is read once, right
        before the flags (the same residual as the index call: git has no
        compare-and-swap, so a stage in the milliseconds after that read)."""
        now = None
        for (p, k), v in sorted(self.verdict.items()):
            # A flag already back is set again when the index call re-created
            # its entry this run (#3986 review: it re-creates it unflagged).
            again = v == "already-restored" and self.out.get((p, "index")) == "restored"
            if k != "flag" or (v != "as-left" and not again):
                continue
            if not self._back(p, "index"):
                self._refuse(p, "flag", "row-refused")
                continue
            if now is None:
                now = self.rp._index_state(self.repo)[0]
            written = self.out.get((p, "index")) == "restored"
            expect = (
                [tuple(e) for e in self.j.foreign[p]["index"]]
                if written
                else self.j.index.get(p, [])
            )
            if now.get(p, []) != expect:
                why = f"{json.dumps(p)[:200]} changed since it was judged"
                self._refuse(p, "flag", "changed-during-restore", why)
                continue
            flag = self.j.foreign[p]["flag"]
            opts = [o for o, on in (("--skip-worktree", flag in "Ss"),
                                    ("--assume-unchanged", flag.islower())) if on]  # fmt: skip
            try:
                for opt in opts:
                    self.rp._git(self.repo, "update-index", opt, "--", p)
                    _step("flag")
            except self.rp.RepairError as exc:
                self._refuse(p, "flag", "flag-failed", str(exc)[:300])
                continue
            self.out[(p, "flag")] = "restored"
            self.wrote.append(p)

    # ── after the writes (§6) ───────────────────────────────────────────────

    def _post(self) -> None:
        """A second judge: every part written must now read already-restored
        (review N-4: a disagreement counts as refused), and so must a flag read
        already-restored whose entry the index call replaced (#3986 review: an
        index that is not the one written may hold it unflagged), and so must an
        index part read already-restored whose entries that call rewrote."""
        if not self.wrote:
            return
        loaded = {**self.loaded, "report": {**self.loaded["report"], "restore": None}}
        try:
            j = self.chk.Judge(self.rp, self.repo, self.ctx.backup, loaded, self.j.newer_ts,
                      records=self.j.records, strict=True, complete=self.j.complete)  # fmt: skip
            again = {(p, k): v for r in j.rows if r["class"] == "foreign" for p, k, v in j.row(r)}
        except (OSError, self.rp.RepairError):
            again = {}
        for key, o in list(self.out.items()):
            check = o == "restored" or (
                o == "already-restored" and key[1] in ("flag", "index") and key[0] in self.called
            )
            if check and again.get(key) != "already-restored":
                self._refuse(*key, "post-check", f"the judge now reads {again.get(key, 'nothing')}")

    def _drop_unused_run(self) -> None:
        """A run folder holding no aside and no index-before.txt keeps nothing
        a later run or a person needs: only staging, put in or refused, whose
        source is the report's own copy, and the empty aside/ directories a
        refused or put-back move left. aside/ is cleaned only while no node
        this run moved there is still in it: an empty directory inside a moved
        node is the person's, never the restore's (#4364 review round 3). It is removed on every exit, a stop
        included, so refused reruns never pile folders up against RUNS_CAP
        (#4025 review SF-6, #4364 review). Decided by rmdir alone, deepest
        first (a directory holding anything stays: ENOTEMPTY), never by a
        listing of the long-held fd, which reads stale on btrfs; and never
        while REPAIR.json's record names this folder."""
        if self.run_fd is None or self.recorded_run == self.run_name:
            return
        with contextlib.suppress(OSError):
            shutil.rmtree("new", dir_fd=self.run_fd)
        if not self.kept:
            _rmdir_tree(self.run_fd, "aside")
        try:
            os.rmdir(self.run_name, dir_fd=self.ctx.bfd)
        except OSError:
            return  # kept: it holds an aside or index-before.txt
        self.run_name = None

    def _finish(self) -> None:
        """restored_at only when nothing was refused (review SF-4); a re-run
        that changes nothing writes nothing. The in-flight record is cleared
        only once its part is back, or its aside holds nothing (#3986 review:
        kept, every rerun names that aside)."""
        refused = sum(o.startswith("refused:") for o in self.out.values())
        stamp = None if refused else time.strftime("%FT%TZ", time.gmtime())
        p = self.prior
        same = p and p["inflight"] == self.hold and p["refused"] == refused
        if same and self.run_name is None and (p["restored_at"] is None) == (stamp is None):
            return
        self._save(self.hold, refused, stamp)

    def _report(self) -> int:
        say, ts, shown = self.say, self.ctx.ts, self.ctx.shown_backup
        out = [(p, k, self.out.get((p, k), "refused:not-reached")) for p, k, _v in self.parts]
        for p, k, o in out:
            say(self.chk._line(p, k, o, self.notes.get((p, k))))
        n = {x: sum(1 for *_, o in out if o == x) for x in ("restored", "already-restored")}
        refused = len(out) - sum(n.values())
        say(
            f"report {ts}: {len(out)} part(s): {n['restored']} restored,"
            f" {n['already-restored']} already-restored, {refused} refused"
        )
        if done := sorted({p for p, _k, o in out if o == "restored"}):
            more = f" and {len(done) - 50} more" if len(done) > 50 else ""
            say(
                "Restored: " + ", ".join(json.dumps(p) for p in done[:50]) + more + ". The"
                " checkout now holds them as uncommitted changes: tracked edits make"
                " deploy_code_only.sh and update.sh refuse to deploy until they are committed"
                " (on a branch, as a PR), moved out or removed; untracked files restored under"
                " src/, config/ or pyproject.toml block deploy_code_only.sh, untracked files"
                " elsewhere block neither. Nothing was restarted: a server that imports from a"
                " directory moved aside fails there until it restarts."
            )
        if self.run_name:
            folder = os.path.join(self.ctx.shown, self.run_name)
            say(f"What the restore replaced is in {folder}, kept while its report is.")
        if self.kept_aside:
            more = f" and {len(self.kept_aside) - 50} more" if len(self.kept_aside) > 50 else ""
            say(
                "Kept aside with their directory (git ignores them): "
                + ", ".join(self.kept_aside[:50])
                + more
            )
        if lost := sum(1 for *_, o in out if o[len("refused:") :] in _PERMANENT):
            say(
                f"{lost} part(s) the restore cannot write: put them back by hand from {shown}/tree"
                " (a saved directory's empty subdirectories are not rebuilt, and stay there),"
                f" then: scripts/deploy_candidates repair-move --dismiss {ts}"
            )
        return 1 if refused else 0
