#!/usr/bin/env python3
"""Bound native publication scans; emit only categories and opaque locations."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

INPUT_LIMIT = 1024 * 1024
OBJECT_LIMIT = 16 * 1024 * 1024
# Complete R7 outgoing range (13 changed files), three owned native scans:
# largest 0.446s. The floor exceeds 20x that measured complete scan.
SCAN_SECONDS = 60.0


def _identity(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ScanResult:
    verdict: str
    identity: str
    findings: tuple[tuple[str, str], ...] = ()
    reasons: tuple[str, ...] = ()
    degraded: tuple[str, ...] = ()
    common_dir: str | None = None


def _checks(parsed, fingerprint_file, *, fingerprint_text=None):
    """R9/R10 extend this lazy sequence using the same canonical scan units."""
    from genesis.contribution.sanitize import _check_fingerprints

    yield from _check_fingerprints(parsed, fingerprint_file, stop_after_first=True,
                                   fingerprint_text=fingerprint_text)


def finish(result: ScanResult) -> int:
    """R9 attaches its foreground repeat policy here, after all earlier gates."""
    if result.verdict == "CLEAN":
        if result.degraded:
            print("Genesis native privacy: complete scan with degraded public-base proof.", file=sys.stderr)
        return 0
    print(f"Genesis native privacy {result.verdict}: publication refused.", file=sys.stderr)
    for category, location in result.findings:
        print(f"  {category} {location}", file=sys.stderr)
    return 2


def bootstrap(source: Path) -> None:
    """The installed chain passes its recorded trusted source/runtime root."""
    sys.path[:0] = [str(source / "src"), str(Path(__file__).resolve().parent)]


class _Unknown(Exception):
    pass


def _file_bytes(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            value = stream.read(OBJECT_LIMIT + 1)
        return value if len(value) <= OBJECT_LIMIT else None
    except OSError:
        return None


def _file_identity(path: Path) -> str:
    value = _file_bytes(path)
    if value is not None:
        return hashlib.sha256(value).hexdigest()
    try:
        stat = path.stat()
        return _identity(["unreadable", stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_mode])
    except OSError:
        return "unavailable"


def _inputs(source: Path, argv: list[str], data: bytes) -> dict:
    fingerprint = Path(os.environ.get("GENESIS_RELEASE_FINGERPRINTS", str(Path.home() / ".genesis/release-fingerprints.txt")))
    paths = [Path(__file__), Path(__file__).with_name("push_destinations.py"),
             source / "src/genesis/contribution/sanitize.py", source / "src/genesis/contribution/findings.py"]
    return {"argv": argv, "tuples": hashlib.sha256(data).hexdigest(),
            "config": _file_identity(Path.home() / ".genesis/config/genesis.yaml"),
            "fingerprints": _file_identity(fingerprint), "policy": [_file_identity(path) for path in paths]}


class _Scan:
    def __init__(self, source: Path, argv: list[str], data: bytes, common_dir: str | None = None):
        self.inputs = _inputs(source, argv, data)
        self.argv, self.data = argv, data
        self.findings: set[tuple[str, str]] = set()
        self.reasons: set[str] = set()
        self.degraded: set[str] = set()
        self.ranges: list = []
        self.common = common_dir
        self.git_options: list[str] = []
        self.fingerprint = Path(os.environ.get("GENESIS_RELEASE_FINGERPRINTS", str(Path.home() / ".genesis/release-fingerprints.txt")))
        raw = _file_bytes(self.fingerprint)
        self.fingerprint_text = raw.decode("utf-8", "surrogateescape") if raw is not None else ""
        self.inputs["fingerprints"] = hashlib.sha256(raw).hexdigest() if raw is not None else _file_identity(self.fingerprint)

    def result(self, verdict: str) -> ScanResult:
        findings, reasons, degraded = tuple(sorted(self.findings)), tuple(sorted(self.reasons)), tuple(sorted(self.degraded))
        return ScanResult(verdict, _identity([self.inputs, self.ranges, findings, reasons, degraded]),
                          findings, reasons, degraded, self.common)

    async def git(self, *args: str, limit: int = OBJECT_LIMIT, missing_ok: bool = False) -> bytes:
        from genesis.util.streams import read_limited

        proc = await asyncio.create_subprocess_exec(
            "git", "--no-replace-objects", *self.git_options, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        )
        # Children stay in this worker's owned group; its supervisor bounds the
        # complete scan and reaps the group, including Git transport children.
        value, total = await read_limited(proc.stdout, limit)
        code = await proc.wait()
        if (code and not (missing_ok and code == 1 and not value)) or total > limit:
            raise _Unknown("git-read")
        return value

    async def local_objects(self) -> None:
        """Qualify local-only reads without a fetch probe or version inference."""
        try:
            await self.git("--no-lazy-fetch", "--version", limit=128)
        except _Unknown:
            # Older Git may silently ignore the environment knob. Only read
            # metadata until Git proves this repository has no promisor source.
            partial = await self.git("config", "--null", "--name-only", "--get-regexp",
                                     r"^(extensions\.partialclone|remote\..*\.partialclonefilter)$", missing_ok=True, limit=INPUT_LIMIT)
            promisors = await self.git("config", "--null", "--type=bool", "--get-regexp",
                                       r"^remote\..*\.promisor$", missing_ok=True, limit=INPUT_LIMIT)
            self.inputs["object-access"] = ["no-promisor", hashlib.sha256(partial + b"\0" + promisors).hexdigest()]
            if partial or any(row.endswith(b"\ntrue") for row in promisors.split(b"\0")):
                raise _Unknown("local-only-objects-unavailable") from None
        else:
            self.git_options = ["--no-lazy-fetch"]
            self.inputs["object-access"] = ["no-lazy-fetch"]

    def inspect(self, parsed, location: str) -> bool:
        from genesis.contribution.sanitize import _FINGERPRINT_NOT_RUN

        for finding in _checks(parsed, self.fingerprint, fingerprint_text=self.fingerprint_text):
            if finding.scanner == "fingerprint" and finding.detail in _FINGERPRINT_NOT_RUN:
                self.reasons.add("fingerprint-unavailable")
                continue
            category = "known-value" if finding.scanner == "fingerprint" else finding.scanner
            safe = _identity([location, finding.file, finding.line, category, finding.kind.value, finding.severity.value])
            self.findings.add((category, safe))
            if finding.scanner == "fingerprint" or str(finding.kind.value) == "secret":
                return True
        return False

    def text(self, value: bytes, location: str, *, lines: bool = False) -> bool:
        from genesis.contribution.sanitize import _ParsedDiff

        decoded = value.decode("utf-8")
        projected = decoded.split("\n") if lines else [decoded]
        return self.inspect(_ParsedDiff([], [(location, number, text) for number, text in enumerate(projected, 1)],
                                        False, len(value)), location)

    async def scope(self) -> bool:
        from push_destinations import (
            _push_url_transport,
            _repo_identity_from_url,
            public_repo_config,
        )

        state, canonical, digest = public_repo_config(limit=INPUT_LIMIT)
        self.inputs["config"] = [state, digest]
        destination = self.argv[1]
        transport = _push_url_transport(destination)
        identity = _repo_identity_from_url(destination)
        if state == "unknown" or transport is None:
            raise _Unknown("destination-unknown")
        if state == "declared":
            if transport in ("local", "file"):
                return False
            if identity is None:
                raise _Unknown("destination-unknown")
            return identity == _repo_identity_from_url("https://github.com/" + canonical)
        urls = (await self.git("remote", "get-url", "--push", "--all", "origin", limit=INPUT_LIMIT)).decode("utf-8").split("\n")
        urls = [url for url in urls if url]
        self.inputs["origin-push"] = urls
        if not urls or any(_push_url_transport(url) is None for url in urls):
            raise _Unknown("origin-unknown")
        if destination in urls:
            return True
        if transport in ("local", "file"):
            return False
        if identity is None or any(_repo_identity_from_url(url) is None for url in urls):
            raise _Unknown("origin-unknown")
        return any(identity == _repo_identity_from_url(url) for url in urls)

    async def public_base(self, oid_pattern: str) -> str | None:
        try:
            # Leave most of the whole-worker budget for conservative fallback.
            # A cancelled query stays in the worker group: final confirmation
            # retires its transport too before any successful result is used.
            value = await asyncio.wait_for(self.git("ls-remote", "--refs", "--", self.argv[1], "refs/heads/main",
                                                    "refs/heads/master", limit=INPUT_LIMIT), min(5.0, SCAN_SECONDS / 10))
            refs = {}
            for line in value.decode("ascii").splitlines():
                oid, name = line.split("\t")
                if name not in ("refs/heads/main", "refs/heads/master") or name in refs or not re.fullmatch(oid_pattern, oid):
                    raise _Unknown("public-base-invalid")
                refs[name] = oid
            name = "refs/heads/main" if "refs/heads/main" in refs else "refs/heads/master"
            oid = refs.get(name)
            self.ranges.append(["public-advertised", name, oid])
            if oid and await self.git("cat-file", "-t", oid, limit=128) == b"commit\n":
                return oid
        except (TimeoutError, OSError, UnicodeError, ValueError, _Unknown):
            pass
        self.degraded.add("public-base-unproved")
        return None

    async def commit(self, oid: str, oid_pattern: str) -> bool:
        from genesis.contribution.sanitize import parse_diff

        raw = await self.git("cat-file", "commit", oid)
        header, separator, message = raw.partition(b"\n\n")
        if not separator:
            raise _Unknown("commit-header")
        parents = []
        for line in header.split(b"\n"):
            if line.startswith(b"parent "):
                parent = line[7:].decode("ascii")
                if not re.fullmatch(oid_pattern, parent):
                    raise _Unknown("commit-parent")
                parents.append(parent)
        if self.text(message, oid + ":message", lines=True):
            return True
        paths = await self.git("ls-tree", "-r", "-z", "--full-tree", oid)
        for entry in paths.split(b"\0")[:-1]:
            metadata, path = entry.split(b"\t", 1)
            if metadata.split(b" ", 1)[0] == b"160000":
                raise _Unknown("submodule")
            if self.text(path, oid + ":path"):
                return True
        flags = ("--no-commit-id", "-p", "--root", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv",
                 "--no-color", "--binary", "--ignore-submodules=none", "--src-prefix=a/", "--dst-prefix=b/",
                 "--output-indicator-new=+", "--output-indicator-old=-", "--output-indicator-context= ")
        for parent in parents or [None]:
            patch = await self.git("-c", "diff.algorithm=myers", "-c", "diff.context=3", "-c", "diff.submodule=short",
                                   "-c", "core.quotePath=true", "diff-tree", *flags, *([parent] if parent else []), oid, "--")
            parsed = parse_diff(patch.decode("utf-8"), strict=True)
            if parsed.is_binary:
                raise _Unknown("binary")
            if self.inspect(parsed, oid + ":" + (parent or "root")):
                return True
        return False

    async def evaluate(self) -> ScanResult:
        try:
            if len(self.argv) != 2 or len(self.data) > INPUT_LIMIT:
                raise _Unknown("native-input")
            self.common = os.fsdecode(await self.git("rev-parse", "--path-format=absolute", "--git-common-dir", limit=INPUT_LIMIT)).removesuffix("\n")
            object_format = (await self.git("rev-parse", "--show-object-format", limit=128)).strip()
            length = {b"sha1": 40, b"sha256": 64}.get(object_format)
            if length is None:
                raise _Unknown("object-format")
            pattern = "[0-9a-fA-F]{" + str(length) + "}"
            rows = []
            for line in self.data.split(b"\n")[:-1]:
                fields = line.decode("utf-8").split(" ")
                if len(fields) != 4 or not fields[0] or not fields[2] or not all(re.fullmatch(pattern, fields[i]) for i in (1, 3)):
                    raise _Unknown("native-input")
                rows.append(fields)
            if not self.data.endswith(b"\n") and self.data:
                raise _Unknown("native-input")
            if not any(row[1] != "0" * length for row in rows) or not await self.scope():
                return self.result("CLEAN")
            await self.local_objects()
            base = await self.public_base(pattern)
            seen = set()
            for local_ref, tip, remote_ref, remote in rows:
                if tip == "0" * length:
                    continue
                if self.text(remote_ref.encode(), "published-ref:" + tip):
                    return self.result("TIER_A")
                tags = set()
                while await self.git("cat-file", "-t", tip, limit=128) == b"tag\n":
                    if tip in tags:
                        raise _Unknown("tag-cycle")
                    tags.add(tip)
                    raw = await self.git("cat-file", "tag", tip)
                    header, separator, message = raw.partition(b"\n\n")
                    if not separator or not header.startswith(b"object "):
                        raise _Unknown("tag-header")
                    if self.text(message, tip + ":tag", lines=True):
                        return self.result("TIER_A")
                    tip = header.split(b"\n", 1)[0][7:].decode("ascii")
                    if not re.fullmatch(pattern, tip):
                        raise _Unknown("tag-target")
                if await self.git("cat-file", "-t", tip, limit=128) != b"commit\n":
                    raise _Unknown("unsupported-object")
                exclusions = [base] if base else []
                try:
                    if remote != "0" * length and await self.git("cat-file", "-t", remote, limit=128) == b"commit\n":
                        exclusions.append(remote)
                except _Unknown:
                    self.degraded.add("remote-tip-unproved")
                self.ranges.append([local_ref, tip, remote_ref, remote, exclusions])
                commits = await self.git("rev-list", "--topo-order", tip, *(["--not", *exclusions] if exclusions else []), "--")
                for oid in commits.decode("ascii").splitlines():
                    if not re.fullmatch(pattern, oid):
                        raise _Unknown("walk-output")
                    if oid not in seen:
                        seen.add(oid)
                        if await self.commit(oid, pattern):
                            return self.result("TIER_A")
            return self.result("COULD_NOT_SCAN" if self.reasons else "TIER_B" if self.findings else "CLEAN")
        except (ImportError, OSError, UnicodeError, ValueError, _Unknown) as exc:
            self.reasons.add(str(exc) if isinstance(exc, _Unknown) else "unreadable-content")
            return self.result("COULD_NOT_SCAN")


async def scan(source: Path, argv: list[str], data: bytes, *, common_dir: str) -> ScanResult:
    from genesis.util.proc_kill import confirm_process_exit, kill_process_group, spawn_owned

    deadline = time.monotonic() + SCAN_SECONDS
    fallback = ScanResult("COULD_NOT_SCAN", _identity([_inputs(source, argv, data), "worker-incomplete"]),
                          reasons=("worker-incomplete",), common_dir=common_dir)
    result = None
    proc = await spawn_owned(sys.executable, __file__, "--worker", str(source), common_dir or "", "--", *argv,
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=65536)

    async def exchange():
        nonlocal result
        proc.stdin.write(data)
        await proc.stdin.drain()
        proc.stdin.close()
        line = await proc.stdout.readline()
        payload = json.loads(line)
        if set(payload) != set(asdict(fallback)) or payload["verdict"] not in ("CLEAN", "TIER_A", "TIER_B", "COULD_NOT_SCAN"):
            raise _Unknown("worker-result")
        if not re.fullmatch("[0-9a-f]{64}", payload["identity"]):
            raise _Unknown("worker-result")
        if payload["common_dir"] != common_dir:
            raise _Unknown("worker-result")
        for key in ("reasons", "degraded"):
            if not isinstance(payload[key], list) or any(not isinstance(item, str) or not re.fullmatch("[a-z0-9_-]+", item) for item in payload[key]):
                raise _Unknown("worker-result")
        if not isinstance(payload["findings"], list):
            raise _Unknown("worker-result")
        for category, location in payload["findings"]:
            if not re.fullmatch("[a-z0-9_-]+", category) or not re.fullmatch("[0-9a-f]{64}", location):
                raise _Unknown("worker-result")
        result = ScanResult(payload["verdict"], payload["identity"], tuple(map(tuple, payload["findings"])),
                            tuple(payload["reasons"]), tuple(payload["degraded"]), payload["common_dir"])
        if await proc.stdout.read(1) or await proc.wait():
            raise _Unknown("worker-exit")

    complete = False
    try:
        await asyncio.wait_for(exchange(), max(0.01, deadline - time.monotonic() - 2))
        complete = True
    except (TimeoutError, OSError, ValueError, TypeError, KeyError, _Unknown):
        pass
    except asyncio.CancelledError:
        # A confirmed known value cannot become the separately overridable
        # unknown category merely because cancellation arrived afterward.
        if result is None or result.verdict != "TIER_A":
            raise
    finally:
        kill_process_group(proc)
        try:
            await confirm_process_exit(proc, confirm_timeout_s=max(0.05, min(2, deadline - time.monotonic())))
        except Exception:
            complete = False
        except asyncio.CancelledError:
            if result is None or result.verdict != "TIER_A":
                raise
            complete = False
    return result if result is not None and (complete or result.verdict == "TIER_A") else fallback


def main() -> int:
    worker = sys.argv[1:2] == ["--worker"]
    args = sys.argv[2:] if worker else sys.argv[1:]
    if len(args) != 5 or args[2] != "--" or not os.path.isabs(args[1]):
        return finish(ScanResult("COULD_NOT_SCAN", _identity("arguments"), reasons=("arguments",)))
    source = Path(args[0])
    common = args[1]
    bootstrap(source)
    data = sys.stdin.buffer.read(INPUT_LIMIT + 1)

    async def invoke():
        if worker:
            return await _Scan(source, args[3:], data, common).evaluate()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        return await scan(source, args[3:], data, common_dir=common)

    try:
        result = asyncio.run(invoke())
    except (Exception, asyncio.CancelledError, KeyboardInterrupt):
        result = ScanResult("COULD_NOT_SCAN", _identity([_inputs(source, args[3:], data), "unavailable"]),
                            reasons=("unavailable",), common_dir=common)
    if worker:
        print(json.dumps(asdict(result)), flush=True)
        return 0
    return finish(result)


if __name__ == "__main__":
    raise SystemExit(main())
