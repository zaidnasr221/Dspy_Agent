"""Accuracy scoring against explicitly labeled, synthetic ground truth."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest
from test_agent import page_result


def inputs():
    expected = page_result(1)
    expected['measurements'][0]['unit'] = 'mm'
    truth = {'version': 1, 'cases': [{'id': 'fixture-top', 'reviewed_by': 'synthetic fixture author',
        'source_sha256': 'a' * 64, 'unit_id': 'p0001:top', 'bbox': [0, 0, 1, .5],
        'expected': expected}]}
    prediction = {'unit': {'unit_id': 'p0001:top', 'pdf_page': 1, 'bbox': [0, 0, 1, .5]},
        'metadata': {'source_sha256': 'a' * 64}, 'extraction': deepcopy(expected)}
    return truth, prediction


def test_benchmark_scores_each_metric_independently_of_model_local_ids():
    from facade_dspy.benchmark import evaluate
    truth, prediction = inputs()
    value = prediction['extraction']
    value['nodes'][0]['id'] = 'other-local-id'
    value['relations'][0]['source'] = 'other-local-id'
    value['measurements'][0]['subject'] = 'other-local-id'
    perfect = evaluate(truth, [prediction])
    assert all(score['f1'] == 1 for score in perfect['metrics'].values())

    value['measurements'][0].update(value=53, unit='cm')
    value['tables'][0]['rows'][0]['cells'][1] = '53'
    relation = value['relations'][0]
    relation['source'], relation['target'] = relation['target'], relation['source']
    metrics = evaluate(truth, [prediction])['metrics']
    assert metrics['article_ids']['true_positive'] == 2
    for name in ('dimensions', 'units', 'relationships'):
        assert metrics[name]['false_positive'] == metrics[name]['false_negative'] == 1
        assert metrics[name]['f1'] == 0
    assert metrics['table_structure']['f1'] == 1
    assert metrics['table_cells']['f1'] == .5


def test_benchmark_reports_missing_regions_and_rejects_ambiguous_predictions():
    from facade_dspy.benchmark import evaluate
    truth, prediction = inputs()
    prediction['unit']['bbox'] = [0, .5, 1, 1]
    report = evaluate(truth, [prediction])
    assert report['cases'][0]['status'] == 'missing_prediction'
    assert report['metrics']['article_ids']['false_negative'] == 2
    assert report['unlabelled_predictions'] == 1
    with pytest.raises(ValueError, match='Duplicate prediction'):
        evaluate(truth, [prediction, prediction])


def test_benchmark_distinguishes_matching_articles_in_different_details():
    from facade_dspy.benchmark import evaluate
    truth, prediction = inputs()
    expected = truth['cases'][0]['expected']
    for value in (expected, prediction['extraction']):
        value['details'].append(dict(value['details'][0], id='alternate', title='Alternate detail'))
        value['nodes'].append(dict(value['nodes'][0], id='alternate-connector', detail_id='alternate'))
    prediction['extraction']['relations'][0]['source'] = 'alternate-connector'

    metrics = evaluate(truth, [prediction])['metrics']
    assert metrics['article_ids']['f1'] == 1
    assert metrics['relationships']['false_positive'] == metrics['relationships']['false_negative'] == 1


def test_benchmark_cli_reads_saved_json_without_extraction(tmp_path):
    truth, prediction = inputs()
    labels = tmp_path / 'truth.json'
    predictions = tmp_path / 'proposals.json'
    labels.write_text(json.dumps(truth), encoding='utf-8')
    predictions.write_text(json.dumps([prediction]), encoding='utf-8')
    result = subprocess.run([sys.executable, '-B', '-m', 'facade_dspy.benchmark',
        '--ground-truth', str(labels), '--predictions', str(predictions)],
        capture_output=True, text=True, encoding='utf-8', timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['metrics']['article_ids']['f1'] == 1
