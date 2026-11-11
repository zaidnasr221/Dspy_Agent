"""JSON views of committed article observations and assembly relationships."""
from collections import defaultdict
import math

from . import rules


ARTICLE_FIELDS = ('depth', 'edge_spacing', 'spacing', 'quantity', 'material', 'finish', 'color')
TABLE_FIELDS = {'depth': 'depth', 'profile depth': 'depth', 'article depth': 'depth',
                'edge spacing': 'edge_spacing', 'c/c spacing': 'spacing', 'centre-to-centre spacing': 'spacing',
                'quantity': 'quantity', 'qty': 'quantity', 'material': 'material',
                'finish': 'finish', 'color': 'color', 'colour': 'color'}
RELATIONS = {'clamped_by': 'clamped', 'bonded_to': 'bonded', 'fixed_to': 'fixed',
             'fastened_by': 'fixed', 'supports': 'supported', 'supported_by': 'supported',
             'moves_relative_to': 'movement', 'inserted_into': 'insertion',
             'clipped_into': 'insertion'}
FASTENER_KINDS = {'fastener', 'screw', 'bolt', 'rivet', 'anchor', 'stud', 'threaded_rod',
                  'anchor_bolt', 'blind_rivet', 'self_tapping_screw', 'machine_screw'}


def _kind(value):
    return rules.norm(value).replace('-', '_').replace(' ', '_')


def _edge_distance(measurement):
    value = measurement['value']
    reference = measurement.get('edge_reference') or {}
    values = value if isinstance(value, list) else [value]
    numeric = len(values) in (1, 2) and all(type(v) in (int, float) and v >= 0 for v in values)
    distance = None
    if numeric and values == sorted(values) and reference.get('rail_end') and measurement['unit']:
        if reference.get('measured_from') == 'rail_end':
            distance = value
        elif (reference.get('measured_from') == 'dimension_line'
              and type(reference.get('offset')) in (int, float)
              and measurement['unit'] and reference.get('offset_unit')
              and rules.norm(measurement['unit']) == rules.norm(reference['offset_unit'])):
            corrected = [v + reference['offset'] for v in values]
            if all(math.isfinite(v) and v >= 0 for v in corrected):
                distance = corrected if isinstance(value, list) else corrected[0]
    return dict(measurement, value=distance, source_value=value,
                calculation_status='end_to_centre' if distance is not None else 'unresolved_reference')


def _article_fields(observations, measurements, tables):
    fields = {name: [] for name in ARTICLE_FIELDS}
    for occurrence in observations:
        for name in ('quantity', 'material', 'finish', 'color'):
            if occurrence.get(name) is not None:
                fields[name].append(dict(value=occurrence[name], provenance=occurrence['provenance'],
                                         occurrence_id=occurrence['canonical_id']))
    for measurement in measurements:
        name = rules.norm(measurement['property']).replace(' ', '_')
        if name == 'edge_spacing':
            fields[name].append(_edge_distance(measurement))
        elif name in fields:
            fields[name].append(measurement)
    for table in tables:
        for cell in table['fields']:
            name = TABLE_FIELDS.get(rules.norm(cell['header']).replace('_', ' '))
            if not name or cell['value'] is None:
                continue
            fact = dict(value=cell['value'], unit=cell['unit'], provenance=table['provenance'],
                        source_header=cell['header'], footnotes=table['footnotes'])
            # A table value alone does not identify a rail end or its reference line.
            if name == 'edge_spacing':
                fact.update(value=None, source_value=cell['value'], calculation_status='unresolved_reference')
            fields[name].append(fact)
    for name in ('material', 'finish', 'color'):
        values = {rules.norm(fact['value']): fact['value'] for fact in fields[name]}
        fields[name + '_observations'] = fields[name]
        fields[name] = next(iter(values.values())) if len(values) == 1 else None
    return fields


def build_databases(components, entities, details, measurements, relations, instructions):
    detail_by_id = {detail['canonical_id']: detail for detail in details}
    nodes = {node['canonical_id']: node for node in entities}
    by_subject = defaultdict(list)
    for measurement in measurements:
        by_subject[measurement['canonical_subject_id']].append(measurement)
    by_detail = defaultdict(list)
    for instruction in instructions:
        by_detail[instruction['canonical_detail_id']].append(instruction)
    by_article = defaultdict(list)
    for node in entities:
        if _kind(node['kind']) in rules.NON_COMPONENT_KINDS or node['entity_class'] in {'connection', 'machining_feature'}:
            continue
        by_article[node['canonical_component_id'] or node['canonical_id']].append(node)
    article_ids = {component['canonical_id']: component for component in components}
    articles = []
    for key in dict.fromkeys([*article_ids, *by_article]):
        occurrences = by_article[key]
        source_rows = article_ids.get(key, {}).get('observations', [])
        table_observations = [
            dict(literal_article_id=row['literal_article_id'], footnotes=row['footnotes'],
                 provenance=row['provenance'], fields=[
                     dict(header=h, unit=u, value=v)
                     for h, u, v in zip(row['headers'], row['units'], row['cells'])])
            for row in source_rows if row['kind'] == 'table_reference'
        ]
        literal = next((node['article_id'] for node in occurrences if node['article_id']), None)
        if not literal and source_rows:
            literal = source_rows[0]['literal_article_id']
        roles = {node.get('profile_role') or node['kind'] for node in occurrences}
        roles.update(cell['value'] for table in table_observations for cell in table['fields']
                     if rules.norm(cell['header']) in {'profile role', 'profile class'} and cell['value'])
        article_measurements = [m for node in occurrences for m in by_subject[node['canonical_id']]]
        positioned = [dict(node, context=detail_by_id[node['canonical_detail_id']]) for node in occurrences]
        articles.append(dict(
            profile_id=literal, profile_role=next(iter(roles)) if len(roles) == 1 else None,
            record_id=key, article_types=sorted({n['article_type'] for n in occurrences if n.get('article_type')}),
            occurrences=positioned,
            profile_geometry=[dict(occurrence_id=node['canonical_id'], provenance=node['provenance'],
                                   drawing_refs=node['context']['drawing_refs'])
                              for node in positioned if node['context'].get('view_type') == 'profile_2d'],
            table_observations=table_observations, measurements=article_measurements,
            **_article_fields(occurrences, article_measurements, table_observations),
        ))

    assemblies = []
    for claim in relations:
        source, target = (nodes[claim[f'canonical_{side}_id']] for side in ('source', 'target'))
        # The model's explicit relation_type wins; otherwise it follows from the controlled predicate.
        relation = claim.get('relation_type') or RELATIONS.get(claim['canonical_predicate'], 'other')
        fastener = nodes.get(claim.get('canonical_fastener_id'))
        if relation == 'fixed' and fastener is None:
            endpoints = [target] if claim['canonical_predicate'] == 'fastened_by' else [source, target]
            candidates = [node for node in endpoints if _kind(node['kind']) in FASTENER_KINDS]
            fastener = candidates[0] if len(candidates) == 1 else None
        assignment = None
        if (relation == 'fixed' and fastener and _kind(fastener['kind']) in FASTENER_KINDS
                and fastener['canonical_detail_id'] == source['canonical_detail_id'] == target['canonical_detail_id']):
            assignment = dict(profile_id=fastener['article_id'], type=fastener['kind'],
                              label=fastener['label'], occurrence_id=fastener['canonical_id'],
                              provenance=fastener['provenance'])
        assemblies.append(dict(
            claim, relation=relation, fastener_assigned=assignment,
            clarification=(claim.get('relation_clarification') or claim['evidence'] or claim['predicate']) if relation == 'other' else None,
            source_component=source, target_component=target,
            context=detail_by_id[source['canonical_detail_id']],
            detail_measurements=by_subject[source['canonical_detail_id']],
            detail_instructions=by_detail[source['canonical_detail_id']],
        ))
    return articles, assemblies
