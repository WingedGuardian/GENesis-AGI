"""Genesis perception — agent_init wrapper.

Copies router + reflection engine references from GenesisRuntime to self.agent.
"""

import logging

from python.helpers.extension import Extension

logger = logging.getLogger(__name__)


class GenesisPerception(Extension):
    async def execute(self, **kwargs):
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if not rt.is_bootstrapped:
                await rt.bootstrap()

            self.agent.genesis_router = rt.router
            self.agent.genesis_reflection_engine = rt.reflection_engine
            logger.info("Genesis perception wired to agent")

        except ImportError:
            logger.warning("Genesis perception not available")
        except Exception:
            logger.exception("Failed to wire Genesis perception")
