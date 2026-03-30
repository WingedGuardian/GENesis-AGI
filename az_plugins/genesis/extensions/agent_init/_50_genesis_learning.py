"""Genesis learning — agent_init wrapper.

Copies memory store, triage pipeline, and learning scheduler references
from GenesisRuntime to self.agent.
"""

import logging

from python.helpers.extension import Extension

logger = logging.getLogger("genesis.extensions.learning")


class GenesisLearning(Extension):
    async def execute(self, **kwargs):
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if not rt.is_bootstrapped:
                await rt.bootstrap()

            self.agent.genesis_memory_store = rt.memory_store
            self.agent.genesis_triage_pipeline = rt.triage_pipeline
            self.agent.genesis_learning_scheduler = rt.learning_scheduler
            logger.info("Genesis learning wired to agent")

        except ImportError:
            logger.warning("Genesis learning not available")
        except Exception:
            logger.exception("Failed to wire Genesis learning")
