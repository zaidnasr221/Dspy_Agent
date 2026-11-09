"""Benchmark behaviour on real ALUPCO 50 SG regions (evaluation/alupco_50sg_ground_truth.json). Offline only."""
import copy
import json
import subprocess
import sys
from pathlib import Path

from facade_dspy.benchmark import evaluate
from facade_dspy.storage import read

TRUTH = Path(__file__).resolve().parents[1] / 'evaluation' / 'alupco_50sg_ground_truth.json'


def predictions(truth=None):
    truth = truth or read(TRUTH)
    return [dict(unit=dict(unit_id=c['unit_id'], pdf_page=c['expected']['pdf_page'], bbox=c['bbox']),
                 metadata=dict(source_sha256=c['source_sha256']), extraction=copy.deepcopy(c['expected']))
            for c in truth['cases']]


def by_page(items, page):
    return next(p for p in items if p['unit']['pdf_page'] == page)['extraction']


def test_labels_are_real_alupco_regions():
    truth = read(TRUTH)
    assert {c['source_sha256'] for c in truth['cases']} == {'d8173d47de9d732d527df7485aa3c0f1bbfcc4d6f5d7ebe6b7aea73ecd211e58'}
    p15 = truth['cases'][0]['expected']
    assert {n['article_id'] for n in p15['nodes'] if n['article_id']} >= {'235 918-11.5', '237 351', '235456', '32478'}
    report = evaluate(truth, predictions(truth))
    assert report['missing_predictions'] == 0
    for name in ('article_ids', 'components', 'table_cells', 'relationships', 'relationship_links'):
        assert report['metrics'][name]['f1'] == 1, name
    assert report['metrics']['components']['true_positive'] == 14


def test_typical_extraction_errors_are_measured():
    truth = read(TRUTH)
    items = predictions(truth)
    p15, p13 = by_page(items, 15), by_page(items, 13)
    # 1. a variant ID split into an ID plus a dimension, 2. a hallucinated article, 3. a missed relationship.
    p15['tables'][0]['rows'][0]['cells'][0] = '235 918 / 11.5'
    p15['nodes'].append(dict(p15['nodes'][-1], id='ghost', article_id='99999', kind='bracket', label='Bracket'))
    p15['relations'].pop()
    # 4. installation positions turned into components and a relationship on page 13.
    box = [.1, .2, .3, .3]
    p13['nodes'] += [dict(id=f'p{i}', detail_id='positions', article_id=None, kind='profile', label=f'Position {i}',
                          material=None, quantity=None, callout=None, bbox=box) for i in (1, 2)]
    p13['relations'].append(dict(source='p1', predicate='fixed_to', target='p2', basis='drawing_reading',
                                 condition=None, bbox=box, evidence='arrow'))
    metrics = evaluate(truth, items)['metrics']
    # Only '99999' is a false component: the split '235 918' is the real isolator 235918 shown in the
    # section, and the dimension '11.5' is never counted as a component.
    assert metrics['components']['false_positive'] == 1
    assert metrics['components']['false_negative'] == 0      # '235 918-11.5' still listed as a node
    assert metrics['table_cells']['false_negative'] == 1
    assert metrics['relationships']['false_negative'] == 1
    assert metrics['relationships']['false_positive'] == 1
    assert metrics['relationships']['precision'] == 0.5


def test_spelling_and_synonyms_do_not_count_as_errors():
    truth = read(TRUTH)
    items = predictions(truth)
    p15 = by_page(items, 15)
    for node in p15['nodes']:
        if node['article_id']:
            node['article_id'] = node['article_id'].replace(' ', '')          # '235 918-11.5' -> '235918-11.5'
    p15['relations'][0]['predicate'] = 'carries'                            # synonym of 'supports'
    for detail in p15['details']:
        detail['title'] = detail['title'].upper()                           # free-text titles differ
    metrics = evaluate(truth, items)['metrics']
    assert metrics['article_ids']['f1'] == metrics['components']['f1'] == 1
    assert metrics['relationship_links']['f1'] == 1
    assert metrics['relationships']['f1'] < 1                              # the strict view still sees the titles


def test_benchmark_cli_runs_on_the_alupco_labels(tmp_path):
    saved = tmp_path / 'all_proposals.json'
    saved.write_text(json.dumps(predictions()), encoding='utf-8')
    result = subprocess.run([sys.executable, '-B', '-m', 'facade_dspy.benchmark', '--ground-truth', str(TRUTH),
                             '--predictions', str(saved)], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['metrics']['relationship_links']['f1'] == 1
