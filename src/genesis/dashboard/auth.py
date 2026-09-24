"""Dashboard authentication — optional password-based access control.

If DASHBOARD_PASSWORD is set in secrets.env, the dashboard requires login.
If unset/empty, auth is disabled and the dashboard works as before.

Session: Flask cookie-based, 30-day lifetime. Password comparison uses
hmac.compare_digest (constant-time, no timing attacks).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from flask import current_app, has_app_context, jsonify, redirect, request, session

from genesis.dashboard._blueprint import blueprint

logger = logging.getLogger(__name__)

# ── Rate limiting ─────────────────────────────────────────────────────
# Simple in-memory rate limiter: 5 failed attempts per IP, 5-minute lockout.

_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300
_failed_attempts: dict[str, list[float]] = defaultdict(list)


def _is_rate_limited(ip: str) -> bool:
    """Check if an IP is locked out from login attempts."""
    import time

    now = time.monotonic()
    # Prune old attempts
    _failed_attempts[ip] = [t for t in _failed_attempts[ip] if now - t < _LOCKOUT_SECONDS]
    return len(_failed_attempts[ip]) >= _MAX_ATTEMPTS


def _record_failed_attempt(ip: str) -> None:
    """Record a failed login attempt for rate limiting."""
    import time

    _failed_attempts[ip].append(time.monotonic())


# ── Password & secret key ────────────────────────────────────────────

def get_dashboard_password() -> str | None:
    """Return configured password, or None if auth is disabled."""
    pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    return pw if pw else None


def get_or_create_secret_key() -> str:
    """Persistent Flask secret key — generates once, reuses across restarts."""
    key_file = Path.home() / ".genesis" / "flask_secret_key"
    if key_file.exists():
        try:
            key = key_file.read_text().strip()
            if key:
                return key
        except OSError:
            logger.warning("Could not read flask secret key file", exc_info=True)
    key = secrets.token_hex(32)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key)
        key_file.chmod(0o600)
    except OSError:
        logger.warning("Could not persist flask secret key", exc_info=True)
    return key


_internal_token_cache: str | None = None


def get_or_create_internal_api_token() -> str:
    """Return the persistent internal API token, generating it once if absent.

    Trusted loopback/host callers send this as a bearer to authenticate to
    ``/api`` mutation endpoints when a dashboard password is set. Generated at
    server boot (mode 0600), cached in-process. INDEPENDENT of the optional
    ``GENESIS_MCP_HTTP_TOKEN`` (unset on typical installs), so the gate always has
    a working token. Mirrors :func:`get_or_create_secret_key`.
    """
    global _internal_token_cache
    if _internal_token_cache:
        return _internal_token_cache
    from genesis.env import internal_api_token_path

    token_file = internal_api_token_path()
    if token_file.exists():
        try:
            tok = token_file.read_text().strip()
            if tok:
                _internal_token_cache = tok
                return tok
        except OSError:
            logger.warning("Could not read internal API token", exc_info=True)
    tok = secrets.token_hex(32)
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(tok)
        token_file.chmod(0o600)
    except OSError:
        logger.warning("Could not persist internal API token", exc_info=True)
    _internal_token_cache = tok
    return tok


# ── Machine-caller bearer auth for /v1/* surfaces ───────────────────
#
# ``_check_auth`` below exempts the whole ``/v1/*`` prefix from the dashboard
# SESSION gate, deliberately: those routes serve machine callers that have no
# browser session. That exemption is not an exemption from auth — each such
# route enforces its own bearer check, and this is the one implementation they
# share so a new ``/v1/*`` surface cannot quietly ship without one.


def check_bearer_token(surface: str) -> tuple[str, int] | None:
    """Validate a machine caller's ``Authorization: Bearer`` header.

    Returns ``(error message, http status)`` on refusal, or ``None`` when the
    caller is authorized.

    Fail-closed: with no ``GENESIS_MCP_HTTP_TOKEN`` configured the surface
    answers 503 rather than opening. These endpoints reach real authority —
    CC invocation, memory writes, the voice graduation write — so
    open-by-default is not acceptable even on a trusted overlay network.

    ``surface`` names the caller in the 503 text only, so an operator who hits
    a disabled endpoint learns which one to configure.
    """
    # .strip() matches get_dashboard_password() — a quoted "   " in secrets.env
    # otherwise reads as a configured token that a blank credential satisfies.
    token = os.environ.get("GENESIS_MCP_HTTP_TOKEN", "").strip()
    if not token:
        return (f"{surface} disabled: GENESIS_MCP_HTTP_TOKEN not configured", 503)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return ("Missing or invalid Authorization header", 401)

    # Compare BYTES: compare_digest refuses non-ASCII str operands, and WSGI
    # decodes headers as latin-1 — so a header carrying any high byte raised
    # TypeError and surfaced as a 500 with a stack trace per request. Still
    # fail-closed, but a spammable 500 where a 401 belongs.
    presented = auth_header[7:].encode("utf-8", "surrogateescape")
    if not hmac.compare_digest(presented, token.encode("utf-8", "surrogateescape")):
        return ("Invalid bearer token", 401)

    return None


def is_authenticated() -> bool:
    """Check if current request has a valid session.

    Returns ``True`` when no password is configured — "auth disabled". That is
    correct for a GATE (``if not is_authenticated(): 401``): an install that
    chose not to set a password is not refused its own dashboard.

    It is WRONG for a disclosure decision. See ``has_verified_credential``.
    """
    if not get_dashboard_password():
        return True  # Auth disabled
    return session.get("authenticated") is True


def has_verified_credential() -> bool:
    """Did this request PROVE who it is? Never true without a password set.

    The distinction from ``is_authenticated`` is the whole point, and the two
    are not interchangeable:

    * ``is_authenticated`` answers *"may this request proceed?"* and opens up
      when no password is configured, so an unconfigured install keeps working.
    * ``has_verified_credential`` answers *"has this caller demonstrated it is
      the operator?"* — and with no password configured, nothing can, because
      there is no credential to present.

    Use this for any decision that REVEALS something rather than admitting
    someone. Gating disclosure on ``is_authenticated`` inverts it: the flag
    that chooses redact-vs-reveal flips to REVEAL on exactly the installs that
    have no credential, so a passwordless box serves its secrets to whoever can
    reach it. That was live on three sites — the provider-key values and two
    backup-config routes — and is what this predicate exists to prevent.

    Deliberately session-only: it does NOT accept the internal bearer token.
    Both current callers are the dashboard's own browser front-end, so no
    machine caller needs it, and a process holding that 0600 token can already
    read the same values straight out of the environment — accepting it here
    would widen the surface while buying nothing.
    """
    pw = get_dashboard_password()
    if not pw:
        return False
    if session.get("authenticated") is not True:
        return False
    # Bind the session to the credential it was issued against, so rotating the
    # password after a suspected compromise actually evicts the old cookie from
    # the disclosure path. A session minted before this existed carries no
    # fingerprint and is refused here — it must log in again to see values.
    # Deliberately NOT applied to ``is_authenticated``: evicting gates would
    # narrow access, which this change promises not to do.
    return hmac.compare_digest(
        str(session.get("pw_fingerprint", "")), _password_fingerprint(pw)
    )


def _password_fingerprint(password: str) -> str:
    """A KEYED tag identifying WHICH password a session was issued for.

    Keyed rather than bare, and that is the whole point of it. A Flask session
    cookie is SIGNED but not ENCRYPTED, so everything inside it is readable by
    anyone holding the cookie. A plain digest of the password would hand that
    holder an offline dictionary attack against a password an operator very
    likely chose by hand — and truncation buys nothing against it, because an
    attacker testing candidates truncates their own digests the same way.
    Truncation costs collision resistance, which is not the property under
    attack. Keying with a secret the client never sees removes the attack.

    NOT a slow KDF, deliberately, and the reasoning is worth keeping because
    the obvious upgrade does not survive it. A KDF would only help against an
    attacker who already holds the Flask secret — and such an attacker does not
    need to crack anything: they forge ``authenticated: True`` directly, which
    is what the terminal WebSocket gates on. The extra cost would buy defence
    against someone who already has a shell, while forcing either a
    per-request delay or a cache that RETAINS a rotated-away plaintext
    password. Both are worse than the thing they defend.

    The key is the one Flask signed the cookie with, so a fingerprint cannot
    outlive the cookie carrying it: rotating the secret invalidates the
    signature and the tag together, and no session is left half-valid.

    Encoded as BYTES with ``surrogateescape`` throughout — the same discipline
    ``check_bearer_token`` uses. That covers every password the environment can
    actually deliver: Python decodes ``os.environ`` with ``surrogateescape``, so
    a value containing bytes that are not valid UTF-8 arrives as U+DC80..U+DCFF
    and re-encodes to the original bytes. VERIFIED, not assumed — a plain
    ``.encode()`` raises on exactly those.

    Stated limit: a LONE high surrogate (U+D800..U+DBFF) still raises, because
    ``surrogateescape`` only reverses the escapes it creates. That range is not
    producible by the environment decode and would have to be assigned to
    ``os.environ`` in-process, so it is named here rather than defended against.
    """
    key = current_app.secret_key if has_app_context() else None
    if not key:
        key = get_or_create_secret_key()
    if isinstance(key, str):
        key = key.encode("utf-8", "surrogateescape")
    message = password.encode("utf-8", "surrogateescape")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16]


def check_password(input_password: str) -> bool:
    """Constant-time password comparison."""
    pw = get_dashboard_password()
    if not pw:
        return True
    return hmac.compare_digest(input_password.encode(), pw.encode())


# ── Static assets (needed by login page before auth) ────────────────

_STATIC_PREFIXES = (
    "/index.css",
    "/css/",
    "/js/",
    "/vendor/",
    "/public/",
    "/favicon",
)


# ── before_request hook ──────────────────────────────────────────────

@blueprint.before_request
def _check_auth():
    """Gate the dashboard web UI behind password auth when configured.

    Auth scope: browser-facing HTML pages ONLY. The dashboard is
    reachable from any IP (proxied through the host VM), so this
    auth gate protects against unauthorized browser access.

    API and programmatic endpoints (/api/*, /v1/*) pass through
    freely — Guardian probes, OpenClaw, MCP tools, and any machine
    caller should never be blocked. Auth is a door on the web
    dashboard, not a lockdown on Genesis's API surface.
    """
    # Auth disabled — no password set
    if not get_dashboard_password():
        return None

    # All API/programmatic routes are open — auth gates the web UI only
    if request.path.startswith("/api/") or request.path.startswith("/v1/"):
        return None

    # Static assets needed by login page
    if any(request.path.startswith(p) for p in _STATIC_PREFIXES):
        return None

    # Note: /genesis/login is an app-level route (standalone.py), not on this
    # blueprint, so blueprint before_request hooks don't fire for it.

    # Check session
    if is_authenticated():
        return None

    # Unauthenticated page request → redirect to login
    return redirect("/genesis/login")


# ── App-level /api mutation gate (registered in standalone.py) ────────

# Env kill switch: an operator can disable the /api mutation gate WITHOUT
# unsetting the dashboard password, if an unforeseen machine caller breaks.
_API_AUTH_OFF = ("off", "0", "false", "no")

# Genesis-OWNED API route prefixes the gate protects. Scoped deliberately: in Agent
# Zero hosting mode the gate is installed on AZ's host-owned Flask app, so a broad
# "/api/" match would also reject AZ's OWN native /api/* routes. Every Genesis
# mutation route lives under one of these (dashboard + outreach are /api/genesis/*;
# the tool API is /api/t/*), so this covers all of ours and none of the host's.
_GENESIS_API_PREFIXES = ("/api/genesis/", "/api/t/")


# CSRF: Sec-Fetch-Site values that indicate a request is NOT a cross-origin ride.
# ``same-origin`` = our own page's fetch; ``none`` = a direct user action (typed
# URL / bookmark). ``same-site`` and ``cross-site`` are the CSRF-risky values.
_SAFE_FETCH_SITES = frozenset({"same-origin", "none"})


def _origin_matches_host(url: str) -> bool:
    """True when ``url``'s host[:port] equals the request's ``Host`` header.

    Host comparison is case-insensitive (hostnames are per RFC; browsers already
    lowercase, this covers hand-crafted same-origin tooling) — a case mismatch can
    only make the check stricter (fail-closed), never open a bypass.
    """
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == (request.host or "").lower()


def _is_same_origin_request() -> bool:
    """Whether a state-changing request is same-origin — the CSRF check for the
    cookie auth path.

    ``SameSite=Lax`` attaches the session cookie on same-*site* requests too (a
    sibling origin on another port/subdomain of the dashboard host), so a valid
    cookie is not proof of same-origin intent. Primary signal is ``Sec-Fetch-Site``
    (browser-set, unforgeable by page JS, present on every current browser): safe
    iff ``same-origin`` or ``none``. When it is absent (older browser / non-browser
    client) fall back to matching the ``Origin`` (then ``Referer``) host against the
    request ``Host``. When NO same-origin signal is present at all, **fail closed** —
    a legitimate machine caller authenticates with the internal bearer, not the
    cookie, so a cookie-only request with no origin signal is refused (per the OWASP
    CSRF Fetch-Metadata guidance: treat absent Sec-Fetch-* as unknown, do not fail
    open).
    """
    sec_fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    if sec_fetch_site:
        return sec_fetch_site in _SAFE_FETCH_SITES
    origin = request.headers.get("Origin", "").strip()
    if origin:
        return _origin_matches_host(origin)
    referer = request.headers.get("Referer", "").strip()
    if referer:
        return _origin_matches_host(referer)
    return False


def check_api_mutation_auth():
    """App-level gate: require auth for ``/api/**`` STATE-CHANGING requests when a
    dashboard password is set.

    Registered as an app-level ``before_request`` (NOT blueprint-level) so it
    covers every blueprint uniformly — the main dashboard blueprint AND the
    separate ``outreach_api`` blueprint, whose mutation routes a blueprint-scoped
    hook would miss.

    Open (returns None): everything when no password is set; when the kill switch
    ``GENESIS_DASHBOARD_API_AUTH=off`` is set; any path outside the Genesis-owned
    API prefixes ``_GENESIS_API_PREFIXES`` (HTML is handled by the blueprint gate;
    ``/v1/*`` enforces its own bearer; a co-hosting framework's own ``/api/*`` routes
    are left alone); GET/HEAD/OPTIONS (non-mutating — guardian/health probes and
    dashboard polling stay open); and the ``/api/genesis/auth/*`` login/logout
    endpoints. A mutation passes with a valid internal bearer token, OR a valid
    session cookie on a same-origin request (Sec-Fetch-Site / Origin — CSRF guard).
    A cookie-authed cross-origin/originless mutation is rejected 403; a request with
    no credential at all is rejected 401.
    """
    if not get_dashboard_password():
        return None
    if os.environ.get("GENESIS_DASHBOARD_API_AUTH", "on").strip().lower() in _API_AUTH_OFF:
        return None

    path = request.path
    if not path.startswith(_GENESIS_API_PREFIXES):
        return None
    if path.startswith("/api/genesis/auth/"):
        return None
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    # Trusted machine caller (internal bearer token) — CSRF-immune (an attacker
    # cannot read the 0600 token file), so it is checked FIRST and is
    # origin-independent.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        expected = get_or_create_internal_api_token()
        if expected and hmac.compare_digest(auth_header[7:], expected):
            return None

    # Trusted browser session (session cookie). A cookie is NOT proof of
    # same-origin intent (``SameSite=Lax`` still attaches it on a same-site sibling
    # origin), so a cookie-authed mutation must ALSO be same-origin — CSRF
    # defense-in-depth. Fail-closed on a cross-origin/originless cookie request.
    if is_authenticated():
        if _is_same_origin_request():
            return None
        return jsonify({"error": "cross-origin request refused"}), 403

    return jsonify({"error": "authentication required"}), 401


def apply_api_mutation_gate(app) -> None:
    """Mint the internal API token and register the app-level ``/api`` mutation gate.

    EVERY supported host that mounts the dashboard/outreach blueprints must call
    this, or setting ``DASHBOARD_PASSWORD`` would leave ``/api`` mutations
    unauthenticated in that mode (the blueprint-level auth hook deliberately exempts
    ``/api/*``). Idempotent — guarded by a flag on the app so a double-call (or a
    host that both creates the app and re-registers blueprints) can't stack the
    hook.
    """
    if getattr(app, "_genesis_api_mutation_gate_applied", False):
        return
    get_or_create_internal_api_token()  # mint once (0600) before the gate needs it
    app.before_request(check_api_mutation_auth)
    app._genesis_api_mutation_gate_applied = True


# ── Routes ────────────────────────────────────────────────────────────

@blueprint.route("/api/genesis/auth/status")
def auth_status():
    """Check whether auth is enabled and whether the user is logged in."""
    pw = get_dashboard_password()
    return jsonify({
        "enabled": pw is not None,
        "authenticated": is_authenticated(),
    })


@blueprint.route("/api/genesis/auth/login", methods=["POST"])
def auth_login():
    """Validate password, create session."""
    ip = request.remote_addr or "unknown"

    if _is_rate_limited(ip):
        logger.warning("Dashboard login rate-limited for %s", ip)
        return jsonify({"error": "Too many attempts. Try again in a few minutes."}), 429

    data = request.get_json(silent=True) or {}
    password = data.get("password", "")

    if not password:
        return jsonify({"error": "Password required"}), 400

    # Mint NOTHING when there is no credential to check against. ``check_password``
    # returns True in that state ("auth disabled"), so without this guard any POST
    # to this route received a permanent 30-day session on a passwordless install —
    # and that cookie outlived the configuration change, so an operator who later
    # set a password inherited a session an attacker had already harvested. That
    # defeats the exact remediation this file recommends, which is why the check
    # is here and not only at the disclosure sites.
    pw = get_dashboard_password()
    if not pw:
        logger.info("Dashboard login attempted from %s while auth is disabled", ip)
        return jsonify({"status": "auth_disabled"})

    if check_password(password):
        session.permanent = True
        session["authenticated"] = True
        session["pw_fingerprint"] = _password_fingerprint(pw)
        logger.info("Dashboard login successful from %s", ip)
        return jsonify({"status": "ok"})

    _record_failed_attempt(ip)
    logger.warning("Dashboard login failed from %s", ip)
    return jsonify({"error": "Invalid password"}), 401


@blueprint.route("/api/genesis/auth/logout", methods=["POST"])
def auth_logout():
    """Clear session, redirect to login."""
    session.clear()
    return jsonify({"status": "ok"})


# ── Login page ────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Genesis — Login</title>
  <link rel="stylesheet" href="/index.css">
  <!-- tokens + components AFTER index.css, for the same reason every other page
       links them: index.css pins `html, body` to `overflow: hidden;
       position: fixed`, written for an app shell, and an ordinary document
       cannot scroll under it. MEASURED on this page before this line existed:
       `html` computed `overflow: hidden`, `position: fixed`, and
       `scrollHeight == innerHeight` with scrolling unavailable — the login card
       fits a normal viewport, so nothing was visibly wrong until the viewport
       was short or the error variant made the card taller.
       components.css carries the shared neutralisation; tokens.css carries the
       variables components.css is built on. Linking them rather than copying
       three declarations into the <style> below is the whole point — this page
       was missed precisely because the fix used to be copied per page. -->
  <link rel="stylesheet" href="/css/tokens.css">
  <link rel="stylesheet" href="/css/components.css">
  <style>
    body {
      margin: 0; padding: 0;
      background: var(--color-bg-primary, #0a0a0f);
      color: var(--color-text-primary, #e0e0e0);
      font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', monospace;
      display: flex; justify-content: center; align-items: center;
      min-height: 100vh;
    }
    .login-card {
      background: var(--color-bg-secondary, #12121a);
      border: 1px solid var(--color-border, #2a2a3a);
      border-radius: 8px; padding: 2rem; width: 320px;
      text-align: center;
    }
    .login-card h1 {
      font-size: 1.2rem; margin: 0 0 0.25rem 0;
      color: var(--color-text-primary, #e0e0e0);
    }
    .login-card .subtitle {
      font-size: 0.72rem; color: var(--color-text-secondary, #888);
      margin-bottom: 1.5rem;
    }
    .login-card input[type="password"] {
      width: 100%; padding: 0.6rem 0.8rem; font-size: 0.85rem;
      border: 1px solid var(--color-border, #2a2a3a);
      border-radius: 4px; background: var(--color-background, #0a0a0f);
      color: var(--color-text-primary, #e0e0e0);
      font-family: inherit; box-sizing: border-box;
      outline: none;
    }
    .login-card input:focus {
      border-color: var(--color-primary, #2196F3);
    }
    .login-card button {
      width: 100%; padding: 0.6rem; margin-top: 0.75rem;
      font-size: 0.82rem; font-family: inherit;
      background: var(--color-primary, #2196F3); color: #fff;
      border: none; border-radius: 4px; cursor: pointer;
    }
    .login-card button:hover { opacity: 0.9; }
    .login-card button:disabled { opacity: 0.5; cursor: wait; }
    .error {
      color: #d9534f; font-size: 0.74rem; margin-top: 0.5rem;
      min-height: 1.2em;
    }
  </style>
</head>
<body>
  <div class="login-card">
    <h1>Genesis</h1>
    <div class="subtitle">Dashboard authentication</div>
    <form id="login-form">
      <input type="password" id="pw" placeholder="Password" autofocus>
      <button type="submit" id="btn">Login</button>
      <div class="error" id="err"></div>
    </form>
  </div>
  <script>
    document.getElementById('login-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('btn');
      const err = document.getElementById('err');
      const pw = document.getElementById('pw').value;
      if (!pw) { err.textContent = 'Enter a password'; return; }
      btn.disabled = true; err.textContent = '';
      try {
        const resp = await fetch('/api/genesis/auth/login', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          credentials: 'same-origin',
          body: JSON.stringify({password: pw}),
        });
        const body = await resp.json().catch(() => ({}));
        if (resp.ok && body.status === 'auth_disabled') {
          // 200, but NOTHING was minted: there is no password to check
          // against. Redirecting here told the operator they were logged in
          // while the session that gates the protected views did not exist,
          // so values stayed hidden with no explanation.
          err.textContent = 'No dashboard password is configured, so there is '
            + 'nothing to log in to. Set one in Secrets to enable the '
            + 'protected views.';
        } else if (resp.ok) {
          window.location.href = '/genesis';
        } else {
          err.textContent = body.error || 'Login failed';
          document.getElementById('pw').select();
        }
      } catch (ex) {
        err.textContent = 'Connection error';
      }
      btn.disabled = false;
    });
  </script>
</body>
</html>
"""


def login_page_html() -> str:
    """Return the login page HTML."""
    return _LOGIN_HTML
