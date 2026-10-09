"""Durable immutable source bodies and one atomic source selector.

Callers hold the writer lease. Destructive cleanup additionally holds the
exclusive publication lease; visibility alone never authorizes retirement.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
import uuid
from pathlib import Path

from genesis.transcript_analytics import catalog


class Failed(RuntimeError):
    """Publication could not establish a durable selector; stop this run."""


class Uncertain(Failed):
    """Replace succeeded but directory durability failed; retain every body."""


def fsync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_directory(path: Path):
    """Create and fence every previously missing ancestor entry."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise Failed("publication directory is not a regular directory")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    # A previous attempt can have created an ancestor then failed its fsync.
    # Existence after retry/restart is not proof of durability. Fence the actual
    # complete entry chain, preserving already-resolved directory aliases.
    resolved = path.resolve(strict=True)
    for directory in (resolved, *resolved.parents):
        if directory.parent == directory:
            break
        fsync_directory(directory.parent)


def publish(data: Path, text: str) -> catalog.Catalog:
    """Validate, file-fence, replace, directory-fence one complete selector."""
    try:
        selected = catalog.parse(text)
    except catalog.Unavailable as exc:
        raise Failed("source selector candidate failed validation") from exc
    temporary = data / ".staging" / f"catalog.{os.getpid()}.{uuid.uuid4().hex}.json"
    replaced = False
    try:
        make_directory(temporary.parent)
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, data / catalog.FILENAME)
        replaced = True
        fsync_directory(data)
    except OSError as exc:
        if replaced:
            failure = Uncertain("source selector publication durability is uncertain")
            try:
                visible = catalog.load(data)
            except catalog.Unavailable:
                visible = None
            # Readback describes visibility only. It never authorizes cleanup
            # or continued publication after the failed durability fence.
            failure.visible_revision = visible.revision if visible is not None else None
            failure.visible_population_revision = (
                visible.population_revision if visible is not None else None
            )
            failure.add_note(
                f"visible source selector revision: {failure.visible_revision or 'unavailable'}; all bodies retained"
            )
            raise failure from exc
        raise Failed("source selector publication failed") from exc
    # Failed private candidates remain for fenced collect. Successful replace
    # already consumed the temporary pathname.
    return selected


def finalize(data: Path, staging: Path, key: str, generation: str) -> Path:
    """Make a validated immutable generation durable before referencing it."""
    if catalog.load(data) is None:
        raise Failed("generation finalization requires a durable bootstrap selector")
    # The selector parser checks references; validate the same restricted names
    # before creating a path even when this body is not yet selected.
    if not catalog._KEY.fullmatch(key) or not catalog._REVISION.fullmatch(generation):
        raise Failed("invalid source generation identity")
    target = data / "sources" / key / generation
    make_directory(target.parent)
    fsync_directory(staging)
    if target.exists() or target.is_symlink():
        raise Failed("immutable generation already exists")
    os.rename(staging, target)
    fsync_directory(target.parent)
    fsync_directory(staging.parent)
    return target


def durable_selector(data: Path) -> catalog.Catalog:
    """Fence the visible selector before ANY body deletion, including restart."""
    selected = catalog.load(data)
    if selected is None:
        raise Failed("source cleanup requires an authoritative selector")
    descriptor = os.open(data / catalog.FILENAME, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(data)
    return selected


def collect(data: Path, tables) -> bool:
    """One bounded pass over only recognized, unreferenced regular bodies.

    Return False and retain everything if the selector fence fails. Callers
    must own both writer and exclusive publication leases throughout.
    """
    try:
        selected = durable_selector(data)
    except (OSError, Failed, catalog.Unavailable):
        return False
    expected = {f"{table}.parquet" for table in tables}
    staging = data / ".staging"
    if staging.is_dir() and not staging.is_symlink():
        staged_sources = staging / "sources"
        if staged_sources.is_dir() and not staged_sources.is_symlink():
            for body in staged_sources.iterdir():
                if (
                    body.is_symlink()
                    or not body.is_dir()
                    or not catalog._REVISION.fullmatch(body.name)
                ):
                    continue
                if any(
                    path.name not in expected or not stat.S_ISREG(path.lstat().st_mode)
                    for path in body.iterdir()
                ):
                    continue
                shutil.rmtree(body)
                fsync_directory(staged_sources)
        legacy_temp = re.compile(
            r"(?:"
            + "|".join(re.escape(table) for table in tables)
            + r")__[0-9a-f]{16}\.[0-9]+\.tmp\Z"
        )
        catalog_temp = re.compile(r"catalog\.[0-9]+\.[0-9a-f]{32}\.json\Z")
        cutoff = time.time() - 3600
        for path in staging.iterdir():
            information = path.lstat()
            if (
                stat.S_ISREG(information.st_mode)
                and information.st_mtime < cutoff
                and (legacy_temp.fullmatch(path.name) or catalog_temp.fullmatch(path.name))
            ):
                path.unlink()
        fsync_directory(staging)
    namespace = data / "sources"
    if namespace.is_dir() and not namespace.is_symlink():
        for source in namespace.iterdir():
            if (
                source.is_symlink()
                or not source.is_dir()
                or not catalog._KEY.fullmatch(source.name)
            ):
                continue
            for body in source.iterdir():
                if (
                    body.is_symlink()
                    or not body.is_dir()
                    or not catalog._REVISION.fullmatch(body.name)
                    or selected.sources.get(source.name) == body.name
                ):
                    continue
                entries = list(body.iterdir())
                if any(
                    p.name not in expected or not stat.S_ISREG(p.lstat().st_mode) for p in entries
                ):
                    continue
                shutil.rmtree(body)
                fsync_directory(source)
    for table in tables:
        for body in data.glob(f"{table}__*.parquet"):
            key = body.name[len(table) + 2 : -len(".parquet")]
            if (
                catalog._KEY.fullmatch(key)
                and selected.sources.get(key) != "legacy"
                and stat.S_ISREG(body.lstat().st_mode)
            ):
                body.unlink()
    fsync_directory(data)
    return True
