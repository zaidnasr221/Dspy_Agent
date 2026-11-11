"""Score predicted detail outputs against reviewed gold files (no model call).

    python evaluation/metric.py data/gold/reviewed <run>/data/detail_outputs.json [report.json]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from facade_dspy.metric import score  # noqa: E402


def main(gold_dir, predicted, out=None):
    gold = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(Path(gold_dir).glob('*.json'))]
    pred = json.loads(Path(predicted).read_text(encoding='utf-8'))
    report = score(gold, pred)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if out:
        Path(out).write_text(text + '\n', encoding='utf-8')
    print(json.dumps(dict(score=report['score'], fields=report['fields'], invented=report['invented']), indent=2))
    return report


if __name__ == '__main__':
    main(*sys.argv[1:4])
