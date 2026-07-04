#!/usr/bin/env python3
"""Generate a compact skill catalog from skill directories.

Scans Tier 1 (.claude/skills/) and Tier 2 directories for SKILL.md or skill
definition files.  Extracts name + one-line description.  Writes to
~/.genesis/skill_catalog.json.

Tier 2 sources (skill library):
  - src/genesis/skills/  — repo-versioned domain skills
  - ~/.genesis/skill-library/ — user-added ad-hoc skills
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TIER1_DIR = REPO_ROOT / ".claude" / "skills"
TIER2_DIRS = [
    REPO_ROOT / "src" / "genesis" / "skills",
    Path.home() / ".genesis" / "skill-library",
]
CATALOG_PATH = Path.home() / ".genesis" / "skill_catalog.json"


def _extract_skill_info(skill_dir: Path) -> dict | None:
    """Extract name and description from a skill directory.

    Looks for SKILL.md with YAML frontmatter, or any .md file with a
    name/description pattern.
    """
    for md_name in ("SKILL.md", "skill.md", "README.md"):
        md_file = skill_dir / md_name
        if md_file.exists():
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                return _parse_frontmatter(content, fallback_name=skill_dir.name)
            except Exception:
                continue

    # Fallback: use directory name
    return {"name": skill_dir.name, "description": "", "keywords": []}


def _parse_frontmatter(content: str, fallback_name: str = "") -> dict:
    """Parse YAML-like frontmatter from a markdown file."""
    name = fallback_name
    description = ""

    # Check for YAML frontmatter (--- delimited)
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            frontmatter = content[3:end]
            name_match = re.search(r'name:\s*["\']?([^"\'\n]+)', frontmatter)
            if name_match:
                name = name_match.group(1).strip()
            # Handle YAML folded/literal scalars (> or |) and inline values
            desc_match = re.search(
                r'description:\s*["\']?([^"\'\n]+)', frontmatter
            )
            if desc_match:
                val = desc_match.group(1).strip()
                if val in (">", "|", ">-", "|-"):
                    # Folded/literal scalar: collect indented continuation lines
                    desc_start = desc_match.end()
                    lines = frontmatter[desc_start:].split("\n")
                    parts = []
                    for line in lines:
                        stripped = line.strip()
                        if not stripped:
                            if parts:
                                break  # blank line ends the block
                            continue
                        # Continuation lines are indented
                        if line.startswith("  ") or line.startswith("\t"):
                            parts.append(stripped)
                        elif parts:
                            break
                    description = " ".join(parts)
                else:
                    description = val

    keywords = []
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            frontmatter = content[3:end]
            kw_match = re.search(
                r"keywords:\s*\[([^\]]*)\]", frontmatter
            )
            if kw_match:
                raw = kw_match.group(1)
                keywords = [
                    k.strip().strip("'\"")
                    for k in raw.split(",")
                    if k.strip()
                ]

    return {"name": name, "description": description, "keywords": keywords}


# A directory without its own SKILL.md may be a container of real skills
# (e.g. gitnexus/<skill>/SKILL.md, or a plugin repo laid out as
# <plugin>/skills/<skill>/SKILL.md). These fixed-depth globs detect that.
_NESTED_SKILL_GLOBS = ("*/SKILL.md", "skills/*/SKILL.md", "*/skills/*/SKILL.md")
# Recursion cap: tier dir = depth 0; deepest known layout is
# skill-library/<vendor>/<plugin>/skills/<skill>/SKILL.md (depth 3).
_MAX_SCAN_DEPTH = 3


def _has_own_skill_md(entry: Path) -> bool:
    """True if the directory is itself a skill (has a SKILL.md marker)."""
    return (entry / "SKILL.md").exists() or (entry / "skill.md").exists()


def _has_nested_skills(entry: Path) -> bool:
    """True if the directory contains skills nested below it."""
    return any(
        next(entry.glob(pattern), None) is not None
        for pattern in _NESTED_SKILL_GLOBS
    )


def _scan_tier(
    tier_dir: Path,
    tier_num: int,
    repo_root: Path | None,
    _depth: int = 0,
) -> list[dict]:
    """Scan a single tier directory for skills, recursing into containers.

    A child directory with its own SKILL.md is indexed as a skill. A child
    without one that holds nested SKILL.md files is a container — recurse
    and index the real skills instead of emitting a phantom entry for the
    container itself. Inside containers, directories with neither marker
    (plugin repos carry hooks/, scripts/, docs/) are skipped; at the top
    level they keep the name-only fallback entry.
    """
    results: list[dict] = []
    if not tier_dir.is_dir():
        return results

    for entry in sorted(tier_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not _has_own_skill_md(entry):
            if _depth < _MAX_SCAN_DEPTH and _has_nested_skills(entry):
                results.extend(_scan_tier(entry, tier_num, repo_root, _depth + 1))
                continue
            if _depth > 0:
                # Support dir inside a container (hooks/, scripts/, …).
                continue
        info = _extract_skill_info(entry)
        if info:
            info["tier"] = tier_num
            if repo_root and entry.is_relative_to(repo_root):
                info["path"] = str(entry.relative_to(repo_root))
            else:
                info["path"] = str(entry)
            results.append(info)
    return results


def generate_catalog() -> dict:
    """Scan skill directories and build the catalog."""
    tier1 = _scan_tier(TIER1_DIR, 1, REPO_ROOT)
    seen_names = {s["name"].lower() for s in tier1}

    tier2: list[dict] = []
    for t2_dir in TIER2_DIRS:
        for skill in _scan_tier(t2_dir, 2, REPO_ROOT):
            # Deduplicate (case-insensitive): skip if name exists in Tier 1
            # or was already added from another Tier 2 directory
            if skill["name"].lower() not in seen_names:
                tier2.append(skill)
                seen_names.add(skill["name"].lower())

    return {
        "tier1": tier1,
        "tier2": tier2,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    catalog = generate_catalog()

    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: the injection hook reads this file on every prompt and
    # regeneration now runs detached in the background — a reader must never
    # see a half-written catalog.
    tmp_path = CATALOG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    tmp_path.replace(CATALOG_PATH)

    total = len(catalog["tier1"]) + len(catalog["tier2"])
    print(
        f"Skill catalog generated: {len(catalog['tier1'])} Tier 1, "
        f"{len(catalog['tier2'])} Tier 2 ({total} total)"
    )
    print(f"Written to: {CATALOG_PATH}")


if __name__ == "__main__":
    main()
