"""Verify the installed graph-engine wheel and place the copy the unit runs.

The engine (redis-server, redis-cli, the FalkorDB module and the shared
libraries the module links against) ships as the ``falkordblite`` wheel, which
scripts/lib/falkordb_install.sh installs with ``pip --require-hashes``. pip
checks the wheel FILE once, at install time, and never again: a later
``Requirement already satisfied`` re-verifies nothing, so the installed files
can come from an unhashed install. And the wheel's paths carry no version, so a
pin bump would replace, in place, the very files a running unit loads. This
script closes both:

1. the installed ``falkordblite`` version must be the one the requirements
   file pins;
2. every engine file the COMMITTED digest list names for that version and the
   installed wheel's tag (``falkordb-engine-digests.txt``, generated from the
   pinned wheel files themselves) must exist in the install, binaries must be
   executable, and each file's sha256 must equal the committed one. The
   wheel's own RECORD is never read: it is written by whatever installed the
   wheel, so it can only check pip against itself;
3. the verified files are COPIED to ``<deps root>/<version>-<wheel tag>/``,
   keeping ``redislite/bin`` and ``falkordblite.libs`` as siblings (the
   module's RPATH is ``$ORIGIN/../../falkordblite.libs``), re-verified there,
   and given an ``engine.sha256`` (``sha256sum -c`` format, absolute paths)
   that the unit's ``ExecStartPre`` re-checks on every start. An existing copy
   whose every file matches is reused untouched; anything else is rebuilt in a
   dotted temp dir and renamed into place, so the target is either the old
   copy or a complete verified new one;
4. it prints ``KEY=value`` lines (the copy's dir, its three binaries and its
   digest file) for the shell to read.

On any failure it prints ONE reason line to stderr, exits non-zero, and leaves
the target directory as it was. Standard library only: it runs under the
venv's interpreter before anything else is known to work.

Usage:
    python -I falkordb_engine_check.py --requirements FILE --digests FILE
                                       --deps-root DIR [--site-packages DIR]
    python -I falkordb_engine_check.py --generate --requirements FILE WHEEL...

``--site-packages`` limits the distribution search to one directory (tests
point it at a fake tree); by default the interpreter's own ``sys.path`` is
searched, which for the venv python is the venv's site-packages.

``--generate`` prints a complete digest file for the given wheel files, after
checking each wheel against the requirements file's pinned hashes. Run it on
every pin bump and commit the output as falkordb-engine-digests.txt.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import re
import shutil
import stat
import sys
import zipfile
from importlib import metadata
from pathlib import Path

DIST = "falkordblite"

#: Binaries the unit runs or loads, in the order their KEY=value lines print.
BINARIES = (
    ("FALKORDB_REDIS_SERVER", "redislite/bin/redis-server"),
    ("FALKORDB_REDIS_CLI", "redislite/bin/redis-cli"),
    ("FALKORDB_MODULE", "redislite/bin/falkordb.so"),
)
_BINARY_RELS = frozenset(rel for _, rel in BINARIES)
#: Shared libraries the module's RPATH resolves ($ORIGIN/../../falkordblite.libs).
LIBS_DIR = "falkordblite.libs"
#: The directories a copy holds, and nothing else.
_COPY_DIRS = frozenset({"redislite", "redislite/bin", LIBS_DIR})
DIGEST_NAME = "engine.sha256"

# A path the unit cannot carry: systemd refuses `"`, `\` and control characters
# in an executable, sha256sum would escape them in the digest file, and systemd
# substitutes `${VAR}` / `$VAR` inside arguments (MEASURED), so `$` is out too.
_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\$]')
# A version or wheel tag that can name a directory: no separators, no dot
# prefix (dotted names are this script's temp and stale dirs).
_NAME_OK = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]*")
_LIB_NAME_OK = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]*")
_SHA_OK = re.compile(r"[0-9a-f]{64}")
# Leftovers of an interrupted build or replacement, direct children only.
_LEFTOVER = re.compile(r"\.(?P<name>.+)\.(?:tmp|stale)-(?P<pid>[0-9]+)")


class CheckFailed(Exception):
    """One human-readable reason the engine is not verified."""


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


# --- inputs ------------------------------------------------------------------


def pinned_version(requirements: Path) -> str:
    """The version the requirements file pins for falkordblite."""
    try:
        text = requirements.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckFailed(f"cannot read {requirements}: {exc.strerror}") from None
    for line in text.splitlines():
        match = re.match(r"\s*falkordblite\s*==\s*([^\s;\\]+)", line, re.IGNORECASE)
        if match:
            return match.group(1)
    raise CheckFailed(f"{requirements} pins no falkordblite version")


def pinned_wheel_hashes(requirements: Path) -> set[str]:
    text = requirements.read_text(encoding="utf-8")
    return set(re.findall(r"--hash=sha256:([0-9a-f]{64})", text))


def _valid_rel(rel: str) -> bool:
    if rel in _BINARY_RELS:
        return True
    prefix = LIBS_DIR + "/"
    return rel.startswith(prefix) and bool(_LIB_NAME_OK.fullmatch(rel[len(prefix) :]))


def read_digests(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """The committed digest list: {(version, wheel tag): {relpath: sha256}}."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckFailed(f"cannot read {path}: {exc.strerror}") from None
    table: dict[tuple[str, str], dict[str, str]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 4:
            raise CheckFailed(f"{path}:{number} is not '<version> <tag> <sha256> <path>'")
        version, tag, sha, rel = fields
        if not (_NAME_OK.fullmatch(version) and _NAME_OK.fullmatch(tag)):
            raise CheckFailed(f"{path}:{number} has an unusable version or tag")
        if not _SHA_OK.fullmatch(sha):
            raise CheckFailed(f"{path}:{number} has a malformed sha256")
        if not _valid_rel(rel):
            raise CheckFailed(f"{path}:{number} names a path outside the engine: {rel}")
        rows = table.setdefault((version, tag), {})
        if rows.get(rel, sha) != sha:
            raise CheckFailed(f"{path}:{number} lists {rel} twice with different digests")
        rows[rel] = sha
    return table


def find_distribution(site_packages: Path | None) -> metadata.Distribution:
    if site_packages is None:
        try:
            return metadata.distribution(DIST)
        except metadata.PackageNotFoundError:
            raise CheckFailed(f"{DIST} is not installed") from None
    for dist in metadata.distributions(path=[str(site_packages)]):
        name = (dist.metadata["Name"] or "").lower().replace("_", "-")
        if name == DIST:
            return dist
    raise CheckFailed(f"{DIST} is not installed in {site_packages}")


def wheel_tags(wheel_text: str | None) -> list[str]:
    """The ``Tag:`` values of a dist-info WHEEL file, in file order."""
    tags = []
    for line in (wheel_text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "Tag" and value.strip():
            tags.append(value.strip())
    return tags


def select(
    table: dict[tuple[str, str], dict[str, str]], version: str, tags: list[str]
) -> tuple[str, dict[str, str]]:
    """(wheel tag, committed {relpath: sha256}) for the installed wheel."""
    for tag in tags:
        rows = table.get((version, tag))
        if rows:
            missing = [rel for rel in sorted(_BINARY_RELS) if rel not in rows]
            if missing:
                raise CheckFailed(
                    f"the committed digests for {DIST} {version} {tag} list no {missing[0]}"
                )
            return tag, rows
    shown = " ".join(tags) if tags else "(the installed wheel names no tag)"
    raise CheckFailed(f"no committed digests for {DIST} {version} {shown}")


# --- verification ------------------------------------------------------------


def _check_file(path: Path, rel: str, sha: str, what: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise CheckFailed(f"{path} is missing") from None
    if not stat.S_ISREG(info.st_mode):
        raise CheckFailed(f"{path} is not a regular file")
    if rel in _BINARY_RELS and not os.access(path, os.X_OK):
        raise CheckFailed(f"{path} is not executable")
    if file_sha256(path) != sha:
        raise CheckFailed(f"{path} does not match the committed digest ({what})")


def verify_installed(dist: metadata.Distribution, version: str, rows: dict[str, str]) -> Path:
    """Check every committed file in the install; returns its site-packages."""
    if dist.version != version:
        raise CheckFailed(f"{DIST} {dist.version} is installed but {version} is pinned")
    site = Path(os.path.abspath(str(dist.locate_file(""))))
    for rel in sorted(rows):
        _check_file(site / rel, rel, rows[rel], "installed wheel")
    # A library the install carries that the list does not name would be loaded
    # by the module but never copied or checked: the list is incomplete.
    libs = site / LIBS_DIR
    with contextlib.suppress(FileNotFoundError, NotADirectoryError):
        for entry in sorted(os.listdir(libs)):
            if f"{LIBS_DIR}/{entry}" not in rows:
                raise CheckFailed(f"{libs / entry} is not in the committed digests")
    return site


def digest_body(target: Path, rows: dict[str, str]) -> str:
    """engine.sha256 for a copy at ``target``: sha256sum -c format, sorted."""
    return "".join(f"{rows[rel]}  {target / rel}\n" for rel in sorted(rows))


def copy_matches(target: Path, rows: dict[str, str]) -> bool:
    """True when ``target`` holds exactly the committed files, each matching,
    plus the exact digest file: the copy can be reused untouched."""
    try:
        if target.is_symlink() or not target.is_dir():
            return False
        files: set[str] = set()
        dirs: set[str] = set()
        for root, dirnames, filenames in os.walk(target):
            base = Path(root).relative_to(target)
            for name in dirnames:
                rel = (base / name).as_posix()
                if (Path(root) / name).is_symlink():
                    return False
                dirs.add(rel)
            for name in filenames:
                files.add((base / name).as_posix())
        if dirs != _COPY_DIRS or files != set(rows) | {DIGEST_NAME}:
            return False
        for rel in sorted(rows):
            _check_file(target / rel, rel, rows[rel], "copy")
        digest = target / DIGEST_NAME
        if digest.is_symlink():
            return False
        return digest.read_text(encoding="utf-8") == digest_body(target, rows)
    except (CheckFailed, OSError, UnicodeDecodeError):
        return False


# --- the copy ----------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file(path: Path, data_from: Path | None, text: str | None, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as out:
        if data_from is not None:
            with data_from.open("rb") as src:
                shutil.copyfileobj(src, out, 1 << 20)
        else:
            out.write((text or "").encode("utf-8"))
        out.flush()
        os.fchmod(out.fileno(), mode)  # the umask never narrows it
        os.fsync(out.fileno())


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_leftovers(deps_root: Path) -> None:
    """Best-effort: drop ``.<name>.tmp-<pid>`` / ``.<name>.stale-<pid>`` direct
    children whose process is gone (a crash mid-copy or mid-replacement)."""
    try:
        entries = list(deps_root.iterdir())
    except OSError:
        return
    for entry in entries:
        match = _LEFTOVER.fullmatch(entry.name)
        if not match:
            continue
        pid = int(match.group("pid"))
        if pid != os.getpid() and _pid_alive(pid):
            continue
        with contextlib.suppress(OSError):
            _remove(entry)


def place_copy(
    site: Path, deps_root: Path, name: str, rows: dict[str, str],
) -> tuple[Path, bool]:
    """The verified copy at ``deps_root/name``: reused if it matches, otherwise
    built beside it and renamed into place. Returns (target, replaced), where
    replaced means a copy already there did NOT match and was rebuilt: the
    event the digests exist to catch, so the caller reports it."""
    target = deps_root / name
    if _UNSAFE.search(str(target)):
        raise CheckFailed(f"{str(target)!r} contains characters the unit cannot carry")
    deps_root.mkdir(parents=True, exist_ok=True)
    remove_leftovers(deps_root)
    if copy_matches(target, rows):
        return target, False
    replaced = os.path.lexists(target)

    pid = os.getpid()
    tmp = deps_root / f".{name}.tmp-{pid}"
    stale = deps_root / f".{name}.stale-{pid}"
    for leftover in (tmp, stale):
        if os.path.lexists(leftover):
            _remove(leftover)
    try:
        tmp.mkdir(mode=0o755)
        for rel in sorted(_COPY_DIRS):
            (tmp / rel).mkdir(mode=0o755)
        for rel in sorted(rows):
            mode = 0o755 if rel in _BINARY_RELS else 0o644
            _write_file(tmp / rel, site / rel, None, mode)
        # Re-verify what landed, not what was read: the copy is what runs.
        for rel in sorted(rows):
            _check_file(tmp / rel, rel, rows[rel], "copy")
        _write_file(tmp / DIGEST_NAME, None, digest_body(target, rows), 0o644)
        for rel in sorted(_COPY_DIRS, reverse=True):
            _fsync_dir(tmp / rel)
        _fsync_dir(tmp)

        if os.path.lexists(target):
            os.rename(target, stale)
        try:
            os.rename(tmp, target)
        except OSError:
            if os.path.lexists(stale):
                with contextlib.suppress(OSError):
                    os.rename(stale, target)
            raise
        _fsync_dir(deps_root)
    except BaseException:
        with contextlib.suppress(OSError):
            if os.path.lexists(tmp):
                _remove(tmp)
        raise
    with contextlib.suppress(OSError):
        if os.path.lexists(stale):
            _remove(stale)
    return target, replaced


# --- generate ------------------------------------------------------------------


def generate(requirements: Path, wheels: list[Path]) -> str:
    """A complete digest file for ``wheels``, each checked against the pins."""
    version = pinned_version(requirements)
    pins = pinned_wheel_hashes(requirements)
    rows: list[tuple[str, str, str, str]] = []
    for wheel in wheels:
        if file_sha256(wheel) not in pins:
            raise CheckFailed(f"{wheel} is not one of the pinned wheel files")
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            info = [n for n in names if re.fullmatch(r"[^/]+\.dist-info/WHEEL", n)]
            meta = [n for n in names if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
            if len(info) != 1 or len(meta) != 1:
                raise CheckFailed(f"{wheel} has no single dist-info")
            got = re.search(r"^Version:\s*(\S+)", archive.read(meta[0]).decode("utf-8"), re.M)
            if not got or got.group(1) != version:
                raise CheckFailed(f"{wheel} is not {DIST} {version}")
            tags = wheel_tags(archive.read(info[0]).decode("utf-8"))
            engine = sorted(n for n in names if _valid_rel(n))
            missing = sorted(_BINARY_RELS - set(engine))
            if not tags or missing:
                raise CheckFailed(f"{wheel} has no tag or lacks {missing}")
            for tag in tags:
                for rel in engine:
                    sha = hashlib.sha256(archive.read(rel)).hexdigest()
                    rows.append((version, tag, sha, rel))
    header = (
        "# The graph engine's files, by sha256, for every pinned falkordblite wheel.\n"
        "# One line per file: <version> <wheel tag> <sha256> <path in the wheel>.\n"
        "#\n"
        "# scripts/lib/falkordb_engine_check.py checks the installed wheel against\n"
        "# these lines (never against the wheel's own RECORD, which whatever installed\n"
        "# the wheel wrote) before it copies the engine to the directory the unit runs.\n"
        "#\n"
        "# Generated from the wheel files themselves, each first checked against the\n"
        "# hashes in falkordb-requirements.txt. Regenerate on EVERY pin bump, from the\n"
        "# new pin's wheel files (one per tag the pin lists a hash for):\n"
        "#\n"
        "#   python3 -I scripts/lib/falkordb_engine_check.py --generate \\\n"
        "#       --requirements scripts/lib/falkordb-requirements.txt <dir>/*.whl \\\n"
        "#       > scripts/lib/falkordb-engine-digests.txt\n"
    )
    return header + "".join(
        f"{v} {t} {s} {r}\n"
        for v, t, s, r in sorted(rows, key=lambda row: (row[0], row[1], row[3]))
    )


# --- entry point -------------------------------------------------------------


def run(args: argparse.Namespace) -> list[tuple[str, Path]]:
    version = pinned_version(args.requirements)
    table = read_digests(args.digests)
    dist = find_distribution(args.site_packages)
    if dist.version != version:
        raise CheckFailed(f"{DIST} {dist.version} is installed but {version} is pinned")
    tag, rows = select(table, version, wheel_tags(dist.read_text("WHEEL")))
    site = verify_installed(dist, version, rows)
    deps_root = Path(os.path.abspath(str(args.deps_root)))
    target, replaced = place_copy(site, deps_root, f"{version}-{tag}", rows)
    out = [("FALKORDB_ENGINE_DIR", target)]
    if replaced:
        # Not a path: one flag line the shell reads like the others.
        out.append(("FALKORDB_REPLACED", "1"))
    out += [(key, target / rel) for key, rel in BINARIES]
    out.append(("FALKORDB_DIGESTS", target / DIGEST_NAME))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--digests", type=Path)
    parser.add_argument("--deps-root", type=Path)
    parser.add_argument("--site-packages", type=Path, default=None)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("wheels", nargs="*", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.generate:
            if not args.wheels:
                parser.error("--generate needs the wheel files")
            sys.stdout.write(generate(args.requirements, args.wheels))
            return 0
        if args.digests is None or args.deps_root is None or args.wheels:
            parser.error("--digests and --deps-root are required, and take no wheels")
        resolved = run(args)
    except (CheckFailed, OSError) as exc:
        print(f"graph engine check failed: {exc}", file=sys.stderr)
        return 1
    for key, path in resolved:
        print(f"{key}={path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
