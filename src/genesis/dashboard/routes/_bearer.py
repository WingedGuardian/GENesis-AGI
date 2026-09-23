"""Shared bearer-token check for the dashboard's self-authenticating routes.

``auth.py`` exempts ``/v1/*`` from the dashboard session check, so every route
under that prefix must enforce its own credential. This is that check, in one
place, so the subtleties below are not re-derived (or re-got-wrong) per
endpoint.

Three of them are not obvious:

* **Compare BYTES, not str.** ``hmac.compare_digest`` raises ``TypeError`` on
  str arguments containing non-ASCII, and Werkzeug decodes header bytes as
  latin-1 — so a header of raw high bytes reaches the comparison and turns a
  routine 401 into a 500 with a traceback, reachable with no credential.
* **A blank-ish token is not a credential.** A trailing space after the ``=``
  in an env file, or a half-filled placeholder, leaves the variable *set*. Such
  a value must behave exactly like unset, or the surface reports itself enabled
  in the boot log while guarding a guessable secret. Empty and whitespace-only
  are refused unconditionally; the length FLOOR is per-surface (``min_chars``),
  because raising it on a surface that already exists would silently disable a
  live integration whose operator chose a shorter token years ago. New surfaces
  take the default.
* **The scheme is case-insensitive.** RFC 7235 section 2.1 defines auth-scheme
  as a case-insensitive token, so a conforming client sending ``bearer`` is not
  sending a malformed request.
"""

from __future__ import annotations

import hmac
import os

from flask import request

# Below this, a configured value is treated as absent rather than as a secret.
# 16 characters is not a cryptographic claim — it is the line under which a
# value is obviously a placeholder or an accident rather than a credential.
MIN_TOKEN_CHARS = 16


def token_is_configured(env_var: str, *, min_chars: int = MIN_TOKEN_CHARS) -> bool:
    """Whether *env_var* holds something that counts as a credential.

    Exposed so a boot-time log can tell the truth about whether a surface is
    enabled, using the SAME rule the request path applies. A warning that says
    "enabled" while every request 503s is worse than no warning.
    """
    value = os.environ.get(env_var, "").strip()
    return bool(value) and len(value) >= min_chars


def require_bearer(
    env_var: str, label: str, *, min_chars: int = MIN_TOKEN_CHARS,
) -> tuple[str, int] | None:
    """Validate the request's bearer token. ``None`` means OK.

    Returns ``(message, status)`` otherwise: 503 when *env_var* is not
    configured (fail-closed — an unconfigured surface is OFF, not open on a
    trusted network), 401 for a missing, malformed or wrong credential.
    """
    token = os.environ.get(env_var, "").strip()
    if not token or len(token) < min_chars:
        return (f"{label} disabled: {env_var} not configured", 503)

    auth_header = request.headers.get("Authorization", "")
    scheme, _, presented = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return ("Missing or invalid Authorization header", 401)

    try:
        ok = hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
    except (UnicodeEncodeError, ValueError):
        # A header carrying bytes that will not round-trip is not a credential.
        # Refusing here keeps a garbage header a 401 instead of a 500.
        ok = False

    if not ok:
        return ("Invalid bearer token", 401)

    return None
