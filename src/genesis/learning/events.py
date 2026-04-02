"""Standard event type constants for the LEARNING subsystem.

Used with GenesisEventBus.emit(subsystem=Subsystem.LEARNING, event_type=...).
"""

from __future__ import annotations

LEARNING_EVENTS: dict[str, str] = {
    "TRIAGE_CLASSIFIED": "triage.classified",
    "CLASSIFICATION_COMPLETED": "classification.completed",
    "CALIBRATION_COMPLETED": "calibration.completed",
    "CALIBRATION_SKIPPED": "calibration.skipped",
    "CALIBRATION_FAILED": "calibration.failed",
    "HARVESTING_COMPLETED": "harvesting.completed",
    "CAPABILITY_GAP_RECORDED": "capability_gap.recorded",
}
