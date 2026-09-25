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
    #: True when the template ships this key COMMENTED — an OPTIONAL OVERRIDE whose
    #: real source is genesis.yaml or a built-in default. Setting one here shadows
    #: that source, so it must also be possible to UNSET it; see `_write_secrets`.
    is_optional_override: bool = False


_SECTION_RE = re.compile(r"^#\s*─{3,}\s*(.+?)\s*─+$")
_LABEL_RE = re.compile(r"^#\s*---\s*(.+?)\s*---")
_USED_BY_RE = re.compile(r"^#\s*Used by:\s*(.+)", re.IGNORECASE)
_SIGNUP_RE = re.compile(r"^#\s*Signup:\s*(.+)", re.IGNORECASE)
_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]+)=")
# A key the template ships commented out — still registered, just unset.
_COMMENTED_KEY_RE = re.compile(r"^#\s*([A-Z][A-Z0-9_]+)=")
# Note: ``_PASS`` covers NAS/SMB passwords (GENESIS_BACKUP_NAS_PASS) and any
# *_PASSWORD key; it also subsumes _PASSPHRASE but that is kept for clarity.
_SENSITIVE_RE = re.compile(r"API_KEY_|_API_KEY|_TOKEN|_PASSPHRASE|_PASS|FIRECRAWL_API")


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

        # Key definition: KEY_NAME=  — or `# KEY_NAME=`, a key the template ships
        # COMMENTED so its default is not force-assigned into secrets.env.
        #
        # A commented key is still a REAL, settable key and must stay in this
        # registry: the PUT route rejects anything not in _KNOWN_KEYS, so dropping
        # it makes the dashboard field vanish and an update 4xx. That bites hardest
        # on exactly the keys the template comments ON PURPOSE — the local-inference
        # URLs and the embedding priority lever are commented precisely so the
        # genesis.yaml equivalents keep working, and the TTS tuning knobs and the
        # dashboard auth toggle were already invisible here for the same reason.
        # `_KEY_RE` anchors at the line start, so the comment form needs its own
        # match rather than the (unreachable) startswith check this replaced.
        m = _KEY_RE.match(line_s)
        commented = False
        if not m:
            m = _COMMENTED_KEY_RE.match(line_s)
            commented = bool(m)
        if m:
            key_name = m.group(1)
            keys.append(SecretKeyDef(
                key=key_name,
                group=group,
                label=label or key_name,
                description=description,
                signup_url=signup_url,
                is_sensitive=bool(_SENSITIVE_RE.search(key_name)),
                is_optional_override=commented,
            ))
            # Reset per-key metadata (label persists for multi-key providers)
            description = ""
            signup_url = ""
            continue

    return keys


# Keys the generic secrets editor must NOT surface or write. Timezone is managed
# by the dedicated dashboard control (POST /api/genesis/settings/timezone →
# genesis.yaml, the authoritative source): USER_TIMEZONE is a deprecated fallback
# and GENESIS_TIMEZONE is install-time OS-clock plumbing with no Python reader —
# a free-text field for either is a footgun that silently no-ops. Excluded from
# BOTH the rendered registry (GET) and _KNOWN_KEYS (so PUT rejects them too).
# secrets.env content is untouched (install seeding still writes them directly).
_HIDDEN_KEYS: frozenset[str] = frozenset({"USER_TIMEZONE", "GENESIS_TIMEZONE"})

# Parse once at import time — defensive to avoid crashing all dashboard routes
try:
    _KEY_REGISTRY: list[SecretKeyDef] = [
        k for k in _parse_example_file() if k.key not in _HIDDEN_KEYS
    ]
except Exception:
    logger.error("Failed to parse secrets.env.example", exc_info=True)
    _KEY_REGISTRY = []
_KNOWN_KEYS: frozenset[str] = frozenset(k.key for k in _KEY_REGISTRY)
#: Keys the template ships COMMENTED — optional overrides whose real source is
#: genesis.yaml or a built-in default. These, and only these, may be CLEARED back
#: to unset through the editor.
_OPTIONAL_OVERRIDE_KEYS: frozenset[str] = frozenset(
    k.key for k in _KEY_REGISTRY if k.is_optional_override
)


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
    """Update keys in secrets.env atomically. Preserves comments and structure.

    An empty-string value UNSETS the key: the assignment is commented out rather
    than written as ``KEY=``. ``None`` is NOT a value here and RAISES, because it
    would otherwise be written literally as ``KEY=None`` — which ``_key_value``
    reads back as ``''`` while ``os.environ`` still holds the string "None" and
    keeps shadowing genesis.yaml. The dashboard would then report the key as unset
    while the live process ignored the yaml: exactly the corruption the
    ``os.environ.pop`` in ``secrets_update`` exists to prevent, reached by another
    door. Callers translate their own spelling of "clear" to ``""`` BEFORE calling
    (``secrets_update`` maps the wire protocol's ``null`` here), so a ``None``
    arriving is a caller bug and fails loudly rather than corrupting the file.
    """
    none_keys = sorted(k for k, v in updates.items() if v is None)
    if none_keys:
        raise TypeError(
            f"_update_secrets_file received None for {none_keys}; pass '' to unset "
            f"(None would be written literally as KEY=None)"
        )

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
            val = remaining.pop(key)
            if val == "":
                # UNSET: comment the assignment out rather than writing `KEY=`.
                # An empty assignment is NOT the same as no assignment — the
                # accessors read `os.environ.get(key) is not None`, so `KEY=`
                # still shadows genesis.yaml, just with an empty string, which is
                # worse than the value it replaced. Commenting preserves the line
                # (and any inline note beside it) as documentation of what the
                # key was, which is exactly how the template ships these.
                new_lines.append(f"# {key}={line.strip().split('=', 1)[1]}\n")
            else:
                new_lines.append(f"{key}={val}\n")
        else:
            new_lines.append(line)

    # Append any keys not found in the existing file
    if remaining:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        for key, val in remaining.items():
            if val == "":
                continue  # unset stays unset — never append a shadowing `KEY=`
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
            # Tells the editor this key may be CLEARED back to unset. Without it the
            # UI cannot distinguish an optional override from a required credential
            # and has to reject every empty value, which is the one-way door.
            "is_optional_override": kdef.is_optional_override,
        }
        groups.setdefault(kdef.group, []).append(entry)

    result = [{"name": name, "keys": keys} for name, keys in groups.items()]
    return jsonify({"groups": result})


@blueprint.route("/api/genesis/secrets", methods=["PUT"])
def secrets_update():
    """Update one or more keys in secrets.env. Write-only.

    The payload says what it means, rather than leaving the caller to infer it:

    ==========================  ===============================================
    key ABSENT from ``keys``    no change (only keys present here are touched)
    ``null``                    CLEAR — optional overrides only
    ``""`` or whitespace        **422** — ambiguous, refused
    non-empty string            set to that value
    ==========================  ===============================================

    The empty string used to mean BOTH "I did not change this field" and "clear
    this setting", and no client could express the difference — so an editor that
    had not been served the current value (they are withheld from a caller that
    has not proved it is the operator) submitted an untouched field as ``""`` and
    silently deleted the override. Making the two spellings distinct removes that
    by construction: an untouched field can no longer produce a destructive write
    from ANY client, however the client is written.

    Same shape as ``routes/attention.py``'s ``acceptance_note`` (present —
    including null — means set, absent means preserve), with one deliberate
    difference: there ``""`` is a legitimate set, here it is the exact collision
    being removed, so it is refused outright.

    Hard switch, no deprecation window. The skew case is a browser tab holding
    cached JS across a deploy, and it fails as a 422 REFUSAL rather than a
    destructive write — the safe direction — so the message says to reload.

    Two limits of a CLEAR, neither introduced here, both worth knowing:

    * A key assigned TWICE in ``secrets.env`` has only its FIRST line commented
      out (``_update_secrets_file`` pops from ``remaining`` on first match), so
      the second assignment survives and this route still answers 200.
    * A clear cannot remove an assignment the file never had — one exported by
      the systemd unit or the shell. Nothing matches, nothing is written,
      ``os.environ.pop`` makes it LOOK gone, and a restart brings it back.

    Because of both, the ``cleared`` list in the response echoes what was ASKED
    FOR, not what changed on disk.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        # A truthy non-dict body ([1,2], "hi", 7) survives `or {}` and used to
        # reach `data.get` as an AttributeError — an HTTP 500 for what is plainly
        # a malformed request.
        return jsonify({"error": "Body must be a JSON object"}), 400
    updates = data.get("keys")
    if not updates or not isinstance(updates, dict):
        return jsonify({"error": "Body must contain 'keys' object"}), 400

    # Validate
    errors = []
    for key, val in updates.items():
        if key not in _KNOWN_KEYS:
            errors.append(f"Unknown key: {key}")
            continue
        if val is None:
            # EXPLICIT CLEAR. Ordered deliberately: AFTER the _KNOWN_KEYS check so
            # {"NOPE": null} reports an unknown key rather than an un-clearable
            # one, and BEFORE the isinstance check so a null reports the right
            # problem instead of "must be a string".
            #
            # `val is None`, never `if not val`: 0 and False are falsy and are NOT
            # a clear — they fall through to the isinstance check and are rejected
            # as non-strings, which is what they are.
            #
            # Only an OPTIONAL OVERRIDE may be cleared. Without that limit the
            # editor is a one-way door: setting one writes an assignment into
            # secrets.env, the environment then shadows genesis.yaml for good, and
            # later yaml edits appear to do nothing — recoverable only by hand-
            # editing the file the dashboard exists to avoid. A required credential
            # has no "unset" state that is not simply a broken install.
            if key not in _OPTIONAL_OVERRIDE_KEYS:
                errors.append(
                    f"{key} cannot be cleared — it has no unset state. "
                    f"Send a new value instead."
                )
            continue
        if not isinstance(val, str):
            errors.append(f"Value for {key} must be a string, or null to clear")
            continue
        if not val.strip():
            # THE AMBIGUITY, refused. This is the whole point of the contract: an
            # empty value cannot say whether it means "unchanged" or "delete it",
            # so it is never acted on. A deliberate clear says null.
            #
            # The remedy is branched because this arm is reached by BOTH kinds of
            # key, and 76 of the 86 have no unset state. Telling those operators
            # to "send null" routes them straight into the next refusal — a
            # two-step dead end in a message whose entire job is to say what to
            # do instead.
            how = (
                "send null to clear it, or a value to set it"
                if key in _OPTIONAL_OVERRIDE_KEYS
                else "this key has no unset state, so send a value"
            )
            errors.append(
                f"Value for {key} is empty, which is ambiguous — {how}. "
                f"If this came from the dashboard, reload the page."
            )
            continue
        if len(val) > 500:
            errors.append(f"Value for {key} too long (max 500 chars)")
        if len(val.splitlines()) > 1 or "\x00" in val:
            # ANY line separator, not a list of the ones we happened to think of.
            # This previously checked "\n" alone, and MEASURED, a bare "\r" got
            # through: the value is written as one physical line, but both
            # `Path.read_text()` (universal newlines) and python-dotenv — which
            # loads this file with `override=True` at startup — treat a lone CR
            # as a line boundary, so `KEY=good\rOTHER=x` parses as TWO
            # assignments. The second one never passed _KNOWN_KEYS or any
            # per-key rule, i.e. an arbitrary env var written straight past the
            # registry allowlist.
            #
            # `str.splitlines()` splits on the full Unicode set — \n \r \r\n \v
            # \f \x1c \x1d \x1e \x85     — which is a SUPERSET of what
            # the loader treats as a boundary. Deriving the check from that
            # instead of enumerating characters means a separator nobody thought
            # of is refused for free, and there is no list to keep in sync.
            errors.append(f"Value for {key} contains a line break or null byte")
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

    # Clean values. THE WIRE PROTOCOL'S `null` BECOMES `""` HERE, at the route
    # boundary, and nowhere deeper: _update_secrets_file keeps its own
    # ""-means-unset contract untouched, so its direct-call tests and its OTHER
    # caller (routes/backup.py) are unaffected by this change. The ambiguity was
    # in the HTTP contract, so the translation belongs in the HTTP layer.
    clean = {
        k: ("" if v is None else v.strip())
        for k, v in updates.items()
        if k in _KNOWN_KEYS
    }
    cleared = sorted(k for k, v in clean.items() if v == "")

    try:
        _update_secrets_file(clean)
        # Update os.environ so the dashboard status refreshes immediately
        # (runtime still needs restart to pick up changes)
        for k, v in clean.items():
            if v == "":
                # POP, don't assign "". The accessors branch on
                # `os.environ.get(key) is not None`, so an empty string still
                # shadows genesis.yaml — the live process would keep ignoring the
                # yaml even though the file on disk no longer assigns anything,
                # and the dashboard would report a state the next restart contradicts.
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        logger.info(
            "Secrets updated via dashboard: %s (cleared: %s)",
            list(clean.keys()),
            cleared or "none",
        )
        return jsonify({
            "status": "ok",
            "updated": list(clean.keys()),
            # Which of `updated` were asked to be CLEARED rather than set.
            #
            # It echoes the REQUEST, not the disk outcome: clearing a key the
            # file never assigned writes nothing (the writer skips an empty
            # append) and the key still appears here. That is deliberate — the
            # caller asked, and the two documented limits in the docstring above
            # mean "asked" and "changed on disk" genuinely differ — but it is
            # not an assertion that anything was removed.
            "cleared": cleared,
            "needs_restart": True,
        })
    except Exception:
        logger.error("Failed to update secrets", exc_info=True)
        return jsonify({"error": "Failed to write secrets.env"}), 500
