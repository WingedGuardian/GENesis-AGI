#!/usr/bin/env python3
"""UserPromptSubmit hook: on-demand skill injection.

Checks prompt keywords against the skill catalog and injects a light
pointer (~30 tokens) for matching skills. Does NOT inject full skill
content — Genesis decides whether to load.

Budget: <50ms (JSON file read + keyword match).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Skip in dispatched sessions
if os.environ.get("GENESIS_CC_SESSION") == "1":
    sys.exit(0)

CATALOG_PATH = Path.home() / ".genesis" / "skill_catalog.json"
_CATALOG_MAX_AGE_S = 3600  # Regenerate catalog if older than 1h

# Minimum confidence threshold — keyword overlap score must exceed this
_MIN_CONFIDENCE = 0.3
# Max nudges per prompt
_MAX_NUDGES = 1


def _ensure_catalog_fresh() -> None:
    """Regenerate the skill catalog if it's missing or stale (>1h old)."""
    try:
        if CATALOG_PATH.exists():
            import time
            age = time.time() - CATALOG_PATH.stat().st_mtime
            if age < _CATALOG_MAX_AGE_S:
                return
        # Locate and run the generator
        gen_script = Path(__file__).resolve().parents[1] / "generate_skill_catalog.py"
        if gen_script.exists():
            import subprocess
            subprocess.run(
                [sys.executable, str(gen_script)],
                capture_output=True, timeout=5,
            )
    except Exception as exc:
        # Never block prompt, but emit diagnostics
        print(f"Catalog refresh failed: {exc}", file=sys.stderr)


def _load_catalog() -> dict:
    """Load the skill catalog from disk."""
    if not CATALOG_PATH.exists():
        return {"tier1": [], "tier2": []}
    try:
        return json.loads(CATALOG_PATH.read_text())
    except Exception:
        return {"tier1": [], "tier2": []}


def _session_nudges_path(session_id: str) -> Path | None:
    """Return the path for session nudge tracking, or None if invalid."""
    if not session_id or "/" in session_id or ".." in session_id:
        return None
    return Path.home() / ".genesis" / "sessions" / session_id / "skill_nudges.json"


def _load_session_nudges(session_id: str) -> set[str]:
    """Load which skills have already been nudged this session."""
    path = _session_nudges_path(session_id)
    if not path or not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except Exception:
        return set()


def _save_session_nudge(session_id: str, skill_name: str) -> None:
    """Record that a skill was nudged in this session."""
    path = _session_nudges_path(session_id)
    if not path:
        return
    existing = _load_session_nudges(session_id)
    existing.add(skill_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(existing)))


def _score_skill(skill: dict, keywords: list[str]) -> float:
    """Score a skill against prompt keywords. Returns 0.0-1.0."""
    if not keywords:
        return 0.0

    name_lower = skill.get("name", "").lower().replace("-", " ").replace("_", " ")
    desc_lower = skill.get("description", "").lower()

    matches = 0
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in name_lower:
            matches += 2  # Name match weighted 2x
        elif kw_lower in desc_lower:
            matches += 1

    max_score = 2 * len(keywords)
    return min(matches / max_score, 1.0) if max_score > 0 else 0.0


def _extract_keywords(prompt: str) -> list[str]:
    """Extract significant keywords from prompt (minimal, no deps)."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in prompt)
    words = cleaned.lower().split()
    stop = {
        "the", "is", "are", "was", "and", "or", "but", "for", "with",
        "this", "that", "can", "you", "how", "what", "when", "where",
        "why", "not", "let", "use",
    }
    return [w for w in words if len(w) >= 3 and w not in stop][:10]


def main() -> None:
    """Hook entry point."""
    try:
        _ensure_catalog_fresh()

        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
        prompt = data.get("prompt", "")
        session_id = data.get("session_id", "")

        if not prompt:
            return

        catalog = _load_catalog()
        if not catalog.get("tier1") and not catalog.get("tier2"):
            return

        keywords = _extract_keywords(prompt)
        if not keywords:
            return

        already_nudged = _load_session_nudges(session_id)

        # Score all skills
        candidates = []
        for skill in catalog.get("tier1", []) + catalog.get("tier2", []):
            name = skill.get("name", "")
            if name in already_nudged:
                continue
            score = _score_skill(skill, keywords)
            if score >= _MIN_CONFIDENCE:
                candidates.append((score, skill))

        candidates.sort(key=lambda x: x[0], reverse=True)

        for _score, skill in candidates[:_MAX_NUDGES]:
            name = skill.get("name", "")
            tier = skill.get("tier", "?")
            desc = skill.get("description", "")

            if tier == 1:
                print(f"[Skill] The '{name}' skill is relevant here. {desc[:80]}")
            else:
                print(
                    f"[Skill] The '{name}' skill matches this task. "
                    f"Load with /skill {name}. {desc[:60]}"
                )

            _save_session_nudge(session_id, name)

        sys.stdout.flush()
    except Exception:
        import traceback

        print(traceback.format_exc(), file=sys.stderr)


if __name__ == "__main__":
    main()
