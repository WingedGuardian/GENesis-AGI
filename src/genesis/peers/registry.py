"""Owner-managed peer relationships and grants on private SQLite connections."""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import aiosqlite

from genesis.db.connection import CACHE_SIZE_KIB, connect_aiosqlite_rw
from genesis.env import db_busy_timeout_ms, genesis_db_path

CAPABILITIES = frozenset(
    {"conversation", "research", "knowledge_offer", "knowledge_promote", "recurrence"}
)
MODES = frozenset({"disabled", "fallback", "sam"})


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("invalid peer identifier")
    return value


def capability(value: str) -> str:
    if not isinstance(value, str) or (
        value not in CAPABILITIES and not re.fullmatch(r"resource:[a-f0-9]{32}", value)
    ):
        raise ValueError("invalid peer capability")
    return value


class PeerRegistry:
    """Own peer SQL and its guarded private connection as one transaction boundary.

    Relationship revisions and grants must commit together under BEGIN IMMEDIATE.
    The shared live-runtime connection cannot own this private transaction while
    unrelated coroutines use it; keeping the parameterized statements here avoids
    changing the shared connection factory or creating a missing/quarantined DB.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else genesis_db_path()

    @asynccontextmanager
    async def connection(self):
        async with connect_aiosqlite_rw(self.path, existing_only=True) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute(f"PRAGMA busy_timeout={db_busy_timeout_ms()}")
            await db.execute(f"PRAGMA cache_size={CACHE_SIZE_KIB}")
            yield db

    @asynccontextmanager
    async def transaction(self):
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def settings(self) -> dict:
        async with self.connection() as db:
            row = await (await db.execute("SELECT * FROM peer_settings WHERE id=1")).fetchone()
            return dict(row) if row else {"mode": "disabled", "sam_realm": None}

    async def configure(
        self, mode: str, sam_realm: str | None = None, service_url: str | None = None
    ) -> None:
        if mode not in MODES or (mode == "sam" and not sam_realm):
            raise ValueError("explicit peer authentication mode and realm required")
        if sam_realm is not None and (
            not isinstance(sam_realm, str)
            or not sam_realm.isascii()
            or not 1 <= len(sam_realm) <= 256
            or any(ord(c) < 33 or ord(c) > 126 for c in sam_realm)
        ):
            raise ValueError("invalid peer realm")
        if service_url is not None:
            if not isinstance(service_url, str):
                raise ValueError("invalid owner-configured peer service URL")
            parsed = urlsplit(service_url)
            if (
                not service_url.isascii()
                or len(service_url) > 2048
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or parsed.query
                or parsed.fragment
                or parsed.path != "/v1/agent/a2a"
                or any(ord(c) < 33 or ord(c) > 126 for c in service_url)
            ):
                raise ValueError("invalid owner-configured peer service URL")
        if mode != "disabled" and not service_url:
            raise ValueError("enabled peers require an owner-configured service URL")
        async with self.transaction() as db:
            await db.execute(
                "INSERT INTO peer_settings(id,mode,sam_realm,service_url) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,sam_realm=excluded.sam_realm,service_url=excluded.service_url",
                (mode, sam_realm, service_url),
            )

    async def register(
        self,
        peer_id: str,
        *,
        same_owner: bool,
        daily_allowance: int = 1,
        token_name: str | None = None,
        sam_realm: str | None = None,
        sam_node: str | None = None,
        principal: str | None = None,
    ) -> None:
        identifier(peer_id)
        if (
            type(same_owner) is not bool
            or type(daily_allowance) is not int
            or not 0 < daily_allowance < 2**63
        ):
            raise ValueError("invalid ownership or legacy compatibility value")
        if token_name is not None and (
            not isinstance(token_name, str)
            or not re.fullmatch(r"GENESIS_PEER_[A-Z0-9_]+_TOKEN", token_name)
            or token_name == "GENESIS_PEER_BACKEND_TOKEN"  # noqa: S105 — credential NAME only
        ):
            raise ValueError("invalid scoped peer credential name")
        if bool(sam_realm) != bool(sam_node) or not (token_name or sam_node):
            raise ValueError("peer requires a scoped credential or pinned node")
        for value in (sam_realm, sam_node, principal):
            if value is not None and (
                not isinstance(value, str)
                or not value.isascii()
                or not 1 <= len(value) <= 256
                or any(ord(c) < 33 or ord(c) > 126 for c in value)
            ):
                raise ValueError("invalid pinned peer identity")
        if principal is not None and not sam_node:
            raise ValueError("delegated identity requires a pinned node")
        async with self.transaction() as db:
            await db.execute(
                "INSERT INTO peers(peer_id,epoch,same_owner,daily_allowance,token_name,sam_realm,sam_node,principal) VALUES(?,?,?,?,?,?,?,?)",
                (
                    peer_id,
                    uuid.uuid4().hex,
                    int(same_owner),
                    daily_allowance,
                    token_name,
                    sam_realm,
                    sam_node,
                    principal,
                ),
            )

    async def rows(self) -> list[dict]:
        async with self.connection() as db:
            return [
                dict(row)
                for row in await (
                    await db.execute("SELECT * FROM peers ORDER BY peer_id")
                ).fetchall()
            ]

    async def get(self, peer_id: str) -> dict | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM peers WHERE peer_id=?", (peer_id,))
            ).fetchone()
            return dict(row) if row else None

    async def grant(self, peer_id: str, key: str, decision: str) -> None:
        identifier(peer_id)
        capability(key)
        if decision not in {"allow", "ask", "deny"}:
            raise ValueError("invalid peer grant decision")
        async with self.transaction() as db:
            if not await (
                await db.execute("SELECT 1 FROM peers WHERE peer_id=?", (peer_id,))
            ).fetchone():
                raise ValueError("unknown peer")
            await db.execute("UPDATE peers SET revision=revision+1 WHERE peer_id=?", (peer_id,))
            await db.execute(
                "INSERT INTO peer_grants(peer_id,capability,decision) VALUES(?,?,?) ON CONFLICT(peer_id,capability) DO UPDATE SET decision=excluded.decision",
                (peer_id, key, decision),
            )

    async def grants(self, peer_id: str) -> dict[str, str]:
        async with self.connection() as db:
            return dict(
                await (
                    await db.execute(
                        "SELECT capability,decision FROM peer_grants WHERE peer_id=?", (peer_id,)
                    )
                ).fetchall()
            )

    async def revoke(self, peer_id: str) -> None:
        async with self.transaction() as db:
            result = await db.execute(
                "UPDATE peers SET active=0,revision=revision+1 WHERE peer_id=?", (peer_id,)
            )
            if result.rowcount != 1:
                raise ValueError("unknown peer")
