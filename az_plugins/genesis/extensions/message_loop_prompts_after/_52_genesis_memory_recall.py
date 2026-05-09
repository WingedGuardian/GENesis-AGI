"""Inject Genesis cross-session knowledge into AZ conversation turns."""

import asyncio
import logging

from agent import LoopData
from python.helpers.extension import Extension

logger = logging.getLogger(__name__)

_TIMEOUT = 3.0


class GenesisMemoryRecall(Extension):
    async def execute(self, loop_data: LoopData | None = None, **kwargs):
        if loop_data is None:
            loop_data = LoopData()
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if not rt.is_bootstrapped or rt.hybrid_retriever is None:
                return

            query = (
                loop_data.user_message.output_text()
                if loop_data.user_message
                else None
            )
            if not query or len(query) <= 3:
                return

            results = await asyncio.wait_for(
                rt.hybrid_retriever.recall(query, source="both", limit=5),
                timeout=_TIMEOUT,
            )

            if not results:
                return

            lines = ["## Cross-Session Knowledge\n"]
            for r in results:
                lines.append(
                    f"- **[{r.memory_type}]** (score: {r.score:.2f}) "
                    f"{r.content[:200]}"
                )
            knowledge_text = "\n".join(lines)

            loop_data.extras_persistent["genesis_knowledge"] = (
                self.agent.parse_prompt(
                    "agent.system.genesis_knowledge.md",
                    genesis_knowledge=knowledge_text,
                )
            )
            logger.debug("Genesis memory recall: %d results injected", len(results))

        except TimeoutError:
            logger.warning("Genesis memory recall timed out")
        except ImportError:
            pass
        except Exception:
            logger.warning("Genesis memory recall failed", exc_info=True)
