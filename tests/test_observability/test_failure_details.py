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


class _HostileStr(Exception):
    """An exception whose own rendering raises — the case every caller is on.

    Not hypothetical machinery for its own sake: `__str__` is arbitrary user
    code that runs at exactly the moment the system is already failing.
    """

    def __str__(self) -> str:
        raise RuntimeError("__str__ exploded")


class TestRenderingNeverEscapes:
    """A diagnostic payload builder must not raise into the failure it reports.

    The module contract says a missing payload DEGRADES an event and never
    breaks it, but rendering runs the exception's own `__str__`. Where that
    escaped, the caller's failure handler died with it: in
    `surplus/dispatch.py` the task's `return False`, its autonomy correction
    and its observation would all be skipped, turning one task failure into a
    dispatch-loop failure and losing the reflex signal for the original
    exception (Codex P2, #1941).

    VERIFY-RED: reverting `_safe_text` to a bare `str(exc)[:500]` makes the
    four RENDERING tests in this class fail with RuntimeError("__str__
    exploded"). The str-typed-lane test below does not depend on `_safe_text`
    and is unaffected — stated precisely because the earlier "every test in
    this class" wording was measurably false once tests were added to the
    class, which is exactly the docstring-outlives-its-claim shape this file
    exists to warn about.
    """

    def test_failure_details_survives_an_unrenderable_exception(self):
        details = failure_details(exc=_HostileStr())
        # The discriminator that actually routes the event still ships.
        assert details["error_type"] == "_HostileStr"
        assert details["error"] == "<unrenderable>"
        # Frames are independent of the message and must still be attempted.
        assert "error_frames" in details

    def test_error_summary_survives_an_unrenderable_exception(self):
        summary = error_summary(_HostileStr())
        assert summary.startswith("_HostileStr: ")
        assert "<unrenderable>" in summary

    def test_the_type_is_never_sacrificed_to_a_bad_message(self):
        """`error_type` is what the reflex arc keys on; only the message is lost.

        Guard the guard: without this the class above would still pass if
        `_safe_text` swallowed the whole payload instead of just the text.
        """
        details = failure_details(exc=_HostileStr())
        assert set(details) == {"error_type", "error", "error_frames"}

    def test_a_reason_that_cannot_render_is_contained_too(self):
        """The semantic lane takes arbitrary objects from job results."""

        class _HostileReason:
            def __str__(self) -> str:
                raise RuntimeError("nope")

        assert failure_details(reason=_HostileReason()) == {"error_reason": "<unrenderable>"}

    def test_the_reason_lane_is_str_typed_so_its_dispatch_cannot_run_user_code(self):
        """Why the `reason` lane needs no containment, recorded so it is not
        "hardened" again.

        `if reason:` is `str.__bool__` — a C slot, unoverridable on the exact
        type. Both call sites declare `error: str | None` and pass
        `reason=None if exc is not None else error`, and both already evaluate
        `bool(error)` themselves before calling. A round of this PR widened the
        annotation to `object`, which invented a hazard that then cost two more
        rounds; the widening was reverted rather than contained further.

        If a caller is ever widened, this test fails to parse the intent — that
        is the point: change the CALLERS' signatures and this docstring
        together, not the leaf.
        """
        import inspect

        sig = inspect.signature(failure_details)
        assert str(sig.parameters["reason"].annotation) in ("str | None", "'str | None'"), (
            "the reason lane's safety rests on it being str-typed — see this docstring"
        )
