"""Closed builtin receipt provenance; model-written claims never grant disclosure."""

import hashlib
import json

from genesis.peers.digests import operation_digest
from genesis.peers.lifecycle_state import decision, disclosure_authorized
from genesis.peers.operation_state import operation_authorized
from genesis.peers.protocol import _finite_float, _nonfinite, _unique_object
from genesis.peers.resources import resource_id
from genesis.peers.tasks import TaskRefusal
from genesis.security.output_scanner import scan_outbound


async def _resource(db, task, value, *, listed=False):
    keys = {"id", "title", "sha256"} if listed else {"id", "title", "sha256", "content"}
    if (
        not isinstance(value, dict)
        or value.keys() != keys
        or any(not isinstance(item, str) for item in value.values())
    ):
        raise TaskRefusal("result_not_ready", 409)
    try:
        identifier = resource_id(value["id"])
    except ValueError:
        raise TaskRefusal("result_not_ready", 409) from None
    resource = await (
        await db.execute("SELECT * FROM peer_resources WHERE id=? AND active=1", (identifier,))
    ).fetchone()
    if (
        resource is None
        or any(resource[key] != value[key] for key in keys)
        or hashlib.sha256(resource["content"].encode()).hexdigest() != resource["sha256"]
        or not scan_outbound(resource["title"] + "\n" + resource["content"]).safe
        or (listed and decision(task, "resource:" + identifier) != "allow")
    ):
        raise TaskRefusal("unauthorized", 401)


async def result_authorized(db, task, *, research=None):
    """Re-derive every completed task receipt; never trust final-segment telemetry."""
    await disclosure_authorized(db, task)
    receipts = await (
        await db.execute(
            "SELECT * FROM peer_operations WHERE task_id=? AND status='completed'", (task["id"],)
        )
    ).fetchall()
    for receipt in receipts:
        if not receipt["immutable_read"]:
            # A later trusted capability must install its own closed provenance policy.
            raise TaskRefusal("result_not_ready", 409)
        try:
            result = json.loads(
                receipt["result_json"],
                object_pairs_hook=_unique_object,
                parse_constant=_nonfinite,
                parse_float=_finite_float,
            )
        except (ValueError, TypeError):
            raise TaskRefusal("result_not_ready", 409) from None
        await operation_authorized(db, task, receipt["capability"], receipt["operation_digest"])
        if receipt["capability"] == "conversation":
            if receipt["operation_digest"] == operation_digest("task_context", {}):
                if (
                    not isinstance(result, dict)
                    or result.keys() != {"task_id", "context"}
                    or (result["task_id"] != task["id"] or not isinstance(result["context"], str))
                ):
                    raise TaskRefusal("result_not_ready", 409)
            elif receipt["operation_digest"] == operation_digest("resources_list", {}):
                if (
                    not isinstance(result, dict)
                    or result.keys() != {"resources"}
                    or not isinstance(result["resources"], list)
                ):
                    raise TaskRefusal("result_not_ready", 409)
                for resource in result["resources"]:
                    await _resource(db, task, resource, listed=True)
            else:
                raise TaskRefusal("result_not_ready", 409)
        elif receipt["capability"].startswith("resource:"):
            await _resource(db, task, result)
            if receipt["capability"] != "resource:" + result["id"] or receipt[
                "operation_digest"
            ] != operation_digest("resource_read", {"resource_id": result["id"]}, result["sha256"]):
                raise TaskRefusal("result_not_ready", 409)
        elif receipt["capability"] == "research":
            try:
                if research is None:
                    raise ValueError
                research.validate(result, receipt["operation_digest"])
            except (ValueError, TypeError):
                raise TaskRefusal("result_not_ready", 409) from None
        else:
            raise TaskRefusal("result_not_ready", 409)
