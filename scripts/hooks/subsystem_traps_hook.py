#!/usr/bin/env python3
"""PostToolUse Edit|Write hook: inject the edited file's subsystem traps.

Code-locality-aware context injection (dev-quality component e): editing a
file under ``src/genesis/<module>/`` surfaces THAT subsystem's
easy-to-forget mechanisms and do-not-touch edges from
``docs/architecture/CURRENT.md`` — once per session per subsystem — so
invariants arrive at edit time instead of at review time.

Design constraints:
- Pure file parse, no LLM calls, no caching (the file is ~25KB; a regex
  parse is <10ms — an mtime cache is unjustified complexity).
- Injected once per session per subsystem entry (dedup file in the session
  dir, same shape as skill_injection_hook's nudge dedup).
- SHORT: trap/do-not-touch lines + first-line-only bullets, hard cap.
- Fail-open: any error exits 0 silently. Advisory context only — the
  documented PostToolUse ``additionalContext`` channel; never blocks.
- Worktree-correct by construction: the genesis-hook wrapper resolves this
  script inside the worktree being edited, so the injected map matches the
  branch's own CURRENT.md.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path

# Same fence pattern the CI guard (scripts/check_subsystem_map.py) enforces,
# so the modules->entry mapping this hook relies on is guaranteed complete.
_BLOCK_RE = re.compile(r"^```yaml[ \t]+subsystem-map[ \t]*$")
_SECTION_RE = re.compile(r"^## ", re.MULTILINE)

_MAX_BULLETS = 6
_MAX_CHARS = 1200
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _current_md_path() -> Path:
    override = os.environ.get("GENESIS_CURRENT_MD_PATH")
    if override:
        return Path(override)
    return _repo_root() / "docs" / "architecture" / "CURRENT.md"


def _sessions_dir() -> Path:
    override = os.environ.get("GENESIS_SESSIONS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".genesis" / "sessions"


def _parse_sections(text: str) -> list[dict]:
    """Split CURRENT.md into ## sections carrying entry/modules + body."""
    sections = []
    starts = [m.start() for m in _SECTION_RE.finditer(text)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        body = text[start:end]
        lines = body.splitlines()
        entry, modules = None, []
        in_block = False
        for line in lines:
            if _BLOCK_RE.match(line):
                in_block = True
                continue
            if in_block:
                if line.startswith("```"):
                    in_block = False
                    continue
                if line.startswith("entry:"):
                    entry = line.split(":", 1)[1].strip()
                elif line.startswith("modules:"):
                    raw = line.split(":", 1)[1].strip().strip("[]")
                    modules = [m.strip() for m in raw.split(",") if m.strip()]
        if entry:
            sections.append(
                {"title": lines[0].lstrip("# ").strip(), "entry": entry,
                 "modules": modules, "body": body}
            )
    return sections


def _extract_traps(body: str) -> list[str]:
    """Priority: **Do not touch:**/**Trap:** lines, then top-level bullets."""
    picked: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("**Do not touch:**", "**Trap:**")):
            picked.append(stripped)
    for line in body.splitlines():
        if len(picked) >= _MAX_BULLETS:
            break
        if line.startswith("- "):  # top-level bullets only, first line only
            picked.append(line[2:].strip())
    return picked[:_MAX_BULLETS]


def _top_module(file_path: str) -> str | None:
    parts = Path(file_path).parts
    try:
        idx = len(parts) - 1 - parts[::-1].index("genesis")
    except ValueError:
        return None
    # .../src/genesis/<top>/... or .../src/genesis/<top>.py
    if idx == 0 or parts[idx - 1] != "src" or idx + 1 >= len(parts):
        return None
    top = parts[idx + 1]
    return top[:-3] if top.endswith(".py") else top


def _already_nudged(session_id: str, entry: str) -> bool:
    if not _SESSION_ID_RE.match(session_id):
        return True  # refuse traversal-shaped ids: stay silent
    dedup = _sessions_dir() / session_id / "subsystem_nudges.json"
    seen: list[str] = []
    try:
        if dedup.is_file():
            loaded = json.loads(dedup.read_text())
            if isinstance(loaded, list):
                seen = [s for s in loaded if isinstance(s, str)]
    except Exception:
        seen = []
    if entry in seen:
        return True
    try:
        dedup.parent.mkdir(parents=True, exist_ok=True)
        dedup.write_text(json.dumps([*seen, entry]))
    except Exception:
        pass  # dedup write failure => worst case a repeat nudge later
    return False


def _process(data: dict) -> None:
    if data.get("tool_name") not in ("Edit", "Write"):
        return
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return
    file_path = tool_input.get("file_path") or ""
    top = _top_module(file_path)
    if not top:
        return
    md_path = _current_md_path()
    if not md_path.is_file():
        return
    section = next(
        (s for s in _parse_sections(md_path.read_text(encoding="utf-8"))
         if top in s["modules"]),
        None,
    )
    if section is None:
        return
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return
    # Extract BEFORE marking the dedup: a section with no extractable traps
    # must not consume the once-per-session slot without injecting anything.
    traps = _extract_traps(section["body"])
    if not traps:
        return
    if _already_nudged(session_id, section["entry"]):
        return
    lines = "\n".join(f"- {t}" for t in traps)
    context = (
        f"[Subsystem map] You are editing '{top}' — subsystem "
        f"'{section['title']}' (docs/architecture/CURRENT.md). "
        f"Load-bearing traps:\n{lines}\n"
        "(Injected once per session per subsystem; consult the full entry "
        "before structural changes.)"
    )[:_MAX_CHARS]
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )


def main() -> int:
    # Hooks fail open — never crash the session.
    with contextlib.suppress(Exception):
        _process(json.loads(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
