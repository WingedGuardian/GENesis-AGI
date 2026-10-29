"""Synthetic diagnostic confidentiality and useful-text controls; no remote calls."""

import pytest

from genesis.modules.external import ipc
from genesis.recon import career_outreach_diagnostics as diagnostics

CONSUMERS = ["adapter-head", "adapter-tail", "monitor-head", "monitor-tail",
             "monitor-unlabelled-head", "monitor-unlabelled-tail"]


def render(consumer, text):
    if consumer.startswith("adapter"):
        return ipc._scrubbed_cut(text, tail=consumer.endswith("tail"))
    return diagnostics.scrubbed_excerpt(
        "stderr", text, head=consumer.endswith("head"),
        labelled="unlabelled" not in consumer,
    )


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("separator", ["\r", "\x00", "\u200b"])
@pytest.mark.parametrize("first_length", [0, 19, 20, 21, 30])
@pytest.mark.parametrize("offset", [0, 299, 300, 301, 1999, 2000, 2001])
def test_control_split_credentials_withheld_before_redaction_and_cut(
    consumer, separator, first_length, offset,
):
    raw = "p" * offset + " sk-" + "A" * first_length + separator + "B" * 40
    result = render(consumer, raw)
    assert "withheld" in result
    assert "B" * 20 not in result and "A" * 20 not in result


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("separator", ["\r", "\x00", "\u200b"])
@pytest.mark.parametrize("suffix_length", [299, 300, 301, 1999, 2000, 2001, 2300])
def test_split_suffix_crossing_excerpt_bounds_is_withheld(consumer, separator, suffix_length):
    raw = "sk-" + "A" * 30 + separator + "B" * suffix_length
    result = render(consumer, raw)
    assert "withheld" in result and "B" * 20 not in result


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("raw", [
    "sk-" + "A" * 30 + " unrelated\x00diagnosis",
    "sk-" + "A" * 30 + "\r\nauth failed",
    "ABC_TOKEN=\x00",  # Only the ORIGINAL view recognizes this assignment.
    "to\u200bken=" + "A" * 30,  # Deletion view only.
    "token=\x00" + "A" * 30,  # Space view and deletion view.
])
def test_approved_control_ambiguity_withholds_whole_excerpt(consumer, raw):
    assert "withheld" in render(consumer, raw)


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("value_length", [30, 2300])
@pytest.mark.parametrize("raw_kind", ["key", "assignment"])
def test_ordinary_credentials_keep_benign_diagnosis(consumer, value_length, raw_kind):
    value = "A" * value_length
    raw = ("sk-" + value if raw_kind == "key" else "token=" + value) + "\nauth failed"
    result = render(consumer, raw)
    assert "auth failed" in result and "[redacted:" in result
    assert "withheld" not in result and "A" * 20 not in result


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("separator", ["\r", "\x00", "\u200b", "\n", "\t", "\u2028"])
def test_benign_controls_and_owner_locations_are_not_suppressed(consumer, separator):
    result = render(consumer, "auth" + separator + "failed at /home/fixture/ localhost:5000")
    assert "withheld" not in result and "failed" in result
    assert "/home/fixture/" in result and "localhost:5000" in result


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("raw", [None, 42, ""])
def test_empty_or_nonstring_behavior_unchanged(consumer, raw):
    assert render(consumer, raw) == ("" if consumer.startswith("adapter") else None)


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_unredactable_card_still_withheld(consumer):
    assert "withheld" in render(consumer, "charge failed 4111 1111 1111 1111")


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("stage", ["scan_diagnostic_controls", "redact_credentials",
                                   "scan_outbound_normalised"])
def test_fail_closed_at_every_diagnostic_stage(consumer, stage, monkeypatch):
    module = ipc if consumer.startswith("adapter") else diagnostics

    def fail(_text):
        raise RuntimeError("synthetic scanner failure")

    monkeypatch.setattr(module, stage, fail)
    result = render(consumer, "auth failed harmless")
    assert "scrubber failed" in result and "auth failed" not in result


@pytest.mark.parametrize("raw,expected", [
    ("ABC_TOKEN=\x00", {"env_variable_secret"}),
    ("to\u200bken=" + "A" * 30, {"credential_assignment"}),
    ("word\x00gsk_" + "A" * 30, {"api_key_groq"}),
    ("word\x004111\x001111\x001111\x001111", {"payment_card"}),
    ("token=" + "A" * 30 + "\n\tauth failed", set()),
    ("token=" + "A" * 30 + "\u2028auth failed", set()),
    ("auth\x00failed", set()),
])
def test_shared_preflight_preserves_raw_and_normalized_views(raw, expected):
    from genesis.security.output_scanner import scan_diagnostic_controls

    assert scan_diagnostic_controls(raw) == expected


@pytest.mark.parametrize("raw,view", [
    ("ABC_TOKEN=\x00", "original"),
    ("to\u200bken=" + "A" * 30, "deleted"),
    ("word\x004111\x001111\x001111\x001111", "spaces"),
])
def test_each_preflight_view_has_an_independent_detection_witness(raw, view):
    from genesis.security.output_scanner import _invisible_replaced, scan_outbound

    variants = {"original": raw, "deleted": _invisible_replaced(raw, ""),
                "spaces": _invisible_replaced(raw, " ")}
    assert {name for name, text in variants.items() if not scan_outbound(text).safe} == {view}


@pytest.mark.parametrize("make_value", [str, RuntimeError])
def test_monitor_only_remote_text_paths_withhold_control_credentials(make_value):
    value = make_value("sk-" + "A" * 30 + "\r" + "B" * 40)
    result = diagnostics.remote_text(value)
    assert "withheld" in result and "B" * 20 not in result
