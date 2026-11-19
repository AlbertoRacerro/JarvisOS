import scratch

assert scratch.merge_sorted([1, 4, 7], [2, 3, 8]) == [1, 2, 3, 4, 7, 8]
assert scratch.merge_sorted([], [5, 6]) == [5, 6]
