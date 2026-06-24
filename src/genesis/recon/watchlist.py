"""Recon watchlist — single source of truth for the tracked-repo list.

Two layers:
  - Base   (``config/recon_watchlist.yaml``)        committed, project-level.
  - Overlay (``config/recon_watchlist.local.yaml``)  gitignored, install-level.

The overlay both ADDS install-specific repos (``projects``) and TOMBSTONES base
repos the user disabled (``disabled``). ``active_entries()`` is what the recon
gatherer / discovery / MCP consume; ``list_entries()`` is the annotated view for
the dashboard editor. Writes only ever touch the overlay — the committed base is
never modified, so ``git pull`` stays clean.

Editing is dashboard-only: the recon MCP tool stays read-only by design, so an
autonomous loop cannot rewrite its own recon targets.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
WATCHLIST_PATH = _CONFIG_DIR / "recon_watchlist.yaml"
LOCAL_PATH = _CONFIG_DIR / "recon_watchlist.local.yaml"

_VALID_TRACK = {"releases", "commits", "issues", "discussions", "stars"}
_VALID_PRIORITY = {"high", "medium", "low"}
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


# ── loading ───────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        logger.error("Failed to read watchlist file %s", path, exc_info=True)
        return {}


def load_base() -> list[dict]:
    """Return the committed base project list (``recon_watchlist.yaml``)."""
    return list(_load_yaml(WATCHLIST_PATH).get("projects") or [])


def load_overlay() -> dict:
    """Return the gitignored overlay (install-added ``projects`` + ``disabled`` tombstones)."""
    data = _load_yaml(LOCAL_PATH)
    return {
        "projects": list(data.get("projects") or []),
        "disabled": list(data.get("disabled") or []),
    }


def active_entries() -> list[dict]:
    """The repos recon should actually watch: base minus tombstoned, plus
    install-added — deduped by repo. Consumed by the gatherer / discovery / MCP.
    """
    overlay = load_overlay()
    disabled = set(overlay["disabled"])
    result = [dict(p) for p in load_base() if p.get("repo") not in disabled]
    seen = {p.get("repo") for p in result}
    for op in overlay["projects"]:
        repo = op.get("repo")
        if repo and repo not in seen and repo not in disabled:
            result.append(dict(op))
            seen.add(repo)
    return result


def list_entries() -> list[dict]:
    """Annotated view for the dashboard editor: every entry with its ``source``
    (base|overlay) and ``disabled`` flag. Base entries are always listed (so a
    disabled one can be re-enabled); install-added entries are listed once.
    """
    overlay = load_overlay()
    disabled = set(overlay["disabled"])
    out: list[dict] = []
    seen: set[str] = set()
    for p in load_base():
        repo = p.get("repo")
        out.append({**p, "source": "base", "disabled": repo in disabled})
        seen.add(repo)
    for op in overlay["projects"]:
        repo = op.get("repo")
        if repo and repo not in seen:
            out.append({**op, "source": "overlay", "disabled": False})
            seen.add(repo)
    return out


# ── validation ────────────────────────────────────────────────────────

def validate_entry(entry: dict) -> tuple[dict | None, str | None]:
    """Validate/normalize an add request. Returns (cleaned_entry, error)."""
    if not isinstance(entry, dict):
        return None, "entry must be an object"
    name = str(entry.get("name") or "").strip()
    repo = str(entry.get("repo") or "").strip()
    track = entry.get("track") or []
    priority = str(entry.get("priority") or "medium").strip().lower()
    notes = str(entry.get("notes") or "").strip()
    urls = entry.get("urls") or []

    if not name:
        return None, "name is required"
    if not _REPO_RE.match(repo):
        return None, "repo must be in owner/repo form (not a URL)"
    if not isinstance(track, list) or not track:
        return None, "track must be a non-empty list"
    bad_track = [t for t in track if t not in _VALID_TRACK]
    if bad_track:
        return None, f"invalid track values {bad_track}; allowed {sorted(_VALID_TRACK)}"
    if priority not in _VALID_PRIORITY:
        return None, f"priority must be one of {sorted(_VALID_PRIORITY)}"
    if not isinstance(urls, list) or any(
        not isinstance(u, str) or not u.startswith("https://") for u in urls
    ):
        return None, "urls must be a list of https:// strings"

    cleaned: dict = {"name": name, "repo": repo, "track": list(track),
                     "priority": priority}
    if notes:
        cleaned["notes"] = notes
    if urls:
        cleaned["urls"] = list(urls)
    return cleaned, None


# ── writes (overlay only) ─────────────────────────────────────────────

def _write_overlay(overlay: dict) -> None:
    """Atomically write the overlay (tmp + replace). Drops empty sections."""
    import yaml
    out: dict = {}
    if overlay.get("projects"):
        out["projects"] = overlay["projects"]
    if overlay.get("disabled"):
        out["disabled"] = overlay["disabled"]

    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(LOCAL_PATH.parent), prefix=".recon_watchlist.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write("# Install-specific recon watchlist overlay (gitignored).\n")
            f.write("# 'projects' add tracked repos; 'disabled' tombstones base "
                    "repos.\n")
            f.write("# Managed by the dashboard → Knowledge → Tracked Repositories.\n")
            if out:
                yaml.safe_dump(out, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp, LOCAL_PATH)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def add_repo(entry: dict) -> dict:
    """Add an install-specific tracked repo to the overlay."""
    cleaned, err = validate_entry(entry)
    if err:
        return {"error": err}
    repo = cleaned["repo"]
    existing = {p.get("repo") for p in load_base()}
    overlay = load_overlay()
    existing |= {p.get("repo") for p in overlay["projects"]}
    if repo in existing:
        return {"error": f"{repo} is already tracked"}
    overlay["projects"].append(cleaned)
    _write_overlay(overlay)
    return {"ok": True, "repo": repo}


def set_base_disabled(repo: str, disabled: bool) -> dict:
    """Tombstone (disable) or re-enable a BASE watchlist entry via the overlay."""
    if repo not in {p.get("repo") for p in load_base()}:
        return {"error": f"{repo} is not a base watchlist entry"}
    overlay = load_overlay()
    dis = overlay["disabled"]
    if disabled and repo not in dis:
        dis.append(repo)
    elif not disabled and repo in dis:
        dis.remove(repo)
    _write_overlay(overlay)
    return {"ok": True, "repo": repo, "disabled": bool(disabled)}


def remove_overlay_repo(repo: str) -> dict:
    """Delete an install-added repo from the overlay. Base repos can only be
    disabled (use ``set_base_disabled``), never deleted here."""
    overlay = load_overlay()
    kept = [p for p in overlay["projects"] if p.get("repo") != repo]
    if len(kept) == len(overlay["projects"]):
        return {"error": f"{repo} is not an install-added entry "
                         "(base entries can only be disabled)"}
    overlay["projects"] = kept
    _write_overlay(overlay)
    return {"ok": True, "repo": repo}
