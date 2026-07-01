"""Direct unit tests for score composition + activation resolution."""
from genesis.attention.scorer import resolve_activation, soft_relevance
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
