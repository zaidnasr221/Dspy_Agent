from facade_dspy.exports import verified_contacts, verified_insertions


BOX = [.2, .2, .3, .3]
SCOPE = [0, 0, 1, 1]


def test_retains_contact_without_confirming_sealing():
    claim = dict(source='g', target='glass', predicate='seals_against', condition=None, bbox=BOX)
    check = dict(claim, claim_id='r1', reason='The gasket meets the pane, but compression is unclear.')
    recovered = verified_contacts([claim], [check], {'r1': 'uncertain'}, set(), SCOPE)
    assert [r['predicate'] for r in recovered] == ['contacts']
    assert not verified_contacts([claim], [dict(check, observed_contact=False)], {'r1': 'uncertain'}, set(), SCOPE)
    assert not verified_contacts([claim], [check], {'r1': 'unsupported'}, set(), SCOPE)


def test_corrected_chamber_requires_same_detail_and_located_evidence():
    claim = dict(source='cleat', target='frame', predicate='inserted_into', condition=None, bbox=BOX)
    check = dict(claim, claim_id='r1',
                 reason='The chamber containing cleat 17804 belongs to vent sash 32444, not vent frame 32443.')
    nodes = dict(cleat=dict(id='cleat', article_id='17804', detail_id='d', bbox=BOX),
                 sash=dict(id='sash', article_id='32444', detail_id='d', bbox=BOX))
    recovered = verified_insertions([claim], [check], {'r1': 'unsupported'}, set(), SCOPE, nodes)
    assert [r['target'] for r in recovered] == ['sash']
    nodes['sash']['detail_id'] = 'another-section'
    assert not verified_insertions([claim], [check], {'r1': 'unsupported'}, set(), SCOPE, nodes)
