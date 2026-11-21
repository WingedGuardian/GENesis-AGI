"""fuzzy_ratio_at_least: cheap upper bounds before difflib's ratio().

The helper must give exactly what ``SequenceMatcher(None, a, b).ratio() >= floor``
gives, for every pair: the bounds may only skip pairs that cannot reach the
floor. These tests compare it, and both call sites, against brute force.
Generic fixtures only.
"""

from __future__ import annotations

import difflib
import random

import pytest

from genesis.memory import entity_adjudication as adj
from genesis.memory import entity_registry as reg

T = 0.85


def _brute(a: str, b: str, floor: float) -> float | None:
    r = difflib.SequenceMatcher(None, a, b).ratio()
    return r if r >= floor else None


def _mutate(rng: random.Random, s: str, alphabet: str) -> str:
    chars = list(s)
    for _ in range(rng.randint(0, 4)):
        op = rng.choice("sid")
        pos = rng.randint(0, len(chars))
        if op == "s" and chars:
            chars[min(pos, len(chars) - 1)] = rng.choice(alphabet)
        elif op == "i":
            chars.insert(pos, rng.choice(alphabet))
        elif op == "d" and chars:
            del chars[min(pos, len(chars) - 1)]
    return "".join(chars)


def _corpus(seed: int = 4242) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    pairs: list[tuple[str, str]] = []
    for _ in range(1500):
        # A small alphabet makes many pairs pass quick_ratio and fail ratio.
        alphabet = rng.choice(["ab", "abc", "abcdefgh", "abcdefghijklmnop qrstuvwxyz"])
        a = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        b = (
            _mutate(rng, a, alphabet)
            if rng.random() < 0.6
            else "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        )
        pairs.append((a, b))
    # Same letters, different order: passes quick_ratio, fails ratio.
    pairs += [("abcdefghij", "jihgfedcba"), ("listen server", "silent revers")]
    # Over 200 characters, so difflib's autojunk heuristic applies.
    long_a = "".join(rng.choice("abcde") for _ in range(260))
    pairs += [(long_a, _mutate(rng, long_a, "abcde")), (long_a, long_a[::-1])]
    pairs += [("", ""), ("", "x"), ("x", "")]
    return pairs


def test_matches_brute_force_across_floors():
    for a, b in _corpus():
        for floor in (0.5, 0.7, T, 0.9, 0.95, 1.0):
            assert reg.fuzzy_ratio_at_least(a, b, floor) == _brute(a, b, floor), (a, b, floor)


@pytest.mark.parametrize(
    "a,b",
    [
        # 20 + 20 characters, 17 matched: ratio is exactly 2*17/40.
        ("abcdefghijklmnopqrst", "abcdefghijklmnopqXYZ"),
        # 17 + 23 characters, a prefix: the length bound itself is exactly 0.85.
        ("abcdefghijklmnopq", "abcdefghijklmnopqrstuvw"),
    ],
)
def test_pair_exactly_at_the_threshold_is_kept(a, b):
    assert difflib.SequenceMatcher(None, a, b).ratio() == T  # the fixture is on the edge
    assert reg.fuzzy_ratio_at_least(a, b, T) == T


def test_length_bound_skips_without_building_a_matcher(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("SequenceMatcher built for a pair the length bound rules out")

    monkeypatch.setattr(reg.difflib, "SequenceMatcher", _boom)
    assert reg.fuzzy_ratio_at_least("aaaaa", "a" * 50, T) is None


def test_closest_picks_the_same_entity_as_brute_force():
    rng = random.Random(7)
    pool = ["".join(rng.choice("abcdef") for _ in range(rng.randint(6, 14))) for _ in range(120)]
    # Duplicate scores, so the last-tie-wins rule is exercised.
    pool += ["abcdefab", "abcdefab", "abcdefba"]
    cands = [(n, f"e{i}", "concept") for i, n in enumerate(pool)]

    def brute_closest(name):
        best_id, best = None, reg.FUZZY_THRESHOLD
        for cand_norm, cid, _t in cands:
            if cand_norm == name:
                continue
            r = difflib.SequenceMatcher(None, name, cand_norm).ratio()
            if r >= best:
                best_id, best = cid, r
        return best_id

    queries = [_mutate(rng, rng.choice(pool), "abcdef") for _ in range(150)] + ["abcdefaa"]
    for q in queries:
        assert reg._closest(q, cands) == brute_closest(q), q


def test_sweep_pairs_unchanged_against_brute_force(monkeypatch):
    rng = random.Random(11)
    seeds = ["".join(rng.choice("abcdeg") for _ in range(rng.randint(8, 16))) for _ in range(30)]
    # Near-duplicates of a few seeds, so some pairs clear the threshold.
    names = seeds + [_mutate(rng, rng.choice(seeds), "abcdeg") for _ in range(120)]
    cands = [(n, f"id{i:03d}", "concept") for i, n in enumerate(names)]
    group = {"cluster": cands, "person": [], "org": []}

    with_bounds = adj._compute_sweep_pairs(cands[:60], group, set(), 10_000)
    monkeypatch.setattr(adj, "fuzzy_ratio_at_least", _brute)
    brute = adj._compute_sweep_pairs(cands[:60], group, set(), 10_000)

    assert with_bounds == brute
    assert with_bounds  # the fixture produces pairs, so the comparison means something
