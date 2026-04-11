"""Dashboard secrets routes — API key management.

Parses secrets.env.example for the canonical key registry (groups, labels,
descriptions, signup URLs). Reads secrets.env for status and current values.
Writes updates atomically.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.auth import is_authenticated
from genesis.env import repo_root, secrets_path

logger = logging.getLogger(__name__)


# ── Key registry (parsed from secrets.env.example) ──────────────────

@dataclass(frozen=True)
class SecretKeyDef:
    key: str
    group: str
    label: str
    description: str
    signup_url: str
    is_sensitive: bool


_SECTION_RE = re.compile(r"^#\s*─{3,}\s*(.+?)\s*─+$")
_LABEL_RE = re.compile(r"^#\s*---\s*(.+?)\s*---")
_USED_BY_RE = re.compile(r"^#\s*Used by:\s*(.+)", re.IGNORECASE)
_SIGNUP_RE = re.compile(r"^#\s*Signup:\s*(.+)", re.IGNORECASE)
_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]+)=")
_SENSITIVE_RE = re.compile(r"API_KEY_|_API_KEY|_TOKEN|_PASSPHRASE|FIRECRAWL_API")


def _parse_example_file() -> list[SecretKeyDef]:
    """Parse secrets.env.example into structured key definitions."""
    example = repo_root() / "secrets.env.example"
    if not example.is_file():
        logger.warning("secrets.env.example not found at %s", example)
        return []

    keys: list[SecretKeyDef] = []
    group = "Other"
    label = ""
    description = ""
    signup_url = ""

    for line in example.read_text().splitlines():
        line_s = line.strip()

        # Section header: # ─── Group Name ───
        m = _SECTION_RE.match(line_s)
        if m:
            group = m.group(1).strip()
            label = ""
            description = ""
            signup_url = ""
            continue

        # Sub-label: # --- Provider Name ---
        m = _LABEL_RE.match(line_s)
        if m:
            label = m.group(1).strip()
            description = ""
            signup_url = ""
            continue

        # Description: # Used by: ...
        m = _USED_BY_RE.match(line_s)
        if m:
            description = m.group(1).strip()
            continue

        # Signup URL: # Signup: ...
        m = _SIGNUP_RE.match(line_s)
        if m:
            signup_url = m.group(1).strip()
            continue

        # Key definition: KEY_NAME=
        m = _KEY_RE.match(line_s)
        if m:
            key_name = m.group(1)
            # Skip commented-out keys
            if line_s.startswith("#"):
                continue
            keys.append(SecretKeyDef(
                key=key_name,
                group=group,
                label=label or key_name,
                description=description,
                signup_url=signup_url,
                is_sensitive=bool(_SENSITIVE_RE.search(key_name)),
            ))
            # Reset per-key metadata (label persists for multi-key providers)
            description = ""
            signup_url = ""
            continue

    return keys


# Parse once at import time — defensive to avoid crashing all dashboard routes
try:
    _KEY_REGISTRY: list[SecretKeyDef] = _parse_example_file()
except Exception:
    logger.error("Failed to parse secrets.env.example", exc_info=True)
    _KEY_REGISTRY = []
_KNOWN_KEYS: frozenset[str] = frozenset(k.key for k in _KEY_REGISTRY)


# ── Helpers ──────────────────────────────────────────────────────────

def _key_status(key_name: str) -> str:
    """Check if a key is configured in the environment."""
    val = os.environ.get(key_name, "")
    if val and val not in ("None", "NA", ""):
        return "configured"
    return "not_set"


def _key_value(key_name: str) -> str:
    """Return the current value of a key from the environment, or empty string."""
    val = os.environ.get(key_name, "")
    if val in ("None", "NA"):
        return ""
    return val


def _update_secrets_file(updates: dict[str, str]) -> None:
    """Update keys in secrets.env atomically. Preserves comments and structure."""
    path = secrets_path()
    if not path.exists():
        # Create from example if missing
        example = repo_root() / "secrets.env.example"
        if example.exists():
            path.write_text(example.read_text())
            os.chmod(path, 0o600)
        else:
            path.write_text("")
            os.chmod(path, 0o600)

    lines = path.read_text().splitlines(keepends=True)
    remaining = dict(updates)
    new_lines: list[str] = []

    for line in lines:
        m = _KEY_RE.match(line.strip())
        if m and m.group(1) in remaining:
            key = m.group(1)
            new_lines.append(f"{key}={remaining.pop(key)}\n")
        else:
            new_lines.append(line)

    # Append any keys not found in the existing file
    if remaining:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        for key, val in remaining.items():
            new_lines.append(f"{key}={val}\n")

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".secrets.env.", suffix=".tmp"
    )
    fd_closed = False
    try:
        os.write(fd, "".join(new_lines).encode())
        os.close(fd)
        fd_closed = True
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(path))
    except BaseException:
        if not fd_closed:
            os.close(fd)
        Path(tmp_path).unlink(missing_ok=True)
        raise


# ── Routes ────────────────────────────────────────────────────────────

@blueprint.route("/api/genesis/secrets")
def secrets_list():
    """Return grouped key registry with status and current values.

    Values are only included for authenticated sessions. Unauthenticated
    callers (monitoring tools, Guardian probes) see status but not values.
    """
    groups: dict[str, list[dict]] = {}
    include_values = is_authenticated()

    for kdef in _KEY_REGISTRY:
        entry = {
            "key": kdef.key,
            "label": kdef.label,
            "status": _key_status(kdef.key),
            "value": _key_value(kdef.key) if include_values else "",
            "description": kdef.description,
            "signup_url": kdef.signup_url,
            "is_sensitive": kdef.is_sensitive,
        }
        groups.setdefault(kdef.group, []).append(entry)

    result = [{"name": name, "keys": keys} for name, keys in groups.items()]
    return jsonify({"groups": result})


@blueprint.route("/api/genesis/secrets", methods=["PUT"])
def secrets_update():
    """Update one or more keys in secrets.env. Write-only."""
    data = request.get_json(silent=True) or {}
    updates = data.get("keys")
    if not updates or not isinstance(updates, dict):
        return jsonify({"error": "Body must contain 'keys' object"}), 400

    # Validate
    errors = []
    for key, val in updates.items():
        if key not in _KNOWN_KEYS:
            errors.append(f"Unknown key: {key}")
            continue
        if not isinstance(val, str) or not val.strip():
            errors.append(f"Value for {key} must be a non-empty string")
            continue
        if len(val) > 500:
            errors.append(f"Value for {key} too long (max 500 chars)")
        if "\n" in val or "\x00" in val:
            errors.append(f"Value for {key} contains invalid characters")
        # Telegram-specific: ALLOWED_USERS must be numeric IDs
        if key == "TELEGRAM_ALLOWED_USERS":
            for uid in val.split(","):
                uid = uid.strip()
                if not uid:
                    continue
                if ":" in uid:
                    errors.append(
                        "TELEGRAM_ALLOWED_USERS looks like a bot token — "
                        "this field needs numeric user IDs "
                        "(get yours from @userinfobot on Telegram)"
                    )
                    break
                if not uid.isdigit():
                    errors.append(
                        f"TELEGRAM_ALLOWED_USERS: '{uid}' is not a valid "
                        f"numeric user ID (get yours from @userinfobot)"
                    )
                    break
    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 422

    # Clean values
    clean = {k: v.strip() for k, v in updates.items() if k in _KNOWN_KEYS}

    try:
        _update_secrets_file(clean)
        # Update os.environ so the dashboard status refreshes immediately
        # (runtime still needs restart to pick up changes)
        for k, v in clean.items():
            os.environ[k] = v
        logger.info("Secrets updated via dashboard: %s", list(clean.keys()))
        return jsonify({
            "status": "ok",
            "updated": list(clean.keys()),
            "needs_restart": True,
        })
    except Exception:
        logger.error("Failed to update secrets", exc_info=True)
        return jsonify({"error": "Failed to write secrets.env"}), 500
