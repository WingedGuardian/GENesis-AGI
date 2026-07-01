from intervals.core import merge_intervals


def longest_busy_run(busy):
    """Length of the longest contiguous busy stretch (after merging)."""
    merged = merge_intervals(busy)
    if not merged:
        return 0
    # BUG: returns the first run's length, not the longest.
    return merged[0][1] - merged[0][0]
