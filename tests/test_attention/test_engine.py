"""Pure-fold engine tests: determinism, sessionization, window, cooldown, clarity
weighting, suppressor precedence, blip handling."""
from genesis.attention.config import AttentionConfig
from genesis.attention.engine import evaluate
from genesis.attention.types import Activation, AmbientUtterance, EngineState


def make_config(**over) -> AttentionConfig:
    raw = {
        "version": "test",
        "aliases": ["genesis"],
        "domain_keywords": ["routing"],
        "known_entities": ["Alice"],
        "lexical_patterns": {"help_seeking": [r"\bi'?m stuck\b", r"\bhow do i\b"]},
        "suppressor_patterns": {"explicit_dismissal": [r"never mind", r"not you", r"just between us"]},
        "weights": {"question": 0.3, "help_seeking": 0.35, "multi_speaker": 0.4,
                    "is_user": 0.1, "domain_keyword": 0.3, "known_entity": 0.25},
        "state_modifiers": {"session_gap_s": 30, "cooldown_s": 30, "cooldown_raise": 0.2,
                            "session_stickiness_mult": 1.3, "context_cap_s": 12, "context_window_s": 8},
        "thresholds": {"soft_perk": 0.6, "l15_graduation": 0.4},
        "suppressors_enabled": ["explicit_dismissal"],
    }
    raw.update(over)
    return AttentionConfig.from_dict(raw)


def make_utt(id, ts, text="", *, is_user=None, speaker_total=None, rms=0.2,
             duration_s=5.0, frac_lt_1=0.0, n_tokens=20, mode_state="unknown",
             has_audio=True) -> AmbientUtterance:
    # defaults = clean / high-clarity so tests isolate the trigger + threshold logic.
    return AmbientUtterance(
        id=id, ts=ts, text=text, duration_s=duration_s, is_user=is_user,
        speaker_total=speaker_total, n_tokens=n_tokens, frac_lt_1=frac_lt_1, rms=rms,
        mode_state=mode_state, source="test", has_audio=has_audio,
    )


def run(utts, config):
    state, events = EngineState(), []
    for u in utts:
        state, ev = evaluate(u, state, config)
        if ev is not None:
            events.append(ev)
    return events


def test_determinism_same_input_same_events():
    cfg = make_config()
    utts = [make_utt(1, 100.0, "hello there?"),
            make_utt(2, 101.0, "we should decide?", speaker_total=2),
            make_utt(3, 200.0, "routing question?")]
    assert run(utts, cfg) == run(utts, cfg)


def test_hard_ambient_name_activation():
    ev = run([make_utt(1, 100.0, "hey genesis look at this")], make_config())
    assert len(ev) == 1 and ev[0].activation == Activation.HARD
    assert any(h.name == "ambient_name" for h in ev[0].triggers_fired)


def test_soft_fires_over_threshold():
    ev = run([make_utt(1, 100.0, "what do you think?", speaker_total=2)], make_config())
    assert len(ev) == 1 and ev[0].activation == Activation.SOFT  # question .3 + multi .4 = .7 >= .6


def test_soft_no_fire_below_threshold():
    assert run([make_utt(1, 100.0, "what?")], make_config()) == []  # question .3 < .6


def test_clarity_weighting_gates_garbled_but_passes_clean():
    cfg = make_config()
    garbled = make_utt(1, 100.0, "what?", speaker_total=2, rms=0.095, duration_s=2.0, frac_lt_1=0.5, n_tokens=4)
    assert run([garbled], cfg) == []              # relevance .7 x clarity .5 = .35 < .6
    clean = make_utt(1, 100.0, "what?", speaker_total=2)  # clarity ~1 -> .7 >= .6
    assert len(run([clean], cfg)) == 1


def test_suppressor_vetoes_soft_fire():
    ev = run([make_utt(1, 100.0, "what do you think? never mind", speaker_total=2)], make_config())
    assert len(ev) == 1 and ev[0].activation == Activation.SUPPRESSED
    assert "explicit_dismissal" in ev[0].suppressors


def test_explicit_invite_beats_suppressor():
    ev = run([make_utt(1, 100.0, "ask genesis, never mind actually")], make_config())
    assert len(ev) == 1 and ev[0].activation == Activation.HARD


def test_lone_suppressor_emits_nothing():
    assert run([make_utt(1, 100.0, "never mind")], make_config()) == []


def test_empty_suppressors_enabled_disables_all_suppressors():
    # allowlist semantics: [] means NO suppressors run (regression: `or None` misread [] as "all").
    cfg = make_config(suppressors_enabled=[])
    ev = run([make_utt(1, 100.0, "what do you think? never mind", speaker_total=2)], cfg)
    assert len(ev) == 1 and ev[0].activation == Activation.SOFT  # dismissal ignored -> soft fire stands
    assert ev[0].suppressors == ()


def test_blip_ignored_and_not_windowed():
    cfg = make_config()
    state = EngineState()
    state, ev = evaluate(make_utt(1, 100.0, "hey genesis", rms=0.01, duration_s=0.3, n_tokens=1), state, cfg)
    assert ev is None                 # blip short-circuits even with an alias
    assert len(state.window) == 0     # never added to the window


def test_text_only_short_utterance_not_blipped():
    # a text-only (has_audio=False) source has no rms — a short utt must be EVALUATED,
    # not dropped as a near-silence physics blip (rms=0 + duration<1 blips audio rows).
    cfg = make_config()
    state = EngineState()
    utt = make_utt(1, 100.0, "hey genesis look at this", rms=0.0, duration_s=0.5,
                   n_tokens=5, has_audio=False)
    state, ev = evaluate(utt, state, cfg)
    assert ev is not None and ev.activation == Activation.HARD
    assert len(state.window) == 1


def test_new_session_on_gap_clears_window():
    cfg = make_config()  # session_gap_s = 30
    state = EngineState()
    state, _ = evaluate(make_utt(1, 100.0, "routing talk", speaker_total=2), state, cfg)
    assert state.session_id == "s1"
    state, _ = evaluate(make_utt(2, 140.0, "different topic"), state, cfg)  # 40s gap
    assert state.session_id == "s2" and len(state.window) == 1


def test_window_evicts_beyond_cap():
    cfg = make_config()  # context_cap_s = 12
    state = EngineState()
    for u in [make_utt(1, 100.0, "a"), make_utt(2, 105.0, "b"), make_utt(3, 116.0, "c")]:
        state, _ = evaluate(u, state, cfg)
    ids = [w.id for w in state.window]
    assert 1 not in ids and 2 in ids and 3 in ids  # utt1 (16s old) evicted, utt2 (11s) kept


def test_cooldown_raises_threshold():
    cfg = make_config(
        weights={"question": 0.5},
        thresholds={"soft_perk": 0.4, "l15_graduation": 0.2},
        state_modifiers={"cooldown_s": 30, "cooldown_raise": 0.3,
                         "session_stickiness_mult": 1.0, "session_gap_s": 300, "context_cap_s": 12},
    )
    state = EngineState()  # thread state explicitly (the fold contract), not via in-place mutation
    state, e1 = evaluate(make_utt(1, 100.0, "q?"), state, cfg)
    assert e1.activation == Activation.SOFT                    # .5 >= .4
    state, e2 = evaluate(make_utt(2, 105.0, "q?"), state, cfg)
    assert e2 is None                                          # cooldown bar .7; .5 + topic_continuation .15 = .65 < .7
    state, e3 = evaluate(make_utt(3, 140.0, "q?"), state, cfg)
    assert e3 is not None and e3.activation == Activation.SOFT  # 40s > cooldown -> fires again


def test_event_carries_source_of_trigger_utterance():
    # device provenance must reach the event (else the store can't tell OMI from home edge)
    ev = run([make_utt(7, 100.0, "hey genesis check this")], make_config())[0]
    assert ev.source == "test"   # make_utt's source, threaded onto the event


def test_event_carries_refs_not_text():
    ev = run([make_utt(7, 100.0, "hey genesis secret plan")], make_config())[0]
    # AttentionEvent must expose no raw transcript — window_ref is ids + ts only.
    assert ev.window_ref.utt_ids == (7,)
    assert not hasattr(ev, "text")
    assert "secret plan" not in repr(ev.window_ref)


# ── PR3a: decay, dilution, new suppressors, look-ahead, determinism ──

def test_new_triggers_dilute_multi_speaker():
    cfg = make_config(
        weights={"multi_speaker": 0.4, "question": 0.3, "decision": 0.3},
        lexical_patterns={"question": [r"\?"], "decision": [r"\bwe should\b"]},
    )
    # a bare multi-speaker window no longer clears the 0.6 bar on its own (0.40 < 0.60)
    assert run([make_utt(1, 100.0, "some chatter", speaker_total=2)], cfg) == []
    # multi-speaker + real intent does (0.40 + 0.30 + 0.30 = 1.00)
    rich = run([make_utt(1, 100.0, "we should ship?", speaker_total=2)], cfg)
    assert len(rich) == 1 and rich[0].activation == Activation.SOFT


def test_decay_reduces_stickiness_over_off_topic_gap():
    cfg = make_config(
        weights={"question": 0.5, "topic_continuation": 0.15},
        lexical_patterns={"question": [r"\?"]},
        thresholds={"soft_perk": 0.6, "l15_graduation": 0.4},
        # unanswered_question pushed out of range so decay is ISOLATED from that bonus
        # (a lingering "?" would otherwise add +unanswered_question to the far case and mask decay).
        state_modifiers={"session_gap_s": 300, "cooldown_s": 0, "session_stickiness_mult": 1.3,
                         "decay_window_s": 30, "decay_floor_mult": 1.0, "context_cap_s": 300,
                         "unanswered_question_s": 100000},
    )
    near = run([make_utt(1, 100.0, "q?"), make_utt(2, 101.0, "q?")], cfg)   # 1s gap: near-full stickiness
    far = run([make_utt(1, 100.0, "q?"), make_utt(2, 128.0, "q?")], cfg)    # 28s gap: decayed toward 1.0
    assert near[-1].score > far[-1].score


def test_last_relevance_ts_advances_only_on_relevant():
    cfg = make_config(weights={"question": 0.5}, lexical_patterns={"question": [r"\?"]})
    state = EngineState()
    state, _ = evaluate(make_utt(1, 100.0, "nothing here"), state, cfg)   # no trigger, window len 1
    assert state.last_relevance_ts is None                                # clock NOT advanced
    state, _ = evaluate(make_utt(2, 101.0, "q?"), state, cfg)             # question fires
    assert state.last_relevance_ts == 101.0                               # advanced on the relevant utt


def test_low_asr_suppressor_off_by_default_on_by_config():
    garbled = make_utt(1, 100.0, "what do you think?", speaker_total=2,
                       frac_lt_1=0.5, rms=0.2, duration_s=5.0, n_tokens=20)
    off = run([garbled], make_config(suppressors_enabled=["explicit_dismissal"]))
    assert off and all(e.activation != Activation.SUPPRESSED for e in off)   # clarity dampens, no veto
    on = run([garbled], make_config(suppressors_enabled=["explicit_dismissal", "low_asr_confidence"]))
    assert len(on) == 1 and on[0].activation == Activation.SUPPRESSED         # frac_lt_1 0.5 >= 0.35


def test_mode_suppressor_inert_offline_but_vetoes_when_active():
    cfg = make_config(suppressors_enabled=["explicit_dismissal", "mode_active_listen"])
    # offline default mode_state="unknown" -> inert -> the soft fire stands
    assert run([make_utt(1, 100.0, "what do you think?", speaker_total=2)], cfg)[0].activation == Activation.SOFT
    # edge mode set -> veto
    active = make_utt(1, 100.0, "what do you think?", speaker_total=2, mode_state="listen_active")
    assert run([active], cfg)[0].activation == Activation.SUPPRESSED


def test_unanswered_question_fires_in_fold():
    cfg = make_config(
        weights={"unanswered_question": 0.7},
        lexical_patterns={"question": [r"\?"]},
        state_modifiers={"unanswered_question_s": 5, "session_gap_s": 300, "cooldown_s": 0,
                         "session_stickiness_mult": 1.0, "context_cap_s": 300},
        thresholds={"soft_perk": 0.6, "l15_graduation": 0.4},
    )
    ev = run([make_utt(1, 100.0, "did it work?"), make_utt(2, 108.0, "yeah anyway")], cfg)  # 8s > 5s
    assert any(h.name == "unanswered_question" for e in ev for h in e.triggers_fired)


def test_determinism_with_pending_and_decay():
    cfg = make_config(
        weights={"question": 0.5, "decision": 0.3, "unanswered_question": 0.4, "topic_continuation": 0.15},
        lexical_patterns={"question": [r"\?"], "decision": [r"\bwe should\b"]},
        state_modifiers={"session_gap_s": 300, "cooldown_s": 30, "session_stickiness_mult": 1.3,
                         "decay_window_s": 30, "context_cap_s": 300},
    )
    utts = [make_utt(1, 100.0, "should we go?"), make_utt(2, 103.0, "we should decide", speaker_total=2),
            make_utt(3, 110.0, "hmm not sure"), make_utt(4, 118.0, "we should go?")]
    assert run(utts, cfg) == run(utts, cfg)
