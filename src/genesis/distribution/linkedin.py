"""LinkedIn distribution via Composio SDK."""

from __future__ import annotations

import logging
import os
from typing import Any

from genesis.distribution.base import PostResult
from genesis.distribution.config import LinkedInConfig

logger = logging.getLogger(__name__)


class LinkedInDistributor:
    """Publishes content to LinkedIn via Composio's official API integration.

    Uses the ``composio-client`` SDK which wraps LinkedIn's OAuth API.
    The Composio API key is read from the ``COMPOSIO_API_KEY`` environment
    variable (loaded via secrets.env at startup).  Connection-specific
    values (connected_account_id, author_urn) come from
    ``~/.genesis/config/distribution.yaml``.
    """

    def __init__(self, config: LinkedInConfig) -> None:
        self._config = config
        self._client: Any | None = None
        self._init_client()

    def _init_client(self) -> None:
        api_key = os.environ.get("COMPOSIO_API_KEY", "")
        if not api_key:
            logger.warning("COMPOSIO_API_KEY not set — LinkedIn distribution unavailable")
            return
        try:
            from composio_client import AsyncComposio

            self._client = AsyncComposio(api_key=api_key)
            logger.info("LinkedIn distributor initialized (account %s)", self._config.connected_account_id)
        except ImportError:
            logger.warning(
                "composio-client not installed — LinkedIn distribution unavailable. "
                "Install with: pip install 'genesis-v3[distribution]'"
            )
        except Exception:
            logger.warning("Failed to initialize Composio client", exc_info=True)

    @property
    def platform(self) -> str:
        return "linkedin"

    @property
    def available(self) -> bool:
        return (
            self._client is not None
            and bool(self._config.connected_account_id)
            and bool(self._config.author_urn)
        )

    async def publish(
        self,
        content: str,
        *,
        visibility: str = "PUBLIC",
    ) -> PostResult:
        """Publish a post to LinkedIn.

        Args:
            content: The post text (commentary).
            visibility: One of PUBLIC, CONNECTIONS, LOGGED_IN.

        Returns:
            PostResult with status and any error details.
        """
        if not self.available:
            return PostResult(
                post_id=None,
                platform="linkedin",
                url=None,
                status="failed",
                error="LinkedIn distributor not configured (missing API key, account ID, or author URN)",
            )

        try:
            result = await self._client.tools.execute(
                "LINKEDIN_CREATE_LINKED_IN_POST",
                arguments={
                    "author": self._config.author_urn,
                    "commentary": content,
                    "visibility": visibility,
                },
                connected_account_id=self._config.connected_account_id,
                user_id=self._config.user_id,
                version="20260424_00",
            )

            successful = result.successful if hasattr(result, "successful") else False
            data = result.data if hasattr(result, "data") else {}
            data = data if isinstance(data, dict) else {}

            if successful:
                # Response shape: {"x_restli_id": "urn:li:share:..."}
                # Fallback: check legacy response_dict for older Composio versions
                post_id = data.get("x_restli_id") or data.get("response_dict", {}).get("id")
                logger.info("Published to LinkedIn: %s", post_id)
                return PostResult(
                    post_id=str(post_id) if post_id else None,
                    platform="linkedin",
                    url=f"https://www.linkedin.com/feed/update/{post_id}" if post_id else None,
                    status="published",
                )
            else:
                error_msg = (result.error if hasattr(result, "error") else None) or str(data)
                logger.warning("LinkedIn publish failed: %s", error_msg)
                return PostResult(
                    post_id=None,
                    platform="linkedin",
                    url=None,
                    status="failed",
                    error=str(error_msg)[:500],
                )
        except Exception as exc:
            logger.error("LinkedIn publish error: %s", exc, exc_info=True)
            return PostResult(
                post_id=None,
                platform="linkedin",
                url=None,
                status="failed",
                error=str(exc)[:500],
            )

    async def delete(self, post_id: str) -> bool:
        """Delete a LinkedIn post by its share ID."""
        if not self.available:
            return False

        try:
            result = await self._client.tools.execute(
                "LINKEDIN_DELETE_LINKED_IN_POST",
                arguments={"share_id": post_id},
                connected_account_id=self._config.connected_account_id,
                user_id=self._config.user_id,
                version="20260424_00",
            )
            return bool(result.successful) if hasattr(result, "successful") else False
        except Exception:
            logger.error("LinkedIn delete failed for %s", post_id, exc_info=True)
            return False

