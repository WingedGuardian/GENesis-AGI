"""Session management with context caching for OpenAI Realtime API."""
import logging
import time
from typing import Optional

from pipecat.frames.frames import Frame, LLMMessagesUpdateFrame, StartFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

logger = logging.getLogger(__name__)


class ContextCacheEntry:
    """Entry in the context cache for a specific client."""

    def __init__(self, context: LLMContext, timestamp: float):
        self.context = context
        self.timestamp = timestamp


class SessionManager:
    """Manages OpenAI Realtime sessions with context caching per client device.

    For each new WebSocket connection, a new session is created, but the context
    from previous sessions for the same client is preserved if the last connection
    closed within the reuse timeout period.
    """

    def __init__(self, reuse_timeout: float = 300.0):
        """Initialize session manager.

        Args:
            reuse_timeout: Time in seconds after which cached context expires
        """
        self.reuse_timeout = reuse_timeout
        # Dictionary mapping client_id to ContextCacheEntry
        self.context_caches: dict[str, ContextCacheEntry] = {}
        # Dictionary mapping client_id to current service
        self.current_services: dict[str, OpenAIRealtimeLLMService] = {}
        # Dictionary mapping client_id to context aggregator pair
        self.context_aggregators: dict[str, LLMContextAggregatorPair] = {}

    def get_cached_context(self, client_id: str) -> LLMContext | None:
        """Get cached context for a specific client if it's still valid.

        Args:
            client_id: Unique identifier for the client device

        Returns:
            Cached LLMContext if valid, None otherwise
        """
        if client_id not in self.context_caches:
            return None

        cache_entry = self.context_caches[client_id]
        time_since_cache = time.time() - cache_entry.timestamp

        if time_since_cache < self.reuse_timeout:
            logger.info(f"♻️ Using cached context for client {client_id} from {time_since_cache:.1f}s ago")
            return cache_entry.context
        else:
            # Cache expired
            logger.info(f"⏰ Context cache expired for client {client_id} ({time_since_cache:.1f}s ago, timeout: {self.reuse_timeout}s)")
            del self.context_caches[client_id]
            return None

    def cache_context_from_service(self, client_id: str, service: OpenAIRealtimeLLMService):
        """Extract and cache context from a service before it's closed.

        Args:
            client_id: Unique identifier for the client device
            service: The OpenAI Realtime service to extract context from
        """
        # First try to get context from the context aggregator (more reliable)
        context = None
        if client_id in self.context_aggregators:
            aggregator_pair = self.context_aggregators[client_id]
            # The context is shared between user and assistant aggregators
            user_aggregator = aggregator_pair.user()
            if hasattr(user_aggregator, '_context') and user_aggregator._context:
                context = user_aggregator._context
                logger.debug(f"🔍 Found context in aggregator for client {client_id}")

        # Fallback: try to get context from service
        if not context and service and hasattr(service, '_context') and service._context:
            context = service._context
            logger.debug(f"🔍 Found context in service for client {client_id}")

        # Cache the context if we found one
        if context:
            messages = context.get_messages() if hasattr(context, 'get_messages') else []
            message_count = len(messages) if messages else 0
            self.context_caches[client_id] = ContextCacheEntry(
                context=context,
                timestamp=time.time()
            )
            logger.info(f"💾 Cached context from previous session for client {client_id} ({message_count} messages)")
        else:
            if not service:
                logger.warning(f"⚠️ No service provided to cache context for client {client_id}")
            elif client_id not in self.context_aggregators:
                logger.warning(f"⚠️ No context aggregator found for client {client_id}")
            elif not hasattr(service, '_context'):
                logger.warning(f"⚠️ Service has no '_context' attribute for client {client_id}")
            elif not service._context:
                logger.warning(f"⚠️ Service context is None for client {client_id}")
            else:
                logger.debug(f"No context to cache from service for client {client_id}")

    def create_context_for_new_session(self, client_id: str) -> LLMContext:
        """Create a new context for a new session, reusing cached context if available.

        Args:
            client_id: Unique identifier for the client device

        Returns:
            LLMContext for the new session (cached or new)
        """
        # Log available cache keys for debugging
        if self.context_caches:
            logger.debug(f"🔍 Available cached contexts: {list(self.context_caches.keys())}")
        else:
            logger.debug("🔍 No cached contexts available")

        cached_context = self.get_cached_context(client_id)
        if cached_context:
            # Create a new context instance with the same messages
            # Use the constructor to properly copy messages and tools
            cached_messages = cached_context.get_messages()
            new_context = LLMContext(
                messages=cached_messages.copy() if cached_messages else None,
                tools=cached_context.tools if hasattr(cached_context, 'tools') else None,
                tool_choice=cached_context.tool_choice if hasattr(cached_context, 'tool_choice') else None
            )
            logger.info(f"✅ Created new context for client {client_id} with {len(new_context.get_messages())} messages from cache")
            return new_context
        else:
            logger.info(f"🆕 Creating new empty context for client {client_id}")
            return LLMContext()

    def get_current_service(self, client_id: str) -> OpenAIRealtimeLLMService | None:
        """Get current OpenAI service for a specific client.

        Args:
            client_id: Unique identifier for the client device

        Returns:
            Current OpenAIRealtimeLLMService if exists, None otherwise
        """
        return self.current_services.get(client_id)

    def set_current_service(self, client_id: str, service: OpenAIRealtimeLLMService):
        """Set the current active service for a client.

        Args:
            client_id: Unique identifier for the client device
            service: The currently active OpenAI Realtime service
        """
        self.current_services[client_id] = service

    def set_context_aggregator(self, client_id: str, aggregator_pair: LLMContextAggregatorPair):
        """Set the context aggregator pair for a client.

        Args:
            client_id: Unique identifier for the client device
            aggregator_pair: The LLMContextAggregatorPair instance for this client
        """
        self.context_aggregators[client_id] = aggregator_pair

    def remove_context_aggregator(self, client_id: str):
        """Remove the context aggregator pair for a client.

        Args:
            client_id: Unique identifier for the client device
        """
        if client_id in self.context_aggregators:
            del self.context_aggregators[client_id]

    def cleanup_before_new_session(self, client_id: str):
        """Cleanup before creating a new session for a client.

        This should be called before creating a new session to cache
        the context from the current service.

        Args:
            client_id: Unique identifier for the client device
        """
        # Cache context from service/aggregator
        if client_id in self.current_services:
            self.cache_context_from_service(client_id, self.current_services[client_id])
            del self.current_services[client_id]

        # Remove context aggregator (will be recreated for new session)
        self.remove_context_aggregator(client_id)

    def create_context_aggregator(self, client_id: str) -> LLMContextAggregatorPair:
        """Create a context aggregator pair for a new session.

        Args:
            client_id: Unique identifier for the client device

        Returns:
            LLMContextAggregatorPair with cached or new context
        """
        context = self.create_context_for_new_session(client_id)
        # Disable local turn strategies — we use server-side SemanticTurnDetection
        # (avoids onnxruntime dependency which is incompatible with HA's Python)
        aggregator_pair = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(user_turn_strategies=None),
        )
        self.set_context_aggregator(client_id, aggregator_pair)
        return aggregator_pair

    def create_context_initializer(self, client_id: str, context_aggregator: LLMContextAggregatorPair) -> Optional['ContextInitializer']:
        """Create a context initializer if cached messages exist.

        Args:
            client_id: Unique identifier for the client device
            context_aggregator: The context aggregator pair

        Returns:
            ContextInitializer if cached messages exist, None otherwise
        """
        context = context_aggregator.user().context
        if len(context.get_messages()) > 0:
            return ContextInitializer(
                context_aggregator=context_aggregator,
                cached_context=context,
                client_id=client_id
            )
        return None

    def handle_client_disconnect(self, client_id: str, service: OpenAIRealtimeLLMService | None = None):
        """Handle client disconnection by caching context.

        Args:
            client_id: Unique identifier for the client device
            service: Optional service instance to cache context from
        """
        logger.info(f"🔌 Client {client_id} disconnected - caching context")

        # Get service to cache from
        service_to_cache = None
        if client_id in self.current_services:
            service_to_cache = self.current_services[client_id]
        elif service:
            service_to_cache = service

        if service_to_cache:
            try:
                self.cache_context_from_service(client_id, service_to_cache)
                if client_id in self.current_services:
                    del self.current_services[client_id]
                logger.info(f"💾 Cached context for disconnected client {client_id}")
            except Exception as e:
                logger.warning(f"⚠️ Error caching context for disconnected client {client_id}: {e}")
        else:
            logger.debug(f"No service found to cache context for client {client_id}")


class ContextInitializer(FrameProcessor):
    """Processor that sends cached context after StartFrame has passed through the pipeline."""

    def __init__(self, context_aggregator, cached_context, client_id, **kwargs):
        super().__init__(**kwargs)
        self.context_aggregator = context_aggregator
        self.cached_context = cached_context
        self.client_id = client_id
        self.context_sent = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and send cached context after StartFrame."""
        if isinstance(frame, StartFrame):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

            # Send cached context after StartFrame has passed through the pipeline
            # Use LLMMessagesUpdateFrame with run_llm=False to set context without triggering a response
            if self.cached_context and not self.context_sent:
                context = self.cached_context
                messages = context.get_messages()
                if len(messages) > 0:
                    # Update messages without triggering LLM response
                    # The bot will wait for the user to speak first
                    update_frame = LLMMessagesUpdateFrame(messages=messages, run_llm=False)
                    await self.context_aggregator.user().push_frame(update_frame)
                    logger.info(f"📤 Sent cached context ({len(messages)} messages) to OpenAI for client {self.client_id} (waiting for user)")
                    self.context_sent = True
            return

        await self.push_frame(frame, direction)

