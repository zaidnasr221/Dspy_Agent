"""Independent acceptance cases for the published engineering JSON contracts."""

from facade_dspy.assembly import _edge
from facade_dspy.catalogue_databases import _edge_distance
from facade_dspy.exports import relationship_status
from facade_dspy.rules import detail_system
from facade_dspy.schema import EdgeSpacing, Measurement, Relation


def test_edge_distance_carries_rail_datum_bound_and_source():
    box = [.1, .2, .3, .4]
    raw = dict(subject='screw-2', property='edge_spacing', value=40, unit='mm', datum='second screw centre',
               condition='wide transom', bbox=box, evidence='MAX 40', rail='lower transom rail', bound='maximum',
               edge_reference=dict(rail_end='x1', measured_from='dimension_line', offset=-5, offset_unit='mm'))
    for unit, expected in [('mm', 35), (None, None)]:
        fact = Measurement.model_validate(dict(raw, unit=unit)).model_dump()
        fact['provenance'] = dict(pdf_page=42, bbox=box)
        result = EdgeSpacing.model_validate(_edge(_edge_distance(fact)))
        assert (result.value, result.printed_value, result.rail, result.datum, result.bound) == (
            expected, 40, raw['rail'], raw['datum'], 'maximum')
        assert result.evidence[0].page == 42 and result.evidence[0].bbox == box


def test_glass_retention_is_generic_and_alignment_cannot_publish():
    box = [.1, .2, .3, .4]
    glass = dict(id='infill', detail_id='section', kind='glass', bbox=box)
    plate = dict(id='plate', detail_id='section', kind='pressure plate', bbox=box)
    legacy = Relation(source='infill', target='plate', predicate='clamped_by', basis='drawing_reading',
                      condition=None, bbox=box, evidence='Plate clamps outer face of glass').model_dump()
    assert relationship_status(legacy, glass, plate, False, verification='supported')[0] == 'proposed'
    for manufacturer in ('Example Curtain Systems', 'Another Profile Supplier'):
        parts = [dict(node, manufacturer=manufacturer) for node in (glass, plate)]
        assert detail_system(parts)['type'] is None
        assert detail_system(parts, [dict(legacy, graph_eligible=True)])['type'] == 'CC'
    alignment = dict(legacy, predicate='fixed_to', fixing_evidence='alignment')
    status, reasons = relationship_status(alignment, glass, plate, False, verification='supported')
    assert status == 'unresolved' and 'fixing_engagement_not_established' in reasons
