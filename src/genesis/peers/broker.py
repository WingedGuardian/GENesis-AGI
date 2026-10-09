"""Private Unix capability broker; only a ready coordinator may install it."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web
from pydantic import BaseModel, ConfigDict

from genesis.peers.protocol import _finite_float, _nonfinite, _unique_object
from genesis.peers.registry import capability as validate_capability
from genesis.peers.resources import PublishedResources, resource_id
from genesis.peers.runner import PeerRunState, _settle
from genesis.peers.session import PeerSessionBinding
from genesis.peers.transport import PrivateAppRunner
from genesis.security.sanitizer import ContentSanitizer, ContentSource

logger = logging.getLogger(__name__)
CAP = 256 * 1024


class BrokerRefusal(Exception):
    def __init__(self, code="permission_denied", status=403):
        self.code, self.status = code, status
        super().__init__(code)


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResourceArguments(EmptyArguments):
    resource_id: str


@dataclass(repr=False)
class _Lease:
    binding: PeerSessionBinding
    active: bool = True
    operations: set = field(default_factory=set)


class PeerBroker:
    def __init__(self, registry, authorize_operation):
        if not callable(authorize_operation):
            raise ValueError("Peer operation authorizer required")
        self.registry = registry
        self.resources = PublishedResources(registry)
        self.authorize_operation = authorize_operation
        self._leases = {}
        self._revoked_segments = set()
        self._runner = None
        self._socket = None
        self._socket_identity = None
        self._operations = {
            "task_context": (EmptyArguments, self._context, "conversation"),
            "resources_list": (EmptyArguments, self._resources, "conversation"),
            "resource_read": (ResourceArguments, self._resource, "resource"),
        }

    def register_operation(self, name, capability, arguments, handler):
        """Trusted startup extension, never callable through the facade."""
        validate_capability(capability)
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or name in self._operations
            or not issubclass(arguments, EmptyArguments)
            or not callable(handler)
        ):
            raise ValueError("Invalid peer broker operation")
        self._operations[name] = (arguments, handler, capability)

    async def start(self, directory: Path):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise ValueError("Private broker directory required")
        socket = directory / "broker.sock"
        if self._runner is not None or socket.exists() or socket.is_symlink():
            raise ValueError("Broker socket requires reconciliation")

        @web.middleware
        async def audit(request, handler):
            outcome = 500
            try:
                response = await handler(request)
                outcome = response.status
                return response
            finally:
                lease = request.get("verified_lease")
                logger.info(
                    "peer_broker_audit timestamp=%s credential=segment_lease endpoint=broker/call task=%s segment=%s operation=%s outcome=%d",
                    datetime.now(UTC).isoformat(),
                    lease.binding.task_id if lease else "-",
                    lease.binding.segment.segment_id if lease else "-",
                    request.get("verified_operation", "unknown"),
                    outcome,
                )

        app = web.Application(client_max_size=CAP, middlewares=[audit])
        app.router.add_post("/call", self._receive)
        runner = PrivateAppRunner(app, access_log=None, handler_cancellation=False)
        await runner.setup()
        try:
            await web.UnixSite(runner, socket).start()
            socket.chmod(0o600)
            info = socket.lstat()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner, self._socket = runner, socket
        self._socket_identity = (info.st_dev, info.st_ino)

    async def issue(self, binding: PeerSessionBinding, lease_path: Path):
        if self._runner is None or not isinstance(binding, PeerSessionBinding):
            raise ValueError("Peer broker is not ready")
        lease = _Lease(binding)
        await self._current(lease)
        if binding.segment.segment_id in self._revoked_segments:
            raise BrokerRefusal("lease_expired")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        path = Path(lease_path)
        info = path.parent.lstat()
        if (
            not path.is_absolute()
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise ValueError("Private lease directory required")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as target:
            json.dump({"socket_path": str(self._socket), "lease": token}, target)
            target.flush()
            os.fsync(target.fileno())
        self._leases[digest] = lease

    async def _current(self, lease, *, executing=False):
        binding = lease.binding
        if not lease.active or binding.segment.deadline_at <= time.time():
            raise BrokerRefusal("lease_expired")
        async with self.registry.connection() as db:
            row = await (
                await db.execute(
                    "SELECT t.*, (SELECT json_group_object(capability,decision) "
                    "FROM peer_grants WHERE peer_id=t.peer_id) AS _current_grants "
                    "FROM peer_tasks t JOIN peers p ON p.peer_id=t.peer_id "
                    "WHERE t.id=? AND t.generation=? AND t.cancel_requested=0 "
                    "AND t.state IN ('submitted','working') AND p.active=1 AND p.epoch=t.epoch",
                    (binding.task_id, binding.generation),
                )
            ).fetchone()
            if row is None or datetime.fromisoformat(row["expires_at"]).timestamp() <= time.time():
                raise BrokerRefusal("lease_expired")
            if executing and (
                row["state"] != "working" or row["work_elapsed_s"] >= row["work_limit_s"]
            ):
                raise BrokerRefusal("lease_expired")
            # One statement binds task, relationship and grant decisions to
            # the same SQLite snapshot; separate reads could mix revisions.
            record = dict(row)
            current = json.loads(record.pop("_current_grants"))
        if (
            not lease.active
            or binding.segment.deadline_at <= time.time()
            or datetime.fromisoformat(row["expires_at"]).timestamp() <= time.time()
        ):
            raise BrokerRefusal("lease_expired")
        snapshot = json.loads(row["grants_json"])
        decisions = {
            key: ("ask" if "ask" in (value, current.get(key)) else "allow")
            for key, value in snapshot.items()
            if value in ("allow", "ask") and current.get(key) in ("allow", "ask")
        }
        if "conversation" not in decisions:
            raise BrokerRefusal()
        return record, decisions

    async def _receive(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or len(header) > 128 or not header.isascii():
            return web.json_response({"code": "unauthorized"}, status=401)
        digest = hashlib.sha256(header[7:].encode()).hexdigest()
        lease = self._leases.get(digest)
        if lease is None or not lease.active:
            return web.json_response({"code": "unauthorized"}, status=401)
        request["verified_lease"] = lease
        try:
            if request.headers.get("Content-Encoding", "identity") != "identity":
                raise BrokerRefusal("validation_error", 400)
            body = bytearray()
            while len(body) <= CAP:
                chunk = await request.content.read(CAP + 1 - len(body))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > CAP:
                raise BrokerRefusal("body_too_large", 413)
            data = json.loads(
                body,
                object_pairs_hook=_unique_object,
                parse_constant=_nonfinite,
                parse_float=_finite_float,
            )
            if not isinstance(data, dict) or data.keys() != {"operation", "arguments"}:
                raise ValueError
            name = data["operation"]
            if not isinstance(name, str) or name not in self._operations:
                raise ValueError
            request["verified_operation"] = name
            schema, handler, capability = self._operations[name]
            arguments = schema.model_validate(data["arguments"])
            if f"mcp__genesis_peer__{name}" not in lease.binding.segment.tools:
                raise BrokerRefusal()
            task = asyncio.create_task(self._operate(lease, name, arguments, handler, capability))
            lease.operations.add(task)
            task.add_done_callback(lease.operations.discard)
            return web.json_response(await task)
        except BrokerRefusal as exc:
            return web.json_response({"code": exc.code}, status=exc.status)
        except asyncio.CancelledError:
            return web.json_response({"code": "lease_expired"}, status=409)
        except Exception:
            return web.json_response({"code": "operation_refused"}, status=400)

    async def _operate(self, lease, name, arguments, handler, capability):
        row, decisions = await self._current(lease, executing=True)
        if capability == "resource":
            capability = f"resource:{resource_id(arguments.resource_id)}"
        if capability not in decisions:
            raise BrokerRefusal()
        resource = (
            await self.resources.get(arguments.resource_id) if name == "resource_read" else None
        )
        if name == "resource_read" and resource is None:
            raise BrokerRefusal("not_found", 404)
        digest = hashlib.sha256(
            json.dumps(
                {
                    "operation": name,
                    "arguments": arguments.model_dump(),
                    "resource_digest": resource["sha256"] if resource else None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if (
            await self.authorize_operation(lease.binding, capability, digest, decisions[capability])
            is not None
        ):
            raise BrokerRefusal()
        row, latest = await self._current(lease, executing=True)
        if latest.get(capability) != decisions[capability]:
            raise BrokerRefusal()
        result = await handler(row, latest, arguments)
        _, final = await self._current(lease, executing=True)
        if final.get(capability) != decisions[capability]:
            raise BrokerRefusal()
        if name == "resources_list":
            result["resources"] = [
                item
                for item in result["resources"]
                if final.get(f"resource:{item['id']}") == "allow"
                and await self.resources.get(item["id"]) is not None
            ]
        if resource is not None:
            check = await self.resources.get(resource["id"])
            if check is None or check["sha256"] != resource["sha256"]:
                raise BrokerRefusal("not_found", 404)
        _, disclosure = await self._current(lease, executing=True)
        if disclosure.get(capability) != decisions[capability]:
            raise BrokerRefusal()
        if name == "resources_list":
            result["resources"] = [
                item
                for item in result["resources"]
                if disclosure.get(f"resource:{item['id']}") == "allow"
            ]
        return result

    async def _context(self, row, decisions, arguments):
        text = json.dumps(json.loads(row["message_json"]), ensure_ascii=False)
        wrapped = ContentSanitizer().wrap_content(text, ContentSource.UNKNOWN)
        return {"task_id": row["id"], "context": wrapped}

    async def _resources(self, row, decisions, arguments):
        result = []
        for key, decision in decisions.items():
            if key.startswith("resource:") and decision == "allow":
                resource = await self.resources.get(key[9:])
                if resource is not None:
                    result.append({key: resource[key] for key in ("id", "title", "sha256")})
        return {"resources": result}

    async def _resource(self, row, decisions, arguments):
        resource = await self.resources.get(arguments.resource_id)
        if resource is None:
            raise BrokerRefusal("not_found", 404)
        return {key: resource[key] for key in ("id", "title", "content", "sha256")}

    async def drain(self, binding):
        self._revoked_segments.add(binding.segment.segment_id)
        leases = [
            lease
            for lease in self._leases.values()
            if lease.binding.segment.segment_id == binding.segment.segment_id
        ]
        operations = set()
        for lease in leases:
            lease.active = False
            operations.update(lease.operations)
        for task in operations:
            task.cancel()
        _, interrupted = await _settle(
            asyncio.gather(*operations, return_exceptions=True), PeerRunState(binding)
        )
        for digest, lease in list(self._leases.items()):
            if lease in leases:
                del self._leases[digest]
        if interrupted:
            raise asyncio.CancelledError

    async def close(self):
        async def closing():
            bindings = [lease.binding for lease in self._leases.values()]
            await asyncio.gather(*(self.drain(binding) for binding in bindings))
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
            if self._socket is not None:
                try:
                    info = self._socket.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (
                        not stat.S_ISSOCK(info.st_mode)
                        or info.st_uid != os.getuid()
                        or (info.st_dev, info.st_ino) != self._socket_identity
                    ):
                        raise ValueError("Broker socket requires reconciliation")
                    self._socket.unlink()
                self._socket = self._socket_identity = None

        _, interrupted = await _settle(closing())
        if interrupted:
            raise asyncio.CancelledError
