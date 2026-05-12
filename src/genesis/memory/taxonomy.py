"""Wing/room taxonomy classifier for memory organization.

Classifies memories into structural domains (wings) and topics (rooms)
based on content analysis, file paths, and existing tags. Inspired by
MemPalace's navigational retrieval structure.

Wings are top-level domains. Rooms are specific topics within a wing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Taxonomy definition
# ---------------------------------------------------------------------------

WINGS = frozenset({
    # Genesis-internal subsystems
    "memory",
    "learning",
    "routing",
    "infrastructure",
    "channels",
    "autonomy",
    # User-work domains (added 2026-05-11 from cluster analysis of the
    # 1,964-row general/uncategorized pile). About what the user works on,
    # not Genesis subsystem internals.
    "dev_workflow",
    "research",
    "integrations",
    "career",
    "general",
})

# Rooms per wing — used for classification and validation
ROOMS: dict[str, list[str]] = {
    "memory": [
        "retrieval", "extraction", "store", "embeddings",
        "proactive_hook", "activation", "graph", "essential_knowledge",
    ],
    "learning": [
        "skills", "evolution", "calibration", "procedures",
        "observations", "reflection",
    ],
    "routing": [
        "model_selection", "call_sites", "circuit_breakers",
        "providers", "cost_tracking",
    ],
    "infrastructure": [
        "guardian", "sentinel", "health", "database",
        "runtime", "scheduler", "updates",
    ],
    "channels": [
        "telegram", "dashboard", "openclaw", "inbox", "mail",
    ],
    "autonomy": [
        "tasks", "permissions", "approval", "protected_paths",
        "adversarial_review",
    ],
    "dev_workflow": [
        "git", "ci", "pull_request", "review", "worktree",
    ],
    "research": [
        "papers", "external_systems", "newsletters", "api_docs",
    ],
    "integrations": [
        "providers", "third_party_apis", "tools", "models",
    ],
    "career": [
        "applications", "profile", "research", "outreach",
    ],
    "general": ["uncategorized"],
}


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of classifying a memory into wing/room."""
    wing: str
    room: str
    confidence: float  # 0.0 - 1.0


# ---------------------------------------------------------------------------
# Path-based classification (strongest signal)
# ---------------------------------------------------------------------------

_PATH_PATTERNS: list[tuple[str, str, str]] = [
    # (regex pattern, wing, room)
    # IMPORTANT: specific patterns MUST come before general catch-alls within
    # each wing group. First match wins.

    # memory wing — specific before catch-all
    (r"src/genesis/memory/retrieval", "memory", "retrieval"),
    (r"src/genesis/memory/extract", "memory", "extraction"),
    (r"src/genesis/memory/store", "memory", "store"),
    (r"src/genesis/memory/embed", "memory", "embeddings"),
    (r"src/genesis/memory/activation", "memory", "activation"),
    (r"src/genesis/memory/graph", "memory", "graph"),
    (r"src/genesis/memory/linker", "memory", "graph"),
    (r"src/genesis/memory/essential", "memory", "essential_knowledge"),
    (r"proactive_memory_hook", "memory", "proactive_hook"),
    (r"src/genesis/memory/", "memory", "store"),  # catch-all LAST

    # learning wing — specific before catch-all
    (r"src/genesis/learning/skill", "learning", "skills"),
    (r"src/genesis/learning/evolution", "learning", "evolution"),
    (r"src/genesis/learning/calibrat", "learning", "calibration"),
    (r"src/genesis/learning/procedur", "learning", "procedures"),
    (r"src/genesis/perception/", "learning", "observations"),
    (r"src/genesis/learning/", "learning", "observations"),  # catch-all LAST

    # routing wing — specific before catch-all
    (r"src/genesis/routing/circuit", "routing", "circuit_breakers"),
    (r"call.?site", "routing", "call_sites"),
    (r"src/genesis/routing/", "routing", "model_selection"),  # catch-all LAST

    # infrastructure wing
    (r"src/genesis/runtime/", "infrastructure", "runtime"),
    (r"src/genesis/surplus/", "infrastructure", "scheduler"),
    (r"src/genesis/db/", "infrastructure", "database"),
    (r"guardian", "infrastructure", "guardian"),
    (r"sentinel", "infrastructure", "sentinel"),
    (r"health", "infrastructure", "health"),

    # channels wing — specific before catch-all
    (r"src/genesis/channels/telegram", "channels", "telegram"),
    (r"dashboard", "channels", "dashboard"),
    (r"inbox", "channels", "inbox"),
    (r"mail", "channels", "mail"),
    (r"src/genesis/channels/", "channels", "openclaw"),  # catch-all LAST

    # autonomy wing
    (r"src/genesis/autonomy/", "autonomy", "tasks"),
    (r"protected_path", "autonomy", "protected_paths"),
    (r"adversarial", "autonomy", "adversarial_review"),
]

# ---------------------------------------------------------------------------
# Keyword-based classification
# ---------------------------------------------------------------------------

_KEYWORD_MAP: dict[str, tuple[str, str]] = {
    # memory wing
    "memory_recall": ("memory", "retrieval"),
    "memory_store": ("memory", "store"),
    "qdrant": ("memory", "store"),
    "embedding": ("memory", "embeddings"),
    "vector search": ("memory", "retrieval"),
    "fts5": ("memory", "retrieval"),
    "retrieval": ("memory", "retrieval"),
    "extraction": ("memory", "extraction"),
    "proactive hook": ("memory", "proactive_hook"),
    "activation score": ("memory", "activation"),
    "memory link": ("memory", "graph"),
    "essential knowledge": ("memory", "essential_knowledge"),
    # learning wing
    "skill": ("learning", "skills"),
    "evolution pipeline": ("learning", "evolution"),
    "calibration": ("learning", "calibration"),
    "procedure": ("learning", "procedures"),
    "observation": ("learning", "observations"),
    "reflection": ("learning", "reflection"),
    "pattern detect": ("learning", "observations"),
    # routing wing
    "router": ("routing", "model_selection"),
    "model selection": ("routing", "model_selection"),
    "call site": ("routing", "call_sites"),
    "circuit breaker": ("routing", "circuit_breakers"),
    "provider": ("routing", "providers"),
    "deepinfra": ("routing", "providers"),
    "gemini": ("routing", "providers"),
    "cost track": ("routing", "cost_tracking"),
    # infrastructure wing
    "guardian": ("infrastructure", "guardian"),
    "sentinel": ("infrastructure", "sentinel"),
    "health probe": ("infrastructure", "health"),
    "database": ("infrastructure", "database"),
    "runtime": ("infrastructure", "runtime"),
    "bootstrap": ("infrastructure", "runtime"),
    "scheduler": ("infrastructure", "scheduler"),
    "surplus": ("infrastructure", "scheduler"),
    "update": ("infrastructure", "updates"),
    # channels wing
    "telegram": ("channels", "telegram"),
    "dashboard": ("channels", "dashboard"),
    "openclaw": ("channels", "openclaw"),
    "inbox": ("channels", "inbox"),
    "mail": ("channels", "mail"),
    # autonomy wing
    "autonomy": ("autonomy", "tasks"),
    "task execut": ("autonomy", "tasks"),
    "permission": ("autonomy", "permissions"),
    "approval gate": ("autonomy", "approval"),
    "protected path": ("autonomy", "protected_paths"),
    "adversarial review": ("autonomy", "adversarial_review"),
    # dev_workflow wing (PRs, worktrees, CI, code review tooling). Kept
    # narrow on purpose: bare "git commit"/"git push" etc. appear too often
    # in operational memories (autonomy, infrastructure) and would steal
    # those classifications via the earliest-position rule. The terms below
    # are dev-workflow-specific.
    "pull request": ("dev_workflow", "pull_request"),
    "worktree": ("dev_workflow", "worktree"),
    "github actions": ("dev_workflow", "ci"),
    "code review": ("dev_workflow", "review"),
    "ruff check": ("dev_workflow", "ci"),
    "greptile": ("dev_workflow", "review"),
    "ultrareview": ("dev_workflow", "review"),
    # research wing (external content, system reading, paper notes)
    "vllm": ("research", "external_systems"),
    "honcho": ("research", "external_systems"),
    "agent zero": ("research", "external_systems"),
    "latent space": ("research", "newsletters"),
    "newsletter": ("research", "newsletters"),
    "research paper": ("research", "papers"),
    "arxiv": ("research", "papers"),
    "api documentation": ("research", "api_docs"),
    # integrations wing (specific third-party services the user is wiring
    # into projects). Deliberately omits "openai api" / "anthropic api"
    # because those phrases overlap heavily with Genesis-internal routing
    # provider discussions — those memories should stay in `routing`.
    "minimax": ("integrations", "providers"),
    "abacus ai": ("integrations", "providers"),
    "litellm": ("integrations", "providers"),
    "openrouter": ("integrations", "providers"),
    "conway": ("integrations", "third_party_apis"),
    "composio": ("integrations", "third_party_apis"),
    # career wing. "resume" intentionally omitted as a content keyword —
    # it collides with "resume the session", "resume crawling", etc. —
    # career classifications via this word should come through the
    # `resume` tag instead (handled in _TAG_WING_MAP below).
    "cv revision": ("career", "applications"),
    "profile.yml": ("career", "profile"),
    "job application": ("career", "applications"),
    "ats integration": ("career", "outreach"),
    "recruiter": ("career", "outreach"),
    "careerops": ("career", "applications"),
    "jerbs": ("career", "applications"),
}

# ---------------------------------------------------------------------------
# Tag-based classification
# ---------------------------------------------------------------------------

_TAG_WING_MAP: dict[str, str] = {
    "memory": "memory",
    "retrieval": "memory",
    "embedding": "memory",
    "extraction": "memory",
    "skill": "learning",
    "evolution": "learning",
    "calibration": "learning",
    "procedure": "learning",
    "observation": "learning",
    "reflection": "learning",
    "routing": "routing",
    "router": "routing",
    "provider": "routing",
    "model": "routing",
    "guardian": "infrastructure",
    "sentinel": "infrastructure",
    "health": "infrastructure",
    "database": "infrastructure",
    "runtime": "infrastructure",
    "scheduler": "infrastructure",
    "surplus": "infrastructure",
    "telegram": "channels",
    "dashboard": "channels",
    "openclaw": "channels",
    "inbox": "channels",
    "mail": "channels",
    "autonomy": "autonomy",
    "task": "autonomy",
    "permission": "autonomy",
    # dev_workflow tag map
    "git": "dev_workflow",
    "worktree": "dev_workflow",
    "pr": "dev_workflow",
    "pull_request": "dev_workflow",
    "ci": "dev_workflow",
    "code_review": "dev_workflow",
    # research tag map
    "newsletter": "research",
    "external_system": "research",
    "research_paper": "research",
    # integrations tag map. NOTE: "provider" deliberately stays mapped to
    # "routing" above (Genesis-internal model providers). User-work
    # integrations come in via keyword classification on specific service
    # names (minimax, abacus, litellm, etc.).
    "third_party_api": "integrations",
    "integration": "integrations",
    # career tag map
    "career": "career",
    "job_search": "career",
    "resume": "career",
    "ats": "career",
}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify(
    content: str,
    *,
    tags: list[str] | None = None,
    source: str = "",
    source_pipeline: str = "",
) -> Classification:
    """Classify a memory into wing/room based on content and metadata.

    Priority order:
    1. File paths in content (strongest signal, 0.9 confidence)
    2. Keywords in content (0.7 confidence)
    3. Tags (0.6 confidence)
    4. Source pipeline (0.5 confidence)
    5. Fallback: general/uncategorized (0.1 confidence)
    """
    content_lower = content.lower()
    tags_lower = [t.lower() for t in (tags or [])]

    # 1. Path-based — strongest signal
    for pattern, wing, room in _PATH_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return Classification(wing=wing, room=room, confidence=0.9)

    # 2. Keyword-based — check content for domain keywords
    best_keyword: tuple[str, str] | None = None
    best_keyword_pos = len(content_lower) + 1  # Prefer earlier matches

    for keyword, (wing, room) in _KEYWORD_MAP.items():
        pos = content_lower.find(keyword)
        if pos != -1 and pos < best_keyword_pos:
            best_keyword = (wing, room)
            best_keyword_pos = pos

    if best_keyword:
        return Classification(wing=best_keyword[0], room=best_keyword[1], confidence=0.7)

    # 3. Tag-based — check existing tags
    for tag in tags_lower:
        # Skip class: tags and garbage JSON
        if tag.startswith("class:") or tag.startswith("{"):
            continue
        for tag_key, wing in _TAG_WING_MAP.items():
            if tag_key in tag:
                # Room defaults to first room in wing
                room = ROOMS[wing][0]
                return Classification(wing=wing, room=room, confidence=0.6)

    # 4. Source pipeline
    pipeline_wing_map = {
        "reflection": ("learning", "reflection"),
        "harvest": ("learning", "observations"),
        "auto_memory_harvest": ("learning", "observations"),
        "conversation": ("general", "uncategorized"),
        "quality_calibration": ("learning", "calibration"),
        "weekly_assessment": ("learning", "reflection"),
        "session_extraction": ("memory", "extraction"),
    }
    if source_pipeline in pipeline_wing_map:
        wing, room = pipeline_wing_map[source_pipeline]
        return Classification(wing=wing, room=room, confidence=0.5)

    # 5. Fallback
    return Classification(wing="general", room="uncategorized", confidence=0.1)


def detect_wing_from_prompt(prompt: str, file_paths: list[str] | None = None) -> str | None:
    """Detect the active wing from a user prompt and recent file paths.

    Used by the proactive memory hook to bias retrieval toward the active domain.
    Returns None if no confident wing detection.
    """
    # Check file paths first (strongest signal)
    if file_paths:
        for path in file_paths:
            for pattern, wing, _room in _PATH_PATTERNS:
                if re.search(pattern, path, re.IGNORECASE):
                    return wing

    # Check prompt keywords
    prompt_lower = prompt.lower()
    wing_votes: dict[str, int] = {}

    for keyword, (wing, _room) in _KEYWORD_MAP.items():
        if keyword in prompt_lower:
            wing_votes[wing] = wing_votes.get(wing, 0) + 1

    if wing_votes:
        # Return wing with most votes (ties broken by alphabetical)
        return max(wing_votes, key=lambda w: (wing_votes[w], w))

    return None
