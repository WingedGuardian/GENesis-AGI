import math

from statslib.core import mean, median, variance, stddev

DATA = [2, 4, 4, 4, 5, 5, 7, 9]  # mean 5, sample variance 32/7


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_median_even():
    assert median([1, 2, 3, 4]) == 2.5


def test_variance():
    assert math.isclose(variance(DATA), 32 / 7)


def test_stddev():
    assert math.isclose(stddev(DATA), math.sqrt(32 / 7))
