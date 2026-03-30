"""Genesis reflection — agent_init wrapper.

Copies reflection scheduler, stability monitor, and context gatherer
references from GenesisRuntime to self.agent.
"""

import logging

from python.helpers.extension import Extension

logger = logging.getLogger("genesis.extensions.reflection")


class GenesisReflection(Extension):
    async def execute(self, **kwargs):
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if not rt.is_bootstrapped:
                await rt.bootstrap()

            self.agent.genesis_reflection_scheduler = rt.reflection_scheduler
            self.agent.genesis_stability_monitor = rt.stability_monitor
            logger.info("Genesis reflection wired to agent")

        except ImportError:
            logger.warning("Genesis reflection not available")
        except Exception:
            logger.exception("Failed to wire Genesis reflection")
