"""Genesis inbox — agent_init wrapper.

Copies inbox monitor reference from GenesisRuntime to self.agent.
"""

import logging

from python.helpers.extension import Extension

logger = logging.getLogger("genesis.extensions.inbox")


class GenesisInbox(Extension):
    async def execute(self, **kwargs):
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if not rt.is_bootstrapped:
                await rt.bootstrap()

            self.agent.genesis_inbox_monitor = rt.inbox_monitor
            logger.info("Genesis inbox wired to agent")

        except ImportError:
            logger.warning("Genesis inbox not available")
        except Exception:
            logger.exception("Failed to wire Genesis inbox")
