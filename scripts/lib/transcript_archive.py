"""Versioned, encrypted Claude Code source archives (stdlib only)."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath


def object_name(relative):
    return "v2-" + hashlib.sha256(os.fsencode(relative)).hexdigest() + ".tar.gpg"


def sources(root, recursive=True):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source root is missing or a symlink")
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_raise):
        dirs[:] = sorted(d for d in dirs if recursive and not (Path(directory) / d).is_symlink())
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
    if not password:
        raise ValueError("backup passphrase required for encrypted payloads")
    if b"\n" in password or b"\0" in password:
        raise ValueError("backup passphrase must be a single line without a terminator")
    if decrypt:
        # Load the standalone recovery boundary without importing Genesis's
        # package or requiring a functioning application environment.
        name = "_genesis_backup_decryption"
        module = sys.modules.get(name)
        if module is None:
            path = Path(__file__).resolve().parents[2] / "src/genesis/guardian/cred_integrity.py"
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        try:
            module.decrypt_backup_file(source, target, password)
        except module._DecryptError as exc:
            # Preserve the original archive API for genuine native failures;
            # an exit-zero message rejected by authentication stays ValueError.
            if exc.returncode is not None:
                raise subprocess.CalledProcessError(exc.returncode, exc.command) from exc
            raise
        return
    command = [
        "gpg",
        "--batch",
        "--yes",
        "--no-symkey-cache",
        "--pinentry-mode",
        "loopback",
        "--passphrase-fd",
        "0",
        "--output",
        str(target),
    ]
    command += ["--symmetric", "--cipher-algo", "AES256"]
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


_INDEX_NAME = ".capture-index.enc"
_CHECKPOINT_OBJECTS = 128
_CHECKPOINT_SECONDS = 60


def _fsync_directory(directory):
    fd = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_directory(directory):
    """Fence the complete entry chain, including directories created on retry."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _fence_directory_entries(directory)


def _fence_directory_entries(directory):
    """Fence ancestors without widening an existing-directory precondition."""
    resolved = directory.resolve(strict=True)
    for entry in (resolved, *resolved.parents):
        if entry.parent == entry:
            break
        _fsync_directory(entry.parent)


def _capture_timestamp(raw):
    """Use the platform seconds range used by CPython's os.utime(ns=...)."""
    value = int(raw)
    width = sysconfig.get_config_var("SIZEOF_TIME_T")
    if type(width) is not int or width <= 0:
        raise ValueError("capture timestamp platform range unavailable")
    limit = 1 << (width * 8 - 1)
    if not -limit <= value // 1_000_000_000 < limit:
        raise ValueError("capture timestamp outside platform range")
    return value


class CaptureCheckpointError(RuntimeError):
    """Publication failure stops this invocation; ordinary object failures do not."""


class CaptureCheckpoint:
    def __init__(self, directory, scratch, password, document, authenticated):
        self.directory, self.scratch, self.password = directory, scratch, password
        self.document = document
        self.dirty = not authenticated
        self.pending = 0
        self.published = time.monotonic()
        self.first_authorization_published = False

    def changed(self, *, authorization=True):
        self.dirty = True
        self.pending += int(authorization)
        if (
            (authorization and not self.first_authorization_published)
            or self.pending >= _CHECKPOINT_OBJECTS
            or time.monotonic() - self.published >= _CHECKPOINT_SECONDS
        ):
            # Preserve progress even when the next object exceeds the remaining
            # invocation window. Invalidation and failed publication cannot
            # consume this first successful authorization checkpoint.
            self.flush()
            if authorization:
                self.first_authorization_published = True

    def flush(self):
        if not self.dirty:
            return
        try:
            publish_capture_index(self.directory, self.scratch, self.password, self.document)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            raise CaptureCheckpointError("capture index checkpoint publication failed") from exc
        self.dirty, self.pending = False, 0
        self.published = time.monotonic()


_VALIDATION_CONTRACT = (
    f"restore-v3-passphrase-auth-time_t-{sysconfig.get_config_var('SIZEOF_TIME_T')}-"
    f"{sys.getfilesystemencoding()}-{sys.getfilesystemencodeerrors()}"
)


def _empty_index():
    return {"version": 1, "sources": {}, "captures": {},
            "validation_contract": _VALIDATION_CONTRACT, "validated_captures": {}}


def _validation_authority(document, checkpoint=None):
    """Invalidate validation optimization without discarding retained inventory."""
    validated = document.get("validated_captures")
    if (document.get("validation_contract") != _VALIDATION_CONTRACT
            or not isinstance(validated, dict)):
        document["validation_contract"] = _VALIDATION_CONTRACT
        document["validated_captures"] = validated = {}
        if checkpoint is not None:
            checkpoint.changed(authorization=False)
    return validated


def capture_index(directory, scratch, password):
    """Only a current-passphrase authenticated index can authorize reuse."""
    path = directory / _INDEX_NAME
    try:
        if not path.exists() and not path.is_symlink():
            return _empty_index(), False
        if path.is_symlink() or not path.is_file():
            raise ValueError("retained capture index is not a regular file")
        with tempfile.TemporaryDirectory(dir=scratch, prefix="capture-index-") as temp:
            plain = Path(temp) / "index.json"
            crypt(path, plain, password, decrypt=True)
            document = json.loads(plain.read_text())
        if (
            not isinstance(document, dict)
            or type(document.get("version")) is not int
            or document["version"] != 1
            or not isinstance(document.get("sources"), dict)
            or not isinstance(document.get("captures"), dict)
        ):
            raise ValueError("invalid capture index")
        for name, digest in document["captures"].items():
            if not _CAPTURE_NAME.fullmatch(name) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("invalid capture attestation")
        for relative, entry in document["sources"].items():
            path = PurePosixPath(relative)
            if (
                "\0" in relative
                or path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or str(path) != relative
                or not isinstance(entry, dict)
                or not isinstance(entry.get("source"), list)
                or len(entry["source"]) != 5
                or any(type(value) is not int for value in entry["source"])
                or not isinstance(entry.get("ciphertext"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", entry["ciphertext"])
            ):
                raise ValueError("invalid capture source fingerprint")
    except (OSError, ValueError, TypeError, subprocess.CalledProcessError):
        # An existing unreadable index may name vanished captures. Enumerating
        # today's objects cannot recover that history, so preserve the original
        # authority and refuse publication until recovery or explicit retirement.
        raise ValueError("retained capture history is unreadable; backup incomplete") from None
    # A previous rename can be visible despite its postreplace fence failing.
    # Retry that exact directory fence before granting cached authority. Keep
    # this outside the unreadable-index fallback: a fence error is not empty
    # history and must stop the caller rather than discard retained inventory.
    _fsync_directory(directory)
    return document, True


def publish_capture_index(directory, scratch, password, document):
    """Publish after ciphertext artifacts; interruption loses optimization only."""
    _fence_directory_entries(directory)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="capture-index-") as temp:
        plain = Path(temp) / "index.json"
        with plain.open("w") as out:
            json.dump(document, out)
        fd, stage = tempfile.mkstemp(dir=directory, prefix=".index-")
        os.close(fd)
        try:
            crypt(plain, Path(stage), password)
            with open(stage, "rb") as encrypted:
                os.fsync(encrypted.fileno())
            os.replace(stage, directory / _INDEX_NAME)
            _fsync_directory(directory)
        finally:
            Path(stage).unlink(missing_ok=True)


def _attest_capture(source, scratch, password, document, checkpoint=None):
    """Grant digest-bound current validation only after stable bytes and fences."""
    validated = _validation_authority(document, checkpoint)
    if source.is_symlink() or not source.is_file() or not _CAPTURE_NAME.fullmatch(source.name):
        raise ValueError("invalid capture name/type")
    before = fingerprint(source.stat())
    digest = checksum(source)
    if fingerprint(source.stat()) != before:
        raise ValueError("capture changed during validation")
    if document["captures"].get(source.name) == digest and validated.get(source.name) == digest:
        return False
    invalidated = validated.pop(source.name, None)
    if invalidated is not None and checkpoint is not None:
        checkpoint.changed(authorization=False)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="capture-check-") as temp:
        plain = Path(temp) / "capture"
        crypt(source, plain, password, decrypt=True)
        if source.name.startswith("v2-"):
            restore(plain, Path(temp) / "validation", source.name, dry_run=True)
    if fingerprint(source.stat()) != before or checksum(source) != digest:
        raise ValueError("capture changed during authentication")
    with source.open("rb") as encrypted:
        os.fsync(encrypted.fileno())
    _fsync_directory(source.parent)
    document["captures"][source.name] = digest
    validated[source.name] = digest
    if checkpoint is not None:
        checkpoint.changed()
    return True


def attest_captures(directory, scratch, password, document, checkpoint=None):
    """Validate every pooled capture, including legacy and vanished sources."""
    _fence_directory_entries(directory)
    # Direct callers may supply an index already read before a failed publish.
    # One entry fence covers cached reuse without a per-object fsync loop.
    _fsync_directory(directory)
    _validation_authority(document, checkpoint)
    changed, failures = False, []
    candidates = sorted(directory.glob("*.gpg"))
    present = {source.name for source in candidates}
    failures.extend(
        f"{name}: authenticated retained capture missing"
        for name in sorted(document["captures"].keys() - present)
    )
    for source in candidates:
        try:
            changed = _attest_capture(source, scratch, password, document, checkpoint) or changed
        except (OSError, ValueError, OverflowError, KeyError, tarfile.TarError, subprocess.CalledProcessError):
            failures.append(f"{source.name}: not authenticated and validated by current contract")
    return changed, failures


def backup(root, destination, scratch, password, project=None):
    _durable_directory(destination)
    failures, count = [], 0
    document, authenticated = capture_index(destination, scratch, password)
    index = document["sources"]
    checkpoint = CaptureCheckpoint(destination, scratch, password, document, authenticated)
    checkpoint_failed = False
    try:
        if project is not None and (
            not project
            or PurePosixPath(project).parts != (project,)
            or project in (".", "..")
            or "\0" in project
        ):
            raise ValueError("project must be one directory name")
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError("source root is not a directory or is a symlink")
        discovery_root = root / project if project is not None else root
        if not discovery_root.exists() and not discovery_root.is_symlink():
            discovered = ()  # no transcripts yet; retained captures still require attestation
        else:
            discovered = sources(discovery_root, recursive=project is None)
        for source in discovered:
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
                    and document["captures"].get(target.name) == cached.get("ciphertext")
                    and fingerprint(source.lstat()) == before
                ):
                    try:
                        _attest_capture(target, scratch, password, document, checkpoint)
                    except (OSError, ValueError, OverflowError, KeyError, tarfile.TarError, subprocess.CalledProcessError):
                        pass  # recapture only this already selected, extant source
                    else:
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
                        _fsync_directory(destination)
                        index[relative] = {"source": captured, "ciphertext": cipher_digest}
                        document["captures"][target.name] = cipher_digest
                        _validation_authority(document, checkpoint)[target.name] = cipher_digest
                        checkpoint.changed()
                    finally:
                        Path(stage).unlink(missing_ok=True)
                count += 1
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                failures.append(f"{relative}: {type(exc).__name__}")
    except CaptureCheckpointError as exc:
        checkpoint_failed = True
        failures.append(str(exc))
    except (OSError, ValueError) as exc:
        failures.append(str(exc))
    if not checkpoint_failed:
        try:
            _, invalid = attest_captures(destination, scratch, password, document, checkpoint)
            failures.extend(invalid)
            checkpoint.flush()
        except CaptureCheckpointError as exc:
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
        if (
            "\0" in member.name
            or relative.is_absolute()
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
        mtime = _capture_timestamp(member.pax_headers["genesis.mtime_ns"])
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
        _durable_directory(destination.parent)
        fd, stage = tempfile.mkstemp(dir=destination.parent, prefix=".restore-")
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as out, archive.extractfile(member) as incoming:
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                if digest.hexdigest() != member.pax_headers["genesis.sha256"]:
                    raise ValueError("payload digest mismatch")
                os.utime(out.fileno(), ns=(mtime, mtime))
                os.fsync(out.fileno())
            os.replace(stage, destination)
            _fsync_directory(destination.parent)
        finally:
            Path(stage).unlink(missing_ok=True)
        return True


def safe_target(root, relative):
    path = PurePosixPath(relative)
    if "\0" in relative or path.is_absolute() or ".." in path.parts or not path.parts or str(path) != relative:
        raise ValueError("invalid destination path")
    target = root.joinpath(*path.parts)
    if any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError("symlink in destination path")
    if target.exists() and not target.is_file():
        raise ValueError("destination is not regular")
    return target


def validate_preferences(preferences):
    """Validate operator input without touching any restore destination."""
    result = {}
    for item in preferences:
        relative, separator, kind = item.rpartition("=")
        path = PurePosixPath(relative)
        if (
            not separator
            or kind not in ("legacy", "v2")
            or relative in result
            or "\0" in relative
            or path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or str(path) != relative
        ):
            raise ValueError("invalid or repeated transcript preference")
        result[relative] = kind
    return result


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
    preference = validate_preferences(preferences)
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
                        mtime = _capture_timestamp(member.pax_headers["genesis.mtime_ns"])
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
            except (OSError, ValueError, OverflowError, KeyError, tarfile.TarError, subprocess.CalledProcessError):
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
                    _durable_directory(target.parent)
                    fd, stage = tempfile.mkstemp(dir=target.parent, prefix=".restore-")
                    try:
                        with os.fdopen(fd, "wb") as out, plain.open("rb") as incoming:
                            shutil.copyfileobj(incoming, out)
                            out.flush()
                            if mtime is not None:
                                os.utime(out.fileno(), ns=(mtime, mtime))
                            os.fsync(out.fileno())
                        os.replace(stage, target)
                        _fsync_directory(target.parent)
                    finally:
                        Path(stage).unlink(missing_ok=True)
                count += 1
            except (OSError, ValueError, OverflowError) as error:
                failures.append(f"{relative}: {error}")
    print(count)
    for failure in failures:
        print(failure, file=sys.stderr)
    return bool(failures)












_POOL_OBJECT = re.compile(r"([0-9]+)-([a-f0-9]{64})\.gpg\Z")
_CAPTURE_NAME = re.compile(r"(?:v2-[a-f0-9]{64}\.tar|[A-Za-z0-9._-]+\.jsonl)\.gpg\Z")
_POOL_PARTIAL = re.compile(r"\.partial-([0-9]+)-[0-9]+-[0-9]+-[0-9]+\Z")


def pool_rows(document, snapshot):
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document.get("version") != 1
        or document.get("snapshot") != snapshot
        or not isinstance(document.get("captures"), list)
    ):
        raise ValueError("invalid or mismatched transcript manifest")
    seen = set()
    rows = []
    for row in document["captures"]:
        if (
            not isinstance(row, list)
            or len(row) != 3
            or not isinstance(row[0], str)
            or not _CAPTURE_NAME.fullmatch(row[0])
            or row[0] in seen
            or not isinstance(row[1], str)
            or not _POOL_OBJECT.fullmatch(row[1])
            or type(row[2]) is not int
            or row[2] < 0
        ):
            raise ValueError("invalid transcript manifest entry")
        seen.add(row[0])
        rows.append(row)
    return rows


def pool_manifest(directory, target, snapshot, scratch, password):
    document, authenticated = capture_index(directory, scratch, password)
    checkpoint = CaptureCheckpoint(directory, scratch, password, document, authenticated)
    _, failures = attest_captures(directory, scratch, password, document, checkpoint)
    # Successful enrollment remains resumable even when another capture is unreadable.
    checkpoint.flush()
    if failures:
        raise ValueError("; ".join(failures))
    rows = []
    for source in sorted(directory.glob("*.gpg")):
        if source.is_symlink() or not source.is_file() or not _CAPTURE_NAME.fullmatch(source.name):
            raise ValueError("invalid local transcript capture")
        before = fingerprint(source.stat())
        digest = checksum(source)
        if (document["captures"].get(source.name) != digest
                or _validation_authority(document).get(source.name) != digest):
            raise ValueError("capture changed after authentication/validation")
        object_id = f"{before[3]}-{digest}.gpg"
        if fingerprint(source.stat()) != before:
            raise ValueError("capture changed while building manifest")
        rows.append([source.name, object_id, before[2]])
    document = {"version": 1, "snapshot": snapshot, "captures": rows}
    pool_rows(document, snapshot)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="transcript-manifest-") as temp:
        plain = Path(temp) / "manifest.json"
        plain.write_text(json.dumps(document))
        crypt(plain, target, password)
    return rows


def pool_read(source, snapshot, scratch, password):
    with tempfile.TemporaryDirectory(dir=scratch, prefix="transcript-manifest-") as temp:
        plain = Path(temp) / "manifest.json"
        crypt(source, plain, password, decrypt=True)
        return pool_rows(json.loads(plain.read_text()), snapshot)


def pool_verify(source, object_id, size):
    match = _POOL_OBJECT.fullmatch(object_id)
    if (
        match is None
        or source.is_symlink()
        or not source.is_file()
        or source.stat().st_size != size
        or checksum(source) != match.group(2)
    ):
        raise ValueError("transcript pooled object checksum/size mismatch")


def pool_missing(references, objects):
    existing = set(objects.read_text().splitlines())
    if any(
        not (_POOL_OBJECT.fullmatch(name) or _POOL_PARTIAL.fullmatch(name)) for name in existing
    ):
        raise ValueError("unknown pooled object")
    rows = [line.split("\t") for line in references.read_text().splitlines()]
    if any(len(row) != 3 or not _POOL_OBJECT.fullmatch(row[1]) for row in rows):
        raise ValueError("invalid pooled inventory")
    return [row for row in rows if row[1] not in existing]


def pool_gc(references, objects, now=None):
    # Caller authenticates every retained/incomplete manifest before invoking this.
    protected = set()
    for line in references.read_text().splitlines():
        row = line.split("\t")
        if len(row) != 3 or not _POOL_OBJECT.fullmatch(row[1]):
            raise ValueError("invalid GC reference inventory")
        protected.add(row[1])
    cutoff = int(((time.time() if now is None else now) - 7 * 86400) * 1_000_000_000)
    names = objects.read_text().splitlines()
    # Unknown objects imply a protocol/inspection mismatch: abort the entire sweep.
    if any(not (_POOL_OBJECT.fullmatch(name) or _POOL_PARTIAL.fullmatch(name)) for name in names):
        raise ValueError("unknown pooled object; refusing GC")
    expired = []
    for name in names:
        if name.startswith(".partial-"):
            stamp = _POOL_PARTIAL.fullmatch(name)
            if stamp and int(stamp.group(1)) * 1_000_000_000 < cutoff:
                expired.append(name)
        elif name not in protected and int(_POOL_OBJECT.fullmatch(name).group(1)) < cutoff:
            expired.append(name)
    return expired


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
            "name",
            "validate-preferences",
            "pool-manifest",
            "pool-read",
            "pool-verify",
            "pool-gc",
            "pool-missing",
        ),
    )
    parser.add_argument("source")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--project")
    parser.add_argument("--snapshot")
    parser.add_argument("--object-id")
    parser.add_argument("--size", type=int)
    parser.add_argument("--selected", type=Path)
    parser.add_argument("--preference", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.operation == "name":
        print(object_name(args.source))
        return 0
    if args.operation == "validate-preferences":
        preferences = validate_preferences(args.preference)
        if args.root is not None:
            for relative in preferences:
                safe_target(args.root, relative)
        return 0
    if args.operation == "pool-verify":
        pool_verify(Path(args.source), args.object_id, args.size)
        return 0
    if args.operation == "pool-missing":
        for row in pool_missing(Path(args.source), args.selected):
            print("\t".join(row))
        return 0
    if args.operation == "pool-gc":
        for name in pool_gc(Path(args.source), args.selected):
            print(name)
        return 0
    password = sys.stdin.buffer.read()
    if args.operation in ("pool-manifest", "pool-read"):
        if args.operation == "pool-manifest":
            rows = pool_manifest(
                Path(args.source), args.destination, args.snapshot, args.scratch, password
            )
        else:
            rows = pool_read(Path(args.source), args.snapshot, args.scratch, password)
        for row in rows:
            print("\t".join(map(str, row)))
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
        CaptureCheckpointError,
        OSError,
        ValueError,
        OverflowError,
        KeyError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"Transcript archive failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
