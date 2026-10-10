"""Authenticated peer health on the existing Flask/runtime boundary."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from flask import Blueprint, current_app, g, jsonify, request

from genesis.dashboard._blueprint import _async_route
from genesis.peers.auth import PeerRefusal, authenticate
from genesis.peers.registry import PeerRegistry

logger = logging.getLogger(__name__)
agent_api_bp = Blueprint("agent_api", __name__)
ROOT = "/v1/agent/a2a"
MAX_BODY_BYTES = 256 * 1024


def _err(code: str, status: int):
    messages = {
        "not_configured": "Peer access is not configured",
        "not_ready": "Peer service is not ready",
        "unauthorized": "Unauthorized",
        "body_too_large": "Request body exceeds the limit",
        "not_found": "Not found",
    }
    return jsonify(error=messages[code], code=code), status


def _registry() -> PeerRegistry:
    return current_app.config.get("GENESIS_PEER_REGISTRY") or PeerRegistry()


@_async_route(timeout=15)
async def _authenticate_request():
    try:
        g.peer_identity = await authenticate(
            _registry(),
            allow_probe=request.path in {ROOT + "/health", ROOT + "/.well-known/agent-card.json"},
        )
    except PeerRefusal as exc:
        return _err(exc.code, exc.status)
    except Exception:
        # Input/SQLite errors can carry private text; do not persist them.
        logger.warning("Peer authentication unavailable")
        return _err("not_ready", 503)
    return None


@agent_api_bp.record_once
def _install_gate(state):
    @state.app.before_request
    def peer_gate():
        if request.path != ROOT and not request.path.startswith(ROOT + "/"):
            return None
        loop = current_app.config.get("GENESIS_EVENT_LOOP")
        if loop is None or not loop.is_running():
            return _err("not_ready", 503)
        refusal = _authenticate_request()
        if refusal is not None:
            return refusal
        # No current route consumes a body, so the stream is never read: a
        # read has no deadline, and a slow or unterminated body would hold this
        # worker. Only a declared oversize length is refused, from the header.
        # The transport that needs a body must add a bounded, deadlined read.
        if (request.content_length or 0) > MAX_BODY_BYTES:
            return _err("body_too_large", 413)
        return None

    @state.app.after_request
    def audit(response):
        if request.path == ROOT or request.path.startswith(ROOT + "/"):
            if response.status_code == 503 and not (response.get_json(silent=True) or {}).get(
                "code"
            ):
                response = current_app.make_response(_err("not_ready", 503))
            identity = getattr(g, "peer_identity", None)
            logger.info(
                "peer_request timestamp=%s credential=%s peer=%s endpoint=%s task_id=%s outcome=%s",
                datetime.now(UTC).isoformat(),
                identity.credential_name if identity else "unverified",
                identity.peer["peer_id"] if identity and identity.peer else "unverified",
                request.endpoint or "unmatched",
                None,
                response.status_code,
            )
        return response


@agent_api_bp.route(ROOT + "/.well-known/agent-card.json")
@_async_route(timeout=15)
async def agent_card():
    # An empty skills list does not make an unavailable HTTP+JSON interface
    # interoperable. The execution slice must install the binding before
    # publishing a card, including during startup or recovery.
    return _err("not_ready", 503)


@agent_api_bp.route(ROOT + "/health")
def agent_health():
    return jsonify(runtime_ready=True, task_service_ready=False, active_tasks=0, queue_depth=0)


@agent_api_bp.route(ROOT, defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@agent_api_bp.route(ROOT + "/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def unavailable_path(path):
    return _err("not_found", 404)
