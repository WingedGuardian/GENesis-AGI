"""Versioned, encrypted Claude Code source archives (stdlib only)."""

import argparse
import hashlib
import os
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def object_name(relative):
    return "v2-" + hashlib.sha256(relative.encode()).hexdigest() + ".tar.gpg"


def sources(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source root is missing or a symlink")
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_raise):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            eligible = name.endswith(".jsonl") or (
                name.startswith("agent-") and name.endswith(".meta.json"))
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
            if (_identity(initial) != _identity(current) or current.st_size < initial.st_size
                    or verify.digest() != digest.digest()):
                raise ValueError("source changed inside captured prefix")
            # Metadata cannot grow incrementally like JSONL records.
            if source.name.endswith(".meta.json") and (
                    initial.st_size != current.st_size or initial.st_mtime_ns != current.st_mtime_ns):
                raise ValueError("metadata changed during capture")
            info = tarfile.TarInfo(relative)
            info.size, info.mode = initial.st_size, 0o600
            info.mtime = initial.st_mtime_ns / 1_000_000_000
            info.pax_headers = {"genesis.mtime_ns": str(initial.st_mtime_ns),
                                "genesis.sha256": digest.hexdigest()}
            prefix.seek(0)
            with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
                archive.addfile(info, prefix)


def crypt(source, target, password, decrypt=False):
    command = ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
               "--passphrase-fd", "0", "--output", str(target)]
    command += ["--decrypt"] if decrypt else ["--symmetric", "--cipher-algo", "AES256"]
    subprocess.run(command + [str(source)], input=password, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def backup(root, destination, scratch, password):
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    failures, count = [], 0
    try:
        for source in sources(root):
            relative = source.relative_to(root).as_posix()
            try:
                with tempfile.TemporaryDirectory(dir=scratch, prefix="transcript-") as temp:
                    plain = Path(temp) / "source.tar"
                    capture(source, relative, plain)
                    # Ciphertext staging shares the destination filesystem for rename.
                    fd, stage = tempfile.mkstemp(dir=destination, prefix=".transcript-")
                    os.close(fd)
                    try:
                        crypt(plain, Path(stage), password)
                        with open(stage, "rb") as encrypted:
                            os.fsync(encrypted.fileno())
                        os.replace(stage, destination / object_name(relative))
                    finally:
                        Path(stage).unlink(missing_ok=True)
                count += 1
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                failures.append(f"{relative}: {type(exc).__name__}")
    except (OSError, ValueError) as exc:
        failures.append(str(exc))
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
        if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                or str(relative) != member.name or object_name(member.name) != name):
            raise ValueError("invalid archive path or object identity")
        destination = root.joinpath(*relative.parts)
        for ancestor in [root, *destination.parents]:
            if ancestor.is_symlink():
                raise ValueError("symlink in destination path")
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ValueError("destination is not regular")
        mtime = int(member.pax_headers["genesis.mtime_ns"])
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


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda number, _frame: sys.exit(128 + number))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore", "name"))
    parser.add_argument("source")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.operation == "name":
        print(object_name(args.source))
        return 0
    password = sys.stdin.buffer.read()
    if not password:
        raise ValueError("backup passphrase required")
    if args.operation == "backup":
        return backup(Path(args.source), args.destination, args.scratch, password)
    with tempfile.TemporaryDirectory(dir=args.scratch, prefix="transcript-restore-") as temp:
        plain = Path(temp) / "source.tar"
        crypt(Path(args.source), plain, password, decrypt=True)
        changed = restore(plain, args.root, Path(args.source).name, args.force, args.dry_run)
        print(int(changed))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f"Transcript archive failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
