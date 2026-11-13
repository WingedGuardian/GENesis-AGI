"""Redact-then-rescan consumers on the shapes adopted from the shared table.

The SSH adapter and the career-outreach monitor keep a remote's own diagnosis
and cut credentials out of it. Synthetic values only; no remote calls.
"""

from __future__ import annotations

import pytest

from genesis.modules.external import ipc
from genesis.recon import career_outreach_diagnostics as diagnostics

VALUES = {
    "jwt": "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 43,
    "nvidia": "nvapi-" + "a" * 40,
    "discord_webhook": "https://discord" + ".com/api/webhooks/" + "1" * 18 + "/" + "G" * 60,
    "telegram_bot_token": "1234567890:" + "H" * 35,
}
CONSUMERS = {
    "adapter_head": lambda text: ipc._scrubbed_cut(text, tail=False),
    "adapter_tail": lambda text: ipc._scrubbed_cut(text, tail=True),
    "monitor_stderr": lambda text: diagnostics.scrubbed_excerpt("stderr", text),
    "monitor_error": diagnostics.remote_text,
}
CASES = [
    pytest.param(render, name, value, id=f"{consumer}-{name}")
    for consumer, render in CONSUMERS.items()
    for name, value in VALUES.items()
]


@pytest.mark.parametrize("render,name,value", CASES)
def test_a_shared_shape_is_redacted_and_the_diagnosis_kept(render, name, value):
    result = render("auth failed\n" + value)
    assert "auth failed" in result
    assert f"[redacted:credential_{name}]" in result
    assert "withheld" not in result
    assert value[-8:] not in result


@pytest.mark.parametrize("render,name,value", CASES)
@pytest.mark.parametrize("control", ["\x00", "​"], ids=["nul", "zero_width"])
def test_a_control_character_beside_a_shared_shape_withholds_the_text(render, name, value, control):
    result = render("auth failed\n" + control + value)
    assert "withheld" in result
    assert "auth failed" not in result and value[-8:] not in result


@pytest.mark.parametrize("render,name,value", CASES)
def test_crlf_diagnostics_with_a_shared_shape_are_withheld_whole(render, name, value):
    """A carriage return is a control character to the preflight, so a CRLF
    diagnostic carrying a credential is withheld, exactly as one carrying a
    locally owned key shape already is. Pinned so the behaviour is deliberate."""
    result = render("auth failed\r\n" + value + "\r\n")
    assert "withheld" in result
    assert "auth failed" not in result and value[-8:] not in result
    # The same text with plain line endings keeps its diagnosis.
    assert "auth failed" in render("auth failed\n" + value + "\n")
