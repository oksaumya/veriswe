"""Tiny statistics helpers."""


def mean(values):
    values = list(values)
    if not values:
        raise ValueError("mean() of empty data")
    return sum(values) / len(values)


def median(values):
    data = sorted(values)
    if not data:
        raise ValueError("median() of empty data")
    mid = len(data) // 2
    return data[mid]
