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
    return {"name": skill_dir.name, "description": ""}


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

    return {"name": name, "description": description}


def _scan_tier(tier_dir: Path, tier_num: int, repo_root: Path | None) -> list[dict]:
    """Scan a single tier directory for skills."""
    results: list[dict] = []
    if not tier_dir.is_dir():
        return results

    for entry in sorted(tier_dir.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
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
    tier1_names = {s["name"] for s in tier1}

    tier2: list[dict] = []
    for t2_dir in TIER2_DIRS:
        for skill in _scan_tier(t2_dir, 2, REPO_ROOT):
            # Deduplicate: if a skill name exists in Tier 1, skip
            if skill["name"] not in tier1_names:
                tier2.append(skill)
                tier1_names.add(skill["name"])  # also dedup across Tier 2 dirs

    return {
        "tier1": tier1,
        "tier2": tier2,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    catalog = generate_catalog()

    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    total = len(catalog["tier1"]) + len(catalog["tier2"])
    print(
        f"Skill catalog generated: {len(catalog['tier1'])} Tier 1, "
        f"{len(catalog['tier2'])} Tier 2 ({total} total)"
    )
    print(f"Written to: {CATALOG_PATH}")


if __name__ == "__main__":
    main()
