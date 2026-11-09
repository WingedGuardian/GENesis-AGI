"""Asynchronous same-owner A2A on the existing Flask/runtime boundary."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import wraps

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
)
from a2a.utils.error_handlers import build_rest_error_payload
from a2a.utils.errors import A2AError, InvalidParamsError, TaskNotFoundError
from flask import Blueprint, current_app, g, jsonify, request
from google.protobuf.json_format import MessageToDict
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, RequestTimeout

from genesis.dashboard._blueprint import _async_route
from genesis.hosting.request_input import INPUT_KEY, MAX_BODY_BYTES
from genesis.peers.auth import PeerRefusal, authenticate
from genesis.peers.conversation import ConversationRefusal, ConversationService, receipt_id
from genesis.peers.protocol import parse_cancel, parse_send, validate_version
from genesis.peers.registry import PeerRegistry
from genesis.peers.results import full_result, project

logger = logging.getLogger(__name__)
agent_api_bp = Blueprint("agent_api", __name__)
ROOT = "/v1/agent/a2a"


def _err(code, status, task_id=None):
    states = {
        400: "INVALID_ARGUMENT",
        401: "UNAUTHENTICATED",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        408: "DEADLINE_EXCEEDED",
        409: "FAILED_PRECONDITION",
        413: "RESOURCE_EXHAUSTED",
        429: "RESOURCE_EXHAUSTED",
        503: "UNAVAILABLE",
    }
    extra = {} if task_id is None else {"taskId": task_id}
    response = jsonify(
        error={
            "code": status,
            "status": states.get(status, "INTERNAL"),
            "message": "Peer request refused",
        },
        code=code,
        **extra,
    )
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response, status


def _registry():
    return current_app.config.get("GENESIS_PEER_REGISTRY") or PeerRegistry()


def _runtime():
    from genesis.runtime import GenesisRuntime

    return current_app.config.get("GENESIS_PEER_RUNTIME") or GenesisRuntime.peek()


def _service(*, require_runner=True):
    runtime = _runtime()
    if runtime is None or runtime._db is None:
        raise ConversationRefusal("not_ready", 503)
    service = current_app.config.get("GENESIS_CONVERSATION_SERVICE")
    if service is None or service.runtime is not runtime:
        service = ConversationService(_registry(), runtime)
        current_app.config["GENESIS_CONVERSATION_SERVICE"] = service
    if require_runner:
        _ = service.runner
    return service


def _handled(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        try:
            if request.path != ROOT + "/.well-known/agent-card.json":
                validate_version(request.headers.get("A2A-Version"))
            return await fn(*args, **kwargs)
        except A2AError as exc:
            payload = build_rest_error_payload(exc)
            return jsonify(payload), payload["error"]["code"]
        except ConversationRefusal as exc:
            if exc.code == "not_found":
                return jsonify(
                    build_rest_error_payload(TaskNotFoundError("Task is unavailable"))
                ), 404
            return _err(exc.code, exc.status)
        except Exception:
            logger.warning("Peer operation unavailable")
            return _err("not_ready", 503, _send_task_id())

    return _async_route(timeout=15)(wrapped)


def _send_task_id():
    """The receipt id a send computed, for its 503 body; None elsewhere.

    The admission is shielded, so it can outlive a send's timeout or a failure
    after it returned. Naming the receipt lets the caller poll it or retry with
    the same messageId instead of starting the work again under a new one. It
    may still be refused, so a poll can 404.
    """
    if request.endpoint != "agent_api.send_message":
        return None
    return getattr(g, "peer_task_id", None)


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
        if (request.content_length or 0) > MAX_BODY_BYTES:
            return _err("body_too_large", 413)
        consumes = request.method == "POST" and (
            request.path == ROOT + "/message:send"
            or (request.path.startswith(ROOT + "/tasks/") and request.path.endswith(":cancel"))
            or (request.path.startswith(ROOT + "/outbox/") and request.path.endswith(":ack"))
        )
        if consumes:
            capability = request.environ.get(INPUT_KEY)
            if capability is None:
                return _err("not_ready", 503)
            try:
                g.peer_body = capability.read()
            except RequestEntityTooLarge:
                return _err("body_too_large", 413)
            except RequestTimeout:
                return _err("body_timeout", 408)
            except (BadRequest, OSError, ValueError):
                return _err("validation_error", 400)
        return None

    @state.app.after_request
    def audit(response):
        if request.path == ROOT or request.path.startswith(ROOT + "/"):
            if response.status_code == 503 and not (response.get_json(silent=True) or {}).get(
                "code"
            ):
                response = current_app.make_response(_err("not_ready", 503, _send_task_id()))
            if response.is_json:
                response.mimetype = "application/a2a+json"
            response.headers["A2A-Version"] = "1.0"
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


async def _view(service, row):
    g.peer_task_id = row["id"]
    return project(row)


@agent_api_bp.route(ROOT + "/message:send", methods=["POST"])
@_handled
async def send_message():
    service = _service()
    incoming = parse_send(g.peer_body)
    g.peer_task_id = receipt_id(g.peer_identity, incoming)
    row = await service.send(g.peer_identity, incoming)
    return jsonify(task=await _view(service, row))


@agent_api_bp.route(ROOT + "/tasks/<task_id>")
@_handled
async def get_task(task_id):
    service = _service()
    if set(request.args) - {"historyLength"}:
        raise InvalidParamsError("Unsupported task options")
    if "historyLength" in request.args:
        _integer("historyLength", 0, 0, 100)
    return jsonify(await _view(service, await service.get(g.peer_identity, task_id)))


def _integer(name, default, low, high):
    try:
        value = int(request.args.get(name, default))
        if len(request.args.getlist(name)) > 1 or not low <= value <= high:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise InvalidParamsError("Invalid pagination") from None


@agent_api_bp.route(ROOT + "/tasks")
@_handled
async def list_tasks():
    if set(request.args) - {"pageSize", "pageToken", "contextId"} or any(
        len(v) > 1 for _, v in request.args.lists()
    ):
        raise InvalidParamsError("Unsupported list options")
    size = _integer("pageSize", 20, 1, 100)
    service = _service()
    rows, token, total = await service.list(
        g.peer_identity, size, request.args.get("pageToken"), request.args.get("contextId")
    )
    return jsonify(
        tasks=[await _view(service, row) for row in rows],
        nextPageToken=token,
        pageSize=size,
        totalSize=total,
    )


@agent_api_bp.route(ROOT + "/tasks/<task_id>:cancel", methods=["POST"])
@_handled
async def cancel_task(task_id):
    parse_cancel(g.peer_body, task_id)
    service = _service()
    return jsonify(await _view(service, await service.cancel(g.peer_identity, task_id)))


@agent_api_bp.route(ROOT + "/tasks/<task_id>/result")
@_handled
async def get_result(task_id):
    row = await _service().get(g.peer_identity, task_id)
    g.peer_task_id = row["id"]
    return current_app.response_class(full_result(row), mimetype="text/plain")


@agent_api_bp.route(ROOT + "/approvals")
@_handled
async def approvals():
    if set(request.args) - {"pageSize", "pageToken"} or any(
        len(v) > 1 for _, v in request.args.lists()
    ):
        raise InvalidParamsError("Unsupported list options")
    size = _integer("pageSize", 20, 1, 100)
    page, token = await _service().approvals(g.peer_identity, size, request.args.get("pageToken"))
    return jsonify(approvals=page, nextPageToken=token, pageSize=size)


@agent_api_bp.route(ROOT + "/.well-known/agent-card.json")
@_handled
async def agent_card():
    service = _service()
    async with service.registry.connection() as db:
        await service._same_database(db)
    settings = await service.registry.settings()
    card = AgentCard(
        name="Genesis",
        description="Same-owner asynchronous conversations with Genesis",
        version="1",
        supported_interfaces=[
            AgentInterface(
                url=settings["service_url"], protocol_binding="HTTP+JSON", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(),
        security_schemes={
            "peerBearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")
            )
        },
        security_requirements=[SecurityRequirement(schemes={"peerBearer": {}})],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="conversation",
                name="Conversation",
                description="Reason, clarify or refuse with permitted native tools. Require returnImmediately=true.",
                tags=["conversation"],
            )
        ],
    )
    return jsonify(MessageToDict(card))


@agent_api_bp.route(ROOT + "/health")
@_async_route(timeout=15)
async def agent_health():
    from genesis.peers.readiness import evaluate

    runtime = _runtime()
    runner = getattr(runtime, "_direct_session_runner", None)
    readiness = await evaluate(current_app)
    if readiness["status"] != "healthy":
        # Keep the readiness body through the existing 503 normalization gate.
        readiness["code"] = "not_ready"
    return jsonify(
        **readiness,
        active_tasks=len(getattr(runner, "_managed_cancel", {})),
        queue_depth=0,
    ), 200 if readiness["status"] == "healthy" else 503


@agent_api_bp.route(ROOT, defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@agent_api_bp.route(ROOT + "/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def unavailable_path(path):
    return _err("not_found", 404)


async def _outbox():
    from genesis.peers.outbox import Outbox

    service = _service(require_runner=False)
    async with service.registry.connection() as db:
        await service._same_database(db)
    return Outbox(service.registry)


@agent_api_bp.route(ROOT + "/outbox", methods=["GET"])
@_handled
async def outbox_list():
    try:
        if set(request.args) - {"pageSize", "pageToken"} or any(
            len(request.args.getlist(k)) != 1 for k in request.args
        ):
            raise ValueError
        size = int(request.args.get("pageSize", "20"))
    except ValueError:
        raise InvalidParamsError("Invalid outbox page") from None
    messages, token = await (await _outbox()).poll(
        g.peer_identity,
        size,
        request.args.get("pageToken"),
    )
    return jsonify(messages=messages, nextPageToken=token)


@agent_api_bp.route(ROOT + "/outbox/<message_id>:ack", methods=["POST"])
@_handled
async def outbox_ack(message_id):
    import json

    from genesis.peers.conversation import uuid_id
    from genesis.peers.protocol import _unique_object

    g.peer_task_id = uuid_id(message_id)
    try:
        body = json.loads(g.peer_body or b"{}", object_pairs_hook=_unique_object)
        if body != {}:
            raise ValueError
    except (ValueError, TypeError):
        raise InvalidParamsError("Invalid collection acknowledgement") from None
    await (await _outbox()).ack(g.peer_identity, message_id)
    return jsonify(messageId=message_id, acknowledged=True)
