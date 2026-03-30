"""CheckpointManager — orchestrates checkpoint-and-resume flow."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from genesis.cc.types import CCInvocation, CCOutput, MessageType, background_session_dir
from genesis.db.crud import message_queue

logger = logging.getLogger(__name__)

_CHECKPOINT_TYPES = {MessageType.QUESTION, MessageType.DECISION, MessageType.ERROR}


class CheckpointManager:
    def __init__(self, *, db, session_manager=None, invoker=None, event_bus=None):
        self._db = db
        self._session_manager = session_manager
        self._invoker = invoker
        self._event_bus = event_bus

    async def poll_pending_messages(self, *, target: str = "user") -> list[dict]:
        return await message_queue.query_pending(self._db, target=target)

    async def deliver_response(self, *, message_id: str, response: dict | str) -> None:
        now = datetime.now(UTC).isoformat()
        resp_str = json.dumps(response) if isinstance(response, dict) else response
        await message_queue.set_response(
            self._db, message_id, response=resp_str, responded_at=now,
        )

    async def resume_session(
        self, *, session_id: str, response_text: str,
    ) -> CCOutput:
        invocation = CCInvocation(
            prompt=response_text, resume_session_id=session_id,
            skip_permissions=True,
            working_dir=background_session_dir(),
        )
        output = await self._invoker.run(invocation)
        if output.is_error:
            logger.error(
                "CC resume failed (session=%s): %s",
                session_id, output.error_message,
            )
        return output

    @staticmethod
    def should_checkpoint(message_type: MessageType | str) -> bool:
        if isinstance(message_type, str):
            message_type = MessageType(message_type)
        return message_type in _CHECKPOINT_TYPES
