"""Direct unit tests for the pluggable L1 triggers."""
from genesis.attention.config import AttentionConfig, default_config_dict
from genesis.attention.triggers import (
    SOFT_TRIGGERS,
    _term_present,
    hard_ambient_name,
    hard_explicit_invite,
    soft_decision,
    soft_dispute,
    soft_domain_keyword,
    soft_emotional,
    soft_lexical_repetition,
    soft_multi_speaker,
    soft_quantity_money,
    soft_question,
    soft_recall_cue,
    soft_task_reminder,
    soft_temporal_deadline,
    soft_topic_continuation,
    suppress_explicit_dismissal,
    suppress_low_asr_confidence,
    suppress_mode_active_listen,
    suppress_mode_active_s2s,
    suppress_mode_global_mute,
    suppress_sensitive_topic,
    unanswered_question_update,
)
from genesis.attention.types import AmbientUtterance, EngineState, TriggerKind

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


# ── PR3a: new soft triggers ──

def test_lexical_bank_triggers_fire_on_default_banks():
    assert soft_decision(_u("we should ship it"), [], CFG).name == "decision"
    assert soft_task_reminder(_u("remind me to call"), [], CFG).name == "task_reminder"
    assert soft_temporal_deadline(_u("it's due tomorrow"), [], CFG).name == "temporal_deadline"
    assert soft_quantity_money(_u("that's $50"), [], CFG).name == "quantity_money"
    assert soft_recall_cue(_u("what did we say about it"), [], CFG).name == "recall_cue"
    assert soft_dispute(_u("are you sure about that"), [], CFG).name == "dispute"


def test_lexical_bank_triggers_silent_on_plain_text():
    for fn in (soft_decision, soft_task_reminder, soft_temporal_deadline,
               soft_quantity_money, soft_recall_cue, soft_dispute):
        assert fn(_u("the weather is nice"), [], CFG) is None


def test_emotional_fires_on_any_bank():
    for text in ("ugh this again", "this is amazing", "i'm so confused", "hurry up"):
        hit = soft_emotional(_u(text), [], CFG)
        assert hit is not None and hit.name == "emotional", text
    assert soft_emotional(_u("a neutral sentence"), [], CFG) is None


def test_emotional_is_single_hit_when_two_banks_match():
    hit = soft_emotional(_u("amazing but i'm confused"), [], CFG)  # excitement + confusion
    assert hit is not None and hit.name == "emotional"            # still exactly one hit


def test_topic_continuation_needs_multi_utt_window():
    cur = _u("continuing on")
    assert soft_topic_continuation(cur, [cur], CFG) is None                       # only this utt
    assert soft_topic_continuation(cur, [_u("earlier"), cur], CFG).name == "topic_continuation"


def test_lexical_repetition_fires_on_shared_content_tokens():
    prev = _u("the migration keeps failing on startup")
    cur = _u("migration failing again on startup")   # shared: migration, failing, startup (>=3)
    assert soft_lexical_repetition(cur, [prev, cur], CFG).name == "lexical_repetition"


def test_lexical_repetition_ignores_distinct_content():
    prev = _u("the routing layer is broken")
    cur = _u("the memory cache is slow")             # 3 content tokens, zero overlap with prev
    assert soft_lexical_repetition(cur, [prev, cur], CFG) is None


def test_lexical_repetition_ignores_too_short():
    cur = _u("routing")                              # 1 content token < min 3
    assert soft_lexical_repetition(cur, [_u("routing here"), cur], CFG) is None


# ── PR3a: new suppressors ──

def test_sensitive_topic_inert_when_bank_empty():
    assert suppress_sensitive_topic(_u("anything at all"), [], CFG) is None       # default bank is []


def test_sensitive_topic_fires_when_bank_filled():
    cfg = AttentionConfig.from_dict({**default_config_dict(),
                                     "suppressor_patterns": {"sensitive_topics": [r"\bsalary\b"]}})
    assert suppress_sensitive_topic(_u("my salary is"), [], cfg).name == "sensitive_topic"


def test_low_asr_confidence_threshold():
    assert suppress_low_asr_confidence(_u("x", frac_lt_1=0.5), [], CFG).name == "low_asr_confidence"
    assert suppress_low_asr_confidence(_u("x", frac_lt_1=0.1), [], CFG) is None
    assert suppress_low_asr_confidence(_u("x", frac_lt_1=0.35), [], CFG) is not None  # boundary (>=)


def test_mode_suppressors_inert_offline_but_fire_on_mode():
    assert suppress_mode_active_listen(_u("x"), [], CFG) is None                   # mode_state="unknown"
    assert suppress_mode_active_listen(_u("x", mode_state="listen_active"), [], CFG).name == "mode_active_listen"
    assert suppress_mode_active_s2s(_u("x", mode_state="s2s_active"), [], CFG).name == "mode_active_s2s"
    assert suppress_mode_global_mute(_u("x", mode_state="global_mute"), [], CFG).name == "mode_global_mute"
    # a mixed multi-mode string still matches by token
    assert suppress_mode_global_mute(_u("x", mode_state="listen_active global_mute"), [], CFG) is not None


# ── PR3a: unanswered-question look-ahead + deferral lock ──

def test_unanswered_question_emits_after_window_and_enqueues():
    st = EngineState()
    assert unanswered_question_update(st, _u("what time is it?", id=1, ts=100.0), CFG) is None
    assert st.pending_questions == [(1, 100.0)]                # enqueued, nothing stale yet
    hit = unanswered_question_update(st, _u("mm", id=2, ts=106.0), CFG)   # 6s > 5s window
    assert hit is not None and hit.name == "unanswered_question"
    assert st.pending_questions == []                          # emitted -> dropped


def test_unanswered_question_dropped_when_answered_in_window():
    st = EngineState()
    unanswered_question_update(st, _u("where are we?", id=1, ts=100.0), CFG)
    hit = unanswered_question_update(st, _u("we are on step three", id=2, ts=102.0), CFG)
    assert hit is None and st.pending_questions == []          # substantive reply within 5s answers it


def test_unknown_speaker_not_registered():
    assert "unknown_speaker" not in SOFT_TRIGGERS              # deferred: no resolved identity field


def test_pending_questions_is_bounded():
    st = EngineState()
    for i in range(80):                                        # a pathological question-burst
        unanswered_question_update(st, _u("q?", id=i, ts=100.0 + i * 0.01), CFG)
    assert len(st.pending_questions) <= 64                     # capped, does not grow unbounded
