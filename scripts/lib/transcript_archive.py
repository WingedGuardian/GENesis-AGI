"""Versioned, encrypted Claude Code source archives (stdlib only)."""

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def object_name(relative):
    return "v2-" + hashlib.sha256(os.fsencode(relative)).hexdigest() + ".tar.gpg"


def sources(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source root is missing or a symlink")
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_raise):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            eligible = name.endswith(".jsonl") or (
                name.startswith("agent-") and name.endswith(".meta.json")
            )
            if eligible and stat.S_ISREG(path.lstat().st_mode):
                yield path


def _raise(error):
    raise error


def _identity(st):
    return st.st_dev, st.st_ino


def capture(source, relative, target):
    """Capture the initially observed prefix; appends are allowed, rewrites fail."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb", buffering=0) as raw:
        initial = os.fstat(raw.fileno())
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError("source is not regular")
        with tempfile.TemporaryFile(dir=target.parent) as prefix:
            remaining, digest = initial.st_size, hashlib.sha256()
            while remaining:
                chunk = raw.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("source truncated during capture")
                remaining -= len(chunk)
                digest.update(chunk)
                prefix.write(chunk)
            raw.seek(0)
            remaining, verify = initial.st_size, hashlib.sha256()
            while remaining:
                chunk = raw.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("source truncated during verification")
                remaining -= len(chunk)
                verify.update(chunk)
            current = source.lstat()
            if (
                _identity(initial) != _identity(current)
                or current.st_size < initial.st_size
                or verify.digest() != digest.digest()
            ):
                raise ValueError("source changed inside captured prefix")
            # Metadata cannot grow incrementally like JSONL records.
            if source.name.endswith(".meta.json") and (
                initial.st_size != current.st_size or initial.st_mtime_ns != current.st_mtime_ns
            ):
                raise ValueError("metadata changed during capture")
            info = tarfile.TarInfo(relative)
            info.size, info.mode = initial.st_size, 0o600
            info.mtime = initial.st_mtime_ns / 1_000_000_000
            info.pax_headers = {
                "genesis.mtime_ns": str(initial.st_mtime_ns),
                "genesis.sha256": digest.hexdigest(),
            }
            prefix.seek(0)
            with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
                archive.addfile(info, prefix)
            return fingerprint(initial)


def crypt(source, target, password, decrypt=False):
    command = [
        "gpg",
        "--batch",
        "--yes",
        "--pinentry-mode",
        "loopback",
        "--passphrase-fd",
        "0",
        "--output",
        str(target),
    ]
    command += ["--decrypt"] if decrypt else ["--symmetric", "--cipher-algo", "AES256"]
    subprocess.run(
        command + [str(source)],
        input=password,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def fingerprint(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as incoming:
        while chunk := incoming.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def backup(root, destination, scratch, password, project=None):
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    failures, count, index_changed = [], 0, False
    index_path = destination / ".capture-index.json"
    try:
        index = json.loads(index_path.read_text())
        if not isinstance(index, dict):
            index = {}
    except (OSError, ValueError):
        index = {}
    try:
        for source in sources(root):
            relative = source.relative_to(root).as_posix()
            if project is not None and (
                source.parent != root / project or source.suffix != ".jsonl"
            ):
                continue
            try:
                target = destination / object_name(relative)
                before = fingerprint(source.lstat())
                cached = index.get(relative, {})
                if (
                    isinstance(cached, dict)
                    and cached.get("source") == before
                    and target.is_file()
                    and not target.is_symlink()
                    and cached.get("ciphertext") == checksum(target)
                    and fingerprint(source.lstat()) == before
                ):
                    count += 1
                    continue
                with tempfile.TemporaryDirectory(dir=scratch, prefix="transcript-") as temp:
                    plain = Path(temp) / "source.tar"
                    captured = capture(source, relative, plain)
                    # Ciphertext staging shares the destination filesystem for rename.
                    fd, stage = tempfile.mkstemp(dir=destination, prefix=".transcript-")
                    os.close(fd)
                    try:
                        crypt(plain, Path(stage), password)
                        with open(stage, "rb") as encrypted:
                            os.fsync(encrypted.fileno())
                        cipher_digest = checksum(Path(stage))
                        os.replace(stage, target)
                        index[relative] = {"source": captured, "ciphertext": cipher_digest}
                        index_changed = True
                    finally:
                        Path(stage).unlink(missing_ok=True)
                count += 1
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                failures.append(f"{relative}: {type(exc).__name__}")
    except (OSError, ValueError) as exc:
        failures.append(str(exc))
    if index_changed:
        try:
            fd_index, index_stage = tempfile.mkstemp(dir=destination, prefix=".index-")
            try:
                with os.fdopen(fd_index, "w") as out:
                    json.dump(index, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(index_stage, index_path)
            finally:
                Path(index_stage).unlink(missing_ok=True)
        except OSError:
            failures.append("capture index publication failed; next run will recapture")
    print(f"Transcripts: {count} sources archived; {len(failures)} incomplete", file=sys.stderr)
    for failure in failures:
        print(failure, file=sys.stderr)
    return bool(failures)


def restore(plain, root, name, force=False, dry_run=False):
    with tarfile.open(plain, "r:") as archive:
        members = archive.getmembers()
        if len(members) != 1 or not members[0].isreg():
            raise ValueError("expected exactly one regular member")
        member = members[0]
        relative = PurePosixPath(member.name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or str(relative) != member.name
            or object_name(member.name) != name
        ):
            raise ValueError("invalid archive path or object identity")
        destination = root.joinpath(*relative.parts)
        for ancestor in [root, *destination.parents]:
            if ancestor.is_symlink():
                raise ValueError("symlink in destination path")
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ValueError("destination is not regular")
        mtime = int(member.pax_headers["genesis.mtime_ns"])
        # Always validate the payload, including skipped and dry-run captures.
        digest = hashlib.sha256()
        with archive.extractfile(member) as incoming:
            while chunk := incoming.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != member.pax_headers["genesis.sha256"]:
            raise ValueError("payload digest mismatch")
        if destination.exists() and destination.stat().st_mtime_ns > mtime and not force:
            return False
        if dry_run:
            return True
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, stage = tempfile.mkstemp(dir=destination.parent, prefix=".restore-")
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as out, archive.extractfile(member) as incoming:
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if digest.hexdigest() != member.pax_headers["genesis.sha256"]:
                raise ValueError("payload digest mismatch")
            os.utime(stage, ns=(mtime, mtime))
            os.replace(stage, destination)
        finally:
            Path(stage).unlink(missing_ok=True)
        return True


def safe_target(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts or str(path) != relative:
        raise ValueError("invalid destination path")
    target = root.joinpath(*path.parts)
    if any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError("symlink in destination path")
    if target.exists() and not target.is_file():
        raise ValueError("destination is not regular")
    return target


def restore_set(
    directory,
    root,
    project,
    scratch,
    password,
    force=False,
    dry_run=False,
    selected=None,
    preferences=(),
):
    """Validate every candidate before choosing; legacy ciphertext has no source time."""
    preference = {}
    for item in preferences:
        relative, separator, kind = item.rpartition("=")
        safe_target(root, relative)
        if not separator or kind not in ("legacy", "v2") or relative in preference:
            raise ValueError("invalid or repeated transcript preference")
        preference[relative] = kind
    candidates, failures, count = {}, [], 0
    with tempfile.TemporaryDirectory(dir=scratch, prefix="transcript-set-") as temp:
        for number, source in enumerate(sorted(directory.iterdir())):
            if selected is not None and source.name not in selected:
                continue
            v2 = source.name.startswith("v2-") and source.name.endswith(".tar.gpg")
            legacy = source.name.endswith((".jsonl", ".jsonl.gpg"))
            if not (v2 or legacy):
                continue
            try:
                if source.is_symlink() or not source.is_file():
                    raise ValueError("archive is not regular")
                plain = Path(temp) / str(number)
                if source.name.endswith(".gpg"):
                    crypt(source, plain, password, decrypt=True)
                else:
                    shutil.copyfile(source, plain)
                if v2:
                    with tarfile.open(plain, "r:") as archive:
                        members = archive.getmembers()
                        if len(members) != 1 or not members[0].isreg():
                            raise ValueError("expected one regular member")
                        member = members[0]
                        relative = member.name
                        mtime = int(member.pax_headers["genesis.mtime_ns"])
                    restore(plain, root, source.name, dry_run=True)
                else:
                    relative = project + "/" + source.name.removesuffix(".gpg")
                    safe_target(root, relative)
                    # Legacy captures have no recorded source timestamp. A cache
                    # or download's mtime is not evidence of source freshness.
                    mtime = None
                candidates.setdefault(relative, []).append(
                    ("v2" if v2 else "legacy", mtime, plain, source.name)
                )
            except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError):
                failures.append(f"invalid transcript capture: {source.name}")
        for relative in preference:
            if relative not in candidates:
                failures.append(f"{relative}: preferred destination absent from capture set")
        for relative, options in candidates.items():
            try:
                chosen_kind = preference.get(relative)
                if chosen_kind:
                    options = [candidate for candidate in options if candidate[0] == chosen_kind]
                    if not options:
                        raise ValueError("preferred capture unavailable")
                if len(options) > 1 and any(option[1] is None for option in options):
                    raise ValueError("incomparable capture freshness; use --transcript-preference")
                kind, mtime, plain, name = max(options, key=lambda c: (c[1] or 0, c[0] == "v2"))
                if kind == "v2":
                    count += int(restore(plain, root, name, force, dry_run))
                    continue
                target = safe_target(root, relative)
                # Legacy ciphertext lacks a comparable timestamp: retain existing data
                # unless the operator explicitly accepts replacement with --force.
                if (
                    target.exists()
                    and not force
                    and (mtime is None or target.stat().st_mtime_ns > mtime)
                ):
                    continue
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd, stage = tempfile.mkstemp(dir=target.parent, prefix=".restore-")
                    try:
                        with os.fdopen(fd, "wb") as out, plain.open("rb") as incoming:
                            shutil.copyfileobj(incoming, out)
                            out.flush()
                            os.fsync(out.fileno())
                        if mtime is not None:
                            os.utime(stage, ns=(mtime, mtime))
                        os.replace(stage, target)
                    finally:
                        Path(stage).unlink(missing_ok=True)
                count += 1
            except (OSError, ValueError) as error:
                failures.append(f"{relative}: {error}")
    print(count)
    for failure in failures:
        print(failure, file=sys.stderr)
    return bool(failures)


def analytics_archive(source, destination, scratch, password):
    if not source.is_dir() or any(p.is_symlink() for p in [source, *source.parents]):
        raise ValueError("analytics source missing or symlinked")
    with tempfile.TemporaryDirectory(dir=scratch, prefix="analytics-backup-") as temp:
        plain = Path(temp) / "analytics.tar"
        with tarfile.open(plain, "w", format=tarfile.PAX_FORMAT) as archive:
            for directory, dirs, files in os.walk(source, onerror=_raise):
                parent = Path(directory)
                if parent == source:
                    dirs[:] = [d for d in dirs if d not in ("derived", ".staging")]
                    files = [f for f in files if f not in ("derived", ".staging")]
                for name in sorted(dirs + files):
                    path = parent / name
                    mode = path.lstat().st_mode
                    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                        raise ValueError("analytics archive contains a link or special file")
                    archive.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
        fd, stage = tempfile.mkstemp(dir=destination.parent, prefix=".analytics-")
        os.close(fd)
        try:
            crypt(plain, Path(stage), password)
            with open(stage, "rb") as encrypted:
                os.fsync(encrypted.fileno())
            os.replace(stage, destination)
        finally:
            Path(stage).unlink(missing_ok=True)


def analytics_restore(source, destination, scratch, password, force=False, dry_run=False):
    if any(p.is_symlink() for p in [destination, *destination.parents]):
        raise ValueError("symlink in analytics destination")
    if destination.exists() and not destination.is_dir():
        raise ValueError("analytics destination is not a directory")
    if destination.exists() and any(destination.iterdir()) and not force:
        raise ValueError("analytics destination exists; use --force")
    with tempfile.TemporaryDirectory(dir=scratch, prefix="analytics-restore-") as temp:
        plain = Path(temp) / "analytics.tar"
        crypt(source, plain, password, decrypt=True)
        with tarfile.open(plain, "r:") as archive:
            members = archive.getmembers()
            seen = set()
            for member in members:
                relative = PurePosixPath(member.name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                    or str(relative) != member.name
                    or member.name in seen
                    or not (member.isdir() or member.isreg())
                    or relative.parts[0] in ("derived", ".staging")
                ):
                    raise ValueError("unsafe analytics archive member")
                seen.add(member.name)
            # Validate hierarchy before any destination mutation.
            regular = {m.name for m in members if m.isreg()}
            for member in members:
                if any(str(p) in regular for p in PurePosixPath(member.name).parents):
                    raise ValueError("file used as analytics directory")
            if dry_run:
                return
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(
                dir=destination.parent, prefix=".analytics-restore-"
            ) as staging:
                stage = Path(staging) / "data"
                stage.mkdir(mode=0o700)
                for member in sorted(
                    members, key=lambda m: (not m.isdir(), len(PurePosixPath(m.name).parts))
                ):
                    target = stage / member.name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if member.isdir():
                        target.mkdir(exist_ok=True, mode=0o700)
                    else:
                        with target.open("wb") as out, archive.extractfile(member) as incoming:
                            shutil.copyfileobj(incoming, out)
                            out.flush()
                            os.fsync(out.fileno())
                        target.chmod(0o600)
                for directory, _dirs, _files in os.walk(stage, topdown=False):
                    directory_fd = os.open(directory, os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                aside = destination.with_name(destination.name + ".pre-restore-" + str(os.getpid()))
                if destination.exists():
                    if aside.exists():
                        raise ValueError("analytics aside already exists")
                    os.rename(destination, aside)
                try:
                    os.rename(stage, destination)
                except OSError:
                    if aside.exists() and not destination.exists():
                        os.rename(aside, destination)
                    raise
                directory_fd = os.open(destination.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda number, _frame: sys.exit(128 + number))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "backup",
            "restore",
            "restore-set",
            "analytics-backup",
            "analytics-restore",
            "name",
        ),
    )
    parser.add_argument("source")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--project")
    parser.add_argument("--selected", type=Path)
    parser.add_argument("--preference", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.operation == "name":
        print(object_name(args.source))
        return 0
    password = sys.stdin.buffer.read()
    if not password:
        raise ValueError("backup passphrase required")
    if args.operation == "analytics-backup":
        analytics_archive(Path(args.source), args.destination, args.scratch, password)
        return 0
    if args.operation == "analytics-restore":
        analytics_restore(
            Path(args.source), args.destination, args.scratch, password, args.force, args.dry_run
        )
        return 0
    if args.operation == "backup":
        return backup(Path(args.source), args.destination, args.scratch, password, args.project)
    if args.operation == "restore-set":
        selected = set(args.selected.read_text().splitlines()) if args.selected else None
        return restore_set(
            Path(args.source),
            args.root,
            args.project,
            args.scratch,
            password,
            args.force,
            args.dry_run,
            selected,
            args.preference,
        )
    with tempfile.TemporaryDirectory(dir=args.scratch, prefix="transcript-restore-") as temp:
        plain = Path(temp) / "source.tar"
        crypt(Path(args.source), plain, password, decrypt=True)
        changed = restore(plain, args.root, Path(args.source).name, args.force, args.dry_run)
        print(int(changed))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"Transcript archive failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
