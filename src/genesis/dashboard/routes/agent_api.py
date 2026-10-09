"""Authenticated peer discovery on the existing Flask/runtime boundary."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from flask import Blueprint, Response, current_app, g, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

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
        "validation_error": "Invalid request body",
        "state_conflict": "Requested state conflicts with existing work",
        "rate_limited": "Peer admission limit reached",
        "spawn_timeout": "Accepted task is still in progress",
        "result_not_ready": "Result is not available",
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
        # Read on the Flask worker, not the shared runtime event loop.
        # Authorization precedes parsing, including absent Content-Length.
        try:
            body = request.stream.read(MAX_BODY_BYTES + 1)
        except RequestEntityTooLarge:
            return _err("body_too_large", 413)
        except BadRequest:
            return _err("validation_error", 400)
        if len(body) > MAX_BODY_BYTES:
            return _err("body_too_large", 413)
        g.peer_body = body
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
                getattr(g, "peer_task_id", None),
                response.status_code,
            )
        return response


@agent_api_bp.route(ROOT + "/.well-known/agent-card.json")
@_async_route(timeout=15)
async def agent_card():
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentSkill,
        HTTPAuthSecurityScheme,
        SecurityRequirement,
        SecurityScheme,
    )
    from google.protobuf.json_format import MessageToDict

    settings = await _registry().settings()
    card = AgentCard(
        name="Genesis",
        description="Independent agent collaboration; capabilities require local authorization.",
        version="1",
        supported_interfaces=[
            AgentInterface(
                url=settings["service_url"], protocol_binding="HTTP+JSON", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(),
        security_schemes={
            "peerBearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer")
            )
        },
        security_requirements=[SecurityRequirement(schemes={"peerBearer": {"list": []}})],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    service = _tasks()
    if (
        service is not None
        and callable(getattr(service, "execution_allowed", None))
        and await service.execution_allowed()
    ):
        card.skills.append(
            AgentSkill(
                id="conversation",
                name="Peer collaboration",
                description="Submit an authorized goal; Genesis selects its own contained execution.",
                tags=["conversation"],
            )
        )
    payload = MessageToDict(card)
    # Required repeated fields must survive protobuf's default omission.
    payload.setdefault("skills", [])
    return jsonify(payload)


@agent_api_bp.route(ROOT + "/health")
@_async_route(timeout=15)
async def agent_health():
    service = _tasks()
    if service is not None and callable(getattr(service, "health", None)):
        try:
            return jsonify(await service.health())
        except Exception:
            logger.error("Peer health counts unavailable")
            return jsonify(
                runtime_ready=True, task_service_ready=False, active_tasks=None, queue_depth=None
            )
    return jsonify(runtime_ready=True, task_service_ready=False, active_tasks=0, queue_depth=0)


@agent_api_bp.route(ROOT + "/approvals")
@_async_route(timeout=15)
async def agent_approvals():
    service = current_app.config.get("GENESIS_PEER_APPROVALS")
    if service is None:
        return _err("not_ready", 503)
    return jsonify(approvals=await service.pending(g.peer_identity.peer))


def _tasks():
    # The serving adapter binds the recovered runtime owner; its admission
    # gate remains authoritative after these Flask references are installed.
    return current_app.config.get("GENESIS_PEER_TASKS")


async def _project(rows):
    service = current_app.config.get("GENESIS_PEER_RESULTS")
    if service is None:
        return rows  # Inactive foundation has status only, never a raw-result fallback.
    return await service.project(g.peer_identity.peer, rows)


@agent_api_bp.route(ROOT + "/tasks/<task_id>/artifacts/<artifact_id>")
@_async_route(timeout=15)
async def get_artifact(task_id, artifact_id):
    from genesis.peers.tasks import TaskRefusal

    g.peer_task_id = task_id
    service = current_app.config.get("GENESIS_PEER_RESULTS")
    if service is None:
        return _err("not_ready", 503)
    try:
        content = await service.fetch(g.peer_identity.peer, task_id, artifact_id)
        return Response(
            content,
            content_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="result.md"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
    except TaskRefusal as error:
        return _err(error.code, error.status)
    except Exception:
        logger.warning("Peer result unavailable")
        return _err("result_not_ready", 409)


def _protocol_error(error):
    from a2a.utils.error_handlers import build_rest_error_payload

    payload = build_rest_error_payload(error)
    return jsonify(payload), payload["error"]["code"]


def _protocol_response(message):
    from a2a.utils.constants import A2A_JSON_MEDIA_TYPE
    from google.protobuf.json_format import MessageToDict

    response = jsonify(MessageToDict(message))
    response.mimetype = A2A_JSON_MEDIA_TYPE
    return response


@agent_api_bp.route(ROOT + "/message:send", methods=["POST"])
@_async_route(timeout=40)
async def send_message():
    from a2a.types import SendMessageResponse
    from a2a.utils.errors import A2AError

    from genesis.peers.protocol import parse_send, task_view, validate_version
    from genesis.peers.tasks import TERMINAL, TaskRefusal

    try:
        validate_version(request.headers.get("A2A-Version"))
        message, immediate = parse_send(g.peer_body)
        service = _tasks()
        if service is None:
            return _err("not_ready", 503)
        row = await service.admit(g.peer_identity.peer, message)
        g.peer_task_id = row["id"]
        if not immediate and row["state"] not in (*TERMINAL, "input_required"):
            try:
                async with asyncio.timeout(30):
                    while row["state"] not in (*TERMINAL, "input_required"):
                        await asyncio.sleep(0.1)
                        row = await service.owned(g.peer_identity.peer, row["id"])
            except TimeoutError:
                payload = {
                    "error": "Accepted task is still in progress",
                    "code": "spawn_timeout",
                    "task_id": row["id"],
                }
                return jsonify(payload), 504
        row = (await _project([row]))[0]
        return _protocol_response(SendMessageResponse(task=task_view(row)))
    except A2AError as error:
        return _protocol_error(error)
    except TaskRefusal as error:
        return _err(error.code, error.status)


@agent_api_bp.route(ROOT + "/tasks/<task_id>")
@_async_route(timeout=15)
async def get_task(task_id):
    from a2a.utils.errors import A2AError, TaskNotFoundError

    from genesis.peers.protocol import task_view, validate_version
    from genesis.peers.tasks import TaskRefusal

    try:
        validate_version(request.headers.get("A2A-Version"))
        service = _tasks()
        if service is None:
            return _err("not_ready", 503)
        row = await service.owned(g.peer_identity.peer, task_id)
        g.peer_task_id = row["id"]
        row = (await _project([row]))[0]
        return _protocol_response(task_view(row))
    except A2AError as error:
        return _protocol_error(error)
    except TaskRefusal:
        return _protocol_error(TaskNotFoundError())


@agent_api_bp.route(ROOT + "/tasks")
@_async_route(timeout=15)
async def list_tasks():
    from a2a.server.routes.common import serialize_list_tasks_response
    from a2a.types import ListTasksRequest, ListTasksResponse
    from a2a.utils.constants import A2A_JSON_MEDIA_TYPE
    from a2a.utils.errors import A2AError, InvalidParamsError, UnsupportedOperationError
    from a2a.utils.proto_utils import parse_params

    from genesis.peers.protocol import task_view, validate_version
    from genesis.peers.tasks import TaskRefusal

    try:
        validate_version(request.headers.get("A2A-Version"))
        allowed = {"pageSize", "pageToken"}
        if set(request.args) - allowed or any(
            len(request.args.getlist(key)) != 1 for key in request.args
        ):
            raise UnsupportedOperationError("Requested task filter is unavailable")
        params = ListTasksRequest()
        try:
            parse_params(request.args, params)
        except Exception:
            raise InvalidParamsError("Invalid task pagination") from None
        service = _tasks()
        if service is None:
            return _err("not_ready", 503)
        size = params.page_size if "pageSize" in request.args else 20
        rows, cursor, total = await service.page(
            g.peer_identity.peer, page_size=size, page_token=params.page_token
        )
        rows = await _project(rows)
        payload = serialize_list_tasks_response(
            ListTasksResponse(
                tasks=[task_view(row) for row in rows],
                next_page_token=cursor,
                page_size=size,
                total_size=total,
            ),
            include_artifacts=False,
        )
        response = jsonify(payload)
        response.mimetype = A2A_JSON_MEDIA_TYPE
        return response
    except A2AError as error:
        return _protocol_error(error)
    except TaskRefusal as error:
        return _err(error.code, error.status)


@agent_api_bp.route(ROOT + "/tasks/<task_id>:cancel", methods=["POST"])
@_async_route(timeout=15)
async def cancel_task(task_id):
    from a2a.utils.errors import (
        A2AError,
        TaskNotCancelableError,
        TaskNotFoundError,
    )

    from genesis.peers.protocol import parse_cancel, task_view, validate_version
    from genesis.peers.tasks import TaskRefusal

    try:
        validate_version(request.headers.get("A2A-Version"))
        parse_cancel(g.peer_body, task_id)
        service = _tasks()
        if service is None:
            return _err("not_ready", 503)
        row = await service.cancel(g.peer_identity.peer, task_id)
        g.peer_task_id = row["id"]
        row = (await _project([row]))[0]
        return _protocol_response(task_view(row))
    except A2AError as error:
        return _protocol_error(error)
    except TaskRefusal as error:
        if error.code == "state_conflict":
            return _protocol_error(TaskNotCancelableError())
        return _protocol_error(TaskNotFoundError())


@agent_api_bp.route(ROOT, defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@agent_api_bp.route(ROOT + "/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def unavailable_path(path):
    if path == "message:stream" or ":subscribe" in path or "pushNotificationConfigs" in path:
        from a2a.utils.errors import UnsupportedOperationError

        return _protocol_error(UnsupportedOperationError())
    return _err("not_found", 404)
