"""Compare a predicted DetailOutput with a reviewed gold DetailOutput, field by field.

Scores are recall/precision style per field. Any predicted article ID, part or numeric value that the
gold does not contain is reported as 'invented' and makes the detail fail (score 0), because an
invented engineering fact is worse than a missing one.
"""
from collections import Counter

from . import rules

ARTICLE_FIELDS = ('profile_role', 'article_type', 'depth', 'edge_spacing', 'spacing', 'quantity',
                  'material', 'finish', 'color')


def _num(value):
    if isinstance(value, list):
        return tuple(round(float(v), 3) for v in value)
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return rules.norm(str(value)) if value is not None else None


def _field(article, name):
    value = article.get(name)
    if isinstance(value, dict):
        return (_num(value.get('value')), rules.norm(value.get('unit') or '') or None)
    return value


def _article_key(article):
    if article.get('profile_id'):
        return 'id:' + rules.article_key(article['profile_id'])
    return 'role:' + str(article.get('profile_role')) + ':' + rules.norm(article.get('label') or '')


def _assembly_key(assembly, names):
    parts = tuple(sorted((names.get(p.get('occurrence_id'), names.get(p['article'],p['article'])),
                          p['role']) for p in assembly['participants']))
    return (assembly['relation'], parts)


def _names(output):
    """Map participant references (profile_id or label) to the article key used for matching."""
    keys = Counter(_article_key(a) for a in output['article_database'])
    names = {}
    for a in output['article_database']:
        key = _article_key(a)
        literal = a.get('profile_id') or a.get('label')
        names[literal] = key
        if a.get('occurrence_id'):
            names[a['occurrence_id']] = key if keys[key] == 1 else key + ':' + rules.norm(a.get('label') or '')
    return names


def _fastener(assembly, names):
    f = assembly.get('fastener_assigned') or {}
    path = f.get('path_occurrence_ids') or f.get('passes_through') or []
    host = f.get('engages_in_occurrence_id') or f.get('engages_in')
    offsets = [(o.get('from_datum'), o.get('to_datum'), _num(o['value']), o.get('unit'))
               for o in f.get('distance_offset') or []]
    return (f.get('type'), tuple(names.get(p, p) for p in path), names.get(host, host),
            f.get('insertion_direction'), f.get('position'), offsets)


def _same_location(a, b):
    if a['page'] != b['page'] or not b.get('bbox'):
        return False
    x, y = a['bbox'], b['bbox']
    intersection = max(0, min(x[2],y[2])-max(x[0],y[0])) * max(0, min(x[3],y[3])-max(x[1],y[1]))
    union = (x[2]-x[0])*(x[3]-x[1]) + (y[2]-y[0])*(y[3]-y[1]) - intersection
    return union > 0 and intersection / union >= .5


def score_detail(gold, pred):
    report = dict(detail_id=gold['detail_id'], fields={}, invented=[], missing=[])
    report['fields']['system_type'] = float(gold['system']['type'] == pred['system']['type'])
    g = {_article_key(a): a for a in gold['article_database']}
    p = {_article_key(a): a for a in pred['article_database']}
    report['fields']['articles_recall'] = len(g.keys() & p.keys()) / len(g) if g else 1.0
    report['fields']['articles_precision'] = len(g.keys() & p.keys()) / len(p) if p else 1.0
    report['missing'] += [f'article {k}' for k in g.keys() - p.keys()]
    report['invented'] += [f'article {k}' for k in p.keys() - g.keys() if k.startswith('id:')]
    for name in ARTICLE_FIELDS:
        pairs = [(g[k], p[k]) for k in g.keys() & p.keys() if _field(g[k], name) is not None]
        if pairs:
            report['fields'][name] = sum(_field(a, name) == _field(b, name) for a, b in pairs) / len(pairs)
        for k in g.keys() & p.keys():
            if _field(g[k], name) is None and _field(p[k], name) not in (None, (None, None)) and name not in ('profile_role', 'article_type'):
                report['invented'].append(f'{k}.{name}={_field(p[k], name)}')
    gn, pn = _names(gold), _names(pred)
    ga = {_assembly_key(a, gn): a for a in gold['assembly_database']}
    pa = {_assembly_key(a, pn): a for a in pred['assembly_database']}
    report['fields']['assemblies_recall'] = len(ga.keys() & pa.keys()) / len(ga) if ga else 1.0
    report['fields']['assemblies_precision'] = len(ga.keys() & pa.keys()) / len(pa) if pa else 1.0
    report['missing'] += [f'assembly {k[0]} {sorted(k[1])}' for k in ga.keys() - pa.keys()]
    fixed = [(ga[k], pa[k]) for k in ga.keys() & pa.keys() if ga[k]['relation'] == 'fixed']
    if fixed:
        report['fields']['fastener_assigned'] = sum(_fastener(a, gn) == _fastener(b, pn) for a, b in fixed) / len(fixed)
    located = [(k, ref) for k,a in ga.items() for ref in a['evidence'] if ref.get('bbox')]
    if located:
        report['fields']['assembly_evidence'] = sum(
            any(_same_location(ref, other) for other in pa.get(k, {}).get('evidence', []))
            for k,ref in located) / len(located)
    values = list(report['fields'].values())
    report['score'] = 0.0 if report['invented'] else (sum(values) / len(values) if values else 0.0)
    return report


def score(gold_outputs, pred_outputs):
    """Match details by (page, detail title or id) and average; unmatched gold details score 0."""
    def key(o):
        return (o['page'], rules.norm(o.get('title') or o['detail_id']))
    preds = {key(o): o for o in pred_outputs}
    reports = []
    for gold in gold_outputs:
        pred = preds.get(key(gold))
        reports.append(score_detail(gold, pred) if pred else dict(detail_id=gold['detail_id'], score=0.0,
                                                                   fields={}, invented=[], missing=['detail not produced']))
    fields = {}
    for r in reports:
        for name, value in r['fields'].items():
            fields.setdefault(name, []).append(value)
    return dict(score=sum(r['score'] for r in reports) / len(reports) if reports else 0.0,
                fields={k: sum(v) / len(v) for k, v in fields.items()},
                invented=sum(len(r['invented']) for r in reports), details=reports)
