"""Failure-payload contract: the structural internal-vs-external discriminator.

The load-bearing invariant here is that ``error_type`` is present IFF a real
exception caused the failure. Downstream classification reads that presence
instead of pattern-matching the message — necessary because one event type
(``weekly_assessment.failed``) is emitted for BOTH a genuine TypeError in
Genesis code and a provider quota block.
"""

from __future__ import annotations

from genesis.observability.failure_details import error_summary, failure_details
from genesis.util.tasks import normalized_frames


def _raise_type_error() -> BaseException:
    """Return a TypeError carrying a real traceback (mirrors the live defect:
    ``float() argument must be a string or a real number, not 'NoneType'``)."""
    try:
        float(None)  # type: ignore[arg-type]
    except TypeError as exc:
        return exc
    raise AssertionError("expected TypeError")


class TestStructuralDiscriminator:
    def test_exception_path_carries_type_and_frames(self):
        exc = _raise_type_error()
        details = failure_details(exc=exc)

        assert details["error_type"] == "TypeError"
        assert "float()" in str(details["error"])
        assert details["error_frames"], "an exception with a traceback must yield frames"
        assert "error_reason" not in details

    def test_semantic_path_carries_no_error_type(self):
        """An external blocker reported as a result reason must NOT look like an
        exception — this is the whole discriminator."""
        details = failure_details(reason='{"api_error_status":429,"result":"weekly limit"}')

        assert "error_type" not in details
        assert "error_frames" not in details
        assert "429" in str(details["error_reason"])

    def test_exception_wins_over_reason(self):
        exc = _raise_type_error()
        details = failure_details(exc=exc, reason="some reason")

        assert details["error_type"] == "TypeError"
        assert "error_reason" not in details

    def test_neither_yields_empty_payload(self):
        """Callers must stay emittable — a missing payload degrades the event,
        it never breaks it."""
        assert failure_details() == {}
        assert failure_details(reason="") == {}

    def test_empty_exception_string_still_diagnosable(self):
        """The live scheduler.job_failed bug: str(exc) was empty, so the TYPE was
        the only signal — and it was the field being dropped."""
        details = failure_details(exc=ValueError())

        assert details["error_type"] == "ValueError"
        assert details["error"] == ""

    def test_long_error_is_truncated(self):
        details = failure_details(exc=RuntimeError("x" * 5000))
        assert len(str(details["error"])) == 500

    def test_long_reason_is_truncated(self):
        details = failure_details(reason="y" * 5000)
        assert len(str(details["error_reason"])) == 500


class TestSingleIdentityBasis:
    """A second traceback normalizer would render one bug two ways and split a
    recurring failure into two fingerprints. Guard that there is exactly one."""

    def test_frames_match_the_canonical_normalizer(self):
        exc = _raise_type_error()
        assert failure_details(exc=exc)["error_frames"] == normalized_frames(exc)

    def test_same_exception_fingerprints_identically_from_any_emitter(self):
        """Two emitters reporting the same failure must produce one fingerprint."""
        from genesis.reflex.fingerprint import fingerprint

        exc = _raise_type_error()
        details = failure_details(exc=exc)

        from_emitter_a = fingerprint(
            "weekly_assessment", str(details["error_type"]), details["error_frames"]
        )
        from_emitter_b = fingerprint(
            "weekly_assessment", type(exc).__name__, normalized_frames(exc)
        )
        assert from_emitter_a == from_emitter_b


class TestErrorSummary:
    def test_prefixes_exception_type(self):
        """job_health.last_error is a single column — the type must survive into
        it, or a blank str(exc) records nothing (observed live for months)."""
        assert error_summary(ValueError()) == "ValueError: "
        assert error_summary(_raise_type_error()).startswith("TypeError: ")

    def test_falls_back_when_no_exception(self):
        assert error_summary(None, "missed") == "missed"
        assert error_summary(None, None) is None

    def test_truncates(self):
        assert len(error_summary(RuntimeError("z" * 5000))) == 500
