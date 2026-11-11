from facade_dspy.compare_batch import page_records


def records(sheet, path):
    participants = [
        dict(role='fixed_part', article='Clamp'),
        dict(role='host', article='32463'),
    ]
    return page_records([
        dict(page=13, system={'type': None}, article_database=[], assembly_database=[
            dict(relation='fixed', participants=participants,
                 fastener_assigned=dict(type='screw', passes_through=path, engages_in='32463')),
            dict(relation='other', participants=[
                dict(role='source', article=sheet), dict(role='target', article='glass')]),
        ])
    ], 13)


def test_fixing_match_is_independent_of_ordered_path():
    reference = records('Silicone sealant', ['Clamp', 'Aluminium bent sheet'])
    equivalent = records('Silicone sealant on backing rod', ['Clamp', 'Aluminum bent sheet'])
    missing = records('Silicone sealant', [])
    reversed_path = records('Silicone sealant', ['Aluminium bent sheet', 'Clamp'])

    assert reference['relationships'] == equivalent['relationships']
    assert reference['fixings'] == equivalent['fixings'] == missing['fixings'] == reversed_path['fixings']
    assert reference['fixing_paths'] == equivalent['fixing_paths']
    assert not missing['fixing_paths']
    assert reference['fixing_paths'] != reversed_path['fixing_paths']
