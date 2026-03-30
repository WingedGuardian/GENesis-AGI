"""Skills directory wiring — locate and load SKILL.md files."""

from __future__ import annotations

from pathlib import Path

_GENESIS_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"


def _az_skills_dir() -> Path:
    """Lazy — avoids freezing AZ_ROOT at import time."""
    from genesis.env import az_root

    return az_root() / "usr" / "plugins" / "genesis" / "skills"


def get_skill_path(skill_name: str) -> Path | None:
    """Return path to a skill's SKILL.md, or None if not found."""
    path = _GENESIS_SKILLS_DIR / skill_name / "SKILL.md"
    return path if path.exists() else None


def list_available_skills() -> list[str]:
    """Return names of all skills with SKILL.md files."""
    if not _GENESIS_SKILLS_DIR.exists():
        return []
    return sorted(
        d.name
        for d in _GENESIS_SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists()
    )


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
