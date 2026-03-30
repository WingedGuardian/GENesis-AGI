"""genesis.research — multi-provider search and research orchestration."""

from genesis.research.orchestrator import ResearchOrchestrator
from genesis.research.types import ResearchResult, SearchResult
from genesis.research.web_adapter import WebSearchAdapter

__all__ = [
    "ResearchOrchestrator",
    "ResearchResult",
    "SearchResult",
    "WebSearchAdapter",
]
