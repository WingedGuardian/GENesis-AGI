"""Container CC login health — refresh-token expiry + fallback-token gating.

The claude.ai OAuth login stored in ``~/.claude/.credentials.json`` has a
FIXED-lifetime refresh token: routine access-token refresh does NOT extend it,
and when it lapses every CC request fails until an interactive ``/login``.
Background sessions ride the same credentials, so a lapsed login silently
kills autonomy. (Discovered 2026-08-18: nothing anywhere read
``refreshTokenExpiresAt``.)

Two consumers:

- the awareness check ``_check_cc_login_expiry`` (warns days ahead via the
  standard critical-observation → Telegram path);
- the invoker's fallback injection: when the login is HARD-expired and a live
  ``claude auth status`` probe CONFIRMS logged-out, CC invocations (foreground turns and
background dispatches alike) get ``CLAUDE_CODE_OAUTH_TOKEN`` from the operator's stored 1-year setup-token
  (``~/.genesis/cc_oauth_token.env``, written by ``scripts/store_cc_token.sh``).

The injection mirrors the host guardian's honest boundary
(``guardian/diagnosis.py``): the env token OVERRIDES a stored login, so it is
injected ONLY on confirmed-dead login — never on ambiguity, never over a
working login. The setup-token deliberately stays out of secrets.env
(``credential_bridge.py`` — load_dotenv would hijack every container CC
subprocess).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from genesis.util.proc_kill import kill_process_group, reap_bounded

logger = logging.getLogger(__name__)

_TOKEN_FILE = Path("~/.genesis/cc_oauth_token.env").expanduser()

# Confirmed-logged-out probe results are cached briefly so a burst of
# background dispatches during an outage doesn't spawn a probe subprocess
# each. A successful /login rewrites credentials.json with a future expiry,
# which flips the CHEAP timestamp gate below immediately — so a stale cached
# "logged out" can never override a restored login.
_PROBE_CACHE_TTL_S = 60.0
_probe_cache: tuple[float, bool] | None = None
_probe_lock: asyncio.Lock | None = None


def _get_probe_lock() -> asyncio.Lock:
    """Single-flight guard: N concurrent dispatches during an outage share
    one probe subprocess per cache window instead of spawning N."""
    global _probe_lock
    if _probe_lock is None:
        _probe_lock = asyncio.Lock()
    return _probe_lock


def reset_probe_cache() -> None:
    """Test hook: clear the cached probe verdict."""
    global _probe_cache
    _probe_cache = None


def credentials_path() -> Path:
    """CLAUDE_CONFIG_DIR-aware path to CC's credentials file."""
    base = os.environ.get("CLAUDE_CONFIG_DIR", "")
    root = Path(base).expanduser() if base else Path.home() / ".claude"
    return root / ".credentials.json"


def refresh_token_expiry() -> datetime | None:
    """The interactive login's refresh-token expiry, or None when unknown.

    None covers: missing file (fresh/API-key-only installs), unreadable or
    mid-rewrite JSON, and absent field — a parse hiccup must read as "no
    signal", never as "expired".
    """
    path = credentials_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    oauth = raw.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    expires_ms = oauth.get("refreshTokenExpiresAt")
    if not isinstance(expires_ms, (int, float)) or isinstance(expires_ms, bool):
        return None
    try:
        return datetime.fromtimestamp(expires_ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def read_fallback_token() -> str | None:
    """The stored 1-year setup-token, or None when not provisioned."""
    try:
        for line in _TOKEN_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


async def probe_logged_out(cc_path: str = "claude", timeout_s: float = 15.0) -> bool:
    """Return True ONLY when ``claude auth status --json`` CONFIRMS
    ``loggedIn: false``. Timeouts, unparseable output, and any other state
    return False (ambiguity → never inject), mirroring guardian/diagnosis.py.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            cc_path,
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # Own group so the timeout can reap any node children too
            # (mirrors the swept guardian/diagnosis.py probe).
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            kill_process_group(proc)
            await reap_bounded(proc)
            return False
        except asyncio.CancelledError:
            kill_process_group(proc)
            await reap_bounded(proc)
            raise
        try:
            logged_in = json.loads(stdout.decode("utf-8", errors="replace")).get(
                "loggedIn",
            )
        except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
            return False
        return logged_in is False
    except Exception:
        logger.debug("CC auth probe failed", exc_info=True)
        return False


async def fallback_env_if_login_dead(
    *,
    probe=None,
    token_reader=read_fallback_token,
    cc_path: str = "claude",
) -> dict[str, str] | None:
    """Env additions for a background CC invocation, or None (the common case).

    Order of gates (each cheap-to-expensive, all must pass):
    1. refresh token HARD-expired per credentials.json (cheap file read; a
       working or merely-near-expiry login never proceeds);
    2. live probe CONFIRMS logged-out (cached _PROBE_CACHE_TTL_S seconds);
    3. a stored setup-token exists.
    """
    if probe is None:
        # Bind the probe to the CALLER'S configured executable — probing a
        # literal PATH `claude` on installs with a non-PATH binary would read
        # permanently ambiguous and the fallback would never activate.
        async def probe() -> bool:
            return await probe_logged_out(cc_path)

    global _probe_cache
    expiry = refresh_token_expiry()
    if expiry is None or expiry > datetime.now(UTC):
        return None

    async with _get_probe_lock():
        now = time.monotonic()
        if _probe_cache is not None and now - _probe_cache[0] < _PROBE_CACHE_TTL_S:
            confirmed_out = _probe_cache[1]
        else:
            confirmed_out = await probe()
            _probe_cache = (now, confirmed_out)
    if not confirmed_out:
        return None

    token = token_reader()
    if not token:
        logger.warning(
            "CC login is expired and confirmed logged-out, but no fallback "
            "setup-token is stored (~/.genesis/cc_oauth_token.env) — background "
            "sessions will fail until /login or `claude setup-token` + "
            "scripts/store_cc_token.sh",
        )
        return None

    logger.warning(
        "CC login expired + confirmed logged-out — injecting the stored "
        "setup-token for this CC invocation (token value not logged).",
    )
    return {"CLAUDE_CODE_OAUTH_TOKEN": token}
