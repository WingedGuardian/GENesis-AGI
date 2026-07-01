from intervals import merge_intervals, total_coverage, free_slots, longest_busy_run


def test_merge_simple():
    assert merge_intervals([(1, 3), (2, 6), (8, 10), (15, 18)]) == [(1, 6), (8, 10), (15, 18)]


def test_merge_adjacent():
    assert merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]


def test_merge_contained():
    assert merge_intervals([(1, 10), (2, 3)]) == [(1, 10)]


def test_merge_empty():
    assert merge_intervals([]) == []


def test_total_coverage():
    assert total_coverage([(1, 3), (2, 6), (8, 10)]) == 7


def test_total_coverage_contained():
    assert total_coverage([(1, 10), (2, 3)]) == 9


def test_free_slots_basic():
    assert free_slots([(2, 4), (6, 8)], 0, 10) == [(0, 2), (4, 6), (8, 10)]


def test_free_slots_empty_busy():
    assert free_slots([], 0, 5) == [(0, 5)]


def test_free_slots_full():
    assert free_slots([(0, 10)], 0, 10) == []


def test_free_slots_overlap():
    assert free_slots([(1, 3), (2, 5)], 0, 6) == [(0, 1), (5, 6)]


def test_longest_busy_run():
    assert longest_busy_run([(1, 4), (5, 6), (7, 12)]) == 5


def test_longest_busy_run_merged():
    assert longest_busy_run([(1, 3), (2, 10)]) == 9
