"""Inbox Monitor type definitions — enums and frozen dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ItemStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class InboxConfig:
    """Configuration for the inbox monitor.

    Attributes:
        watch_path: Directory to scan for inbox items.
        response_dir: Directory name pattern to *exclude* from scanning
            (e.g. "_genesis"). This is NOT the write target for responses —
            responses are written as sibling .genesis.md files next to the
            source. This field only prevents the scanner from treating files
            inside a matching subdirectory as inbox items.
        max_retries: Maximum retry attempts for failed items before they are
            permanently excluded from reprocessing (default 3).
        recursive: When True, scan subdirectories recursively (rglob).
            When False (default), only scan the top-level watch_path.
    """

    watch_path: Path
    response_dir: str = "_genesis"
    check_interval_seconds: int = 1800
    batch_size: int = 5
    # Max evaluation *items* (URLs / prose blocks) per CC call. A file's delta
    # is segmented into items and grouped into batches of this size, so a
    # 16-URL drop becomes ~4 evals instead of one mega-batch. ``batch_size``
    # above is legacy (files-per-cycle) and no longer governs eval grouping.
    items_per_eval: int = 5
    enabled: bool = True
    model: str = "sonnet"
    effort: str = "high"
    timeout_s: int = 3600
    max_retries: int = 3
    recursive: bool = False
    # timezone removed — uses genesis.env.user_timezone()
    evaluation_cooldown_seconds: int = 3600


@dataclass(frozen=True)
class InboxItem:
    """A single inbox item detected by the scanner."""

    id: str
    file_path: str
    content: str
    content_hash: str
    detected_at: str
    source_content: str = ""
    """The lines this item/batch owns, used both for the eval prompt and for
    the per-batch baseline merge at completion.

    In the URL-level model an item is ONE eval-batch (<= items_per_eval
    segmented items from a file's delta), so ``content`` and ``source_content``
    are the same batch slice — merging ``source_content`` into the file
    baseline marks exactly this batch's lines evaluated, leaving a failed
    sibling batch's lines un-baselined for retry.
    """
    drop_id: str = ""
    """Groups the eval-batches carved from one file's delta. All batches of a
    drop share one approval; empty for legacy/singleton rows."""
    approval_reqid: str = ""
    """For resume batches: the approval request id (from the awaiting marker)
    so the drop's approval is consumed once after dispatch."""


@dataclass(frozen=True)
class InboxBatch:
    """A batch of items to send to a single CC session."""

    batch_id: str
    items: list[InboxItem] = field(default_factory=list)
    created_at: str = ""


@dataclass(frozen=True)
class CheckResult:
    """Result of a single inbox check cycle."""

    items_found: int = 0
    items_new: int = 0
    items_modified: int = 0
    items_retried: int = 0
    batches_dispatched: int = 0
    errors: list[str] = field(default_factory=list)
