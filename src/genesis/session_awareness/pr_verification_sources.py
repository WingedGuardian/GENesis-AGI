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
from dataclasses import asdict, dataclass
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


def reconcile_diff(raw: bytes, files: list[dict], *, timeout: float) -> None:
    """Use Git's parser, reconciling every destination and textual line count."""
    output = _run(
        ["git", "-C", os.path.abspath(os.sep), "apply", "--numstat", "-z", "--allow-empty"],
        timeout=timeout,
        limit=MAX_METADATA,
        data=raw,
    )
    actual = {}
    try:
        if output and not output.endswith(b"\0"):
            raise ValueError("unfinished numstat")
        for record in output.split(b"\0")[:-1]:
            added, removed, path = record.split(b"\t", 2)
            name = path.decode("utf-8")
            if name in actual:
                raise ValueError("duplicate patch destination")
            actual[name] = None if (added, removed) == (b"-", b"-") else (int(added), int(removed))
        expected = {item["filename"]: (item["additions"], item["deletions"]) for item in files}
        if len(expected) != len(files) or set(actual) != set(expected):
            raise ValueError("changed-file census does not match raw diff")
        if any(counts is not None and counts != expected[name] for name, counts in actual.items()):
            raise ValueError("text change counts do not match raw diff")
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise SourceError(f"incomplete PR diff: {exc}") from None


def _json(raw: bytes):
    from genesis.session_awareness.pr_recording import _no_duplicates

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
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
        if result["status"] == "renamed" and previous is None:
            raise ValueError("rename lacks previous filename")
        result["filename"].encode("utf-8")
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
