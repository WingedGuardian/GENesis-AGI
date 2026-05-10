"""Session observer — async processor for PostToolUse hook observations.

Reads per-session JSONL files written by the PostToolUse hook, batches
them into LLM calls for structured extraction, and stores the results
as memories via MemoryStore.  Called from the awareness loop tick.

The hook writes raw tool observations to:
    ~/.genesis/sessions/{session_id}/tool_observations.jsonl

This processor:
1. Scans for unprocessed JSONL files
2. Batches observations (up to MAX_OBS_PER_LLM_CALL per LLM request)
3. Extracts structured notes via router (call site 21_session_observer)
4. Stores results in memory with source_pipeline="session_observer"
5. Renames processed files to .done to prevent re-processing
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Processing limits
MAX_OBS_PER_TICK = 50  # Total observations to process per awareness tick
MAX_OBS_PER_LLM_CALL = 15  # Observations to batch per LLM call
MAX_PROCESSING_TIME_S = 45.0  # Budget per tick (leave headroom under 60s)
# Files older than this are cleaned up (stale sessions)
STALE_FILE_AGE_S = 7 * 24 * 3600  # 7 days

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

OBSERVER_PROMPT = """\
You are analyzing tool activity from a coding session. Extract a concise
session note summarizing what happened.

For each batch of tool uses below, produce a JSON object with:
- "notes": array of structured notes, each with:
  - "title": one-line summary (under 80 chars)
  - "type": one of: decision, bugfix, feature, refactor, discovery, investigation, configuration
  - "narrative": 1-3 sentence description of what happened and why
  - "files": array of file paths involved (deduplicated)
  - "concepts": array of key technical concepts/terms (max 5)

Focus on SUBSTANCE:
- What files were read/modified and why
- What commands were run and their purpose
- What was discovered or decided
- Skip: routine reads with no follow-up, glob/grep with no actionable result

Consolidate related tool uses into single notes. A Read followed by Edit
on the same file is one note, not two.

Respond with JSON inside triple backticks:

```json
{
  "notes": [
    {
      "title": "Fixed memory_recall compact mode filtering",
      "type": "bugfix",
      "narrative": "Wing/room post-retrieval filter was returning too few results because it filtered after limiting. Fixed by over-fetching 3x when filters are active.",
      "files": ["src/genesis/mcp/memory/core.py"],
      "concepts": ["memory_recall", "post-retrieval filtering", "wing/room taxonomy"]
    }
  ]
}
```

If the tool activity has no extractable substance (e.g., only glob searches
with no follow-up), return: `{"notes": []}`

Here is the tool activity to analyze:

{observations_text}
"""


@dataclass
class SessionNote:
    """A single extracted session note."""

    title: str
    note_type: str
    narrative: str
    files: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)


@dataclass
class ProcessingResult:
    """Summary of one processing cycle."""

    files_processed: int = 0
    observations_read: int = 0
    notes_extracted: int = 0
    notes_stored: int = 0
    llm_calls: int = 0
    errors: int = 0
    elapsed_s: float = 0.0


def _sessions_dir() -> Path:
    return Path(os.path.expanduser("~/.genesis/sessions"))


def _find_observation_files() -> list[Path]:
    """Find all unprocessed tool_observations.jsonl files."""
    sessions_dir = _sessions_dir()
    if not sessions_dir.exists():
        return []
    files = []
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        obs_file = session_dir / "tool_observations.jsonl"
        if obs_file.exists() and obs_file.stat().st_size > 0:
            files.append(obs_file)
    return files


def _read_observations(path: Path, limit: int) -> list[dict]:
    """Read observations from a JSONL file, up to limit."""
    observations = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    observations.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(observations) >= limit:
                    break
    except OSError:
        logger.warning("Failed to read observations from %s", path, exc_info=True)
    return observations


def _format_observations_for_prompt(observations: list[dict]) -> str:
    """Format a batch of observations into readable text for the LLM."""
    lines = []
    for i, obs in enumerate(observations, 1):
        tool = obs.get("tool_name", "?")
        info = obs.get("key_info", {})
        output = obs.get("output_summary", "")

        # Format key info compactly
        info_parts = []
        for k, v in info.items():
            if v:
                info_parts.append(f"{k}={v}")
        info_str = ", ".join(info_parts) if info_parts else "(no input)"

        # Truncate output further for the prompt
        if output and len(output) > 300:
            output = output[:300] + "..."

        lines.append(f"{i}. [{tool}] {info_str}")
        if output:
            lines.append(f"   Output: {output}")
        lines.append("")

    return "\n".join(lines)


def _parse_notes(response_text: str) -> list[SessionNote]:
    """Parse LLM response into SessionNote objects."""
    match = _JSON_BLOCK_RE.search(response_text)
    raw = match.group(1).strip() if match else response_text.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse session observer JSON")
        return []

    if isinstance(data, dict):
        notes_data = data.get("notes", [])
    elif isinstance(data, list):
        notes_data = data
    else:
        return []

    notes = []
    for item in notes_data:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        notes.append(SessionNote(
            title=item.get("title", ""),
            note_type=item.get("type", "discovery"),
            narrative=item.get("narrative", ""),
            files=[str(f) for f in item.get("files", []) if f],
            concepts=[str(c) for c in item.get("concepts", []) if c],
        ))
    return notes


def _infer_wing_from_files(files: list[str]) -> str | None:
    """Infer a memory wing from file paths (best effort)."""
    for f in files:
        if "/memory/" in f or "/retrieval" in f:
            return "memory"
        if any(p in f for p in ("/awareness/", "/runtime/", "/guardian/", "/sentinel/",
                                "/hooks/", "/config/", "/.claude/", "/scripts/")):
            return "infrastructure"
        if "/learning/" in f or "/perception/" in f or "/reflection/" in f:
            return "learning"
        if "/routing/" in f or "/providers/" in f:
            return "routing"
        if "/outreach/" in f or "/channels/" in f or "/dashboard" in f:
            return "channels"
        if "/autonomy/" in f or "/tasks/" in f:
            return "autonomy"
    return None


async def process_pending_observations(
    *,
    store: MemoryStore,
    router: Router,
) -> ProcessingResult:
    """Process pending session observations. Called from awareness loop tick.

    Reads JSONL files, batches into LLM calls, stores extracted notes.
    Returns a summary for observability.
    """
    result = ProcessingResult()
    start = time.monotonic()

    obs_files = _find_observation_files()
    if not obs_files:
        return result

    # Atomic rename: move each JSONL to .processing so the hook creates
    # a fresh file for new writes.  This prevents race conditions where
    # the hook appends between our read and truncate.
    processing_files: list[tuple[Path, Path]] = []  # (original, processing)
    for obs_file in obs_files:
        processing_path = obs_file.with_suffix(".jsonl.processing")
        try:
            os.rename(obs_file, processing_path)
            processing_files.append((obs_file, processing_path))
        except OSError:
            # File may have been removed or renamed by another process
            continue

    if not processing_files:
        _cleanup_stale_files(obs_files)
        return result

    # Read observations from renamed files, respecting budget
    all_observations: list[tuple[Path, list[dict]]] = []
    total_obs = 0

    for _original, processing_path in processing_files:
        remaining = MAX_OBS_PER_TICK - total_obs
        if remaining <= 0:
            break
        observations = _read_observations(processing_path, limit=remaining)
        if observations:
            all_observations.append((processing_path, observations))
            total_obs += len(observations)
            result.observations_read += len(observations)

    if not all_observations:
        # Clean up processing files (they were empty or unreadable)
        import contextlib
        for _original, processing_path in processing_files:
            with contextlib.suppress(OSError):
                processing_path.unlink(missing_ok=True)
        return result

    # Process in batches via LLM
    batch: list[dict] = []
    for _path, observations in all_observations:
        batch.extend(observations)

    # Split into LLM-sized batches
    for batch_start in range(0, len(batch), MAX_OBS_PER_LLM_CALL):
        if time.monotonic() - start > MAX_PROCESSING_TIME_S:
            logger.info("Session observer hit time budget, stopping")
            break

        batch_slice = batch[batch_start:batch_start + MAX_OBS_PER_LLM_CALL]
        obs_text = _format_observations_for_prompt(batch_slice)
        prompt = OBSERVER_PROMPT.replace("{observations_text}", obs_text)

        try:
            # 21_session_observer — session-observer-driven memory writes.
            # Routes via the free chain for opportunistic context capture.
            response = await router.route_call(
                call_site_id="21_session_observer",
                messages=[{"role": "user", "content": prompt}],
            )
            result.llm_calls += 1

            if not response.success:
                logger.warning("Session observer LLM call failed: %s", response.error)
                result.errors += 1
                continue

            notes = _parse_notes(response.content)
            result.notes_extracted += len(notes)

            # Store each note as a memory
            for note in notes:
                try:
                    content = f"[{note.note_type}] {note.title}\n{note.narrative}"
                    tags = ["session_note", note.note_type]
                    tags.extend(note.concepts[:3])

                    wing = _infer_wing_from_files(note.files)

                    await store.store(
                        content=content,
                        source="session_observer",
                        memory_type="episodic",
                        tags=tags,
                        confidence=0.6,
                        source_pipeline="session_observer",
                        wing=wing,
                    )
                    result.notes_stored += 1
                except Exception:
                    logger.warning("Failed to store session note: %s", note.title, exc_info=True)
                    result.errors += 1

        except Exception:
            logger.warning("Session observer batch processing failed", exc_info=True)
            result.errors += 1

    # Clean up processing files — already fully read, safe to remove
    for processing_path, _observations in all_observations:
        try:
            processing_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove %s", processing_path, exc_info=True)

    # Also clean up any processing files we didn't read (budget exceeded)
    for _original, processing_path in processing_files:
        if processing_path.exists():
            try:
                # These had observations we didn't process — rename back
                # so they're picked up next tick
                original = processing_path.with_suffix(".jsonl")
                if original.exists():
                    # Original was recreated by hook — append our unprocessed
                    # data to the new file
                    with open(original, "a") as dst, open(processing_path) as src:
                        dst.write(src.read())
                    processing_path.unlink(missing_ok=True)
                else:
                    os.rename(processing_path, original)
            except OSError:
                logger.warning("Failed to restore %s", processing_path, exc_info=True)

    result.files_processed = len(all_observations)
    result.elapsed_s = time.monotonic() - start

    if result.notes_stored > 0:
        logger.info(
            "Session observer: %d notes stored from %d observations (%.1fs, %d LLM calls)",
            result.notes_stored, result.observations_read, result.elapsed_s, result.llm_calls,
        )

    return result


def _cleanup_stale_files(files: list[Path]) -> None:
    """Remove .done files older than STALE_FILE_AGE_S."""
    now = time.time()
    for obs_file in files:
        done_file = obs_file.with_suffix(".jsonl.done")
        try:
            if done_file.exists() and (now - done_file.stat().st_mtime) > STALE_FILE_AGE_S:
                done_file.unlink()
        except OSError:
            pass
