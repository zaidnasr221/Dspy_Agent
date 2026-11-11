from facade_dspy.relationship_coverage import Addition, Verdict, apply_checked, proposals, region_inputs


def test_missing_screw_requires_a_reviewed_ordered_path():
    box = [.2, .2, .4, .4]
    def node(key, kind):
        return dict(id=key, detail_id='d', kind=kind, label=kind, article_id=None,
                    material=None, quantity=None, callout=None, bbox=box)

    record = dict(unit=dict(unit_id='u', pdf_page=1, bbox=[0, 0, 1, 1]),
                  extraction=dict(pdf_page=1, page_kind='detail',
                                  details=[dict(id='d', title='Section', drawing_refs=[], bbox=box)],
                                  nodes=[node('clamp', 'accessory'), node('screw', 'screw'), node('host', 'profile')],
                                  relations=[], measurements=[], tables=[], instructions=[], unresolved=[]))
    assert region_inputs(record)['orphan_fasteners'] == ['screw']
    fixing = Addition(region='u', source='clamp', target='host', predicate='fixed_to',
                      bbox=box, evidence='Threads enter host', fastener_id='screw', path=['clamp'])
    assert not proposals([fixing.model_copy(update={'path': []})], {'u': record})
    candidates = proposals([fixing, fixing], {'u': record})
    assert len(candidates) == 1
    verification = {'verdicts': []}
    apply_checked(record, verification, candidates, [])
    assert not record['extraction']['relations']
    apply_checked(record, verification, candidates, [Verdict(candidate=0, supported=True, reason='Visible threads')])
    assert record['extraction']['relations'][0]['passes_through'] == ['clamp']
    assert verification['verdicts'][0]['claim_id'] == 'r1'
    assert not region_inputs(record)['orphan_fasteners']
