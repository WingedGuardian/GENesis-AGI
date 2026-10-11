"""Scoped peer authentication; SAM claims are trusted only behind backend auth."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from flask import Flask, current_app, has_app_context, request

from genesis.dashboard.auth import (
    check_bearer_token,
    conflicts_with_cached_internal_token,
    get_dashboard_password,
    presented_bearer_is,
)
from genesis.env import bearer_matches, bearer_token
from genesis.peers.registry import PeerRegistry

BACKEND_TOKEN = "GENESIS_PEER_BACKEND_TOKEN"  # noqa: S105 — credential name only


class PeerRefusal(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class PeerIdentity:
    credential_name: str
    peer: dict | None


def credential_conflicts(names: tuple[str, ...], *, app: Flask | None = None) -> bool:
    """Reject scope collapse, including inactive peer and boot-loaded owner keys."""
    other_names = (
        {name for name in os.environ if name.startswith("GENESIS_") and name.endswith("_TOKEN")}
        | set(names)
        | {BACKEND_TOKEN}
    )
    configured = {name: bearer_token(name) for name in other_names}
    dashboard_password = get_dashboard_password() or ""
    if app is None and has_app_context():
        app = current_app
    # Match Flask's loaded active/fallback signing authority. Requests never
    # reopen the credential file, and a cleared file cannot change a live key.
    signing_keys = (
        (app.secret_key, *(app.config.get("SECRET_KEY_FALLBACKS") or ()))
        if app is not None and app.secret_key
        else ()
    )
    for name in names:
        value = configured[name]
        if value and (
            conflicts_with_cached_internal_token(value)
            or bearer_matches(value.encode("ascii"), dashboard_password)
            or any(
                hmac.compare_digest(
                    value.encode("ascii"), key if isinstance(key, bytes) else key.encode("utf-8")
                )
                for key in signing_keys
            )
            or any(
                key != name and other and hmac.compare_digest(value, other)
                for key, other in configured.items()
            )
        ):
            return True
    return False


async def authenticate(registry: PeerRegistry, *, allow_probe: bool = False) -> PeerIdentity:
    settings = await registry.settings()
    mode = settings["mode"]
    if mode == "disabled" or not settings.get("service_url"):
        raise PeerRefusal("not_configured", 503)
    rows = await registry.rows()
    all_names = tuple(row["token_name"] for row in rows if row["token_name"])
    accepted = (
        (BACKEND_TOKEN,)
        if mode == "sam"
        else tuple(row["token_name"] for row in rows if row["active"] and row["token_name"])
    )
    if not accepted or credential_conflicts((*all_names, BACKEND_TOKEN)):
        raise PeerRefusal("not_configured", 503)
    failure = check_bearer_token("peer API", accept=accepted)
    if failure:
        raise PeerRefusal("not_configured" if failure[1] == 503 else "unauthorized", failure[1])
    name = next(name for name in accepted if presented_bearer_is(name))
    if mode == "fallback":
        # SAM identity headers are intentionally ignored in this explicit mode.
        peer = next(row for row in rows if row["active"] and row["token_name"] == name)
    else:
        node = request.headers.get("X-Peer-Id")
        if not node and allow_probe:
            return PeerIdentity(name, None)
        peer = next(
            (
                row
                for row in rows
                if row["active"]
                and row["sam_realm"] == settings["sam_realm"]
                and row["sam_node"] == node
            ),
            None,
        )
        if peer is None or (
            peer["principal"] is not None
            and peer["principal"] != request.headers.get("X-Sam-Principal")
        ):
            raise PeerRefusal("unauthorized", 401)
    return PeerIdentity(name, peer)


async def configuration_warning(registry: PeerRegistry, *, app: Flask | None = None) -> str | None:
    """Boot diagnostics without credential values or request-side file access."""
    settings = await registry.settings()
    if settings["mode"] == "disabled":
        return "Peer API disabled: configure peers explicitly; scoped GENESIS_PEER_<ID>_TOKEN or GENESIS_PEER_BACKEND_TOKEN required."
    rows = await registry.rows()
    names = tuple(row["token_name"] for row in rows if row["token_name"])
    if credential_conflicts((*names, BACKEND_TOKEN), app=app):
        return "Peer API disabled: peer credential equals another surface credential; configure distinct scoped values."
    accepted = (
        (BACKEND_TOKEN,)
        if settings["mode"] == "sam"
        else tuple(row["token_name"] for row in rows if row["active"] and row["token_name"])
    )
    if not accepted or not any(bearer_token(name) for name in accepted):
        return "Peer API disabled: selected scoped peer credentials are unconfigured."
    return None
