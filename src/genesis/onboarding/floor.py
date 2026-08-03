"""The live *functional floor* — the honest "is this Genesis actually working" signal.

This is deliberately **decoupled** from the ``~/.genesis/setup-complete`` marker.
That marker means only "bootstrap finished" (``scripts/bootstrap.sh`` touches it
unconditionally at the end of every install, and the terminal onboarding skill
writes it too). It says nothing about whether the install can *think*.

The floor, by contrast, is **computed live** from persisted state every time it is
asked — so it stays correct if a key is later removed or expires, which a one-shot
marker file could never represent. A functional install needs:

* a working brain — Claude Code OAuth login (primary cognition is ``claude -p``);
* at least one routing-consumed **LLM** key; and
* at least one **embedding** key (memory needs vectors).

Telegram, web-search, and surplus providers are strongly recommended for a *good*
install but are **not** part of the floor — 24/7 throughput is a spectrum, not a
threshold.

Single source of truth: the dashboard ``setup-status`` route, the ego cadence gate
(``genesis.ego.cadence``), and the CC session-start onboarding prompt
(``scripts/genesis_session_context.py``) all import from here so the definition of
"functional" cannot drift between surfaces.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from genesis.env import secrets_path

logger = logging.getLogger(__name__)

# Canonical env-var names, exactly as defined in ``secrets.env.example`` (the
# dashboard secrets registry only accepts these forms). Note the provider-suffix
# forms for Google / OpenAI — NOT ``API_KEY_*`` — must match or a fresh install
# that configured them would read as "missing".
#
# ANTHROPIC_API_KEY is intentionally EXCLUDED from the LLM set: routing has no
# ``type: anthropic`` provider (the ``anthropic/claude-*`` entries in
# ``config/model_routing.yaml`` are OpenRouter *slugs* under ``type: openrouter``),
# and a standing directive forbids wiring ANTHROPIC_API_KEY into the runtime. A
# bare Anthropic key therefore does NOT make Genesis able to think — counting it
# would let a non-functional install read as "floor met".
LLM_KEY_NAMES: tuple[str, ...] = (
    "API_KEY_OPENROUTER",
    "API_KEY_GROQ",
    "API_KEY_DEEPSEEK",
    "API_KEY_MISTRAL",
    "GOOGLE_API_KEY",
    "API_KEY_ZENMUX",
)
EMBEDDING_KEY_NAMES: tuple[str, ...] = (
    "API_KEY_VOYAGE",
    "API_KEY_DEEPINFRA",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
)

# Values that mean "not really set" even when the key line exists in secrets.env.
UNSET_SENTINELS = ("", "None", "NA")


@dataclass(frozen=True)
class FloorStatus:
    """The three floor legs and their conjunction."""

    cc_oauth: bool
    llm_key_present: bool
    embedding_key_present: bool

    @property
    def floor_met(self) -> bool:
        return self.cc_oauth and self.llm_key_present and self.embedding_key_present

    def as_dict(self) -> dict[str, bool]:
        return {
            "cc_oauth": self.cc_oauth,
            "llm_key_present": self.llm_key_present,
            "embedding_key_present": self.embedding_key_present,
            "floor_met": self.floor_met,
        }


def _credentials_json_has_oauth() -> bool:
    """True when CC's credentials file exists and carries the ``claudeAiOauth`` key.

    Mirrors the validity signal used by ``guardian/cred_integrity.py`` (the one
    stable structural key CC always writes on login). File-presence + structural
    key only — never a live network call. Honors ``CLAUDE_CONFIG_DIR``.
    """
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(cfg).expanduser() if cfg else (Path.home() / ".claude")
    creds = base / ".credentials.json"
    try:
        if not creds.is_file():
            return False
        data = json.loads(creds.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and "claudeAiOauth" in data


def cc_oauth_present() -> bool:
    """Whether Claude Code is authenticated (the primary cognition brain).

    Checks the same signals the runtime uses, in override order:

    1. ``CLAUDE_CODE_OAUTH_TOKEN`` env var (explicit token overrides everything —
       matches ``guardian/diagnosis.py``'s fallback-injection path);
    2. the dedicated synced token file ``~/.genesis/cc_oauth_token.env`` (the exact
       path ``credential_bridge._CC_TOKEN_SOURCE`` reads — deliberately NOT
       GENESIS_HOME-relative, so we match where the token actually lives);
    3. CC's own ``~/.claude/.credentials.json`` (or ``$CLAUDE_CONFIG_DIR``) carrying
       the ``claudeAiOauth`` structural key.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return True

    tok_file = Path("~/.genesis/cc_oauth_token.env").expanduser()
    try:
        if tok_file.is_file():
            for raw in tok_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") and line.split("=", 1)[1].strip():
                    return True
    except OSError:
        pass

    return _credentials_json_has_oauth()


def read_persisted_secrets() -> dict[str, str]:
    """Read ``secrets.env`` from disk WITHOUT touching ``os.environ``.

    ``dotenv_values`` parses the file directly, so a key the user just saved through
    the dashboard is visible immediately (``os.environ`` stays stale until the next
    server restart). Never raises.

    An ABSENT file is a normal fresh-install state and returns ``{}`` quietly. A file
    that EXISTS but cannot be read is a real fault (lock / permission / disk) and is
    logged at WARNING — a gate that keys off the floor (ego cadence) then sees "floor
    unmet" and the warning explains why, rather than autonomy skipping in silence.
    """
    path = secrets_path()
    try:
        exists = path.is_file()
    except OSError:
        logger.warning("floor: could not stat secrets.env at %s", path, exc_info=True)
        return {}
    if not exists:
        return {}
    try:
        from dotenv import dotenv_values

        return {k: (v or "") for k, v in dotenv_values(path).items()}
    except Exception:  # noqa: BLE001 - the floor must never raise into its callers
        logger.warning("floor: secrets.env exists but could not be read", exc_info=True)
        return {}


def _has_any(secrets: Mapping[str, str], names: tuple[str, ...]) -> bool:
    return any(str(secrets.get(n, "")).strip() not in UNSET_SENTINELS for n in names)


def compute_floor(secrets: Mapping[str, str] | None = None) -> FloorStatus:
    """Compute the live floor.

    ``secrets`` may be supplied (e.g. a caller that already parsed ``secrets.env``);
    when omitted it is read fresh from the persisted file. Pure/​read-only — safe to
    call from a hot gate.
    """
    if secrets is None:
        secrets = read_persisted_secrets()
    return FloorStatus(
        cc_oauth=cc_oauth_present(),
        llm_key_present=_has_any(secrets, LLM_KEY_NAMES),
        embedding_key_present=_has_any(secrets, EMBEDDING_KEY_NAMES),
    )
