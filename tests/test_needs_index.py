from pipeline.s7_needs_index import normalize, combine_scores


def test_normalize_percentile_basic():
    assert normalize([10.0, 20.0, 30.0], method='percentile') == [0.0, 0.5, 1.0]


def test_normalize_percentile_all_equal():
    assert normalize([5.0, 5.0, 5.0], method='percentile') == [0.0, 0.0, 0.0]


def test_normalize_percentile_single():
    assert normalize([42.0], method='percentile') == [0.0]


def test_normalize_percentile_ties_collapse_to_bottom():
    # Tied minima (e.g. many zero-value links) must all map to 0.0, not spread up the range.
    assert normalize([0.0, 0.0, 0.0, 10.0], method='percentile') == [0.0, 0.0, 0.0, 1.0]


def test_normalize_minmax_basic():
    assert normalize([0.0, 5.0, 10.0], method='minmax') == [0.0, 0.5, 1.0]


def test_normalize_minmax_all_equal():
    assert normalize([3.0, 3.0], method='minmax') == [0.0, 0.0]


def test_normalize_minmax_single():
    assert normalize([7.0], method='minmax') == [0.0]


def test_combine_scores_basic():
    scores = combine_scores({'a': [1.0, 0.0], 'b': [0.0, 1.0]}, {'a': 0.5, 'b': 0.5})
    assert len(scores) == 2
    assert abs(scores[0] - 50.0) < 1e-9
    assert abs(scores[1] - 50.0) < 1e-9


def test_combine_scores_weights_not_summing_to_1():
    scores = combine_scores({'a': [1.0, 0.0], 'b': [0.0, 1.0]}, {'a': 1.0, 'b': 1.0})
    assert abs(scores[0] - 50.0) < 1e-9
    assert abs(scores[1] - 50.0) < 1e-9


def test_combine_scores_returns_0_to_100():
    scores = combine_scores({'x': [0.0, 0.5, 1.0]}, {'x': 1.0})
    assert scores[0] == 0.0
    assert scores[2] == 100.0
