"""Federation transport — the swappable rendezvous layer.

The transport is a DUMB store-and-forward relay: it moves opaque envelopes
between per-peer mailboxes and never inspects, decrypts, or reasons about the
payload (all confidentiality + integrity is end-to-end, in ``crypto``). Keeping
this an interface lets v1 test the whole secure boundary against an in-process
:class:`MockTransport`, and lets the concrete PyNaCl-relay client (or a future
Matrix backend) drop in without touching the subsystem above it.

Delivery contract: **at-least-once**. ``envelope["envelope_id"]`` is the
idempotency key — re-sending the same id is a no-op; the RECEIVER is the dedup +
ordering authority (via the message hash-chain), never the relay. ``poll``
returns items strictly after an opaque cursor; the reader advances its own
cursor and calls ``ack`` so the relay may reclaim delivered items.
"""

from __future__ import annotations

import asyncio
import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class Transport(ABC):
    """The store-and-forward interface every backend satisfies."""

    @abstractmethod
    async def send(self, mailbox_id: str, envelope: dict) -> str:
        """Append an opaque ``envelope`` to ``mailbox_id``. Idempotent on
        ``envelope['envelope_id']``. Returns that id."""

    @abstractmethod
    async def poll(self, mailbox_id: str, after_cursor: str | None = None) -> list[dict]:
        """Return items in ``mailbox_id`` with cursor strictly after
        ``after_cursor`` (None = from the start), in delivery order. Each item is
        ``{"cursor": str, "envelope": dict}``."""

    @abstractmethod
    async def ack(self, mailbox_id: str, cursor: str) -> None:
        """Acknowledge delivery up to and including ``cursor`` — the relay may
        reclaim those items. Advancing the reader's own cursor is separate."""


@dataclass
class _Stored:
    cursor: int
    envelope_id: str
    envelope: dict


@dataclass
class MockTransport(Transport):
    """In-process loopback relay for tests. A SINGLE instance is shared by both
    simulated installs; each polls its own ``mailbox_id``. Faithful to the
    at-least-once + idempotency contract, so it exercises the real receive path.

    It deliberately stores ``envelope`` opaquely and never looks inside — the
    same blindness the real relay has.
    """

    _mailboxes: dict[str, list[_Stored]] = field(default_factory=dict)
    _next_cursor: dict[str, int] = field(default_factory=dict)
    _acked: dict[str, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, mailbox_id: str, envelope: dict) -> str:
        envelope_id = envelope.get("envelope_id")
        if not envelope_id:
            raise ValueError("envelope must carry a non-empty 'envelope_id'")
        async with self._lock:
            box = self._mailboxes.setdefault(mailbox_id, [])
            # idempotency: a re-send of the same id is a no-op (at-least-once).
            if any(item.envelope_id == envelope_id for item in box):
                return envelope_id
            cursor = self._next_cursor.get(mailbox_id, 0) + 1
            self._next_cursor[mailbox_id] = cursor
            # deep-copy so a caller mutating a NESTED payload dict after send()
            # cannot alter the stored message (a real serialized relay wouldn't).
            box.append(
                _Stored(cursor=cursor, envelope_id=envelope_id, envelope=copy.deepcopy(envelope))
            )
            return envelope_id

    async def poll(self, mailbox_id: str, after_cursor: str | None = None) -> list[dict]:
        after = int(after_cursor) if after_cursor else 0
        async with self._lock:
            box = self._mailboxes.get(mailbox_id, [])
            return [
                {"cursor": str(item.cursor), "envelope": copy.deepcopy(item.envelope)}
                for item in box
                if item.cursor > after
            ]

    async def ack(self, mailbox_id: str, cursor: str) -> None:
        async with self._lock:
            self._acked[mailbox_id] = max(self._acked.get(mailbox_id, 0), int(cursor))


class RelayTransport(Transport):
    """The real PyNaCl-relay client (encrypt+sign → POST to relay; poll → the
    receiver verifies+decrypts). Wired in the live-Matrix/relay increment.

    # GROUNDWORK(federation-relay): the concrete over-the-wire client lands with
    the relay server; v1 proves the boundary against MockTransport. Do not delete
    as dead code — this is the deliberate seam the interface exists for.
    """

    def __init__(self, relay_url: str) -> None:
        self._relay_url = relay_url

    async def send(self, mailbox_id: str, envelope: dict) -> str:  # pragma: no cover
        raise NotImplementedError("RelayTransport lands with the relay-server increment")

    async def poll(
        self, mailbox_id: str, after_cursor: str | None = None
    ) -> list[dict]:  # pragma: no cover
        raise NotImplementedError("RelayTransport lands with the relay-server increment")

    async def ack(self, mailbox_id: str, cursor: str) -> None:  # pragma: no cover
        raise NotImplementedError("RelayTransport lands with the relay-server increment")
