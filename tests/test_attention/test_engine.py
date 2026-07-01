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
             duration_s=5.0, frac_lt_1=0.0, n_tokens=20, mode_state="unknown") -> AmbientUtterance:
    # defaults = clean / high-clarity so tests isolate the trigger + threshold logic.
    return AmbientUtterance(
        id=id, ts=ts, text=text, duration_s=duration_s, is_user=is_user,
        speaker_total=speaker_total, n_tokens=n_tokens, frac_lt_1=frac_lt_1, rms=rms,
        mode_state=mode_state, source="test",
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


def test_blip_ignored_and_not_windowed():
    cfg = make_config()
    state = EngineState()
    state, ev = evaluate(make_utt(1, 100.0, "hey genesis", rms=0.01, duration_s=0.3, n_tokens=1), state, cfg)
    assert ev is None                 # blip short-circuits even with an alias
    assert len(state.window) == 0     # never added to the window


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
    state = EngineState()
    _, e1 = evaluate(make_utt(1, 100.0, "q?"), state, cfg)
    assert e1.activation == Activation.SOFT                    # .5 >= .4
    _, e2 = evaluate(make_utt(2, 105.0, "q?"), state, cfg)
    assert e2 is None                                          # in cooldown -> bar .7, .5 < .7
    _, e3 = evaluate(make_utt(3, 140.0, "q?"), state, cfg)
    assert e3 is not None and e3.activation == Activation.SOFT  # 40s > cooldown -> fires again


def test_event_carries_refs_not_text():
    ev = run([make_utt(7, 100.0, "hey genesis secret plan")], make_config())[0]
    # AttentionEvent must expose no raw transcript — window_ref is ids + ts only.
    assert ev.window_ref.utt_ids == (7,)
    assert not hasattr(ev, "text")
    assert "secret plan" not in repr(ev.window_ref)
