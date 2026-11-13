#!/usr/bin/env python3
"""Install the live-move repair kit outside the checkout (#3492 D2).

The live-move repair (live_move_repair.py) heals a checkout that a killed switch
left half-written, so it cannot run from that checkout. It runs from a kit under
<home>/.genesis/lib, installed as one verified unit:

    live-repair.d/<kit_id>/   the KIT_SOURCES files and KIT.json
    live-repair/current       a relative symlink to ../live-repair.d/<kit_id>

The names are KIT_SOURCES, one literal tuple in live_move_repair.py, read from
HEAD's blob with ast: nothing of the tree is ever run. Every byte comes from the
object store at HEAD, never the working tree, and only from a HEAD on main or
live. A new kit is written beside the old one, verified, renamed into place, and
`current` is swapped by rename(2), so a reader finds a whole old kit or a whole
new one. Two versioned kits are kept, and the prune touches no name that is not
a kit id; a symlinked live-repair.d or live-repair is moved aside before any write,
and anything set aside (a kit that fails to verify, a `current` that is not a
link) is kept a week from the move. The prune skips a kit whose KIT.json is
flocked; the repair takes that shared hold from #3492 D2a-2 on (until then
nothing holds it, and a run keeps its own files open while it runs).

The installed kit is HEAD's at the last successful install (bootstrap,
deploy_code_only.sh past its state refusal, an update's rollback when its code
restore succeeded), so it can be newer than the code that wrote the update
state: an update killed after its bootstrap, or a rollback to a tag from before
this installer (whose install fails and leaves the newer kit). The kit must
therefore read the previous release's update state.

    install <repo> <home>   exit 0 installed or already current; 1 not (the old kit is kept)
    status <repo> <home>    one JSON object: state (ok, missing, foreign, corrupt,
                            partial, stale), kit_id, commit, differs

Exit 2 on usage. Standard library only: run as python3 -I -S.
"""

from __future__ import annotations

import argparse
import ast
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

SCHEMA = 1
ENTRY = "live_move_repair.py"
KEEP_S = 7 * 86400  # leftovers (.tmp, .stale, a foreign `current` set aside) live a week
# A hung git (a wedged filesystem) must not hold a deploy's update.lock for good;
# the guard bounds its own git calls the same way.
GIT_TIMEOUT_S = 120
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_KID = re.compile(r"[0-9a-f]{16}\Z")  # only these are kits: the prune never touches another name
_LINK = re.compile(r"\.\./live-repair\.d/([0-9a-f]{16})\Z")


class KitError(Exception):
    pass


def _step(name: str) -> None:
    """A point a test can stop the install at (a kill)."""


def _say(msg: str) -> None:
    print(f"  live-repair kit: {msg}", flush=True)


def _git(repo: str, *args: str) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    cmd = ["git", "-c", "core.fsmonitor=false", "-C", repo, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, env=env, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise KitError(f"git {args[0]} could not run ({e})") from None
    if p.returncode != 0:
        raise KitError(f"git {args[0]} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def blob_oid(data: bytes, fmt: str = "sha1") -> str:
    """git's object id of a blob holding *data* (sha1 or sha256 repositories)."""
    h = hashlib.new(fmt)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def kit_id(files: dict[str, str]) -> str:
    lines = "".join(f"{name}\0{oid}\n" for name, oid in sorted(files.items()))
    return hashlib.sha256(lines.encode()).hexdigest()[:16]


def read_kit_sources(source: bytes) -> tuple[str, ...]:
    """KIT_SOURCES from live_move_repair.py's text: the one top-level
    assignment, a literal tuple of file names. Never executes the source."""
    tree = ast.parse(source)
    values = [
        n.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == "KIT_SOURCES"
    ]
    if len(values) != 1:
        raise ValueError("not one KIT_SOURCES assignment")
    names = ast.literal_eval(values[0])
    if not (
        isinstance(names, tuple)
        and all(isinstance(n, str) and _NAME.match(n) and n != "KIT.json" for n in names)
        and len(set(names)) == len(names)
        and ENTRY in names
    ):
        raise ValueError("KIT_SOURCES is not a tuple of distinct file names holding the entry")
    return names


def _read(path: str, cap: int = 1 << 20) -> bytes:
    """A regular file's bytes, never through a link, at most *cap*."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            raise OSError(errno.EINVAL, "not a regular file under the size cap", path)
        return fh.read(cap + 1)


def _fmt(oid: str) -> str:
    return "sha256" if len(oid) == 64 else "sha1"


def describe(kit: str, names, commit: str | None = None, fmt: str = "sha1") -> dict:
    """The KIT.json of the files *names* in the directory *kit*."""
    files = {n: blob_oid(_read(os.path.join(kit, n)), fmt) for n in names}
    return {
        "schema": SCHEMA,
        "kit_id": kit_id(files),
        "commit": commit,
        "files": files,
        "kit_sources": list(names),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _check(kit: str) -> tuple[tuple[str, str] | None, dict | None]:
    """(None, manifest) when every file in *kit* hashes to its KIT.json oid;
    otherwise ((state, why), manifest or None)."""
    try:
        m = json.loads(_read(os.path.join(kit, "KIT.json"), 64 << 10))
        files, names = m["files"], m["kit_sources"]
        if not (
            m.get("schema") == SCHEMA
            and isinstance(files, dict)
            and isinstance(names, list)
            and all(isinstance(n, str) and _NAME.match(n) for n in [*names, *files])
            and all(isinstance(o, str) and _OID.match(o) for o in files.values())
        ):
            raise ValueError("unexpected shape")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return ("corrupt", f"KIT.json is missing or invalid ({e})"), None
    if set(files) != set(names):
        return ("partial", "KIT.json's files are not its kit_sources"), m
    for name in names:
        path = os.path.join(kit, name)
        if not os.path.lexists(path):
            return ("partial", f"{name} is missing"), m
        try:
            data = _read(path)
        except OSError as e:
            return ("corrupt", f"{name} is unreadable ({e.strerror})"), m
        if blob_oid(data, _fmt(files[name])) != files[name]:
            return ("corrupt", f"{name} differs from its object id"), m
    return None, m


def _head(repo: str, branch: bool = True) -> tuple[str, str, tuple[str, ...], dict[str, str]]:
    """HEAD's commit, object format, KIT_SOURCES and each source's blob id."""
    if branch:
        try:
            ref = _git(repo, "symbolic-ref", "-q", "HEAD").decode().strip()
        except KitError:
            ref = "detached"
        if ref not in ("refs/heads/main", "refs/heads/live"):
            raise KitError(f"HEAD is {ref}, not main or live; the installed kit is kept")
    commit = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    fmt = _git(repo, "rev-parse", "--show-object-format").decode().strip() or "sha1"
    try:
        names = read_kit_sources(_git(repo, "cat-file", "blob", f"{commit}:scripts/lib/{ENTRY}"))
    except (KitError, ValueError, SyntaxError):
        raise KitError(
            "the kit list could not be read at HEAD; the installed kit is kept"
        ) from None
    found = {}
    for rec in _git(
        repo, "ls-tree", "-z", commit, "--", *(f"scripts/lib/{n}" for n in names)
    ).split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            mode, typ, oid = meta.decode().split()
            if typ == "blob" and mode in ("100644", "100755"):
                found[os.path.basename(os.fsdecode(path))] = oid
    missing = [n for n in names if n not in found]
    if missing:
        raise KitError(f"not a file at HEAD: {', '.join(missing)}; the installed kit is kept")
    return commit, fmt, names, {n: found[n] for n in names}


def _write(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fchmod(fh.fileno(), 0o644)
        os.fsync(fh.fileno())


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ts() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"


def _good(kit: str, files: dict[str, str]) -> bool:
    if not os.path.isdir(kit) or os.path.islink(kit):
        return False
    bad, m = _check(kit)
    return bad is None and m is not None and m["files"] == files


def _place(repo: str, vdir: str, kid: str, commit: str, fmt: str, names, files) -> None:
    """Write the kit beside the others and rename it into live-repair.d/<kid>."""
    final = os.path.join(vdir, kid)
    tmp = os.path.join(vdir, f".tmp-{kid}-{os.getpid()}")
    if os.path.lexists(tmp):
        shutil.rmtree(tmp)
    os.mkdir(tmp, 0o755)
    try:
        for name in names:
            data = _git(repo, "cat-file", "blob", files[name])
            if blob_oid(data, fmt) != files[name]:
                raise KitError(f"{name}'s bytes at HEAD do not hash to its object id")
            _write(os.path.join(tmp, name), data)
        manifest = {**describe(tmp, names, commit, fmt), "kit_id": kid}
        _write(os.path.join(tmp, "KIT.json"), json.dumps(manifest, indent=1).encode() + b"\n")
        if not _good(tmp, files):
            raise KitError("the written kit does not verify")
        _fsync_dir(tmp)
        _step("tmp-written")
        if os.path.lexists(final):
            if _good(final, files):  # another installer placed the same kit meanwhile
                shutil.rmtree(tmp)
                _fsync_dir(vdir)
                _step("dir-renamed")
                return
            # There, but it does not verify: set aside for a week, never overwritten.
            stale = os.path.join(vdir, f".stale-{kid}-{_ts()}")
            os.rename(final, stale)
            os.utime(stale, follow_symlinks=False)  # its week starts now (rename keeps mtime)
        try:
            os.rename(tmp, final)
        except OSError as e:  # a concurrent install placed the same kit first
            if e.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not _good(final, files):
                raise
            shutil.rmtree(tmp)
        _fsync_dir(vdir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _step("dir-renamed")


def _swap(ldir: str, kid: str, final: str) -> None:
    cur = os.path.join(ldir, "current")
    if os.path.lexists(cur) and not os.path.islink(cur):
        aside = os.path.join(ldir, f"current.foreign-{_ts()}")
        os.rename(cur, aside)
        os.utime(aside, follow_symlinks=False)  # its week starts now (rename keeps mtime)
        _say(f"WARNING: {cur} was not a link into live-repair.d; it was moved to {aside}")
    tmp = os.path.join(ldir, f"current.tmp.{os.getpid()}")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(f"../live-repair.d/{kid}", tmp)
    try:
        _step("link-made")
        os.replace(tmp, cur)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    os.utime(final)  # the newest kit, for the keep-2 prune
    _fsync_dir(ldir)


def _remove(path: str) -> None:
    """Remove a kit directory unless a running repair holds its KIT.json (F9)."""
    try:
        fd = os.open(os.path.join(path, "KIT.json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        fd = None
    try:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _say(f"NOTE: {os.path.basename(path)} is in use by a running repair; kept for now")
                return
        shutil.rmtree(path, ignore_errors=True)
    finally:
        if fd is not None:
            os.close(fd)


def _leftover(name: str, mtime: float, now: float) -> bool:
    """A .tmp/.stale/current.* leftover that may go: a week old, or a write
    whose process is gone (this one's own included: its write is done)."""
    if now - mtime > KEEP_S:
        return True
    m = re.fullmatch(r"\.tmp-[0-9a-f]+-(\d+)|current\.tmp\.(\d+)", name)
    if not m:
        return False
    pid = int(m.group(1) or m.group(2))
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


def _prune(vdir: str, ldir: str, kid: str) -> None:
    now = time.time()
    kits = []
    for e in os.scandir(vdir):
        st = e.stat(follow_symlinks=False)
        if e.name.startswith("."):
            if e.name.startswith((".tmp-", ".stale-")) and _leftover(e.name, st.st_mtime, now):
                _remove(e.path)
        elif e.name != kid and _KID.match(e.name) and stat.S_ISDIR(st.st_mode):
            kits.append((st.st_mtime_ns, e.path))
    for _, path in sorted(kits, reverse=True)[1:]:  # keep current's and the newest other
        _remove(path)
    for e in os.scandir(ldir):
        st = e.stat(follow_symlinks=False)
        if e.name.startswith(("current.tmp.", "current.foreign-")) and _leftover(
            e.name, st.st_mtime, now
        ):
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(e.path, ignore_errors=True)
            else:
                os.unlink(e.path)


def install(repo: str, home: str) -> int:
    try:
        if not os.path.isdir(home):
            raise KitError(f"{home} is not a directory")
        commit, fmt, names, files = _head(repo)
        kid = kit_id(files)
        root = os.path.join(home, ".genesis", "lib")
        vdir, ldir = os.path.join(root, "live-repair.d"), os.path.join(root, "live-repair")
        for d in (vdir, ldir):  # never write into, or prune, a directory elsewhere
            if os.path.islink(d):  # moving the link touches nothing it points to
                aside = f"{d}.foreign-{_ts()}"
                os.rename(d, aside)
                _say(f"WARNING: {d} was a symlink; it was moved to {aside}")
            os.makedirs(d, exist_ok=True)
        final, cur = os.path.join(vdir, kid), os.path.join(ldir, "current")
        current = os.path.islink(cur) and os.readlink(cur) == f"../live-repair.d/{kid}"
        if current and _good(final, files):
            _say(f"{kid} is current ({commit[:12]})")
        else:
            if not _good(final, files):
                _place(repo, vdir, kid, commit, fmt, names, files)
            _swap(ldir, kid, final)
            _say(f"installed {kid} from {commit[:12]}")
        _prune(vdir, ldir, kid)
        return 0
    except (KitError, OSError) as e:
        _say(f"not installed: {e}")
        return 1


def status(repo: str, home: str) -> dict:
    out: dict = {"state": "missing", "kit_id": None, "commit": None, "differs": []}
    root = os.path.join(home, ".genesis", "lib")
    cur = os.path.join(root, "live-repair", "current")
    if not os.path.lexists(cur):
        return out
    m = _LINK.match(os.readlink(cur)) if os.path.islink(cur) else None
    if m is None:
        return {**out, "state": "foreign"}
    kit = os.path.join(root, "live-repair.d", m.group(1))
    if not os.path.isdir(kit) or os.path.islink(kit):
        return out  # dangling
    bad, manifest = _check(kit)
    if manifest is not None:
        out.update(kit_id=manifest.get("kit_id"), commit=manifest.get("commit"))
    if bad is not None:
        return {**out, "state": bad[0], "why": bad[1]}
    try:
        _, _, names, files = _head(repo, branch=False)
    except KitError as e:
        return {**out, "state": "stale", "why": str(e)}
    have = manifest["files"] if manifest else {}
    out["differs"] = sorted(n for n in {*names, *have} if have.get(n) != files.get(n))
    out["state"] = "stale" if out["differs"] else "ok"
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="live_repair_kit.py", description=__doc__.split("\n")[0])
    p.add_argument("command", choices=("install", "status"))
    p.add_argument("repo")
    p.add_argument("home", help="the HOME whose .genesis/lib holds the kit (no default)")
    a = p.parse_args(argv)
    if a.command == "status":
        print(json.dumps(status(a.repo, a.home)))
        return 0
    return install(a.repo, a.home)


if __name__ == "__main__":
    sys.exit(main())
