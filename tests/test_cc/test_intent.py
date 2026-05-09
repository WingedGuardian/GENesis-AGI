"""Tests for intent parser — slash commands only, NL passes through."""

from genesis.cc.intent import IntentParser
from genesis.cc.types import CCModel, EffortLevel

parser = IntentParser()


def test_slash_model_opus():
    r = parser.parse("/model opus hello there")
    assert r.model_override == CCModel.OPUS
    assert r.cleaned_text == "hello there"


def test_slash_effort_high():
    r = parser.parse("/effort high can you help?")
    assert r.effort_override == EffortLevel.HIGH
    assert r.cleaned_text == "can you help?"


def test_slash_effort_xhigh():
    """Opus 4.7 xhigh tier (CC 2.1.111+) must parse correctly."""
    r = parser.parse("/effort xhigh investigate this thoroughly")
    assert r.effort_override == EffortLevel.XHIGH
    assert r.cleaned_text == "investigate this thoroughly"


def test_slash_effort_max():
    r = parser.parse("/effort max push hard")
    assert r.effort_override == EffortLevel.MAX
    assert r.cleaned_text == "push hard"


def test_slash_resume():
    r = parser.parse("/resume")
    assert r.resume_requested is True


def test_slash_resume_with_id():
    r = parser.parse("/resume sess-abc-123")
    assert r.resume_requested is True
    assert r.resume_session_id == "sess-abc-123"


def test_slash_task():
    r = parser.parse("/task renew my registration")
    assert r.task_requested is True
    assert r.cleaned_text == "renew my registration"


def test_nl_switch_passes_through():
    """NL model phrases are NOT extracted — they go to CC as-is."""
    r = parser.parse("switch to opus")
    assert r.model_override is None
    assert r.cleaned_text == "switch to opus"


def test_nl_think_harder_passes_through():
    """NL effort phrases are NOT extracted."""
    r = parser.parse("think harder about this problem")
    assert r.effort_override is None
    assert r.cleaned_text == "think harder about this problem"


def test_nl_resume_passes_through():
    """NL resume phrases are NOT extracted."""
    r = parser.parse("go back to our last conversation")
    assert r.resume_requested is False
    assert r.cleaned_text == "go back to our last conversation"


def test_nl_make_task_passes_through():
    """NL task phrases are NOT extracted."""
    r = parser.parse("make a task out of this")
    assert r.task_requested is False
    assert r.cleaned_text == "make a task out of this"


def test_plain_text_passthrough():
    r = parser.parse("what's the weather like?")
    assert r.model_override is None
    assert r.effort_override is None
    assert not r.resume_requested
    assert not r.task_requested
    assert r.cleaned_text == "what's the weather like?"


def test_combined_commands():
    r = parser.parse("/model opus /effort high do this")
    assert r.model_override == CCModel.OPUS
    assert r.effort_override == EffortLevel.HIGH
    assert r.cleaned_text == "do this"


def test_case_insensitive_slash():
    """Only slash commands are case-insensitive extracted."""
    r = parser.parse("/Model Opus")
    assert r.model_override == CCModel.OPUS


def test_nl_case_insensitive_passes_through():
    """NL phrases pass through regardless of case."""
    r = parser.parse("Switch To Opus")
    assert r.model_override is None
    assert r.cleaned_text == "Switch To Opus"


def test_use_haiku_passes_through():
    """NL 'use haiku' is NOT extracted."""
    r = parser.parse("use haiku for this")
    assert r.model_override is None
    assert r.cleaned_text == "use haiku for this"


def test_intent_only_slash_model():
    r = parser.parse("/model sonnet")
    assert r.intent_only is True
    assert r.model_override == CCModel.SONNET


def test_intent_only_slash_effort():
    r = parser.parse("/effort high")
    assert r.intent_only is True


def test_not_intent_only_with_text():
    r = parser.parse("/model opus tell me about X")
    assert r.intent_only is False
    assert r.cleaned_text == "tell me about X"


def test_resume_not_intent_only():
    """resume needs CC to execute — never intent_only."""
    r = parser.parse("/resume")
    assert r.intent_only is False


def test_quick_summary_no_false_positive():
    """'quick' should NOT trigger effort=LOW — NL patterns removed."""
    r = parser.parse("give me a quick summary")
    assert r.effort_override is None
    assert r.cleaned_text == "give me a quick summary"
