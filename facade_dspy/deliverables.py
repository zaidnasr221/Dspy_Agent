"""Catalogue deliverables of the classification process.

ARTICLE DB : one record per article (profile_id) across the whole catalogue, with
             profile_id, profile_role, depth, edge_spacing, spacing, quantity, material, finish,
             color and all else found ('other'); every value keeps its evidence.
ASSEMBLY DB: every verified relation (clamped, bonded, fixed, supported, movement, insertion,
             other + clarification) with fastener_assigned for 'fixed' only.

Built only from validated per-detail outputs, so nothing here is new extraction: it is the
merge of what each detail proved. Disagreeing values are never averaged or chosen silently;
the first explicit value is kept and every other one is listed in 'conflicts' with its page.
"""
from .schema import validate_deliverables

FIELDS = ('depth', 'edge_spacing', 'spacing', 'quantity', 'material', 'finish', 'color')


def _key(row, output):
    if row['profile_id']:
        return 'id:' + row['profile_id']
    # A part without a printed ID cannot be matched across details: it stays its own record.
    return f"local:{output['detail_id']}:{row['occurrence_id'] or row['label']}"


def _same(a, b):
    return (a or {}).get('value') == (b or {}).get('value') and (a or {}).get('unit') == (b or {}).get('unit')


def _rank(row):
    return 0 if row['evidence_status'] == 'explicit' else 1


def _merge(rows):
    rows = sorted(rows, key=lambda pair: _rank(pair[0]))
    first, _ = rows[0]
    record = dict(profile_id=first['profile_id'], profile_role=first['profile_role'],
                  role_clarification=first.get('role_clarification'), article_type=first['article_type'],
                  label=first['label'], other={}, systems=[], occurrences=[], sources=[], conflicts=[],
                  missing_information=[], evidence_status=first['evidence_status'])
    roles = {r['profile_role'] for r, _ in rows if r['profile_role'] != 'other'}
    if len(roles) > 1:
        record['conflicts'].append('profile_role: ' + ', '.join(sorted(roles)))
    if record['profile_role'] == 'other' and roles:
        record['profile_role'], record['role_clarification'] = sorted(roles)[0], None
    types = {r['article_type'] for r, _ in rows if r['article_type']}
    if len(types) > 1:
        record['conflicts'].append('article_type: ' + ', '.join(sorted(types)))
    if record['article_type'] is None and types:
        record['article_type'] = sorted(types)[0]
    for field in FIELDS:
        chosen = None
        for row, output in rows:
            value = row.get(field)
            if not value:
                continue
            if chosen is None:
                chosen = value
            elif not _same(chosen, value):
                record['conflicts'].append(f"{field}: {value.get('value')} {value.get('unit') or ''} on page {output['page']}".strip())
        record[field] = chosen
        if chosen is None:
            record['missing_information'].append(field)
    if record['article_type'] not in (None, 'piece'):
        record['missing_information'] = [m for m in record['missing_information'] if m not in ('edge_spacing', 'spacing')]
    for row, output in rows:
        for name, value in row.get('other', {}).items():
            if name not in record['other']:
                record['other'][name] = value
            elif not _same(record['other'][name], value):
                record['conflicts'].append(f"{name}: {value.get('value')} on page {output['page']}")
        record['systems'] = sorted(set(record['systems']) | set(row.get('system') or []))
        record['occurrences'].append(dict(page=output['page'], detail_id=output['detail_id'],
                                          occurrence_id=row.get('occurrence_id'), label=row['label'],
                                          view_type=output.get('view_type')))
        record['sources'] += [s for s in row['sources'] if s not in record['sources']]
        record['missing_information'] += [m for m in row['missing_information']
                                          if m not in FIELDS and m not in record['missing_information']]
    if any(r['evidence_status'] == 'explicit' for r, _ in rows):
        record['evidence_status'] = 'explicit'
    record['conflicts'] = list(dict.fromkeys(record['conflicts']))
    return record


def build(catalogue, outputs):
    """(article_db, assembly_db) for one catalogue, validated against the deliverable contract."""
    grouped, order = {}, []
    for output in outputs:
        for row in output['article_database']:
            key = _key(row, output)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append((row, output))
    articles = [_merge(grouped[key]) for key in order]
    labels = {a['label'] for a in articles}
    assemblies = []
    for output in outputs:
        for item in output['assembly_database']:
            assemblies.append(dict(item, catalogue=catalogue, detail_title=output.get('title')))
    # A participant without a printed ID is named by its label; keep it resolvable in the Article DB.
    missing = {p['article'] for a in assemblies for p in a['participants']} - labels - {a['profile_id'] for a in articles}
    if missing:
        raise ValueError(f'Assembly participants missing from the Article DB: {sorted(missing)}')
    return validate_deliverables(dict(catalogue=catalogue, articles=articles),
                                 dict(catalogue=catalogue, assemblies=assemblies))
