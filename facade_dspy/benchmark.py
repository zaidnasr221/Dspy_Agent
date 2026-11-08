"""Score saved region proposals against reviewed ground truth without a model."""
import argparse
from collections import Counter
import json
import re
from pathlib import Path

from .publication import published_directory
from .schema import validate
from .storage import read


METRICS = ('article_ids', 'dimensions', 'units', 'table_structure', 'table_cells', 'relationships')


def region_key(source_sha, page, unit_id, bbox):
    if not isinstance(source_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', source_sha):
        raise ValueError('A region requires the original PDF SHA-256')
    if type(page) is not int or page < 1 or not isinstance(unit_id, str) or not unit_id.strip():
        raise ValueError('A region requires a physical PDF page and unit ID')
    if (not isinstance(bbox, list) or len(bbox) != 4
            or any(type(value) not in (int, float) for value in bbox)):
        raise ValueError('A region requires four numeric bounding coordinates')
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError('Invalid ground-truth or prediction region bounding box')
    return source_sha, page, unit_id, tuple(bbox)


def observations(result):
    """Use source semantics as match keys because model-local IDs can change."""
    values = {metric: Counter() for metric in METRICS}

    def add(metric, *fields):
        values[metric][json.dumps(fields, ensure_ascii=False, separators=(',', ':'))] += 1

    details = {
        detail['id']: ['detail', detail['title'], sorted(detail['drawing_refs'])]
        for detail in result['details']
    }
    nodes = {}
    for node in result['nodes']:
        article = node['article_id']
        if article:
            add('article_ids', article)
            nodes[node['id']] = ['article', article, node['callout']]
        else:
            nodes[node['id']] = ['unlabelled_article', node['kind'], node['label'],
                                 node['callout'], details[node['detail_id']]]
    subjects = details | nodes
    for measurement in result['measurements']:
        anchor = (subjects[measurement['subject']], measurement['property'],
                  measurement['datum'], measurement['condition'])
        if measurement['value'] is not None:
            add('dimensions', *anchor, measurement['value'])
        unit = measurement['unit']
        if unit and unit.strip().casefold() not in ('', 'unknown', '(none)'):
            add('units', *anchor, unit.strip())
    for table in result['tables']:
        add('table_structure', table['title'], table['columns'], table['units'], table['footnotes'])
        for row_index, row in enumerate(table['rows'], 1):
            for column_index, value in enumerate(row['cells'], 1):
                add('table_cells', table['title'], row_index, column_index,
                    table['columns'][column_index - 1], value)
    for relation in result['relations']:
        add('relationships', nodes[relation['source']], relation['predicate'],
            nodes[relation['target']], relation['basis'], relation['condition'])
    return values


def scores(tp, fp, fn):
    return {
        'true_positive': tp, 'false_positive': fp, 'false_negative': fn,
        'precision': tp / (tp + fp) if tp + fp else None,
        'recall': tp / (tp + fn) if tp + fn else None,
        'f1': 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None,
    }


def evaluate(ground_truth, proposals):
    if ground_truth.get('version') != 1 or not ground_truth.get('cases'):
        raise ValueError('Expected a version 1 ground-truth manifest with labeled cases')
    predictions = {}
    for proposal in proposals:
        unit = proposal['unit']
        key = region_key(proposal['metadata']['source_sha256'], unit['pdf_page'],
                         unit['unit_id'], unit['bbox'])
        if key in predictions:
            raise ValueError(f'Duplicate prediction for {key[2]} on page {key[1]}')
        predictions[key] = proposal['extraction']

    totals = {metric: [0, 0, 0] for metric in METRICS}
    cases, seen_ids, seen_regions = [], set(), set()
    for case in ground_truth['cases']:
        case_id = case['id']
        reviewer = case.get('reviewed_by')
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen_ids:
            raise ValueError('Ground-truth case IDs must be nonempty and unique')
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError(f'Ground-truth case {case_id} requires reviewed_by')
        expected = case['expected']
        page = expected['pdf_page']
        key = region_key(case['source_sha256'], page, case['unit_id'], case['bbox'])
        if key in seen_regions:
            raise ValueError(f'Duplicate labeled region in {case_id}')
        seen_ids.add(case_id)
        seen_regions.add(key)
        truth = observations(validate(expected, page))
        available = key in predictions
        predicted = observations(validate(predictions[key], page)) if available else {
            metric: Counter() for metric in METRICS
        }
        metrics = {}
        for metric in METRICS:
            want, got = truth[metric], predicted[metric]
            extra, missing = got - want, want - got
            counts = sum((want & got).values()), sum(extra.values()), sum(missing.values())
            for i, count in enumerate(counts):
                totals[metric][i] += count
            metrics[metric] = dict(scores(*counts),
                unexpected=[{'key': json.loads(value), 'count': count} for value, count in sorted(extra.items())],
                missed=[{'key': json.loads(value), 'count': count} for value, count in sorted(missing.items())])
        cases.append(dict(id=case_id, source_sha256=key[0], pdf_page=page, unit_id=key[2],
            bbox=case['bbox'], reviewed_by=reviewer,
            status='scored' if available else 'missing_prediction', metrics=metrics))
    return dict(version=1, metrics={metric: scores(*counts) for metric, counts in totals.items()},
        cases=cases, missing_predictions=sum(case['status']=='missing_prediction' for case in cases),
        unlabelled_predictions=len(predictions.keys() - seen_regions))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ground-truth', type=Path, required=True)
    parser.add_argument('--predictions', type=Path, action='append', required=True,
                        help='Run directory, snapshot, or all_proposals JSON. Repeat for multiple documents.')
    args = parser.parse_args()
    proposals = []
    for path in args.predictions:
        if path.is_dir():
            path = published_directory(path) / 'data/all_proposals.json'
        records = read(path)
        if not isinstance(records, list):
            parser.error(f'Expected a JSON list of proposal envelopes in {path}')
        proposals.extend(records)
    print(json.dumps(evaluate(read(args.ground_truth), proposals), indent=2))


if __name__ == '__main__':
    main()
