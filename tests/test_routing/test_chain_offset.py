"""Tests for chain_offset rotation logic (B1).

Tests the rotation math directly rather than constructing a full Router,
since chain rotation is a simple list operation applied inside route_call.
"""



def _rotate_chain(chain: list[str], offset: int) -> list[str]:
    """Reproduce the rotation logic from Router.route_call."""
    if not chain or not offset:
        return list(chain)
    n = offset % len(chain)
    return chain[n:] + chain[:n]


def test_offset_zero_unchanged():
    """chain_offset=0 returns original order."""
    assert _rotate_chain(["A", "B", "C"], 0) == ["A", "B", "C"]


def test_offset_one():
    """chain_offset=1 starts at the 2nd provider."""
    assert _rotate_chain(["A", "B", "C", "D", "E"], 1) == ["B", "C", "D", "E", "A"]


def test_offset_two():
    """chain_offset=2 starts at the 3rd provider."""
    assert _rotate_chain(["A", "B", "C", "D", "E"], 2) == ["C", "D", "E", "A", "B"]


def test_offset_wraps_modulo():
    """chain_offset > len(chain) wraps correctly."""
    assert _rotate_chain(["A", "B", "C"], 7) == ["B", "C", "A"]  # 7 % 3 = 1


def test_offset_equal_to_length():
    """chain_offset == len(chain) is equivalent to offset=0."""
    assert _rotate_chain(["A", "B", "C"], 3) == ["A", "B", "C"]


def test_single_provider_unaffected():
    """Single-provider chain is unaffected by any offset."""
    for offset in [0, 1, 5, 100]:
        assert _rotate_chain(["A"], offset) == ["A"]


def test_distillation_round_robin():
    """Simulate 10 chunks across 5 providers — each gets 2 chunks."""
    providers = ["cerebras", "groq", "gemini", "mistral", "deepseek"]
    assignments = {}
    for chunk_idx in range(10):
        rotated = _rotate_chain(providers, chunk_idx)
        primary = rotated[0]
        assignments.setdefault(primary, []).append(chunk_idx)

    # Each provider should be primary for exactly 2 chunks
    for p in providers:
        assert len(assignments[p]) == 2, f"{p} got {assignments[p]}"
