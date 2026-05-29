"""CC memory staleness scanner — identifies stale Claude Code memory files.

Weekly surplus task that scans ~/.claude/projects/-home-ubuntu-genesis/memory/
for stale content.  Flags candidates for human review via observations.
Does NOT auto-delete anything.

Task type: CC_MEMORY_STALENESS
Compute tier: FREE_API (no LLM — pure filesystem + subprocess)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.surplus.types import ExecutorResult, SurplusTask

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────

def _memory_dir() -> Path:
    """Resolve CC memory directory using the project-slug utility."""
    try:
        from genesis.env import cc_project_dir
        return Path.home() / ".claude" / "projects" / cc_project_dir() / "memory"
    except Exception:
        return Path.home() / ".claude" / "projects" / "-home-ubuntu-genesis" / "memory"
_SKIP_FILES = {"MEMORY.md", "MEMORY_STAGED.md"}
_PR_PATTERN = re.compile(r"PR\s*#?(\d{3,5})")
_ACTIVE_LANGUAGE = re.compile(
    r"\b(active|pending|in progress|WIP|open|blocked|TODO|awaiting|deferred)\b",
    re.IGNORECASE,
)
_IP_PATTERN = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
_API_KEY_PATTERN = re.compile(r"api[_-]?key|API_KEY", re.IGNORECASE)
_GH_TIMEOUT = 10  # seconds per gh call
_MAX_PR_CHECKS = 20  # cap gh calls per scan


# ─── Data structures ────────────────────────────────────────────────────

@dataclass
class MemoryFile:
    """Parsed CC memory file."""

    path: Path
    stem: str
    name: str
    description: str
    type: str
    content: str
    mtime: datetime
    pr_numbers: set[int]


@dataclass
class StalenessFlag:
    """A memory file flagged for staleness review."""

    file: MemoryFile
    reason: str
    detail: str


# ─── Executor ────────────────────────────────────────────────────────────

class CCMemoryStalenessExecutor:
    """Scans CC memory files for staleness indicators.

    Tier 1 (observation-only): flags candidates, never modifies files.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        """Run the staleness scan."""
        now = datetime.now(UTC)

        # 1. Discover and parse memory files
        files = _discover_files()
        if not files:
            return ExecutorResult(
                success=True,
                content="No CC memory files found.",
                insights=[],
            )

        parsed = []
        for path in files:
            mem = _parse_memory_file(path)
            if mem is not None:
                parsed.append(mem)

        # 2. Collect unique PR numbers from project files
        all_prs: set[int] = set()
        for mem in parsed:
            if mem.type == "project":
                all_prs.update(mem.pr_numbers)

        # 3. Batch check PR states
        pr_states = await _check_pr_states(all_prs)

        # 4. Assess staleness per file
        flags: list[StalenessFlag] = []
        for mem in parsed:
            flag = None
            if mem.type == "project":
                flag = _assess_project(mem, pr_states, now)
            elif mem.type == "reference":
                flag = _assess_reference(mem, now)
            if flag is not None:
                flags.append(flag)

        # 5. Write observations
        written = await self._write_observations(flags, now)

        # 6. Build report
        report_lines = [f"CC Memory Staleness Scan: {len(parsed)} files, {len(flags)} flagged, {written} observations written."]
        for f in flags:
            age = (now - f.file.mtime).days
            report_lines.append(f"  - {f.file.stem} ({f.reason}, {age}d old): {f.detail}")

        content = "\n".join(report_lines)
        insights = []
        if flags:
            insights.append({
                "content": content,
                "source_task_type": str(task.task_type),
                "generating_model": "cc_memory_staleness",
                "drive_alignment": task.drive_alignment,
                "confidence": 0.8,
                "flagged_count": len(flags),
                "scanned_count": len(parsed),
            })

        logger.info(
            "CC memory staleness scan: %d files scanned, %d flagged",
            len(parsed), len(flags),
        )
        return ExecutorResult(success=True, content=content, insights=insights)

    async def _write_observations(
        self, flags: list[StalenessFlag], now: datetime,
    ) -> int:
        """Write observations for flagged candidates. Returns count written."""
        from datetime import timedelta

        from genesis.db.crud import observations

        written = 0
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(days=14)).isoformat()

        for flag in flags:
            obs_id = f"cc_memory_stale_{flag.file.stem}"

            # Don't re-flag if recently resolved (within 30 days)
            try:
                cursor = await self._db.execute(
                    "SELECT resolved, resolved_at FROM observations WHERE id = ?",
                    (obs_id,),
                )
                row = await cursor.fetchone()
                if row and row[0]:  # resolved == 1
                    resolved_at = row[1]
                    if resolved_at:
                        resolved_dt = datetime.fromisoformat(resolved_at)
                        if (now - resolved_dt).days < 30:
                            continue
            except Exception:
                pass  # DB error — proceed with upsert

            age_days = (now - flag.file.mtime).days
            try:
                await observations.upsert(
                    self._db,
                    id=obs_id,
                    source="cc_memory_staleness",
                    type="cc_memory_staleness",
                    content=f"[{flag.reason}] {flag.file.stem}.md: {flag.detail} (last modified {age_days}d ago)",
                    priority="low",
                    created_at=now_iso,
                    category="memory_hygiene",
                    expires_at=expires_iso,
                )
                written += 1
            except Exception:
                logger.warning(
                    "Failed to upsert staleness observation for %s",
                    flag.file.stem, exc_info=True,
                )

        return written


# ─── File discovery & parsing ────────────────────────────────────────────

def _discover_files() -> list[Path]:
    """Find all .md memory files, excluding index files."""
    mem_dir = _memory_dir()
    if not mem_dir.is_dir():
        return []
    return [
        f for f in sorted(mem_dir.glob("*.md"))
        if f.name not in _SKIP_FILES
    ]


def _parse_memory_file(path: Path) -> MemoryFile | None:
    """Parse YAML frontmatter and content from a memory file."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    # Parse simple YAML frontmatter (between --- delimiters)
    name = path.stem
    description = ""
    mem_type = "unknown"
    content = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            content = parts[2]
            for line in frontmatter.strip().splitlines():
                line = line.strip()
                if line.startswith("name:"):
                    name = line[5:].strip().strip('"').strip("'")
                elif line.startswith("description:"):
                    description = line[12:].strip().strip('"').strip("'")
                elif line.startswith("type:"):
                    mem_type = line[5:].strip().strip('"').strip("'")

    # Extract PR references
    pr_numbers = {int(m) for m in _PR_PATTERN.findall(text)}

    # Get file mtime
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except Exception:
        mtime = datetime.now(UTC)

    return MemoryFile(
        path=path,
        stem=path.stem,
        name=name,
        description=description,
        type=mem_type,
        content=content,
        mtime=mtime,
        pr_numbers=pr_numbers,
    )


# ─── PR status checking ─────────────────────────────────────────────────

async def _check_pr_states(pr_numbers: set[int]) -> dict[int, dict]:
    """Check PR states via gh CLI. Returns {pr_num: {state, mergedAt}}."""
    if not pr_numbers:
        return {}

    results: dict[int, dict] = {}
    # Cap to avoid rate limiting
    to_check = sorted(pr_numbers)[:_MAX_PR_CHECKS]

    for pr_num in to_check:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "view", str(pr_num),
                "--json", "state,mergedAt",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_GH_TIMEOUT,
            )
            if proc.returncode == 0 and stdout:
                data = json.loads(stdout.decode())
                results[pr_num] = data
        except TimeoutError:
            logger.debug("gh pr view %d timed out", pr_num)
        except Exception:
            logger.debug("gh pr view %d failed", pr_num, exc_info=True)

    return results


# ─── Staleness assessment ────────────────────────────────────────────────

def _assess_project(
    mem: MemoryFile,
    pr_states: dict[int, dict],
    now: datetime,
) -> StalenessFlag | None:
    """Assess staleness for a project-type memory."""
    age_days = (now - mem.mtime).days

    # Check if all referenced PRs are merged
    if mem.pr_numbers and age_days > 7:
        checked_prs = {pr for pr in mem.pr_numbers if pr in pr_states}
        if checked_prs:
            all_merged = all(
                pr_states[pr].get("state") == "MERGED"
                for pr in checked_prs
            )
            if all_merged:
                pr_list = ", ".join(f"#{pr}" for pr in sorted(checked_prs))
                return StalenessFlag(
                    file=mem,
                    reason="completed project",
                    detail=f"All referenced PRs merged ({pr_list})",
                )

    # Check for active language in old files
    if age_days > 30 and _ACTIVE_LANGUAGE.search(mem.content):
        return StalenessFlag(
            file=mem,
            reason="stale active project",
            detail="Contains active-language markers but not modified in 30+ days",
        )

    return None


def _assess_reference(
    mem: MemoryFile,
    now: datetime,
) -> StalenessFlag | None:
    """Assess staleness for a reference-type memory."""
    age_days = (now - mem.mtime).days

    # Sensitive references (IPs, API keys) — shorter threshold
    if age_days > 45 and (_IP_PATTERN.search(mem.content) or _API_KEY_PATTERN.search(mem.content)):
            return StalenessFlag(
                file=mem,
                reason="verify reference",
                detail="Contains IPs or API key references, not modified in 45+ days",
            )

    # General reference — longer threshold
    if age_days > 60:
        return StalenessFlag(
            file=mem,
            reason="may be outdated",
            detail="Reference not modified in 60+ days",
        )

    return None
