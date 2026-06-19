"""feedback/calibration.py — measure-only ego confidence calibration.

Computes whether the ego's stated confidence tracks actual outcomes, from the
Outcome Bus T1 (ground-truth) rows: "the ego said 90%, it was right 82%". Writes
one snapshot per run to ``ego_calibration_snapshots`` so the ECE trend over time
accrues — the self-improvement signal.

DARK by construction:
- Writes only to ``ego_calibration_snapshots`` (no cognitive-path reader).
- Never writes ``calibration_curves`` (auto-read by ``perception/context.py``).
- Never injects calibration back into the ego — self-correction is a deliberate,
  separately-flagged future PR.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import aiosqlite

from genesis.calibration.metrics import compute_ece, compute_mce
from genesis.calibration.types import bucket_confidence
from genesis.db.crud import ego_calibration as cal_crud
from genesis.db.crud import outcome_events as oe_crud

logger = logging.getLogger(__name__)

EGO_DOMAIN = "ego"

# A snapshot is flagged low-confidence (a noisy estimate) below these — so the
# user surface never reports a thin ECE=0.0 as "perfectly calibrated".
_LOW_CONF_MIN_SAMPLES = 20
_LOW_CONF_MIN_BUCKETS = 3


def _bucket_midpoint(bucket: str) -> float:
    """Parse a '0.8-0.9' bucket label to its midpoint 0.85.

    Mirrors the parse in ``calibration/curves.py`` so ego calibration and the
    existing outreach/triage calibration stay consistent.
    """
    try:
        low, high = bucket.split("-")
        return (float(low) + float(high)) / 2
    except (ValueError, IndexError):
        return 0.5


def build_curve(pairs: list[dict]) -> list[dict]:
    """Bucket (stated_confidence, value) pairs into a calibration curve.

    Reuses ``calibration.types.bucket_confidence`` for binning. Returns only
    POPULATED buckets, each shaped for ``compute_ece``/``compute_mce``:
    ``{confidence_bucket, predicted_confidence, actual_success_rate, sample_count}``.
    """
    buckets: dict[str, list[float]] = defaultdict(list)
    for p in pairs:
        conf = p.get("stated_confidence")
        val = p.get("value")
        if conf is None or val is None:
            continue
        buckets[bucket_confidence(float(conf))].append(float(val))

    curve: list[dict] = []
    for bucket, values in sorted(buckets.items()):
        n = len(values)
        curve.append(
            {
                "confidence_bucket": bucket,
                "predicted_confidence": _bucket_midpoint(bucket),
                "actual_success_rate": sum(values) / n,
                "sample_count": n,
            }
        )
    return curve


async def compute_ego_calibration(
    db: aiosqlite.Connection, *, days: int = 90
) -> dict | None:
    """Compute + persist one ego calibration snapshot.

    Returns the snapshot dict, or ``None`` if there are no calibratable T1 rows
    yet (in which case NOTHING is written — a missing snapshot reads as "no data",
    never as a spurious perfect ECE=0.0).
    """
    pairs = await oe_crud.calibration_pairs(db, source="ego", tier=1, days=days)
    if not pairs:
        logger.info("ego calibration: no T1 rows yet — skipping snapshot")
        return None

    curve = build_curve(pairs)
    if not curve:
        logger.info("ego calibration: no calibratable buckets — skipping snapshot")
        return None

    ece = compute_ece(curve)
    mce = compute_mce(curve)
    sample_count = sum(b["sample_count"] for b in curve)
    bucket_count = len(curve)
    low_confidence = (
        sample_count < _LOW_CONF_MIN_SAMPLES or bucket_count < _LOW_CONF_MIN_BUCKETS
    )

    await cal_crud.record_snapshot(
        db,
        domain=EGO_DOMAIN,
        ece=ece,
        mce=mce,
        sample_count=sample_count,
        bucket_count=bucket_count,
        low_confidence=low_confidence,
        curve=curve,
    )
    logger.info(
        "ego calibration: ECE=%.4f MCE=%.4f n=%d buckets=%d%s",
        ece, mce, sample_count, bucket_count,
        " (low-confidence estimate)" if low_confidence else "",
    )
    return {
        "domain": EGO_DOMAIN,
        "ece": ece,
        "mce": mce,
        "sample_count": sample_count,
        "bucket_count": bucket_count,
        "low_confidence": low_confidence,
        "curve": curve,
    }
