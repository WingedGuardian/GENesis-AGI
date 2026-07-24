"""Structured failure payloads for ERROR-severity events.

Before this module, ``util/tasks.py`` was the ONLY emitter in the codebase that
attached ``error_type`` to a failure event. Every other failure event type
(~25 of them) emitted a bare message, so a recurring failure recorded nothing a
diagnosis could act on. Worst observed case, live for months::

    event:   scheduler.job_failed
    details: {"job_id": "memory_extraction"}
    message: "Scheduled job 'memory_extraction' failed: "   # str(exc) was empty

An empty ``str(exc)`` is exactly when the exception TYPE carries all the signal,
and that was the one field being dropped.

The structural discriminator
----------------------------
``error_type`` is present **if and only if** a real Python exception caused the
failure. A failure reported as a semantic *result* — an external blocker such as
an HTTP 429 quota response surfaced through a job result's ``reason`` — carries
``error_reason`` and **no** ``error_type``.

This distinction is structural on purpose. The same event type can carry either
kind: ``weekly_assessment.failed`` is emitted both for a genuine ``TypeError``
in Genesis code (``reflection/scheduler.py`` exception path) and for a provider
quota block (its ``result.reason`` path). Downstream consumers must therefore
never infer internal-vs-external by pattern-matching the message — they read the
presence of ``error_type`` instead.

Frames come from :func:`genesis.util.tasks.normalized_frames`, the single
identity basis for failure fingerprints. Do not add a second normalizer here or
anywhere else: two normalizers render one bug two ways and split a single
recurring failure into two fingerprints.
"""

from __future__ import annotations

from genesis.util.tasks import normalized_frames

# Mirrors the reflex arc's own cap on stored message length so a payload can
# never balloon an events row on a pathological exception string.
_MAX_ERROR_CHARS = 500


def failure_details(
    *,
    exc: BaseException | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    """Build the ``details`` payload for a failure event.

    Parameters
    ----------
    exc:
        The exception that caused the failure, when one exists. Takes
        precedence over *reason* — an exception is always the stronger signal.
    reason:
        A semantic failure reason with no exception behind it (e.g. a job
        result's ``reason`` field). Emitted as ``error_reason``.

    Returns
    -------
    dict
        ``{"error_type", "error", "error_frames"}`` for the exception path,
        ``{"error_reason"}`` for the semantic path, or ``{}`` when neither is
        supplied (callers must stay emittable — a missing payload degrades the
        event, it never breaks it).
    """
    if exc is not None:
        return {
            "error_type": type(exc).__name__,
            "error": str(exc)[:_MAX_ERROR_CHARS],
            "error_frames": normalized_frames(exc),
        }
    if reason:
        return {"error_reason": str(reason)[:_MAX_ERROR_CHARS]}
    return {}


def error_summary(exc: BaseException | None, fallback: str | None = None) -> str | None:
    """Render a failure for a single text column (e.g. ``job_health.last_error``).

    Prefixes the exception type so a blank ``str(exc)`` still records something
    diagnosable: ``"TypeError: "`` beats ``""``. Falls back to *fallback* when
    there is no exception.
    """
    if exc is not None:
        return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
    return fallback[:_MAX_ERROR_CHARS] if fallback else fallback
