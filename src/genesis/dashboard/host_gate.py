"""App-level Host-header gate for the dashboard's content routes.

The network-scope gate in ``auth`` decides by the TCP peer, and the host's proxy
delivers LAN traffic as loopback, which is trusted. A web page whose own DNS
name is re-pointed at the dashboard's address (DNS rebinding) is then served
from the visitor's browser as if it were the dashboard: the browser treats it
as same-origin, so the page can read the reply. What it cannot change is the
Host header, which carries the page's name, not one of ours. So content routes
answer only to Hosts that name this machine.

An IP-literal Host is accepted: a rebinding page is served under a DNS name,
never under the victim's own address. That reasoning covers READS, which the
browser's same-origin policy protects. It does not cover a cross-site
WebSocket or a form POST, which a page can aim at an IP address directly;
those need an Origin check (the terminal WebSocket has its own).

Out of scope: ``/v1/*`` (every route carries its own bearer, which a page
cannot present), static assets (no stored content), and the supervisor's
network-open routes (``auth._OPEN_FROM_ANY_NETWORK``), which anyone who can
reach the port can already use.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time

from flask import Response, jsonify, request
from werkzeug.sansio.utils import host_is_trusted

from genesis.dashboard.auth import _OPEN_FROM_ANY_NETWORK, has_internal_bearer

logger = logging.getLogger(__name__)

_HOSTNAME_CHECK_OFF = ("off", "0", "false", "no")
_HOST_GATED_PREFIXES = ("/api/genesis/", "/api/t/")
_HOST_GATED_EXACT = ("/ws/terminal", "/genesis")

# A Host header we are willing to interpret: a bracketed IPv6 literal or a
# name/IPv4 of DNS characters, then an optional numeric port, nothing else.
# Anything that does not match is refused, never parsed around.
_HOST_SHAPE = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(:[0-9]{1,5})?")

# How long a FAILED MagicDNS lookup is remembered before the next request may
# try again.
_MAGICDNS_RETRY_SECONDS = 300.0
# How long a SUCCESSFUL lookup is trusted before the next request re-reads it,
# so a renamed tailnet device is accepted without a server restart. The old
# names keep answering while a refresh runs or if it fails.
_MAGICDNS_REFRESH_SECONDS = 600.0
# `tailscale status --self --json` is a local socket query: measured 23-79 ms
# over 5 runs on a live install. It runs inside an HTTP request (the first one
# addressed by a DNS name we do not otherwise know), so a wedged tailscaled must
# not stall that request indefinitely; 5 s is over 60x the measured worst case.
_TAILSCALE_STATUS_TIMEOUT_S = 5.0
# Refused Hosts are logged at most once per interval so a scan cannot flood the
# journal; the next line reports how many were suppressed.
_REFUSAL_LOG_INTERVAL_S = 60.0

_lock = threading.Lock()
_magicdns_names: tuple[str, ...] | None = None
_magicdns_read_at: float | None = None
_magicdns_failed_at: float | None = None
_magicdns_inflight = False
_machine_names: tuple[str, ...] | None = None
_refusal_logged_at: float | None = None
_refusals_suppressed = 0

_REFUSAL = (
    "This dashboard does not answer to that host name. Open it by its IP address, "
    "or add the name to GENESIS_DASHBOARD_ALLOWED_HOSTS."
)


def _reset_cache() -> None:
    """Forget every cached lookup and the refusal-log throttle (tests only)."""
    global _magicdns_names, _magicdns_read_at, _magicdns_failed_at, _magicdns_inflight
    global _machine_names, _refusal_logged_at, _refusals_suppressed
    with _lock:
        _magicdns_names = None
        _magicdns_read_at = None
        _magicdns_failed_at = None
        _magicdns_inflight = False
        _machine_names = None
        _refusal_logged_at = None
        _refusals_suppressed = 0


def _gate_applies(path: str) -> bool:
    return (
        path.startswith(_HOST_GATED_PREFIXES)
        or path in _HOST_GATED_EXACT
        or path.startswith("/genesis/")
    )


def _normalise_name(name: str) -> str:
    return name.strip().rstrip(".").lower()


def _split_host(host: str) -> tuple[str, str] | None:
    """``"Box.:5000"`` -> ``("box", ":5000")``; None when the header is malformed.

    The name comes back lowercased, without brackets and without a trailing
    dot; the port part is kept verbatim so a name match can re-attach it.
    """
    match = _HOST_SHAPE.fullmatch(host.strip().lower())
    if match is None:
        return None
    name, port = match.group(1), match.group(2) or ""
    name = name[1:-1] if name.startswith("[") else name.rstrip(".")
    if not name or (port and not 0 < int(port[1:]) < 65536):
        return None
    return name, port


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _matchable(name: str) -> bool:
    """Whether werkzeug's matcher can compare against ``name`` without raising.

    ``host_is_trusted`` IDNA-encodes each trusted entry and catches only
    ``UnicodeEncodeError``; an empty or over-long label raises a plain
    ``UnicodeError`` instead, which would turn every request into a 500. Every
    list the gate builds goes through this, whatever its source.
    """
    try:
        name.lstrip(".").encode("idna")
    except UnicodeError:
        return False
    return bool(name.lstrip("."))


def _run_tailscale_status() -> str | None:
    """Raw ``tailscale status --self --json`` output, or None when unavailable."""
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--self", "--json"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_TAILSCALE_STATUS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _parse_magicdns(raw: str | None) -> tuple[str, ...] | None:
    """The full MagicDNS name and its short form, or None when unreadable."""
    if not raw:
        return None
    try:
        name = json.loads(raw)["Self"]["DNSName"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(name, str) or not _normalise_name(name):
        return None
    full = _normalise_name(name)
    names = tuple(n for n in (full, full.split(".")[0]) if _matchable(n))
    return names or None


def _magicdns() -> tuple[str, ...]:
    """This machine's Tailscale names, looked up lazily and cached.

    A successful read is re-read after ``_MAGICDNS_REFRESH_SECONDS``; a failed
    one is retried after ``_MAGICDNS_RETRY_SECONDS``. The subprocess runs outside
    the lock; callers that arrive while a lookup is in flight, or while a failure
    is being waited out, get the last names read (none, before the first
    success) rather than queueing.
    """
    global _magicdns_names, _magicdns_read_at, _magicdns_failed_at, _magicdns_inflight
    with _lock:
        now = time.monotonic()
        known = _magicdns_names or ()
        if _magicdns_read_at is not None and now - _magicdns_read_at < _MAGICDNS_REFRESH_SECONDS:
            return known
        if _magicdns_inflight:
            return known
        if _magicdns_failed_at is not None and now - _magicdns_failed_at < _MAGICDNS_RETRY_SECONDS:
            return known
        _magicdns_inflight = True
    names = None
    try:
        names = _parse_magicdns(_run_tailscale_status())
    finally:
        # One critical section clears the in-flight flag AND records the
        # outcome, so no caller sees "not in flight" with nothing recorded. An
        # unexpected exception is recorded as a failure, so it is retried after
        # the interval rather than on every request.
        with _lock:
            _magicdns_inflight = False
            if names is None:
                _magicdns_failed_at = time.monotonic()
            else:
                _magicdns_names = names
                _magicdns_read_at = time.monotonic()
                _magicdns_failed_at = None
            result = _magicdns_names or ()
    return result


def _machine_hostnames() -> tuple[str, ...]:
    """``localhost`` and this machine's hostnames, resolved once per process."""
    global _machine_names
    with _lock:
        if _machine_names is not None:
            return _machine_names
    names = {"localhost"}
    for raw in (socket.gethostname(), socket.getfqdn()):
        name = _normalise_name(raw or "")
        if name:
            names.add(name)
            names.add(name.split(".")[0] + ".local")
    with _lock:
        _machine_names = tuple(sorted(n for n in names if _matchable(n)))
        return _machine_names


def _operator_names() -> list[str]:
    """``GENESIS_DASHBOARD_ALLOWED_HOSTS``, read per request; invalid entries skipped.

    A leading dot is werkzeug's suffix form (``.example`` matches
    ``box.example``); a trailing dot is DNS's root and is dropped. An entry
    werkzeug cannot IDNA-encode would make its matcher raise on every request,
    so it is skipped and logged instead.
    """
    names = []
    for entry in os.environ.get("GENESIS_DASHBOARD_ALLOWED_HOSTS", "").split(","):
        entry = entry.strip().lower().rstrip(".")
        if not entry.lstrip("."):
            continue
        if not _matchable(entry):
            logger.warning("GENESIS_DASHBOARD_ALLOWED_HOSTS: ignoring invalid entry")
            continue
        names.append(entry)
    return names


def _log_refused(host: str) -> None:
    global _refusal_logged_at, _refusals_suppressed
    with _lock:
        now = time.monotonic()
        if _refusal_logged_at is not None and now - _refusal_logged_at < _REFUSAL_LOG_INTERVAL_S:
            _refusals_suppressed += 1
            return
        suppressed, _refusals_suppressed = _refusals_suppressed, 0
        _refusal_logged_at = now
    shown = host if len(host) <= 255 and host.isprintable() else f"<omitted: {len(host)} chars>"
    logger.warning(
        "Dashboard refused a request addressed to Host %r%s; allow a name with "
        "GENESIS_DASHBOARD_ALLOWED_HOSTS or open the dashboard by IP address",
        shown,
        f" ({suppressed} more refusals since the last notice)" if suppressed else "",
    )


def _refuse(raw: str, path: str):
    _log_refused(raw)
    if path.startswith(("/api/", "/ws/")):
        return jsonify({"error": _REFUSAL}), 403
    return Response(_REFUSAL + "\n", status=403, mimetype="text/plain")


def check_host_header():
    """App-level gate: content routes answer only to Hosts that name this machine.

    Accepted: no Host header at all (no browser omits it), any IP-literal Host,
    ``localhost``, the machine's hostnames and ``<hostname>.local``, its
    Tailscale MagicDNS name (full and short), names in
    ``GENESIS_DASHBOARD_ALLOWED_HOSTS``, and requests carrying the internal
    bearer. A malformed Host is refused. Out of scope: everything outside
    ``_gate_applies`` and the ``_OPEN_FROM_ANY_NETWORK`` pairs.
    ``GENESIS_DASHBOARD_HOSTNAME_CHECK=off`` disables the gate. Everything else
    gets 403.
    """
    if (
        os.environ.get("GENESIS_DASHBOARD_HOSTNAME_CHECK", "on").strip().lower()
        in _HOSTNAME_CHECK_OFF
    ):
        return None
    path = request.path
    if not _gate_applies(path):
        return None
    if (request.method, path) in _OPEN_FROM_ANY_NETWORK:
        return None
    # The raw header, never ``request.host``: that falls back to SERVER_NAME
    # when Host is absent, which would let a configured name decide.
    raw = request.headers.get("Host")
    if raw is None:
        return None
    if has_internal_bearer():
        return None
    parsed = _split_host(raw)
    if parsed is None:
        return _refuse(raw, path)
    name, port = parsed
    if _is_ip_literal(name) or name == "localhost":
        return None
    # werkzeug's matcher is case-sensitive and treats an EMPTY list as "allow
    # all": ``name`` is already lowercased and dot-stripped, and every list
    # passed here is non-empty (the machine list always holds "localhost").
    host = name + port
    if host_is_trusted(host, _machine_hostnames()):
        return None
    operator = _operator_names()
    if operator and host_is_trusted(host, operator):
        return None
    magic = _magicdns()
    if magic and host_is_trusted(host, magic):
        return None
    return _refuse(raw, path)
