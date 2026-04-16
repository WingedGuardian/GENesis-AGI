"""Skills directory wiring — locate and load SKILL.md files."""

from __future__ import annotations

from pathlib import Path

_GENESIS_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
_CLAUDE_SKILLS_DIR = Path(__file__).resolve().parents[4] / ".claude" / "skills"


def _skill_dirs() -> tuple[Path, ...]:
    """Return search paths for skill directories (supports test patching)."""
    return (_GENESIS_SKILLS_DIR, _CLAUDE_SKILLS_DIR)


def get_skill_path(skill_name: str) -> Path | None:
    """Return path to a skill's SKILL.md, or None if not found.

    Searches both src/genesis/skills/ and .claude/skills/ tiers.
    """
    for base in _skill_dirs():
        path = base / skill_name / "SKILL.md"
        if path.exists():
            return path
    return None


def is_genesis_core_skill(path: Path) -> bool:
    """Check if a skill path is in the Genesis core tier (src/genesis/skills/)."""
    try:
        path.resolve().relative_to(_GENESIS_SKILLS_DIR.resolve())
        return True
    except ValueError:
        return False


def list_available_skills() -> list[str]:
    """Return names of all skills with SKILL.md files across both tiers."""
    names: set[str] = set()
    for base in _skill_dirs():
        if not base.exists():
            continue
        for d in base.iterdir():
            if d.is_dir() and (d / "SKILL.md").exists():
                names.add(d.name)
    return sorted(names)


def load_skill(skill_name: str) -> str | None:
    """Load a skill's SKILL.md content."""
    path = get_skill_path(skill_name)
    if path is None:
        return None
    return path.read_text(encoding="utf-8")


def write_skill(skill_name: str, content: str) -> bool:
    """Write content to a skill's SKILL.md. Returns True on success."""
    path = _GENESIS_SKILLS_DIR / skill_name / "SKILL.md"
    if not path.parent.exists():
        return False
    path.write_text(content, encoding="utf-8")
    return True


def read_skill(skill_name: str) -> str | None:
    """Read a skill's SKILL.md content. Alias for load_skill."""
    return load_skill(skill_name)
