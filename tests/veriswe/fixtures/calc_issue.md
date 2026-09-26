# median() returns the wrong value for even-length inputs

`calc.stats.median([1, 2, 3, 4])` returns `3`, but the median of an even number of values should be the
mean of the two middle values, i.e. `2.5`. Odd-length inputs work fine.
