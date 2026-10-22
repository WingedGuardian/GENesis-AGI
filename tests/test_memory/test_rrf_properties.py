"""Property tests for reciprocal rank fusion (#3187).

``_rrf_fuse`` exists twice, in ``retrieval`` and ``drift``; every property runs
against both copies, and one asserts they agree.
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from genesis.memory import drift, retrieval

PROFILE = settings(max_examples=200, derandomize=True, database=None, deadline=None)
FUSERS = pytest.mark.parametrize(
    "fuse", [retrieval._rrf_fuse, drift._rrf_fuse], ids=["retrieval", "drift"]
)

memory_id = st.sampled_from([f"m{i}" for i in range(30)])
lane = st.lists(memory_id, unique=True, max_size=25)
lanes = st.lists(lane, max_size=6)


def _close(a: dict[str, float], b: dict[str, float]) -> bool:
    return a.keys() == b.keys() and all(math.isclose(a[k], b[k], rel_tol=1e-12) for k in a)


@FUSERS
@PROFILE
@given(lanes=lanes, k=st.integers(1, 120))
def test_scores_match_exact_arithmetic(fuse, lanes: list[list[str]], k: int) -> None:
    exact: dict[str, Fraction] = {}
    for ranked in lanes:
        for rank, mid in enumerate(ranked, 1):
            exact[mid] = exact.get(mid, Fraction(0)) + Fraction(1, k + rank)
    assert _close(fuse(lanes, k=k), {m: float(v) for m, v in exact.items()})


@FUSERS
@PROFILE
@given(lanes=lanes, order=st.randoms(use_true_random=False))
def test_lane_order_does_not_change_scores(fuse, lanes: list[list[str]], order) -> None:
    shuffled = list(lanes)
    order.shuffle(shuffled)
    assert _close(fuse(lanes), fuse(shuffled))


@FUSERS
@PROFILE
@given(lanes=lanes, extra=lane)
def test_adding_a_lane_never_lowers_a_score(fuse, lanes: list[list[str]], extra: list[str]) -> None:
    before, after = fuse(lanes), fuse([*lanes, extra])
    assert all(after[m] >= before[m] for m in before)


@FUSERS
@PROFILE
@given(lanes=lanes)
def test_a_memory_first_in_every_lane_comes_out_on_top(fuse, lanes: list[list[str]]) -> None:
    lanes = [ranked for ranked in lanes if ranked]
    assume(lanes)
    top = lanes[0][0]
    lanes = [[top, *(m for m in ranked if m != top)] for ranked in lanes]
    scores = fuse(lanes)
    assert all(scores[top] > s for m, s in scores.items() if m != top)


@FUSERS
@PROFILE
@given(lanes=lanes)
def test_exact_ties_keep_first_appearance_order(fuse, lanes: list[list[str]]) -> None:
    scores = fuse(lanes)
    ranked = sorted(scores, key=scores.__getitem__, reverse=True)
    first_seen: dict[str, tuple[int, int]] = {}
    for lane_index, ranked_lane in enumerate(lanes):
        for position, mid in enumerate(ranked_lane):
            first_seen.setdefault(mid, (lane_index, position))
    for a, b in zip(ranked, ranked[1:], strict=False):
        if scores[a] == scores[b]:
            assert first_seen[a] < first_seen[b]


@PROFILE
@given(lanes=lanes, k=st.integers(1, 120))
def test_the_two_copies_agree(lanes: list[list[str]], k: int) -> None:
    assert retrieval._rrf_fuse(lanes) == drift._rrf_fuse(lanes)
    assert retrieval._rrf_fuse(lanes, k=k) == drift._rrf_fuse(lanes, k=k)
