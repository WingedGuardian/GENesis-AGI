"""Dashboard authentication — optional password-based access control.

If DASHBOARD_PASSWORD is set in secrets.env, the dashboard requires login.
If unset/empty, auth is disabled and the dashboard works as before.

Session: Flask cookie-based, 30-day lifetime. Password comparison uses
hmac.compare_digest (constant-time, no timing attacks).
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from collections import defaultdict
from pathlib import Path

from flask import jsonify, redirect, request, session

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


def is_authenticated() -> bool:
    """Check if current request has a valid session."""
    if not get_dashboard_password():
        return True  # Auth disabled
    return session.get("authenticated") is True


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

    if check_password(password):
        session.permanent = True
        session["authenticated"] = True
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
        if (resp.ok) {
          window.location.href = '/genesis';
        } else {
          const d = await resp.json().catch(() => ({}));
          err.textContent = d.error || 'Login failed';
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
