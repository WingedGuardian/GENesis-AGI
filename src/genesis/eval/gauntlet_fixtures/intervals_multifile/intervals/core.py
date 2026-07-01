"""Interval utilities. Some of this is buggy, and one function is a stub."""


def merge_intervals(intervals):
    """Merge overlapping or adjacent (start, end) intervals."""
    if not intervals:
        return []
    s = sorted(intervals, key=lambda x: x[0])
    res = [list(s[0])]
    for a, b in s[1:]:
        if a <= res[-1][1]:
            # BUG: a fully-contained interval shortens the merged end.
            res[-1][1] = b
        else:
            res.append([a, b])
    return [tuple(x) for x in res]


def total_coverage(intervals):
    """Total length of the real line covered by the intervals (overlaps once)."""
    return sum(b - a for a, b in merge_intervals(intervals))


def free_slots(busy, start, end):
    """Return the free (uncovered) intervals within [start, end].

    Given a list of busy (start, end) intervals, return a sorted list of
    (start, end) tuples for the gaps inside [start, end] not covered by any
    busy interval. Busy intervals may overlap each other and may extend
    outside [start, end] (clip them to the window). If the window is fully
    covered, return an empty list. If there are no busy intervals, the whole
    window is free.
    """
    raise NotImplementedError
