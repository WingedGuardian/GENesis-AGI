"""Config file viewer and editor routes.

Serves three categories of editable files:
  - Identity markdown (SOUL.md, CONVERSATION.md, etc.) from src/genesis/identity/
  - YAML configs (autonomy.yaml, outreach.yaml, etc.) from config/
  - Auto-memory files (project_*.md, feedback_*.md, etc.) from ~/.claude/projects/
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint
from genesis.env import cc_project_dir

logger = logging.getLogger(__name__)

_CATEGORY_PREFIXES = {
    "REFLECTION": "reflection",
    "SELF_ASSESSMENT": "reflection",
    "QUALITY_CALIBRATION": "reflection",
    "LIGHT_TEMPLATE": "reflection",
    "MICRO_TEMPLATE": "reflection",
    "INBOX_EVALUATE": "triage",
    "TRIAGE_CALIBRATION": "triage",
    "SOUL": "identity",
    "USER": "identity",
    "CONVERSATION": "identity",
}

_YAML_CATEGORIES = {
    "autonomy": "system",
    "resilience": "system",
    "protected_paths": "system",
    "guardian": "system",
    "model_routing": "routing",
    "model_profiles": "routing",
    "outreach": "outreach",
    "inbox_monitor": "inbox",
    "tts": "channels",
    "content_sanitization": "security",
    "recon_schedules": "recon",
    "recon_sources": "recon",
    "recon_watchlist": "recon",
    "procedure_triggers": "learning",
}

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GENESIS_ROOT = _REPO_ROOT / "src" / "genesis"
_CONFIG_DIR = _REPO_ROOT / "config"
_IDENTITY_DIR = _GENESIS_ROOT / "identity"
_SKILLS_DIR = _GENESIS_ROOT / "skills"
_MEMORY_DIR = Path.home() / ".claude" / "projects" / cc_project_dir() / "memory"


def _categorize_identity(filename: str) -> str:
    name_no_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
    for prefix, category in _CATEGORY_PREFIXES.items():
        if name_no_ext.startswith(prefix):
            return category
    return "identity"


def _categorize_memory(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    if stem.startswith("feedback_"):
        return "memory-feedback"
    if stem.startswith("project_"):
        return "memory-project"
    if stem.startswith("user_"):
        return "memory-user"
    if stem == "MEMORY":
        return "memory-index"
    return "memory-reference"


@blueprint.route("/api/genesis/config-files")
def config_files():
    """Return list of all editable config, identity, and memory files."""
    results: list[dict] = []

    # Identity markdown files
    if _IDENTITY_DIR.is_dir():
        for p in sorted(_IDENTITY_DIR.glob("*.md")):
            if p.is_file() and p.stem == p.stem.upper():
                results.append({
                    "path": str(p.resolve()),
                    "name": p.name,
                    "category": _categorize_identity(p.name),
                    "editable": True,
                    "syntax": "markdown",
                })

    # Skills files
    if _SKILLS_DIR.is_dir():
        for p in sorted(_SKILLS_DIR.rglob("SKILL.md")):
            if p.is_file():
                results.append({
                    "path": str(p.resolve()),
                    "name": p.parent.name + "/" + p.name,
                    "category": "skills",
                    "editable": False,
                    "syntax": "markdown",
                })
        for p in sorted(_SKILLS_DIR.glob("*.md")):
            if p.is_file() and p.stem == p.stem.upper():
                results.append({
                    "path": str(p.resolve()),
                    "name": p.name,
                    "category": "skills",
                    "editable": False,
                    "syntax": "markdown",
                })

    # CLAUDE.md (read-only)
    claude_md = _REPO_ROOT / "CLAUDE.md"
    if claude_md.is_file():
        results.append({
            "path": str(claude_md.resolve()),
            "name": "CLAUDE.md",
            "category": "identity",
            "editable": False,
            "syntax": "markdown",
        })

    # YAML config files
    if _CONFIG_DIR.is_dir():
        for p in sorted(_CONFIG_DIR.glob("*.yaml")):
            if p.is_file():
                results.append({
                    "path": str(p.resolve()),
                    "name": p.name,
                    "category": _YAML_CATEGORIES.get(p.stem, "config"),
                    "editable": p.stem != "protected_paths",
                    "syntax": "yaml",
                })
        for sub in ("behavioral_rules", "research-profiles"):
            sub_dir = _CONFIG_DIR / sub
            if sub_dir.is_dir():
                for p in sorted(sub_dir.glob("*.yaml")):
                    if p.is_file():
                        results.append({
                            "path": str(p.resolve()),
                            "name": f"{sub}/{p.name}",
                            "category": _YAML_CATEGORIES.get(p.stem, "config"),
                            "editable": True,
                            "syntax": "yaml",
                        })

    # Auto-memory files
    if _MEMORY_DIR.is_dir():
        for p in sorted(_MEMORY_DIR.glob("*.md")):
            if p.is_file() and not p.name.startswith("."):
                results.append({
                    "path": str(p.resolve()),
                    "name": f"memory/{p.name}",
                    "category": _categorize_memory(p.name),
                    "editable": True,
                    "deletable": p.name != "MEMORY.md",
                    "syntax": "markdown",
                })

    return jsonify(results)


@blueprint.route("/api/genesis/config-files/<path:name>")
def config_file_content(name: str):
    """Return the content of a config, identity, or memory file."""
    target, syntax = _resolve_file(name)
    if target is None:
        return jsonify({"error": "not found"}), 404
    try:
        content = target.read_text(encoding="utf-8")
        return jsonify({
            "name": name,
            "path": str(target.resolve()),
            "content": content,
            "syntax": syntax,
        })
    except Exception as exc:
        logger.error("Failed to read config file %s: %s", name, exc, exc_info=True)
        return jsonify({"name": name, "content": "(failed to read file)"}), 500


@blueprint.route("/api/genesis/config-files/<path:name>", methods=["PUT"])
def config_file_update(name: str):
    """Update a config, identity, or memory file with validation."""
    if name == "CLAUDE.md":
        return jsonify({"error": "CLAUDE.md cannot be edited via dashboard"}), 403

    data = request.get_json(silent=True)
    if not data or "content" not in data:
        return jsonify({"error": "missing 'content' in request body"}), 400

    content = data["content"]
    if len(content) > 1_000_000:
        return jsonify({"error": "content too large (max 1MB)"}), 413

    target, allowed_dir = _resolve_file_for_write(name)

    if target is None or allowed_dir is None:
        return jsonify({"error": "file not found"}), 404
    if not target.resolve().is_relative_to(allowed_dir.resolve()):
        return jsonify({"error": "path traversal blocked"}), 403

    # YAML validation
    if name.endswith(".yaml"):
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            if mark:
                detail = f"line {mark.line + 1}, col {mark.column + 1}: {exc.problem}"
            else:
                detail = str(exc)
            return jsonify({"error": f"Invalid YAML: {detail}"}), 422

    try:
        target.write_text(content, encoding="utf-8")
        return jsonify({"status": "ok", "name": name, "path": str(target.resolve())})
    except Exception as exc:
        logger.error("Failed to write config file %s: %s", name, exc, exc_info=True)
        return jsonify({"error": "write failed"}), 500


@blueprint.route("/api/genesis/config-files/<path:name>", methods=["DELETE"])
def config_file_delete(name: str):
    """Delete an auto-memory file."""
    if not name.startswith("memory/"):
        return jsonify({"error": "only memory files can be deleted"}), 403

    mem_name = name[len("memory/"):]
    target = _MEMORY_DIR / mem_name
    if not target.is_file():
        return jsonify({"error": "file not found"}), 404
    if not target.resolve().is_relative_to(_MEMORY_DIR.resolve()):
        return jsonify({"error": "path traversal blocked"}), 403

    try:
        target.unlink()
        return jsonify({"status": "ok", "name": name})
    except Exception as exc:
        logger.error("Failed to delete memory file %s: %s", name, exc, exc_info=True)
        return jsonify({"error": "delete failed"}), 500


def _resolve_file(name: str) -> tuple[Path | None, str]:
    """Resolve a file name to its path and syntax mode. Returns (path, syntax) or (None, '')."""
    # CLAUDE.md
    if name == "CLAUDE.md":
        p = _REPO_ROOT / "CLAUDE.md"
        return (p, "markdown") if p.is_file() else (None, "")

    # YAML config files
    if name.endswith(".yaml"):
        candidate = _CONFIG_DIR / name
        if candidate.is_file() and candidate.resolve().is_relative_to(_CONFIG_DIR.resolve()):
            return candidate, "yaml"
        return None, ""

    # Auto-memory files
    if name.startswith("memory/"):
        mem_name = name[len("memory/"):]
        candidate = _MEMORY_DIR / mem_name
        if candidate.is_file() and candidate.resolve().is_relative_to(_MEMORY_DIR.resolve()):
            return candidate, "markdown"
        return None, ""

    # Skills files
    if "/" in name:
        candidate = _SKILLS_DIR / name
        if candidate.is_file() and candidate.resolve().is_relative_to(_SKILLS_DIR.resolve()):
            return candidate, "markdown"

    # Identity files (fallback search)
    basename = name.split("/")[-1]
    for search_dir in [_IDENTITY_DIR, _SKILLS_DIR]:
        if not search_dir.is_dir():
            continue
        for p in search_dir.rglob(basename):
            if p.is_file() and p.name == basename and p.resolve().is_relative_to(search_dir.resolve()):
                return p, "markdown"

    return None, ""


def _resolve_file_for_write(name: str) -> tuple[Path | None, Path | None]:
    """Resolve a file name to (target_path, allowed_directory) for write operations."""
    if name.endswith(".yaml"):
        basename = name.split("/")[-1]
        if basename == "protected_paths.yaml":
            return None, None  # blocked
        candidate = _CONFIG_DIR / name
        if candidate.is_file():
            return candidate, _CONFIG_DIR
        return None, None

    if name.startswith("memory/"):
        mem_name = name[len("memory/"):]
        candidate = _MEMORY_DIR / mem_name
        if candidate.is_file():
            return candidate, _MEMORY_DIR
        return None, None

    # Identity files
    basename = name.split("/")[-1]
    candidate = _IDENTITY_DIR / basename
    if candidate.is_file():
        return candidate, _IDENTITY_DIR
    return None, None


@blueprint.route("/api/genesis/subsystems")
def subsystems_list():
    """Return list of subsystem names from the Subsystem enum."""
    from genesis.observability.types import Subsystem

    return jsonify(sorted(s.value for s in Subsystem))
