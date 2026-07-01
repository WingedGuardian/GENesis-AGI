"""Tiny stats library. Two bugs are hiding in here."""
import math


def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    s = sorted(xs)
    n = len(s)
    # BUG: for even-length input this returns a single middle element
    # instead of the average of the two middle elements.
    return s[n // 2]


def variance(xs):
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def stddev(xs):
    # BUG: standard deviation is the square root of the variance.
    return variance(xs)
