"""The genesis-health side of a card release: the vault service's release ops.

One JSON line out and one JSON line back per connection (``vault_service``'s
protocol). The write side stays open until the reply arrives: the service
reads a closed or half-closed client as gone and refuses a claim to it.

Every failure is a :class:`ReleaseClientError` with a fixed code, never the
socket error's text. Nothing here logs a request or a reply; the claim's reply
is the card's values, and the caller drops it as soon as the fields are typed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from genesis.env import vault_socket_path

# A release op answers in milliseconds, except a claim, which decrypts the
# card and writes an audit row (each about a second at worst behind a busy
# database). The service gives up on a silent client after 10 s.
_OP_TIMEOUT_S = 20.0
# A claim's audit row can wait about 22 s behind a held SQLite write lock
# (MEASURED, vault_service.py's stop comment). Leaving earlier only turns an
# approved release into "claim_unanswered", so the claim waits longer.
_CLAIM_TIMEOUT_S = 90.0
_MAX_REPLY = 16 * 1024


class ReleaseClientError(Exception):
    """``code``: ``vault_unavailable`` (no service, or it did not answer in
    time) or ``bad_reply`` (an answer that is not one JSON object)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _ask(payload: dict, timeout: float | None = None) -> dict:
    writer = None
    try:
        async with asyncio.timeout(timeout or _OP_TIMEOUT_S):
            reader, writer = await asyncio.open_unix_connection(
                str(vault_socket_path()), limit=_MAX_REPLY
            )
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
    except (OSError, TimeoutError, ValueError):
        raise ReleaseClientError("vault_unavailable") from None
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    # Raised outside the handler: a JSONDecodeError holds the whole line (a
    # claim's reply is card values), and `from None` would still keep it as
    # the new exception's __context__.
    reply = None
    with contextlib.suppress(ValueError):
        reply = json.loads(line)
    del line
    if not isinstance(reply, dict):
        raise ReleaseClientError("bad_reply")
    return reply


async def request_release(
    card_ref: str,
    host: str,
    page_title: str,
    amount: str,
    claim_hash: str,
    field_origins: dict[str, str],
    button_label: str,
    also_filled: list[str],
    consent_labels: list[str] | None = None,
    step_labels: list[str] | None = None,
) -> dict:
    """``{ok, nonce, shown}`` or ``{ok: False, error}`` (the service's fixed reason).
    ``button_label`` is the Pay control's visible text, ``also_filled`` the
    names (never values) of the non-card fields filled, and ``consent_labels``
    the labels of the checkboxes the fill will tick after the tap; the tap shows
    all three."""
    request = {
        "op": "release_request",
        "card_ref": card_ref,
        "host": host,
        "page_title": page_title,
        "amount": amount,
        "claim_hash": claim_hash,
        "field_origins": field_origins,
        "button_label": button_label,
        "also_filled": also_filled,
    }
    # Only when there are any; the reply's "shown" says the tap showed them.
    if consent_labels:
        request["consent_labels"] = consent_labels
    if step_labels:
        request["step_labels"] = step_labels
    return await _ask(request)


async def release_status(nonce: str) -> dict:
    """``{ok, state, expires_in_s}``; state is pending, granted, refused or expired."""
    return await _ask({"op": "release_status", "nonce": nonce})


async def claim(nonce: str, secret: str) -> dict:
    """``{ok, card: {...}}`` once, or ``{ok: False, error}``. The caller owns
    the card values in the reply and must drop them."""
    return await _ask({"op": "release_claim", "nonce": nonce, "claim": secret}, _CLAIM_TIMEOUT_S)


async def cancel(nonce: str) -> dict:
    return await _ask({"op": "release_cancel", "nonce": nonce})
