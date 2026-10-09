"""Git-repository health detection — the outage-class detector (F.1).

The thin-pool outage zeroed ``.git/config``, ``packed-refs``, and ~30 loose
objects with ZERO detection, silently disabling the guardian's ``REVERT_CODE``
recovery lever (which needs healthy local git). These probes catch that class:

- ``check_git_cheap`` — fast structural plumbing (config/HEAD/refs/packed-refs)
  + a rootfs read-only probe, safe to run on every awareness tick.
- ``check_git_deep`` — ``git fsck --full``, a slower CONTENT-verifying scan for a
  daily job (``--connectivity-only`` is deliberately NOT used: it skips object
  rehashing, so a zero-filled-but-present loose blob — the exact outage pattern —
  passes it; ``--full`` recomputes SHA-1s and catches it).

Both write a verdict to the shared mount (``<shared>/guardian/git_health.json``)
so the host guardian can enrich its own alert with the failure detail.

Never raises into the caller — a health probe that crashes the tick is worse
than the condition it detects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from genesis.env import genesis_home, repo_root

logger = logging.getLogger(__name__)

# Local git plumbing returns in milliseconds on a healthy disk; the ONLY way it
# takes longer is a wedged / read-only filesystem — which is precisely the
# condition being detected. 10 s bounds the awareness tick without ever killing
# legitimate work; a timeout is itself reported as a failure, never swallowed.
_CHEAP_TIMEOUT_S = 10
# `git fsck --full` (content-verifying) on this repo's ~82 MB object store is ~6 s
# healthy (measured); 900 s gives ~150x headroom on an IO-pressured pool while
# bounding the daily job. A timeout is emitted as a signal, not a silent skip.
_DEEP_TIMEOUT_S = 900

_VERDICT_DIR = "guardian"
_VERDICT_FILE = "git_health.json"


@dataclass(frozen=True)
class GitHealthReport:
    """Outcome of a git-health probe. ``ok`` iff ``failures`` is empty."""

    ok: bool
    failures: list[str]
    details: dict
    kind: str  # "cheap" | "deep"
    checked_at: str

    def to_json(self) -> dict:
        return {
            "version": 1,
            "ok": self.ok,
            "failures": list(self.failures),
            "kind": self.kind,
            "checked_at": self.checked_at,
            "details": self.details,
        }


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _run_git(repo: Path, *args: str, timeout: float) -> tuple[int, str, str]:
    """Run a git plumbing command; never raises. Returns (rc, stdout, stderr).

    rc = -1 on timeout (the wedged-fs signal), -2 on any other exec failure.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as exc:  # git missing, repo path gone, etc.
        return -2, "", str(exc)


def _mount_is_readonly(path: Path, mounts_text: str | None = None) -> bool:
    """True if the filesystem containing ``path`` is mounted read-only.

    Longest-prefix match over /proc/mounts (mirrors awareness/loop._fs_type_for),
    reading the mount OPTIONS field. ``mounts_text`` is injectable for tests.
    Returns False when it can't be determined — never false-alarms on a probe
    failure (the write-probe is the authoritative RO signal).
    """
    try:
        if mounts_text is None:
            mounts_text = Path("/proc/mounts").read_text()
    except OSError:
        return False
    target = str(path)
    best, ro = "", False
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mnt, opts = parts[1], parts[3]
        if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
            best = mnt
            ro = "ro" in opts.split(",")
    return ro


def _rootfs_writable(probe_dir: Path) -> bool:
    """Write-probe: create + unlink a dotfile in ``probe_dir`` (the resolved git
    dir). Catches an RO remount that /proc/mounts hasn't reflected yet, and any
    other write failure. The git dir is git-internal so this never touches the
    working tree.
    """
    probe = probe_dir / f".genesis-health-probe-{os.getpid()}"
    try:
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def _dedup(items: list[str]) -> list[str]:
    seen: list[str] = []
    for x in items:
        if x not in seen:
            seen.append(x)
    return seen


async def check_git_cheap(repo: Path | None = None) -> GitHealthReport:
    """Fast structural git-integrity + rootfs-writability check (per-tick safe)."""
    repo = repo or repo_root()
    return await asyncio.to_thread(_check_git_cheap_sync, repo)


def _check_git_cheap_sync(repo: Path) -> GitHealthReport:
    failures: list[str] = []
    details: dict = {}

    # Resolve the real git dir via git itself, so the file-level checks below are
    # correct for a normal repo (.git/ is a dir) AND a linked worktree (.git is a
    # FILE pointing at the main repo's .git/worktrees/<name>; --git-common-dir
    # returns the shared .git either way). A zeroed config does not break this —
    # locating .git doesn't read config content.
    rc, gcd, _ = _run_git(repo, "rev-parse", "--git-common-dir", timeout=_CHEAP_TIMEOUT_S)
    git_common: Path | None
    if rc == -1:
        failures.append("cheap_timeout")
        git_common = None
    elif rc != 0 or not gcd.strip():
        failures.append("git_dir_unresolvable")
        git_common = None
    else:
        gc = Path(gcd.strip())
        git_common = gc if gc.is_absolute() else (repo / gc)

    # Config PARSEABILITY — NOT remote presence. `git config --list` fails only on
    # a genuinely corrupt/unreadable config (e.g. the incident's null-filled
    # .git/config → "fatal: bad config"); a valid local clone with no origin, or a
    # truncated-to-empty config, parses fine and is recoverable (git falls back to
    # global config, and REVERT_CODE's `git revert` is purely local). Remote
    # presence is recorded as informational only — a missing origin degrades just
    # the git_repair fetch rung, not local recovery, so it is NOT a failure.
    rc, out, _ = _run_git(repo, "config", "--list", timeout=_CHEAP_TIMEOUT_S)
    if rc == -1:
        failures.append("cheap_timeout")
    elif rc != 0:
        failures.append("config_invalid")
    else:
        details["remote_url_present"] = any(
            line.startswith("remote.origin.url=") and line.split("=", 1)[1].strip()
            for line in out.splitlines()
        )

    # HEAD resolves to a real commit.
    rc, _, _ = _run_git(repo, "rev-parse", "--verify", "HEAD^{commit}", timeout=_CHEAP_TIMEOUT_S)
    if rc == -1:
        failures.append("cheap_timeout")
    elif rc != 0:
        failures.append("head_unresolvable")

    # Refs are enumerable.
    rc, _, _ = _run_git(repo, "for-each-ref", "--count=1", timeout=_CHEAP_TIMEOUT_S)
    if rc == -1:
        failures.append("cheap_timeout")
    elif rc != 0:
        failures.append("refs_unreadable")

    # packed-refs NULLED — the exact incident signature: the file still "exists"
    # so higher-level git may not scream immediately, but its bytes are \x00.
    # NOTE: a 0-byte packed-refs is a LEGITIMATE git state (all refs loose, or a
    # gc that packed nothing), so empty is NOT flagged here — a truncation that
    # actually loses refs surfaces as `refs_unreadable` above instead.
    if git_common is not None:
        pr = git_common / "packed-refs"
        try:
            if pr.exists():
                head = pr.read_bytes()[:512]
                if head[:1] == b"\x00":
                    failures.append("packed_refs_corrupt")
        except OSError:
            failures.append("packed_refs_unreadable")

    # Rootfs read-only / unwritable (thin-pool-exhaustion symptom). Probe the
    # resolved git dir (a real directory in both repo and worktree layouts);
    # fall back to the repo dir if we couldn't resolve it.
    probe_dir = git_common if (git_common is not None and git_common.is_dir()) else repo
    if _mount_is_readonly(repo) or not _rootfs_writable(probe_dir):
        failures.append("rootfs_readonly")

    failures = _dedup(failures)
    return GitHealthReport(
        ok=not failures, failures=failures, details=details, kind="cheap", checked_at=_utc_now_iso()
    )


async def check_git_deep(repo: Path | None = None) -> GitHealthReport:
    """Deep content-verifying scan (`git fsck --full`) for a daily job.

    A failing run is re-checked once, ``_FSCK_RECHECK_DELAY_S`` later, before it is
    reported. In a repo whose object store is shared by hundreds of worktrees, a
    scan races other writers: measured 2026-10-09, a concurrent `git fetch` wrote a
    ref mid-scan and fsck reported that ref's commit, tree and blobs "missing",
    all present seconds later (a concurrent gc/prune is another candidate). Every
    recorded deep failure since 2026-07 cleared without repair. A real corruption
    persists, so the re-check reproduces it. Only a re-check that itself exits
    non-zero counts as reproduced; one that times out, is killed or cannot run is
    inconclusive and the first run's evidence is kept. A re-check can race too
    (measured: 2 of 3 live scans on 2026-10-09 did), so when every line it reports
    is a ``missing`` object and every one of those objects exists on lookup right
    after, it is recorded as a transient as well; any other line still pages. The
    wait is an ``asyncio.sleep`` (cancellable at shutdown); only git runs in a thread.
    """
    repo = repo or repo_root()
    details: dict = {}
    rc, out, err = await asyncio.to_thread(_run_fsck, repo)
    _abort_if_terminated(rc)
    if rc == -1:
        return _deep_report(["fsck_timeout"], details)
    if rc == -2:  # git could not be run at all; nothing to re-check
        details["fsck_rc"] = rc
        details["fsck_stderr"] = _fsck_problem_lines(out, err)
        return _deep_report(["fsck_failed"], details)
    if rc != 0:  # > 0: fsck found problems; < -2: fsck was killed by a signal
        first = _fsck_problem_lines(out, err)
        await _asleep(_FSCK_RECHECK_DELAY_S)
        rc2, out2, err2 = await asyncio.to_thread(_run_fsck, repo)
        _abort_if_terminated(rc2)
        raced = rc2 > 0 and await asyncio.to_thread(_only_raced_missing, repo, out2, err2)
        if rc2 == 0 or raced:
            details["fsck_transient"] = {"rc": rc, "lines": first, "delay_s": _FSCK_RECHECK_DELAY_S}
            if raced:
                details["fsck_transient"]["race"] = "re-check's missing objects present on lookup"
                details["fsck_transient"]["recheck_lines"] = _fsck_problem_lines(out2, err2)
            logger.warning(
                "git fsck failed (rc=%d) and %s on re-check %ds later: %s",
                rc,
                "raced again (every missing object present)" if raced else "passed",
                _FSCK_RECHECK_DELAY_S,
                first[:500],
            )
            return _deep_report([], details)
        if rc2 > 0:
            rc, first = rc2, _fsck_problem_lines(out2, err2)
            details["fsck_reproduced"] = True
        else:
            details["fsck_recheck"] = "timeout" if rc2 == -1 else f"incomplete (rc={rc2})"
        details["fsck_rc"] = rc
        details["fsck_stderr"] = first
        return _deep_report(["fsck_failed"], details)
    return _deep_report([], details)


# Every recorded deep failure cleared without repair; the transients measured by
# hand (2026-10-08, 2026-10-09) were clean on a re-run under 2 min later.
# This is a pause before a confirming re-run, not a timeout.
_FSCK_RECHECK_DELAY_S = 120
_asleep = asyncio.sleep  # test seam
_EVIDENCE_CHARS = 2000


def _deep_report(failures: list[str], details: dict) -> GitHealthReport:
    return GitHealthReport(
        ok=not failures, failures=failures, details=details, kind="deep", checked_at=_utc_now_iso()
    )


def _run_fsck(repo: Path) -> tuple[int, str, str]:
    # --full recomputes every object's SHA-1, so it catches a zero-filled-but-
    # present loose blob (the outage pattern) that --connectivity-only would miss
    # (that flag only checks reachability, not content). Missing/corrupt objects →
    # non-zero. Dangling objects → exit 0 (benign, not a failure).
    #
    # --no-reflogs: this check exists to detect OBJECT-content corruption reachable
    # from refs; reflog integrity is irrelevant to it (and to REVERT_CODE, which the
    # guardian gates on a live cheap probe, not this verdict). Without it, routine
    # branch/worktree churn + gc leaves a branch reflog referencing a pruned commit,
    # so `git fsck --full` prints "invalid reflog entry" and exits non-zero — firing
    # a recurring FALSE "objects corrupt" CRITICAL (verified 2026-08-25: the recorded
    # stderr was reflog noise, not corruption). --no-reflogs drops that class while
    # still rehashing every object, so a real zeroed/missing blob still fails.
    # Narrowing (intended): an object referenced ONLY by a reflog and by no ref/
    # index is no longer scanned — outside this check's ref-reachable-corruption
    # and REVERT_CODE scope.
    #
    # --no-dangling only stops fsck PRINTING dangling objects (exit codes are
    # unchanged): thousands of them otherwise filled the whole evidence budget and
    # hid the real error lines (#2745).
    return _run_git(
        repo,
        "fsck",
        "--no-progress",
        "--full",
        "--no-reflogs",
        "--no-dangling",
        timeout=_DEEP_TIMEOUT_S,
    )


def _abort_if_terminated(rc: int) -> None:
    """A fsck killed by SIGTERM is a service stop (systemd signals the whole
    cgroup, git included): it proves nothing, so abort the scan with no verdict
    and no alert instead of reporting a failure that pages on the next start."""
    if rc == -signal.SIGTERM:
        logger.info("git fsck terminated by SIGTERM; deep scan aborted, no verdict")
        raise asyncio.CancelledError("git fsck terminated by SIGTERM (service stop)")


_MISSING_LINE = re.compile(r"missing (blob|tree|commit|tag) ([0-9a-f]{40}|[0-9a-f]{64})")
# Objects git may answer from memory with nothing on disk: batch-check reports the
# empty tree present even when its file is gone (measured, git 2.43, sha1 and
# sha256), and the empty blob is in the same family. A lookup cannot vouch for
# them, so a "missing" one always pages.
_BUILT_IN_OBJECTS = frozenset(
    {
        "4b825dc642cb6eb9a060e54bf8d69288fbee4904",  # empty tree, sha1  # pragma: allowlist secret
        "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",  # empty blob, sha1  # pragma: allowlist secret
        "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",  # empty tree, sha256  # pragma: allowlist secret
        "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813",  # empty blob, sha256  # pragma: allowlist secret
    }
)


def _only_raced_missing(repo: Path, out: str, err: str) -> bool:
    """True only if EVERY problem line is ``missing <type> <sha>`` and every such
    object exists now: fsck read a ref or index written mid-scan, after it had
    listed the objects. Any other line (``error:``, ``broken link``, hash
    mismatch, an unknown line), an absent object or a failed lookup returns False,
    so it pages. Reads the full output, never the capped evidence.

    ``git cat-file --batch-check`` exits 0 either way and prints ``<sha> missing``
    for an absent, deleted or unreadable (e.g. zeroed) object, the last also with
    ``error:`` on stderr (measured, git 2.43); present means ``<sha> <type> <size>``
    with the type fsck named, and a clean stderr. It reads only the header, so an
    object corrupt behind a valid header reads as present; fsck reports those as
    ``error:`` lines, which return False above.
    """
    wanted: list[tuple[str, str]] = []  # (type, sha)
    for ln in _problem_lines(out, err):
        m = _MISSING_LINE.fullmatch(ln)
        if m is None:
            return False
        wanted.append((m.group(1), m.group(2)))
    shas = [sha for _, sha in wanted]
    if not shas or _BUILT_IN_OBJECTS.intersection(shas):
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch-check"],
            input="".join(f"{sha}\n" for sha in shas),
            capture_output=True,
            text=True,
            timeout=_LOOKUP_TIMEOUT_S,
            # fsck ignores replace refs; so must the lookup, or a replacement
            # could vouch for an object that is not there.
            env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
        )
    except Exception:  # timeout, git gone: unverified, so it pages
        return False
    _abort_if_terminated(proc.returncode)  # a stop mid-lookup is not a failure either
    rows = proc.stdout.splitlines()
    if proc.returncode != 0 or proc.stderr.strip() or len(rows) != len(shas):
        return False
    for (kind, sha), row in zip(wanted, rows, strict=True):
        parts = row.split()
        if len(parts) != 3 or parts[0] != sha or parts[1] != kind:
            return False
    return True


# One batched lookup of the objects a re-check named; milliseconds when healthy.
# Same rationale as the cheap probes: only a wedged filesystem takes longer, and
# a timeout fails toward paging.
_LOOKUP_TIMEOUT_S = 60


def _problem_lines(out: str, err: str) -> list[str]:
    """Every non-noise line, stderr first, warnings last (uncapped)."""
    lines = [
        ln.strip()
        for ln in (err or "").splitlines() + (out or "").splitlines()
        if ln.strip() and not ln.strip().startswith(("dangling ", "notice:"))
    ]
    lines.sort(key=lambda ln: ln.startswith("warning"))  # stable: keeps order otherwise
    return lines


def _fsck_problem_lines(out: str, err: str) -> str:
    """The lines that explain a failing fsck, capped at ``_EVIDENCE_CHARS``.

    Drops only known noise (``dangling`` objects, ``notice:`` lines); every other
    line is kept, unknown ones included. stderr comes first (``error:`` lines with
    object paths), then stdout (``missing``/``broken link``), warnings last.
    """
    lines = _problem_lines(out, err)
    kept: list[str] = []
    used = 0
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > _EVIDENCE_CHARS:
            kept.append(f"(+{len(lines) - i} more lines)")
            break
        kept.append(ln)
        used += len(ln) + 1
    return "\n".join(kept)


_VERDICT_SCHEMA = 2  # v2: per-kind slots (see _merge_verdict)


def _report_slot(report: GitHealthReport) -> dict:
    return {
        "ok": report.ok,
        "failures": list(report.failures),
        "checked_at": report.checked_at,
        "details": report.details,
    }


def _merge_verdict(existing: dict | None, report: GitHealthReport) -> dict:
    """Fold ``report`` into a two-slot (``cheap`` / ``deep``) verdict.

    The cheap per-tick probe and the daily deep fsck share ``git_health.json``.
    Deep-only corruption (a zeroed-but-present reachable blob) is INVISIBLE to the
    cheap probe, so if a passing cheap tick simply overwrote the file it would
    erase a failed deep verdict within one tick — the shared state would report
    healthy until the next daily fsck (~24h). Instead each writer updates only its
    own slot and preserves the other; the daily deep run refreshes its slot, so a
    deep failure persists exactly until re-checked (never unboundedly stale).

    Top-level ``ok`` / ``failures`` are the UNION across slots — the reader
    contract the host guardian relies on (``_verdict_detail`` reads ``failures``).
    """
    cheap = deep = None
    if isinstance(existing, dict):
        if isinstance(existing.get("cheap"), dict):
            cheap = existing["cheap"]
        if isinstance(existing.get("deep"), dict):
            deep = existing["deep"]
        # v1 migration: a legacy single-kind verdict has top-level kind/failures
        # but no slots. Seed the matching slot from it so a pre-upgrade FAILED
        # deep verdict isn't dropped by the first v2 write (which would reopen the
        # ~24h blind spot this format closes). The new report below still wins for
        # its own slot.
        if cheap is None and deep is None and existing.get("kind") in ("cheap", "deep"):
            legacy = {
                "ok": bool(existing.get("ok", True)),
                "failures": list(existing.get("failures") or []),
                "checked_at": existing.get("checked_at", ""),
                "details": existing.get("details") or {},
            }
            if existing["kind"] == "deep":
                deep = legacy
            else:
                cheap = legacy
    if report.kind == "deep":
        deep = _report_slot(report)
    else:  # any non-deep kind is the live cheap slot
        cheap = _report_slot(report)

    slots = [s for s in (cheap, deep) if s is not None]
    failures: list[str] = []
    for s in slots:
        for f in s.get("failures") or []:
            if f not in failures:
                failures.append(f)
    return {
        "version": _VERDICT_SCHEMA,
        "ok": all(s.get("ok", True) for s in slots) and not failures,
        "failures": failures,
        "kind": report.kind,  # the writer of THIS update
        "checked_at": report.checked_at,
        "cheap": cheap,
        "deep": deep,
    }


def write_git_health_verdict(
    report: GitHealthReport, shared_dir: Path | None = None
) -> Path | None:
    """Atomically write the verdict to ``<shared>/guardian/git_health.json`` (0600).

    Merges into a two-slot cheap/deep verdict (``_merge_verdict``) so a passing
    cheap tick never erases a failed deep result. Returns the path, or None if the
    shared mount is absent (no-guardian install). Never raises.
    """
    try:
        shared = shared_dir if shared_dir is not None else (genesis_home() / "shared")
        if not shared.exists():
            return None
        dest_dir = shared / _VERDICT_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / _VERDICT_FILE
        existing: dict | None = None
        try:
            existing = json.loads(dest.read_text())
        except (OSError, json.JSONDecodeError):
            existing = None
        merged = _merge_verdict(existing, report)
        tmp = dest_dir / f".{_VERDICT_FILE}.tmp"
        tmp.write_text(json.dumps(merged, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        return dest
    except Exception:
        logger.debug("Failed to write git_health verdict", exc_info=True)
        return None
