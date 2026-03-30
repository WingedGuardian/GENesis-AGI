"""Skills inventory — enumeration of all Genesis skills across phases."""

from __future__ import annotations

SKILLS_INVENTORY: dict[str, dict] = {
    # Phase 6 skills
    "evaluate": {"consumer": "cc_background_research", "phase": 6, "description": "Evaluate technologies and competitive developments"},
    "retrospective": {"consumer": "cc_background_reflection", "phase": 6, "description": "Post-interaction retrospective — extract lessons, process improvements, procedure updates"},
    "research": {"consumer": "cc_background_research", "phase": 6, "description": "Deep research — unfamiliar domains, complex questions, multi-source investigation"},
    "debugging": {"consumer": "cc_background_task", "phase": 6, "description": "Systematic debugging — test failures, runtime errors, anomalous behavior"},
    "obstacle-resolution": {"consumer": "cc_background_task", "phase": 6, "description": "Resolve obstacles — fallback chains when approaches fail or dependencies are unavailable"},
    "triage-calibration": {"consumer": "daily_calibration", "phase": 6, "description": "Daily triage calibration — verify classification accuracy, adjust confidence thresholds"},
    # Phase 7 skills
    "forecasting": {"consumer": "cc_background_research", "phase": 7, "description": "Superforecasting with Brier score tracking"},
    "osint": {"consumer": "cc_background_task", "phase": 7, "description": "OSINT investigation on people, companies, technologies"},
    "lead-generation": {"consumer": "cc_background_task", "phase": 7, "description": "Prospect discovery, enrichment, and scoring"},
    "video-processing": {"consumer": "cc_background_task", "phase": 7, "description": "Video download, transcription, clipping, and formatting"},
    "browser-automation": {"consumer": "cc_background_task", "phase": 7, "description": "Web automation patterns and error recovery via Playwright"},
    "deep-reflection": {"consumer": "cc_background_reflection", "phase": 7, "description": "Deep reflection cycle — memory consolidation, lessons extraction, surplus review, skill effectiveness review, cognitive state regeneration"},
    "strategic-reflection": {"consumer": "cc_background_reflection", "phase": 7, "description": "Strategic reflection — weekly self-assessment, quality calibration, learning stability monitoring"},
    "self-assessment": {"consumer": "cc_background_reflection", "phase": 7, "description": "Weekly self-assessment — evaluate reflection quality, procedure effectiveness, learning velocity, blind spots"},
    "skill-evolution": {"consumer": "cc_background_reflection", "phase": 7, "description": "Skill effectiveness analysis and refinement — analyze skill-tagged session outcomes, propose and apply improvements to SKILL.md files based on evidence"},
    # Setup skills
    "onboarding": {"consumer": "cc_foreground", "phase": "setup", "description": "First-run onboarding — guides new users through Genesis configuration and verification"},
    # Content skills
    "voice-master": {"consumer": "cc_foreground", "phase": "content", "description": "Foundational voice authority — writes content in user's authentic voice"},
    # Phase 8+
    "morning-report": {"consumer": "daily_report", "phase": 8},
    "outreach": {"consumer": "outreach", "phase": 8},
    "task-planning": {"consumer": "cc_background_task", "phase": 9},
    "verification": {"consumer": "cc_background_task", "phase": 9},
    "inbox-classify": {"consumer": "inbox_monitor", "phase": "post-6"},
    "inbox-research": {"consumer": "inbox_monitor", "phase": "post-6"},
}


def get_skills_for_phase(phase: int | str) -> dict[str, dict]:
    """Return skills scheduled for a given phase."""
    return {k: v for k, v in SKILLS_INVENTORY.items() if v.get("phase") == phase}


def get_skills_for_consumer(consumer: str) -> dict[str, dict]:
    """Return skills for a given consumer."""
    return {k: v for k, v in SKILLS_INVENTORY.items() if v.get("consumer") == consumer}
