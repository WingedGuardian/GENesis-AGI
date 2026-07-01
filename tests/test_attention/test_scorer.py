"""Direct unit tests for score composition + activation resolution."""
from genesis.attention.scorer import resolve_activation, soft_relevance, stickiness_multiplier
from genesis.attention.types import Activation, TriggerHit, TriggerKind


def _h(name, contrib=0.0, kind=TriggerKind.SOFT) -> TriggerHit:
    return TriggerHit(name, kind, contrib)


def test_soft_relevance_sums_contributions():
    assert soft_relevance([_h("a", 0.3), _h("b", 0.4)]) == 0.7
    assert soft_relevance([]) == 0.0


def test_resolve_none_below_threshold():
    assert resolve_activation(hard_hits=[], suppressor_hits=[], effective=0.3, threshold=0.6) is None


def test_resolve_soft_over_threshold():
    assert resolve_activation(hard_hits=[], suppressor_hits=[], effective=0.7, threshold=0.6) == Activation.SOFT


def test_resolve_hard_on_hard_hit():
    hard = [_h("ambient_name", kind=TriggerKind.HARD)]
    assert resolve_activation(hard_hits=hard, suppressor_hits=[], effective=0.0, threshold=0.6) == Activation.HARD


def test_suppressor_precedence():
    supp = [_h("explicit_dismissal", kind=TriggerKind.SUPPRESSOR)]
    # a would-fire soft is vetoed -> SUPPRESSED
    assert resolve_activation(hard_hits=[], suppressor_hits=supp, effective=0.7, threshold=0.6) == Activation.SUPPRESSED
    # an explicit-invite summons beats the suppressor -> HARD
    inv = [_h("explicit_invite", kind=TriggerKind.HARD)]
    assert resolve_activation(hard_hits=inv, suppressor_hits=supp, effective=0.0, threshold=0.6) == Activation.HARD
    # a lone suppressor with nothing to suppress -> no event
    assert resolve_activation(hard_hits=[], suppressor_hits=supp, effective=0.1, threshold=0.6) is None


# ── PR3a: decay ──

def test_stickiness_full_when_on_topic():
    assert stickiness_multiplier(1.3, 0.0, 30.0) == 1.3


def test_stickiness_half_decay():
    # 15s off-topic of a 30s window -> halfway from 1.3 to 1.0 = 1.15
    assert round(stickiness_multiplier(1.3, 15.0, 30.0), 4) == 1.15


def test_stickiness_floors_at_window_and_beyond():
    assert stickiness_multiplier(1.3, 30.0, 30.0) == 1.0
    assert stickiness_multiplier(1.3, 100.0, 30.0) == 1.0   # clamped, never below floor


def test_stickiness_none_off_topic_is_floor():
    # first utt of a session (no prior relevant utt) -> no bonus AND no crash (BLOCKER-1)
    assert stickiness_multiplier(1.3, None, 30.0) == 1.0


def test_stickiness_zero_window_safe():
    assert stickiness_multiplier(1.3, 5.0, 0.0) == 1.0   # no div-by-zero


def test_stickiness_never_below_floor_when_misconfigured():
    # base < floor (decay_floor_mult > session_stickiness_mult) must NOT dip below floor
    assert stickiness_multiplier(0.8, 0.0, 30.0, floor=1.0) == 1.0
    assert stickiness_multiplier(0.8, 15.0, 30.0, floor=1.0) == 1.0
