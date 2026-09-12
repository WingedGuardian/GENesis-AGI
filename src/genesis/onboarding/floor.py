"""The live *functional floor* — the honest "is this Genesis actually working" signal.

This is deliberately **decoupled** from the ``~/.genesis/setup-complete`` marker.
That marker means only "bootstrap finished" (``scripts/bootstrap.sh`` touches it
unconditionally at the end of every install, and the terminal onboarding skill
writes it too). It says nothing about whether the install can *think*.

The floor, by contrast, is **computed live** from persisted state every time it is
asked — so it stays correct if a key is later removed, which a one-shot marker
file could never represent. It is deliberately **presence-based**: it checks that
keys are *configured*, never that they are currently *valid* — live validation
belongs to the routing circuit breakers (which route around a failing provider)
and the wizard's on-demand ``keys/test`` endpoint, NOT to a check on the
ego-tick / session-start hot path, where per-call network validation would add
latency, burn quota, and turn any transient provider blip into a silent autonomy
stop. A functional install needs:

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
from functools import lru_cache
from pathlib import Path

from genesis.env import repo_root, secrets_path

logger = logging.getLogger(__name__)

# Values that mean "not really set" even when the key line exists in secrets.env.
UNSET_SENTINELS = ("", "None", "NA")

# ── LLM floor: derived from what routing ACTUALLY consumes ─────────────────────
# The set of LLM keys that satisfy the floor is NOT hand-maintained (a hard-coded
# list drifted repeatedly: it once counted a dead Anthropic key, then missed
# NVIDIA and counted a disabled DeepSeek, then counted providers that are DECLARED
# but referenced by no active call-site chain). Instead the LLM leg is derived from
# the SAME merged routing config the router itself loads — ``routing.config.
# load_config`` (which applies the install-local ``model_routing.local.yaml``
# overlay + env expansion) — and from the provider TYPES actually referenced in some
# call-site ``chain``. A provider that is enabled but in no chain is never used, so
# its key must not satisfy the floor. Key resolution uses the SAME three env-var
# patterns as the runtime resolver ``routing.litellm_delegate._resolve_api_key``
# (parity test).

# Provider types that need NO API key (local backends) — excluded from the LLM leg.
_KEYLESS_PROVIDER_TYPES = frozenset({"ollama", "lmstudio"})

# Only used if the routing config can't be loaded: the key names for the currently
# chain-referenced cloud provider types, so the floor degrades to "reasonable",
# never "empty". (The live path derives this from the merged config.)
_LLM_KEY_NAMES_FALLBACK: tuple[str, ...] = (
    "API_KEY_OPENROUTER",
    "API_KEY_GROQ",
    "API_KEY_MISTRAL",
    "GOOGLE_API_KEY",
    "API_KEY_ZENMUX",
    "API_KEY_NVIDIA_NIM",
)

# Cloud EMBEDDING backends actually consumed by ``providers/embedding.py`` (deepinfra
# + dashscope/qwen). These do NOT route through model_routing call-sites, so they are
# pinned here rather than derived. NOTE: Voyage is rerank-ONLY, and there is no
# OpenAI/Google embedding backend — counting those (as an earlier hard-coded list
# did) let a box with no real embedding backend read as "floor met". Local Ollama
# embeddings are keyless (a bonus, not a floor-satisfier — the floor measures cloud
# capability).
EMBEDDING_KEY_NAMES: tuple[str, ...] = ("API_KEY_DEEPINFRA", "API_KEY_QWEN")

_ROUTING_CONFIG_PATH = repo_root() / "config" / "model_routing.yaml"


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


@lru_cache(maxsize=1)
def _chain_referenced_cloud_provider_types() -> tuple[str, ...]:
    """Key-requiring provider TYPES that are actually USED by routing.

    Loaded through the router's OWN ``routing.config.load_config`` (so the
    install-local ``model_routing.local.yaml`` overlay + env expansion are honored,
    exactly as the runtime sees them), then reduced to the provider types referenced
    by at least one call-site ``chain`` — a declared-but-unchained provider is never
    routed, so its key must not satisfy the floor. Local/keyless types are excluded.

    Cached — the routing config is static at runtime. Returns ``()`` on any load
    failure, which signals the static fallback.
    """
    try:
        from genesis.routing.config import load_config

        cfg = load_config(_ROUTING_CONFIG_PATH, check_api_keys=False)
    except Exception:  # noqa: BLE001 - fall back rather than raise into the floor
        logger.warning(
            "floor: could not load routing config %s; using static LLM key fallback",
            _ROUTING_CONFIG_PATH,
            exc_info=True,
        )
        return ()
    referenced: set[str] = set()
    for call_site in cfg.call_sites.values():
        referenced.update(getattr(call_site, "chain", ()) or ())
    types: set[str] = set()
    for name in referenced:
        prov = cfg.providers.get(name)  # cfg.providers holds only ENABLED providers
        if prov is not None and prov.provider_type not in _KEYLESS_PROVIDER_TYPES:
            types.add(prov.provider_type)
    return tuple(sorted(types))


def provider_key_present(provider_type: str, secrets: Mapping[str, str]) -> bool:
    """Whether a key for ``provider_type`` is configured in ``secrets``.

    Uses the SAME three env-var patterns as the runtime key resolver
    ``genesis.routing.litellm_delegate._resolve_api_key`` (``API_KEY_{TYPE}`` /
    ``{TYPE}_API_KEY`` / ``{TYPE}_API_TOKEN``), so "is a provider usable" means the
    same thing to the floor and to routing. Kept in sync by
    ``tests/test_onboarding/test_floor.py::test_key_pattern_parity_with_runtime``.
    """
    service = provider_type.upper()
    for pattern in (f"API_KEY_{service}", f"{service}_API_KEY", f"{service}_API_TOKEN"):
        if str(secrets.get(pattern, "")).strip() not in UNSET_SENTINELS:
            return True
    return False


def invalidate_provider_type_cache() -> None:
    """Drop the cached chain-referenced provider-type set.

    Called by ``routing.router.Router.reload_config`` after a hot-reload of the
    routing config (dashboard ``routing/reload`` + per-call-site PUT), so the
    floor re-derives from the freshly-written config file instead of serving the
    pre-reload set until a server restart.
    """
    _chain_referenced_cloud_provider_types.cache_clear()


def _llm_key_present(secrets: Mapping[str, str]) -> bool:
    """True when a chain-referenced cloud LLM provider has its key configured."""
    types = _chain_referenced_cloud_provider_types()
    if types:
        return any(provider_key_present(t, secrets) for t in types)
    # Config unloadable → check the static fallback directly (rare degraded path).
    return _has_any(secrets, _LLM_KEY_NAMES_FALLBACK)


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
        llm_key_present=_llm_key_present(secrets),
        embedding_key_present=_has_any(secrets, EMBEDDING_KEY_NAMES),
    )
