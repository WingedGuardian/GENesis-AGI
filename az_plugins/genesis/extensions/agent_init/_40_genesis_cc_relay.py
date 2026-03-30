"""Genesis CC relay — agent_init wrapper.

Copies CC invoker, session manager, checkpoint manager, and reflection bridge
references from GenesisRuntime to self.agent.
"""

import logging

from python.helpers.extension import Extension

logger = logging.getLogger("genesis.cc_relay")


class GenesisCCRelay(Extension):
    async def execute(self, **kwargs):
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if not rt.is_bootstrapped:
                await rt.bootstrap()

            self.agent.genesis_cc_invoker = rt.cc_invoker
            self.agent.genesis_session_manager = rt.session_manager
            self.agent.genesis_checkpoint_manager = rt.checkpoint_manager
            self.agent.genesis_cc_reflection_bridge = rt.cc_reflection_bridge
            logger.info("Genesis CC relay wired to agent")

        except ImportError:
            logger.warning("Genesis CC not available")
        except Exception:
            logger.exception("Failed to wire Genesis CC relay")
