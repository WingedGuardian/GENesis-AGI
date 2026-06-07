"""Campaign subsystem data models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CampaignStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class RunOutcome(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    SKIP = "skip"
    ERROR = "error"


class TriggerType(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    EVENT = "event"


@dataclass
class Campaign:
    id: str
    name: str
    strategy_doc_path: str
    cron_cadence: str
    model: str = "sonnet"
    effort: str = "medium"
    session_profile: str = "interact"
    status: CampaignStatus = CampaignStatus.ACTIVE
    state_json: str = "{}"
    pre_checks: str = '["rate_limit", "budget", "slots_available"]'
    max_daily_cost_usd: float = 1.0
    created_at: str = ""
    paused_at: str | None = None
    last_run_at: str | None = None
    total_runs: int = 0
    total_cost_usd: float = 0.0


@dataclass
class CampaignRun:
    id: str
    campaign_id: str
    started_at: str
    finished_at: str | None = None
    trigger_type: TriggerType = TriggerType.SCHEDULED
    outcome: RunOutcome = RunOutcome.PENDING
    skip_reason: str | None = None
    summary: str | None = None
    cost_usd: float = 0.0
    session_id: str | None = None
    state_snapshot: str | None = None
