from facade_dspy.exports import occurrence_keys
from facade_dspy.source import band_split


def test_record_keys_do_not_depend_on_list_order():
    a = [{'p': 'fixed_to', 'n': 1}, {'p': 'seals', 'n': 2}, {'p': 'fixed_to', 'n': 1}]
    content = lambda r: [r['p'], r['n']]
    forward = occurrence_keys(a, content)
    backward = occurrence_keys(a[::-1], content)
    assert set(forward) == set(backward)
    assert len(set(forward)) == 3  # identical records stay distinct by occurrence


def test_band_split_avoids_cutting_tables():
    assert band_split([]) == .5
    assert band_split([{'bbox': [0, .4, 1, .6]}]) == .4
    assert band_split([{'bbox': [0, .1, 1, .9]}]) == .5  # no safe edge: unchanged midline
    assert band_split([{'triage_error': 'X'}]) == .5
