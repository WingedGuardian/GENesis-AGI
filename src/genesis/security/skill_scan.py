"""Wrap NVIDIA SkillSpector to scan installed skills and file findings to recon.

SkillSpector (https://github.com/NVIDIA/SkillSpector) is an external dependency,
installed separately (it is not vendored). This module shells out to it, parses
its JSON report, and stores ranked findings as recon observations
(source='recon', type='finding', category='skill-security') so they surface via
`recon_findings(job_type="skill-security")` and the dashboard.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

JOB_TYPE = "skill-security"

# SkillSpector severity label → recon finding priority.
_SEVERITY_PRIORITY = {
    "CRITICAL": "high",
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
}


def severity_to_priority(severity: str) -> str:
    """Map a SkillSpector severity label to a recon finding priority.

    Unknown/blank severities default to "low" so a malformed report never
    escalates noise into the high-priority recon lane.
    """
    return _SEVERITY_PRIORITY.get((severity or "").strip().upper(), "low")


@dataclass(frozen=True)
class ScanResult:
    """The fields we care about from a SkillSpector JSON report."""

    name: str
    source: str
    score: int
    severity: str
    recommendation: str
    issues: list[dict] = field(default_factory=list)
    has_executable_scripts: bool = False


def parse_report(report: dict) -> ScanResult:
    """Parse a SkillSpector JSON report into a ScanResult.

    Tolerant of missing fields — a partial report yields a low/zero result
    rather than raising, so one odd skill never aborts a full sweep.
    """
    skill = report.get("skill") or {}
    risk = report.get("risk_assessment") or {}
    meta = report.get("metadata") or {}
    return ScanResult(
        name=skill.get("name") or "unknown",
        source=skill.get("source") or "",
        score=int(risk.get("score") or 0),
        severity=(risk.get("severity") or "LOW").upper(),
        recommendation=risk.get("recommendation") or "",
        issues=list(report.get("issues") or []),
        has_executable_scripts=bool(meta.get("has_executable_scripts", False)),
    )


def _format_location(loc: object) -> str:
    """Render a SkillSpector issue location as 'file:line'.

    SkillSpector emits ``location`` as either a string ("file:line") or a dict
    ({"file", "start_line", "end_line"}); normalize both to a clean string so a
    raw dict repr never leaks into a human-facing finding.
    """
    if isinstance(loc, dict):
        file = loc.get("file") or ""
        line = loc.get("start_line")
        return f"{file}:{line}" if file and line is not None else (file or "")
    return str(loc or "")


def report_to_finding(result: ScanResult) -> dict:
    """Build a recon finding payload (title/summary/priority/source_url/job_type).

    Title leads with the skill name + score so the recon lane is scannable;
    summary names the top issues (capped) so triage is actionable at a glance.
    """
    priority = severity_to_priority(result.severity)
    title = f"Skill '{result.name}': {result.severity} risk ({result.score}/100)"

    lines: list[str] = []
    if result.recommendation:
        lines.append(f"Recommendation: {result.recommendation}")
    if result.issues:
        lines.append(f"{len(result.issues)} issue(s):")
        for issue in result.issues[:10]:
            cat = issue.get("category", "?")
            sev = issue.get("severity", "?")
            loc = _format_location(issue.get("location"))
            lines.append(f"  - [{sev}] {cat}{f' @ {loc}' if loc else ''}")
    else:
        lines.append("No issues detected.")

    return {
        "title": title,
        "summary": "\n".join(lines),
        "priority": priority,
        "job_type": JOB_TYPE,
        "source_url": result.source or None,
    }


def discover_skill_dirs(roots: Iterable[Path]) -> list[Path]:
    """Return immediate subdirectories of each root that contain a SKILL.md.

    Non-existent roots are skipped. Discovery is one level deep on purpose: we
    point SkillSpector at the skill directory (it recurses internally), so we
    avoid mistaking a skill's bundled sub-tree for separate skills.
    """
    found: list[Path] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file():
                found.append(child)
    return found


# A scanner takes a skill dir and returns a SkillSpector JSON report, or None on
# failure/timeout. A storer persists one finding payload. Both are injected so
# the orchestration is testable without a live subprocess or DB.
ScannerFn = Callable[[Path], "dict | None"]
StorerFn = Callable[[object, dict], Awaitable[None]]


async def scan_and_store(
    db: object,
    skill_dirs: Iterable[Path],
    *,
    scanner: ScannerFn,
    storer: StorerFn,
    min_score: int = 1,
) -> list[ScanResult]:
    """Scan each skill dir, parse the report, and file a recon finding.

    Every successfully-scanned skill is returned; only those at/above
    ``min_score`` are filed to recon (keeps clean skills from swamping the lane).
    A failed/timed-out scan (scanner returns None) is skipped, not fatal.
    """
    results: list[ScanResult] = []
    for skill_dir in skill_dirs:
        report = scanner(Path(skill_dir))
        if report is None:
            continue
        result = parse_report(report)
        results.append(result)
        if result.score >= min_score:
            await storer(db, report_to_finding(result))
    return results


# --- I/O integration (verified end-to-end, not unit-tested: real subprocess + DB) ---


def run_skillspector(
    skill_dir: Path,
    *,
    skillspector_bin: str | None = None,
    timeout_s: int = 120,
    no_llm: bool = True,
) -> dict | None:
    """Run ``skillspector scan <dir> -f json`` and return the parsed report.

    Returns None on timeout, exec error, or unreadable output — one bad/huge
    skill (e.g. a skill bundling thousands of files) is logged and skipped, never
    fatal to the sweep. A NON-ZERO exit is NOT treated as failure: SkillSpector
    signals risk level via the exit code (CRITICAL -> rc=1), so the parsed JSON
    is trusted whenever it parses. SkillSpector must be installed separately;
    pass ``skillspector_bin`` or have it on PATH.
    """
    import json
    import os
    import shutil
    import subprocess
    import tempfile

    binary = skillspector_bin or shutil.which("skillspector")
    if not binary:
        raise FileNotFoundError(
            "skillspector not found on PATH. Install NVIDIA/SkillSpector "
            "(github.com/NVIDIA/SkillSpector) and pass skillspector_bin= or add it to PATH."
        )

    tmpdir = os.path.expanduser("~/tmp")
    os.makedirs(tmpdir, exist_ok=True)
    fd, out_path = tempfile.mkstemp(suffix=".json", dir=tmpdir)
    os.close(fd)
    cmd = [binary, "scan", str(skill_dir), "-f", "json", "-o", out_path]
    if no_llm:
        cmd.append("--no-llm")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        logger.warning("skillspector scan timed out for %s (%ss)", skill_dir, timeout_s)
        _safe_unlink(out_path)
        return None
    except OSError as exc:
        logger.warning("skillspector could not be executed for %s: %s", skill_dir, exc)
        _safe_unlink(out_path)
        return None

    # SkillSpector exits NON-ZERO to signal risk level (e.g. a CRITICAL skill →
    # rc=1), not failure — it still writes a valid report. So trust the JSON
    # whenever it parses; treat only missing/invalid output as a real failure.
    # (Gating on returncode here would silently drop the highest-risk skills.)
    try:
        with open(out_path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "skillspector produced no usable report for %s (rc=%s): %s | %s",
            skill_dir, proc.returncode, exc, (proc.stderr or "")[:200],
        )
        return None
    finally:
        _safe_unlink(out_path)


def _safe_unlink(path: str) -> None:
    import contextlib
    import os

    with contextlib.suppress(OSError):
        os.unlink(path)


async def store_finding(db: object, finding: dict) -> str | None:
    """Persist one finding as a recon observation (source='recon', type='finding').

    Mirrors ``recon_store_finding``'s storage contract so findings surface via
    ``recon_findings(job_type="skill-security")``. Dedupes on content hash, so
    re-scanning an unchanged skill does not pile up duplicate findings.
    """
    import uuid
    from datetime import UTC, datetime

    from genesis.db.crud import observations as obs_crud

    content = finding["title"]
    if finding.get("summary"):
        content += f"\n\n{finding['summary']}"
    if finding.get("source_url"):
        content += f"\n\nSource: {finding['source_url']}"

    finding_id = await obs_crud.create(
        db,  # type: ignore[arg-type]
        id=str(uuid.uuid4()),
        source="recon",
        type="finding",
        category=finding["job_type"],
        content=content,
        priority=finding["priority"],
        created_at=datetime.now(UTC).isoformat(),
        skip_if_duplicate=True,
    )
    await db.commit()  # type: ignore[attr-defined]
    return finding_id


def _default_roots() -> list[Path]:
    """Default skill roots to sweep (some live outside the repo)."""
    roots = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".genesis" / "skill-library",
    ]
    try:
        import genesis

        repo = Path(genesis.__file__).resolve().parents[2]
        roots += [repo / ".claude" / "skills", repo / "src" / "genesis" / "skills"]
    except Exception:  # pragma: no cover - best-effort repo-root resolution
        pass
    return roots


def main(argv: list[str] | None = None) -> int:
    """CLI: scan installed skills with SkillSpector and file recon findings."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="Scan installed skills with NVIDIA SkillSpector → recon findings.",
    )
    parser.add_argument("--roots", nargs="*", help="Skill root dirs (default: standard skill locations).")
    parser.add_argument("--skillspector-bin", default=None, help="Path to the skillspector binary.")
    parser.add_argument("--timeout", type=int, default=120, help="Per-skill scan timeout (seconds).")
    parser.add_argument("--min-score", type=int, default=1, help="Only file findings at/above this score.")
    parser.add_argument("--llm", action="store_true", help="Enable SkillSpector's LLM stage (default: static-only).")
    parser.add_argument("--no-store", action="store_true", help="Scan + print only; do not write recon findings.")
    args = parser.parse_args(argv)

    roots = [Path(r) for r in args.roots] if args.roots else _default_roots()
    skill_dirs = discover_skill_dirs(roots)
    print(f"Discovered {len(skill_dirs)} skills across {len(roots)} roots.")

    def scanner(skill_dir: Path) -> dict | None:
        return run_skillspector(
            skill_dir,
            skillspector_bin=args.skillspector_bin,
            timeout_s=args.timeout,
            no_llm=not args.llm,
        )

    async def _go() -> int:
        if args.no_store:
            async def _noop(_db: object, _finding: dict) -> None:
                return None

            results = await scan_and_store(
                None, skill_dirs, scanner=scanner, storer=_noop, min_score=args.min_score
            )
        else:
            import aiosqlite

            from genesis.env import genesis_db_path

            async with aiosqlite.connect(str(genesis_db_path())) as db:
                await db.execute("PRAGMA busy_timeout=5000")
                results = await scan_and_store(
                    db, skill_dirs, scanner=scanner, storer=store_finding, min_score=args.min_score
                )

        filed = sum(1 for r in results if r.score >= args.min_score)
        print(f"Scanned {len(results)} skills; filed {filed} findings (min_score={args.min_score}).\n")
        for r in sorted(results, key=lambda r: r.score, reverse=True):
            print(f"  {r.score:3d}/100  {r.severity:8s}  {r.name}  ({len(r.issues)} issues)")
        return 0

    return asyncio.run(_go())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
