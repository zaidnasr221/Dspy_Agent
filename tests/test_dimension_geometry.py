from facade_dspy.dimension_geometry import dimension_bars, profile_owners


def test_dimension_bar_rejoins_around_printed_value():
    strokes = [[(.2, .29, .1), (.32, .5, .1), (.1, .2, .1)], []]
    assert list(dimension_bars([.29, .09, .32, .11], strokes)) == [(0, .2, .5, .1)]


def test_profile_depth_rejects_glazing_cavity_with_matching_contour():
    contours = [[(.3, .55, .5)], []]
    profile = dict(id='sash', detail_id='section', bbox=[.3, .5, .55, .56])
    front_pane = dict(detail_id='section', bbox=[.26, .38, .28, .7])
    rear_pane = dict(detail_id='section', bbox=[.55, .38, .57, .7])

    assert list(profile_owners(0, .3, .55, .2, contours, [profile], [front_pane]))[0][0] == 'sash'
    assert not list(profile_owners(0, .3, .55, .2, contours, [profile], [front_pane, rear_pane]))
