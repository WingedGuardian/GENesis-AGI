"""Direct unit tests for the pluggable L1 triggers."""
from genesis.attention.config import AttentionConfig, default_config_dict
from genesis.attention.triggers import (
    _term_present,
    hard_ambient_name,
    hard_explicit_invite,
    soft_domain_keyword,
    soft_multi_speaker,
    soft_question,
    suppress_explicit_dismissal,
)
from genesis.attention.types import AmbientUtterance, TriggerKind

CFG = AttentionConfig.from_dict(default_config_dict())


def _u(text, **kw) -> AmbientUtterance:
    d = dict(id=1, ts=0.0, duration_s=1.0, is_user=None, speaker_total=None,
             n_tokens=5, frac_lt_1=0.0, rms=0.1)
    d.update(kw)
    return AmbientUtterance(text=text, **d)


def test_term_present_word_boundary():
    assert _term_present("hey genesis there", ["genesis"])
    assert not _term_present("regenesis rules", ["genesis"])  # substring, not a whole word


def test_ambient_name_fires_on_alias():
    hit = hard_ambient_name(_u("ask genesis"), [], CFG)
    assert hit and hit.name == "ambient_name" and hit.kind == TriggerKind.HARD


def test_explicit_invite_needs_alias_and_cue():
    assert hard_explicit_invite(_u("what does genesis think"), [], CFG)
    assert hard_explicit_invite(_u("genesis is cool"), [], CFG) is None  # alias, no invite cue


def test_question_fires_on_qmark():
    assert soft_question(_u("really?"), [], CFG)
    assert soft_question(_u("a flat statement"), [], CFG) is None


def test_multi_speaker_needs_two():
    assert soft_multi_speaker(_u("x", speaker_total=2), [], CFG)
    assert soft_multi_speaker(_u("x", speaker_total=1), [], CFG) is None
    assert soft_multi_speaker(_u("x", speaker_total=None), [], CFG) is None


def test_domain_keyword_substring():
    assert soft_domain_keyword(_u("about the routing layer"), [], CFG)  # 'routing' is a default kw
    assert soft_domain_keyword(_u("blah blah okay"), [], CFG) is None


def test_suppressor_dismissal():
    assert suppress_explicit_dismissal(_u("never mind"), [], CFG)
    assert suppress_explicit_dismissal(_u("carry on then"), [], CFG) is None
