"""Explicit operator publication of immutable snapshots, never raw recall."""

from __future__ import annotations

import hashlib
import re
import uuid

from genesis.peers.registry import PeerRegistry
from genesis.security.output_scanner import scan_outbound

MAX_RESOURCE_BYTES = 256 * 1024


def resource_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid published resource identifier")
    return value


class PublishedResources:
    def __init__(self, registry: PeerRegistry):
        self.registry = registry

    async def publish(self, title: str, content: str) -> dict:
        # This is a trusted operator call. No incoming route/facade may call it.
        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= 200
            or not isinstance(content, str)
            or not content
            or len(content.encode("utf-8")) > MAX_RESOURCE_BYTES
            or not scan_outbound(title + "\n" + content).safe
        ):
            raise ValueError("Published resource refused")
        identifier = uuid.uuid4().hex
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        async with self.registry.transaction() as db:
            await db.execute(
                "INSERT INTO peer_resources(id,title,content,sha256) VALUES(?,?,?,?)",
                (identifier, title, content, digest),
            )
        return {"resource_id": identifier, "sha256": digest}

    async def get(self, identifier: str) -> dict | None:
        resource_id(identifier)
        async with self.registry.connection() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM peer_resources WHERE id=? AND active=1", (identifier,)
                )
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if (
            hashlib.sha256(result["content"].encode("utf-8")).hexdigest() != result["sha256"]
            or not scan_outbound(result["title"] + "\n" + result["content"]).safe
        ):
            raise ValueError("Published resource unavailable")
        return result

    async def retire(self, identifier: str) -> None:
        resource_id(identifier)
        async with self.registry.transaction() as db:
            cursor = await db.execute("UPDATE peer_resources SET active=0 WHERE id=?", (identifier,))
            if cursor.rowcount != 1:
                raise ValueError("Unknown published resource")
