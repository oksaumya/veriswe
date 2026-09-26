import pytest

from calc.stats import mean, median


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_mean_empty():
    with pytest.raises(ValueError):
        mean([])


def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_median_empty():
    with pytest.raises(ValueError):
        median([])
