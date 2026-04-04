"""Async client for Agent Zero's REST API.

Moved from genesis.channels.az_client — backward-compat shim remains there.
"""

import base64
import logging

import aiohttp

log = logging.getLogger(__name__)


class AZClient:
    """Thin wrapper around Agent Zero's POST /api_message endpoint."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 300):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def send_message(
        self,
        text: str,
        context_id: str = "",
        attachments: list[dict] | None = None,
    ) -> tuple[str, str]:
        """Send a message to Agent Zero and return (response_text, context_id).

        AZ creates a new context when context_id is empty. When a known
        context_id is provided, it reuses that conversation. Callers should
        store the returned context_id for subsequent messages.
        """
        payload: dict = {
            "message": text,
            "context_id": context_id,
        }
        if attachments:
            payload["attachments"] = attachments

        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            url = f"{self.base_url}/api_message"
            log.debug("POST %s context=%s", url, context_id)
            async with session.post(url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("response", ""), data.get("context_id", context_id)

    async def send_message_with_file(
        self,
        text: str,
        file_bytes: bytes,
        filename: str,
        context_id: str = "",
    ) -> tuple[str, str]:
        """Send a message with a binary file attachment (base64-encoded)."""
        b64 = base64.b64encode(file_bytes).decode()
        attachments = [{"base64": b64, "filename": filename}]
        return await self.send_message(text, context_id, attachments)

    async def health_check(self) -> bool:
        """Check if Agent Zero is reachable."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session, session.get(f"{self.base_url}/") as resp:
                return resp.status == 200
        except Exception:
            return False
