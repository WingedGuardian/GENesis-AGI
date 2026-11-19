"""Complete, identity-bound GitHub sources for independent verification review.

This acquires review inputs; it neither judges claims nor writes the ledger.
GitHub checks govern merge-time state, not this install's private measurements.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from genesis.util.atomic import atomic_write_text

MAX_DIFF = 8 * 1024 * 1024
MAX_METADATA = 4 * 1024 * 1024
MAX_FILES = 3000
ACQUISITION_SECONDS = 120
_OID = re.compile(r"[0-9a-f]{40}\Z")
_REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")


class SourceError(ValueError):
    """Source acquisition is incomplete or inconsistent; no ready packet exists."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _run(argv: list[str], *, timeout: float, limit: int, data: bytes | None = None) -> bytes:
    """Bound both pipes while draining them; never return truncated output."""
    if timeout <= 0:
        raise SourceError("source acquisition deadline expired")
    deadline = time.monotonic() + timeout
    env = dict(os.environ, GH_PAGER="cat", GIT_PAGER="cat", GH_PROMPT_DISABLED="1")
    env.pop("GH_DEBUG", None)  # Credential lookup must never enable gh's HTTP diagnostics.
    if argv[0] == "git":
        env = {key: value for key, value in env.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    try:
        with subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
        ) as proc:
            buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
            try:
                with selectors.DefaultSelector() as ready:
                    for pipe in buffers:
                        ready.register(pipe, selectors.EVENT_READ)
                    pending = memoryview(data or b"")
                    if proc.stdin is not None:
                        os.set_blocking(proc.stdin.fileno(), False)
                        ready.register(proc.stdin, selectors.EVENT_WRITE)
                    while ready.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise SourceError("source command exceeded acquisition deadline")
                        for key, _ in ready.select(remaining):
                            if key.fileobj is proc.stdin:
                                try:
                                    pending = pending[os.write(key.fd, pending[:4096]) :]
                                except BrokenPipeError:
                                    pending = pending[:0]
                                if not pending:
                                    ready.unregister(proc.stdin)
                                    proc.stdin.close()
                                continue
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                ready.unregister(key.fileobj)
                                continue
                            buffer = buffers[key.fileobj]
                            if len(buffer) + len(chunk) > limit:
                                raise SourceError(
                                    "source command exceeded byte limit; not truncated"
                                )
                            buffer.extend(chunk)
                    proc.wait(timeout=max(0.001, deadline - time.monotonic()))
                if proc.returncode:
                    raise SourceError(f"source command {argv[0]} exited {proc.returncode}")
                return bytes(buffers[proc.stdout])
            finally:
                # Owned session group only; children can retain pipe descriptors.
                if proc.pid > 1:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"source command unavailable: {type(exc).__name__}") from None


_EMPTY_BLOB = hashlib.sha1(b"blob 0\0", usedforsecurity=False).hexdigest()  # Git empty-file id
# GitHub REST "List pull requests files", the status enum (docs.github.com, read 2026-11-19).
_FILE_STATUSES = frozenset(
    ("added", "removed", "modified", "renamed", "copied", "changed", "unchanged")
)
_ABBREV = re.compile(r"[0-9a-f]{7,40}\Z")
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(path: str) -> str:
    """Undo Git's C-style path quoting (``"renamed\\ttab.txt"``, ``\\351``)."""
    if not (len(path) >= 2 and path[0] == path[-1] == '"'):
        return path
    out, body, i = bytearray(), path[1:-1], 0
    while i < len(body):
        if body[i] != "\\":
            out += body[i].encode("utf-8")
            i += 1
        elif body[i + 1 : i + 4].isdigit():
            out.append(int(body[i + 1 : i + 4], 8))
            i += 4
        else:
            out.append(_C_ESCAPES[body[i + 1]])
            i += 2
    return out.decode("utf-8")


@dataclass
class _Block:
    """What one ``diff --git`` file header says, beyond Git's counts."""

    modes: set[str] = field(default_factory=set)  # which of old/new mode appeared
    index: tuple[str, str] | None = None
    index_mode: bool = False  # the index line ends with the (unchanged) mode
    payload: bool = False  # a hunk or a binary marker follows
    created: bool = False
    deleted: bool = False
    renamed_from: str | None = None


def _blocks(raw: bytes) -> list[_Block]:
    """Read each block's header lines. Hunk lines start with a space, + or -."""
    blocks: list[_Block] = []
    for line in raw.decode("utf-8").split("\n"):
        if line.startswith("diff --git "):
            blocks.append(_Block())
        elif not blocks or blocks[-1].payload:
            continue
        elif line.startswith(("@@", "GIT binary patch", "Binary files ")):
            blocks[-1].payload = True
        elif line.startswith(("old mode ", "new mode ")):
            blocks[-1].modes.add(line[:3])
        elif line.startswith("new file mode "):
            blocks[-1].created = True
        elif line.startswith("deleted file mode "):
            blocks[-1].deleted = True
        elif line.startswith("rename from "):
            blocks[-1].renamed_from = _unquote(line[len("rename from ") :])
        elif line.startswith("index "):
            parts = line.split(" ")
            pre, _, post = parts[1].partition("..")
            if not (_ABBREV.fullmatch(pre) and _ABBREV.fullmatch(post)) or len(parts) > 3:
                raise ValueError("malformed index line")
            blocks[-1].index, blocks[-1].index_mode = (pre, post), len(parts) == 3
    return blocks


def _empty(oid: str) -> bool:
    return set(oid) == {"0"} or _EMPTY_BLOB.startswith(oid)


def _check_block(block: _Block) -> None:
    # Git's numstat reports a path with half a mode pair, or with its change
    # payload cut off after the index line, as if whole (measured, git 2.43).
    if len(block.modes) == 1:
        raise ValueError("unpaired file mode header")
    # Git leaves the mode off the index line only when a mode pair, a create or a
    # delete explains it, so a bare index line betrays a mode pair stripped whole.
    if block.index and not block.index_mode and not (block.modes or block.created or block.deleted):
        raise ValueError("index line omits the mode with no mode header")
    index = block.index
    if index and index[0] != index[1] and not block.payload and not all(map(_empty, index)):
        raise ValueError("content change has no hunk or binary marker")


def _check_identity(name: str, block: _Block, entry: dict) -> None:
    """Bind the census blob and rename source to what the diff header says.

    Measured on six merged PRs (111 files): the census ``sha`` is the diff's
    post-image id for added, modified and renamed files, and the pre-image for
    removed ones. A block with no index line (pure rename, mode-only) has no id
    to compare.
    """
    sha, status = entry.get("sha"), entry.get("status")
    if block.index and sha:
        oid = block.index[0] if status == "removed" else block.index[1]
        if not sha.startswith(oid):
            raise ValueError(f"census blob for {name!r} differs from the raw diff")
    if status is not None and (status == "renamed") != (block.renamed_from is not None):
        raise ValueError(f"census rename status for {name!r} differs from the raw diff")
    if block.renamed_from is not None and block.renamed_from != entry.get("previous_filename"):
        raise ValueError(f"census rename source for {name!r} differs from the raw diff")


def reconcile_diff(raw: bytes, files: list[dict], *, timeout: float) -> dict[str, list[str]]:
    """Use Git's parser, then bind every block's header to the file census.

    Git reconciles destinations and textual line counts; each block's header is
    then checked for what those counts cannot see: mode pairs, a change payload,
    blob identities and rename sources. Returns, sorted, the census paths each
    check could not cover, so the caller can name those evidence gaps: ``blob``
    (no index line: a pure rename, a mode-only change) and ``counts`` (binary).
    """
    output = _run(
        ["git", "-C", os.path.abspath(os.sep), "apply", "--numstat", "-z", "--allow-empty"],
        timeout=timeout,
        limit=MAX_METADATA,
        data=raw,
    )
    try:
        if output and not output.endswith(b"\0"):
            raise ValueError("unfinished numstat")
        records = output.split(b"\0")[:-1]
        blocks = _blocks(raw)
        if raw and not blocks:
            raise ValueError("response is not a recognised patch")
        if len(records) != len(blocks):
            raise ValueError("Git patch records do not match the diff's file blocks")
        expected = {item["filename"]: item for item in files}
        if len(expected) != len(files):
            raise ValueError("changed-file census repeats a path")
        actual: dict[str, list] = {}
        gaps: dict[str, list[str]] = {"blob": [], "counts": []}
        for record, block in zip(records, blocks, strict=True):
            added, removed, path = record.split(b"\t", 2)
            _check_block(block)
            counts = None if (added, removed) == (b"-", b"-") else (int(added), int(removed))
            actual.setdefault(path.decode("utf-8"), []).append((counts, block))
        if set(actual) != set(expected):
            raise ValueError("changed-file census does not match raw diff")
        for name, pairs in actual.items():
            entry = expected[name]
            if len(pairs) == 2 and pairs[0][1].deleted and pairs[1][1].created:
                # A type change (symlink to file) is a delete and a create of one path.
                (old, _), (new, block) = pairs
                counts = None if old is None or new is None else (old[0] + new[0], old[1] + new[1])
            elif len(pairs) == 1:
                counts, block = pairs[0]
            else:
                raise ValueError("duplicate patch destination")
            if counts is not None and counts != (entry["additions"], entry["deletions"]):
                raise ValueError("text change counts do not match raw diff")
            _check_identity(name, block, entry)
            if block.index is None:
                gaps["blob"].append(name)
            if counts is None:
                gaps["counts"].append(name)
    except (ValueError, KeyError, TypeError, UnicodeError, IndexError) as exc:
        raise SourceError(f"incomplete PR diff: {exc}") from None
    return {kind: sorted(paths) for kind, paths in gaps.items()}


def _no_constant(name: str):
    raise ValueError(f"non-JSON constant {name}")  # Python accepts NaN/Infinity by default


def _json(raw: bytes):
    from genesis.session_awareness.pr_recording import _no_duplicates

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise SourceError("source metadata is not strict UTF-8 JSON") from None


def _metadata(data: object) -> dict:
    try:
        if not isinstance(data, dict) or data.get("merged") is not True:
            raise ValueError("PR is not merged")
        body = data["body"]
        if body is None:
            body = ""  # GitHub REST represents an absent body as null.
        if not isinstance(body, str) or len(body) > 65536:
            raise ValueError("missing or oversized body")
        body.encode("utf-8")
        count = data["changed_files"]
        if type(count) is not int or not 0 <= count <= MAX_FILES:
            raise ValueError("unsupported changed-file count (maximum 3000)")
        ids = [data["merge_commit_sha"], data["head"]["sha"], data["base"]["sha"]]
        if any(not isinstance(oid, str) or not _OID.fullmatch(oid) for oid in ids):
            raise ValueError("missing full PR identities")
        return dict(merge_commit=ids[0], head=ids[1], base=ids[2], body=body, changed_files=count)
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"incomplete PR metadata: {exc}") from None


def _file(data: object) -> dict:
    try:
        if not isinstance(data, dict):
            raise ValueError("file entry is not an object")
        result = {key: data[key] for key in ("filename", "status", "sha", "additions", "deletions")}
        if any(
            not isinstance(result[k], str) or not result[k] for k in ("filename", "status", "sha")
        ):
            raise ValueError("missing file identity")
        if not _OID.fullmatch(result["sha"]):
            raise ValueError("invalid file blob SHA")
        if any(type(result[k]) is not int or result[k] < 0 for k in ("additions", "deletions")):
            raise ValueError("invalid line counts")
        previous = data.get("previous_filename")
        if previous is not None:
            if not isinstance(previous, str) or not previous:
                raise ValueError("invalid previous filename")
            result["previous_filename"] = previous
        if result["status"] not in _FILE_STATUSES:
            raise ValueError("unknown file status")
        if result["status"] == "renamed" and previous is None:
            raise ValueError("rename lacks previous filename")
        # Every retained string must encode before save_packet reserves a destination.
        result["filename"].encode("utf-8")
        result["status"].encode("utf-8")
        if previous is not None:
            previous.encode("utf-8")
        return result
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"incomplete changed-file entry: {exc}") from None


@dataclass(frozen=True)
class PrSourceSnapshot:
    repo: str
    pr: int
    merge_commit: str
    head: str
    base: str
    body: str
    diff: str
    files: list[dict]
    acquired_at: str
    packet_version: int = 1

    def packet(self) -> dict:
        result = asdict(self)
        result.update(
            body_sha256=digest(self.body.encode()),
            diff_sha256=digest(self.diff.encode()),
            files_sha256=digest(canonical_json(self.files).encode()),
        )
        return result


def acquire_sources(repo: str, pr: int, *, run=_run, transport=None) -> PrSourceSnapshot:
    """Bracket complete metadata/diff acquisition with stable PR identity reads."""
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or type(pr) is not int or pr <= 0:
        raise SourceError("expected canonical owner/repository and positive integer PR")
    deadline = time.monotonic() + ACQUISITION_SECONDS
    credential = run(
        ["gh", "auth", "token", "--hostname", "github.com"],
        timeout=deadline - time.monotonic(),
        limit=16384,
    ).rstrip(b"\r\n")
    # Do not assume a token prefix: github.com supports multiple token families.
    if not credential or any(byte < 33 or byte > 126 for byte in credential):
        raise SourceError("github.com credential is missing or malformed")
    try:
        return asyncio.run(_acquire(repo, pr, credential.decode("ascii"), deadline, transport))
    except TimeoutError:
        raise SourceError("source acquisition deadline expired") from None
    except httpx.HTTPError:
        # HTTP exceptions may carry request headers or untrusted response text.
        raise SourceError("GitHub source request failed") from None


async def _acquire(repo, pr, credential, deadline, transport):
    async with asyncio.timeout(max(0, deadline - time.monotonic())):
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {credential}", "Accept-Encoding": "identity"},
        ) as client:
            return await _collect(client, repo, pr, deadline)


async def _collect(client, repo, pr, deadline):
    endpoint = f"repos/{repo}/pulls/{pr}"

    async def request(path: str, *, diff: bool = False) -> bytes:
        media = "application/vnd.github.diff" if diff else "application/vnd.github+json"
        limit = MAX_DIFF if diff else MAX_METADATA
        async with client.stream(
            "GET",
            f"https://api.github.com/{path}",
            headers={"Accept": media},
            timeout=max(0.001, deadline - time.monotonic()),
        ) as response:
            if response.status_code != 200:
                raise SourceError(f"GitHub source returned HTTP {response.status_code}")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise SourceError("GitHub source returned unexpected content encoding")
            output = bytearray()
            async for chunk in response.aiter_raw():
                if len(output) + len(chunk) > limit:
                    raise SourceError("GitHub source exceeded byte limit; not truncated")
                output.extend(chunk)
            return bytes(output)

    before = _metadata(_json(await request(endpoint)))
    raw = await request(endpoint, diff=True)
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise SourceError("raw diff is not UTF-8; source recovery required") from None
    files, size, page = [], 0, 1
    while True:
        page_raw = await request(f"{endpoint}/files?per_page=100&page={page}")
        size += len(page_raw)
        if size > MAX_METADATA:
            raise SourceError("file metadata exceeds total byte limit")
        entries = _json(page_raw)
        if not isinstance(entries, list) or len(entries) > 100:
            raise SourceError("invalid changed-file page")
        files.extend(_file(item) for item in entries)
        if len(files) > before["changed_files"]:
            raise SourceError("file census exceeds reported count")
        if len(files) == before["changed_files"] or len(entries) < 100:
            break
        page += 1
    if len(files) != before["changed_files"]:
        raise SourceError("file pagination ended before the reported count")
    files.sort(key=lambda item: item["filename"])
    reconcile_diff(raw, files, timeout=deadline - time.monotonic())
    after = _metadata(_json(await request(endpoint)))
    if before != after:
        raise SourceError("PR identity or body changed during source acquisition; retry")
    return PrSourceSnapshot(
        repo=repo,
        pr=pr,
        merge_commit=before["merge_commit"],
        head=before["head"],
        base=before["base"],
        body=before["body"],
        diff=text,
        files=files,
        acquired_at=datetime.now(UTC).isoformat(),
    )


def save_packet(snapshot: PrSourceSnapshot, destination: Path) -> Path:
    """Reserve a fresh private directory; only a complete packet.json is ready.

    No directory rename can replace another run's empty output directory. A
    killed writer may leave an unready directory; the reader requires packet.json.
    """
    destination.mkdir(mode=0o700)  # Existing output, even empty, is refused.
    target = destination / "packet.json"
    atomic_write_text(
        target, canonical_json(snapshot.packet()) + "\n", encoding="utf-8", overwrite=False
    )
    return target
