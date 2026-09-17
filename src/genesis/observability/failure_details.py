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
            "error": _safe_text(exc),
            "error_frames": _safe_frames(exc),
        }
    # `reason` is a str by contract at every call site, so its truthiness is
    # str.__bool__ — a C slot that cannot be overridden and cannot raise. A
    # round of this PR widened the annotation to `object` and then spent two
    # more rounds containing the hazard that widening invented; both call sites
    # pass `str | None` and already evaluate `bool(error)` themselves one line
    # earlier, so the containment guarded a gate the caller had walked through.
    # Reverted rather than hardened further. Widening belongs at the CALLERS'
    # signatures if it is ever wanted, not at this leaf.
    if reason:
        return {"error_reason": _safe_text(reason)}
    return {}


def _safe_text(value: object) -> str:
    """``str(value)``, capped, that cannot raise into a failure path.

    This module's contract is that a missing payload DEGRADES an event and
    never breaks it — but rendering is the one step here that runs arbitrary
    user code: ``str(exc)`` calls the exception's own ``__str__``, and an
    exception whose ``__str__`` itself raises would propagate out of the very
    helper built to contain it. Every caller is on a failure path where that
    escape costs more than the text: in ``surplus/dispatch.py`` it would skip
    the task's own ``return False``, its autonomy correction and its
    observation, converting one task failure into a dispatch-loop failure and
    losing the reflex signal for the original exception (Codex P2, #1941).
    """
    try:
        return str(value)[:_MAX_ERROR_CHARS]
    except Exception:  # noqa: BLE001 - a diagnostic must never raise into a failure path
        # behavioral-lint: ignore no-hide-problems — the TYPE still ships in
        # error_type, which is the field the reflex arc keys on; only the
        # unrenderable message is lost, and it is named as unrenderable.
        return "<unrenderable>"


def _safe_frames(exc: BaseException) -> list[str]:
    """``normalized_frames(exc)`` that cannot raise into a failure path.

    Same contract as :func:`_safe_text`. Traceback walking touches frame
    objects whose ``repr`` can also be user code, so the fingerprint basis is
    worth protecting rather than assuming.
    """
    try:
        return normalized_frames(exc)
    except Exception:  # noqa: BLE001 - see _safe_text
        # behavioral-lint: ignore no-hide-problems — an empty frame list is a
        # WEAKER fingerprint, never a wrong one; the alternative is no event.
        return []


def error_summary(exc: BaseException | None, fallback: str | None = None) -> str | None:
    """Render a failure for a single text column (e.g. ``job_health.last_error``).

    Prefixes the exception type so a blank ``str(exc)`` still records something
    diagnosable: ``"TypeError: "`` beats ``""``. Falls back to *fallback* when
    there is no exception.
    """
    if exc is not None:
        # Same fail-safe rendering as failure_details: this is written to
        # job_health.last_error on a failure path, and a raising __str__ here
        # would escape into the caller's own error handler.
        return f"{type(exc).__name__}: {_safe_text(exc)}"[:_MAX_ERROR_CHARS]
    return fallback[:_MAX_ERROR_CHARS] if fallback else fallback
