"""Per-detail output: ARTICLE DATABASE + ASSEMBLY DATABASE for every detail (schema.DetailOutput).

Built deterministically from the committed, verified extraction (no model call):
- articles: every part shown in the detail, with depth / edge_spacing / spacing / quantity /
  material / finish / color and all else found, taken from the detail first and then from linked
  records on other pages (tables, profile sheets), always with the source page;
- system: SG / CC / semi-SG computed from verified glazing retention relationships and
  compared with the model's reading;
- assemblies: only graph-eligible (verified, undisputed) relationships, typed as
  clamped / bonded / fixed / supported / movement / insertion / other, with fastener_assigned only
  for 'fixed'. Everything that cannot be stated with evidence goes to missing_information/unresolved.
"""
import re
from collections import Counter

from pydantic import ValidationError

from . import articles as article_index
from . import rules
from .schema import (ArticleRecord, AssemblyRecord, DetailOutput, validate_output)

FIELDS = ('depth', 'edge_spacing', 'spacing', 'quantity')
ROLE_BY_PREDICATE = {
    'fastened_by': ('fixed_part', 'fastener'), 'fixed_to': ('fixed_part', 'host'),
    'clamped_by': ('clamped_part', 'clamping_part'), 'bonded_to': ('bonded_part', 'substrate'),
    'supports': ('support', 'supported_part'), 'supported_by': ('supported_part', 'support'),
    'inserted_into': ('inserted_part', 'host'), 'clipped_into': ('inserted_part', 'host'),
    'moves_relative_to': ('moving_part', 'reference_part'),
}
RELATION_BY_PREDICATE = {'clipped_into': 'insertion', 'seals_against': 'other', 'isolates': 'other', 'spaces': 'other'}


def _ref(provenance, kind='detail', text=None, page=None):
    box = (provenance or {}).get('bbox')
    return dict(page=page or provenance['pdf_page'], kind=kind, bbox=box if box and len(box) == 4 else None,
                text=text)


def _fact_ref(fact):
    if fact.get('provenance'):
        kind = 'table' if fact.get('source_header') else 'detail'
        return _ref(fact['provenance'], kind, fact.get('evidence') or fact.get('source_header'))
    return dict(page=fact['page'], kind='other', bbox=None, text=f"linked article {fact.get('linked_from')}")


def _clean(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, list) and all(isinstance(v, (int, float)) for v in value):
        return [float(v) for v in value]
    return value if isinstance(value, (str, bool)) or value is None else str(value)


def _pick(facts, occurrence_ids):
    """Facts printed for this occurrence first, then the rest; returns (chosen, conflicting values)."""
    local = [f for f in facts if f.get('canonical_subject_id') in occurrence_ids or f.get('occurrence_id') in occurrence_ids]
    ordered = local + [f for f in facts if f not in local]
    usable = [f for f in ordered if f.get('value') is not None or f.get('source_value') is not None]
    if not usable:
        return None, []
    values = {str((f.get('value'), f.get('unit'))) for f in usable}
    return usable[0], (sorted(values) if len(values) > 1 else [])


def _value(fact, **extra):
    return dict(value=_clean(fact.get('value')), unit=fact.get('unit'), bound=fact.get('bound'),
                condition=fact.get('condition'), datum=fact.get('datum'), evidence=[_fact_ref(fact)], **extra)


def _edge(fact):
    reference = fact.get('edge_reference')
    printed = fact.get('source_value')
    offset = (reference or {}).get('offset')
    applied = None
    if reference:
        applied = ('none' if reference.get('measured_from') == 'rail_end' else
                   'added' if isinstance(offset, (int, float)) and offset > 0 else
                   'subtracted' if isinstance(offset, (int, float)) and offset < 0 else None)
    if fact.get('calculation_status') == 'unresolved_reference':
        applied = None
    return _value(fact, edge_reference=reference, rail=fact.get('rail'),
                  printed_value=_clean(printed),
                  offset_applied=applied)


def _label(node, used):
    base = node.get('label') or node.get('kind') or 'part'
    label = base if base not in used else f"{base} ({node['id']})"
    used.add(label)
    return label


def _article(record, nodes, index, used):
    node = nodes[0]
    record = article_index.for_occurrence(record, node, index)
    ids = {n['canonical_id'] for n in nodes}
    name = rules.family(node) or rules.family(dict(kind=record.get('profile_role') or ''))
    item = dict(profile_id=record.get('profile_id'), label=_label(node, used),
                occurrence_id=node['canonical_id'],
                profile_role=name or 'other', role_clarification=None if name else (node.get('kind') or node.get('label')),
                article_type=node.get('article_type') or rules.default_article_type(name),
                system=[], sources=[], conflicts=[], other={}, missing_information=[])
    for field in FIELDS:
        facts = article_index.linked_facts(record, index, field)
        if field in article_index.INSTALLATION_FIELDS:
            facts = [f for f in facts if f.get('canonical_subject_id') in ids or f.get('occurrence_id') in ids]
        if field == 'quantity':
            # A stated quantity rule takes precedence over the count seen in a drawing.
            facts = [f for f in facts if 'canonical_subject_id' in f]
        fact, conflicts = _pick(facts, ids)
        if fact is None:
            item['missing_information'].append(field)
            continue
        item[field] = _edge(fact) if field == 'edge_spacing' else _value(fact)
        if field == 'spacing':
            item[field]['rail'] = fact.get('rail')
            if not fact.get('rail') or not fact.get('unit'):
                item['missing_information'].append('spacing rail or unit unresolved')
        if field == 'edge_spacing' and fact.get('value') is None:
            item['missing_information'].append('edge_spacing reference line/rail end unresolved')
        if field == 'quantity':
            item[field].update(per=fact.get('per'), per_article=fact.get('per_article'),
                               per_occurrence_id=fact.get('per_occurrence_id'))
            if (fact.get('per') or fact.get('per_node_id')) and not fact.get('per_occurrence_id'):
                item['missing_information'].append('quantity reference occurrence unresolved')
        item['conflicts'] += [f'{field}: {c}' for c in conflicts]
    for field in ('material', 'finish', 'color'):
        facts = [dict(value=n[field], provenance=n['provenance'], occurrence_id=n['canonical_id'])
                 for n in nodes if n.get(field)] + list(record.get(field + '_observations') or [])
        fact, conflicts = _pick(facts, ids)
        if fact is None:
            item['missing_information'].append(field)
        else:
            item[field] = _value(fact)
            item['conflicts'] += [f'{field}: {c}' for c in conflicts]
    for table in record.get('table_observations', []):
        for cell in article_index.property_cells(table):
            header = rules.norm(cell['header']).replace(' ', '_')
            if not cell['value'] or header in item['other']:
                continue
            item['other'][header] = dict(value=_clean(cell['value']), unit=cell.get('unit'),
                                         evidence=[_ref(table['provenance'], 'table', cell['header'])])
    for field in article_index.other_fields(record, index):
        facts = article_index.linked_facts(record, index, field)
        fact, conflicts = _pick(facts, ids)
        if fact is not None:
            item['other'][field] = _value(fact)
            item['conflicts'] += [f'{field}: {c}' for c in conflicts]
    for n in nodes:
        item['sources'].append(_ref(n['provenance'], 'detail', n.get('callout') or n.get('label')))
    for t in record.get('table_observations', []):
        item['sources'].append(_ref(t['provenance'], 'table', ' | '.join(str(f['value']) for f in t['fields'] if f['value'])))
    for link in record.get('linked', []):
        item['sources'] += [dict(page=p, kind='other', bbox=None, text=f"linked article {link['article_key']}") for p in link['pages']]
    if record.get('link_status') == 'variant_unresolved':
        item['missing_information'].append('variant not identified: ' + ', '.join(record.get('variant_candidates', [])))
    explicit = bool(record.get('profile_id')) or bool(record.get('table_observations'))
    item['evidence_status'] = 'explicit' if explicit else 'visual'
    if not explicit:
        item['inference_reason'] = 'part drawn in the detail without a printed article ID'
        item['missing_information'].append('article id not printed')
    return item


def _relation(claim, refs, local_nodes, article_rows, system_type, unresolved, shared_fasteners=()):
    predicate = claim['canonical_predicate']
    relation = claim.get('relation_type') or claim.get('relation') or RELATION_BY_PREDICATE.get(predicate, 'other')
    source, target = claim['source_component'], claim['target_component']
    if source['canonical_id'] not in refs or target['canonical_id'] not in refs:
        unresolved.append(f"{claim['claim_id']}: relationship endpoint is not a component")
        return None
    roles = ROLE_BY_PREDICATE.get(predicate, ('source', 'target'))
    participants = [(local_nodes[p['node_id']],p['role']) for p in claim.get('participants') or []]
    participants = participants or [(source,roles[0]),(target,roles[1])]
    if any(node['canonical_id'] not in refs for node,_ in participants):
        unresolved.append(f"{claim['claim_id']}: assembly participant is not an exported component")
        return None
    item = dict(assembly_id=claim['claim_id'], detail_id=claim['context']['canonical_id'],
                page=claim['provenance']['pdf_page'], view_type=claim['context'].get('view_type'),
                relation=relation, participants=[dict(article=refs[node['canonical_id']], role=role,
                                                      occurrence_id=node['canonical_id']) for node,role in participants],
                evidence=[_ref(claim['provenance'], 'detail', claim.get('evidence'))],
                conditions=[claim['condition']] if claim.get('condition') else [],
                missing_information=[], other=dict(predicate=dict(value=claim['predicate'], evidence=[])))
    if relation == 'other':
        item['relation_clarification'] = claim.get('relation_clarification') or claim.get('clarification') or claim['predicate']
    if claim['basis'] == 'explicit_text':
        item['evidence_status'] = 'explicit'
    else:
        item['evidence_status'] = 'visual'
        item['inference_reason'] = claim.get('evidence') or 'read from the drawing'
    path = [local_nodes[i] for i in (claim.get('passes_through') or []) if i in local_nodes]
    engages = local_nodes.get(claim.get('engages_in'))
    fastener = claim.get('fastener_assigned')
    if relation == 'fixed':
        if fastener and fastener['occurrence_id'] in refs:
            node = next(n for n in local_nodes.values() if n['canonical_id'] == fastener['occurrence_id'])
            fastener_article = article_rows[node['canonical_id']]
            scoped_fields = ('edge_spacing', 'spacing', 'quantity')
            if node['canonical_id'] in shared_fasteners and any(fastener_article[field] for field in scoped_fields):
                item['missing_information'].append('article measurements not assigned to a specific fixing')
                fastener_article = dict.fromkeys(scoped_fields)
            path_ids = [n['canonical_id'] for n in path if n['canonical_id'] in refs]
            engagement_id = engages['canonical_id'] if engages and engages['canonical_id'] in refs else None
            item['fastener_assigned'] = dict(
                article_id=fastener.get('profile_id'), type=rules.family(node) or fastener.get('type'),
                occurrence_id=node['canonical_id'], size=claim.get('fastener_size'),
                passes_through=[refs[ident] for ident in path_ids], path_occurrence_ids=path_ids,
                engages_in=refs.get(engagement_id), engages_in_occurrence_id=engagement_id,
                insertion_direction=claim.get('insertion_direction'), position=claim.get('fastener_position'),
                edge_spacing=fastener_article['edge_spacing'], spacing=fastener_article['spacing'],
                quantity=fastener_article['quantity'],
                distance_offset=[dict(value=o['value'],unit=o['unit'],from_datum=o['from_datum'],
                                      to_datum=o['to_datum'],evidence=[_ref(dict(claim['provenance'],bbox=o['bbox']),
                                                                           'detail',o['evidence'])])
                                 for o in claim.get('distance_offsets') or []])
            if not engagement_id:
                item['missing_information'].append('fastener engagement part not stated')
        else:
            item['missing_information'].append('fastener not identified')
    members = [node for node,_ in participants] + path + ([engages] if engages else [])
    if system_type == 'semi-SG':
        item['system_part'] = rules.retention_system(claim, local_nodes) or rules.system_part_of(members)
    else:
        item['system_part'] = system_type
    item['_axis'] = rules.axis_of(members)
    return item


def conventional_glazing(detail, nodes, claims, source_text):
    """Use an explicit catalogue heading when a verified pressure-plate fixing shows the mechanism."""
    if detail.get('system_type') != 'CC' or not any(rules.family(node) in {'glass','infill'} for node in nodes):
        return None
    page = detail['provenance']['pdf_page']
    heading = next((row['text'] for row in source_text if row['provenance']['pdf_page']==page
                    and rules.norm(row['text']) in {'conventional glazing','conventional glazing system'}), None)
    if not heading:
        return None
    fixing = next((claim['evidence'] for claim in claims if claim.get('graph_eligible')
                   and claim.get('relation')=='fixed' and claim.get('fastener_assigned')
                   and (claim.get('evidence') or '').strip()
                   and rules.family(claim['source_component'])=='pressure_plate'
                   and rules.family(claim['target_component']) in {'mullion','transom'}), None)
    if fixing:
        return dict(type='CC', evidence=[f'PDF page {page}: {heading}', fixing], evidence_status='explicit')
    return None


def detail_outputs(catalogue, details, entities, article_database, assembly_database, index, references=(), source_text=()):
    """One validated DetailOutput per source detail, plus the issues found while building them."""
    outputs, issues = [], []
    by_detail = {}
    for record in article_database:
        for occurrence in record.get('occurrences', []):
            by_detail.setdefault(occurrence['canonical_detail_id'], {}).setdefault(record['record_id'], (record, []))[1].append(occurrence)
    for detail in details:
        if detail.get('record_class') == 'synthetic_review_bucket':
            continue
        did = detail['canonical_id']
        nodes = [e for e in entities if e['canonical_detail_id'] == did]
        claims = [claim for claim in assembly_database if claim['context']['canonical_id'] == did]
        computed, system_problems = rules.system_issues(detail.get('system_type'), nodes, claims)
        if computed['type'] is None:
            explicit = conventional_glazing(detail, nodes, claims, source_text)
            if explicit:
                computed, system_problems = explicit, []
        system_type = computed['type']
        unresolved = [f"system: {p['issue']} (declared {p.get('declared')}, computed {p.get('computed')})" for p in system_problems]
        used, refs, rows = set(), {}, []
        for record, occurrence in ((r,o) for r,items in by_detail.get(did, {}).values() for o in items):
            try:
                row = ArticleRecord.model_validate(_article(record, [occurrence], index, used)).model_dump()
            except ValidationError as error:
                unresolved.append(f"article {record.get('profile_id') or occurrence['label']}: {error.errors()[0]['msg']}")
                continue
            row['system'] = [system_type] if system_type else []
            rows.append(row)
            refs[occurrence['canonical_id']] = row['profile_id'] or row['label']
        local_nodes = {e['id']: e for e in nodes}
        article_rows = {row['occurrence_id']:row for row in rows}
        fixing_fasteners = Counter(claim['fastener_assigned']['occurrence_id'] for claim in claims
                                   if claim['graph_eligible'] and claim.get('fastener_assigned'))
        shared_fasteners = {key for key, count in fixing_fasteners.items() if count > 1}
        assemblies, pending = [], []
        for claim in claims:
            if not claim['graph_eligible']:
                unresolved.append(f"{claim['claim_id']} {claim['source']} -{claim['predicate']}-> {claim['target']}: "
                                  f"{claim['relationship_status']} ({', '.join(claim['status_reasons'])})")
                continue
            for problem in rules.assembly_issues(claim, local_nodes):
                unresolved.append(f"{claim['claim_id']}: {problem}")
            item = _relation(claim, refs, local_nodes, article_rows, system_type, unresolved, shared_fasteners)
            if item is not None:
                pending.append(item)
        per_axis = None
        if system_type == 'semi-SG':
            # semi-SG: one axis CC, the other SG. Learn each axis's system from relations whose parts
            # identify it, then give the same system part to the other relations on that axis.
            seen = {}
            for item in pending:
                if item['system_part'] and item['_axis']:
                    seen.setdefault(item['_axis'], set()).add(item['system_part'])
            per_axis, axis_problems = rules.semi_axes(seen)
            unresolved += [f'system: {p}' for p in axis_problems]
            issues += [dict(category='system_type_check', detail_id=did, issue=p, provenance=detail['provenance'])
                       for p in axis_problems]
            for item in pending:
                if not item['system_part'] and item['_axis'] in per_axis:
                    item['system_part'] = per_axis[item['_axis']]
                    item['missing_information'].append(f"system part taken from the {item['_axis']} axis")
        for item in pending:
            item.pop('_axis')
            if system_type == 'semi-SG' and not item['system_part']:
                unresolved.append(f"{item['assembly_id']}: system part (SG or CC) not identifiable in semi-SG detail")
                continue
            try:
                assemblies.append(AssemblyRecord.model_validate(item).model_dump())
            except ValidationError as error:
                unresolved.append(f"{item['assembly_id']}: {error.errors()[0]['msg']}")
        unresolved += [f"reference '{r['quote']}' unresolved" for r in references
                       if r['from_detail'] == did and r['status'] == 'unresolved']
        output = dict(catalogue=catalogue, page=detail['provenance']['pdf_page'], detail_id=did,
                      title=detail.get('title'), view_type=detail.get('view_type'), position=detail.get('position'),
                      system=dict(**computed,declared_type=detail.get('system_type'),
                                  per_axis=per_axis or None),
                      article_database=rows, assembly_database=assemblies, unresolved=unresolved)
        try:
            outputs.append(validate_output(output))
        except (ValidationError, ValueError) as error:
            issues.append(dict(category='detail_output_invalid', detail_id=did, message=str(error)[:500],
                               provenance=detail['provenance']))
        issues += [dict(category='system_type_check', detail_id=did, **p, provenance=detail['provenance'])
                   for p in system_problems]
    return outputs, issues


def file_name(output):
    local = re.sub(r'[^A-Za-z0-9]+', '_', output['detail_id'].split(':')[-1])[:40]
    return f"p{output['page']:04}_{local}.json"
