"""Targeted host checks for cleat insertions with more than one plausible profile."""
import argparse
import re
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import dspy
from dotenv import load_dotenv
from pydantic import BaseModel

from .budget import Budget
from .index_dspy_pilot import DrawingAdapter
from .openai_api import OpenAIResponses
from .rules import article_key
from .storage import dump, objecthash, read


class HostDecision(BaseModel):
    source: str
    host: str | None
    target_evidence: str
    support_evidence: str


class HostDecisions(BaseModel):
    decisions: list[HostDecision]


class CheckHosts(dspy.Signature):
    """Resolve the listed cleat hosts in the target drawing. Follow the continuous metal outline
    around each cleat pocket to its printed article callout. Distinguish touching or adjacent contours
    and gaps, seals and isolators between them. Use the support page listed for each question to corroborate the same article
    and pocket in another view. Support may disambiguate identity; the interface itself must be visible
    on the target. Return the target occurrence ID for the host, or null if the outline is unclear.
    Describe the specific outline and cite what the support drawing confirms.
    """
    page: int = dspy.InputField()
    support_pages: list[int] = dspy.InputField()
    questions: list[dict] = dspy.InputField()
    answer: HostDecisions = dspy.OutputField()


HOST_LABEL = re.compile(r'\b(?:frame|sash|adaptor|adapter|mullion|transom|profile)\b', re.I)
CLEAT_LABEL = re.compile(r'\bcleat\b', re.I)


def build_article_index(results):
    index = defaultdict(list)
    for result in results:
        for part in result['observations']['parts']:
            if part['article_id']:
                index[article_key(part['article_id'])].append(dict(
                    page=result['page'], occurrence=part['id'], label=part['label'], location=part['location']))
    return dict(sorted(index.items()))


def questions_for_page(result, index):
    page = result['page']
    observations = result['observations']
    parts = {p['id']: p for p in observations['parts']}
    pages = {key: {r['page'] for r in rows} for key, rows in index.items()}
    questions = []
    for edge in observations['interfaces']:
        source, target = parts[edge['source']], parts[edge['target']]
        if (edge['relation'] != 'insertion' or not CLEAT_LABEL.search(source['label'])
                or not source['article_id'] or not target['article_id']):
            continue
        cleat_pages = pages.get(article_key(source['article_id']), set()) - {page}
        shared = defaultdict(list)
        for node in parts.values():
            if not node['article_id'] or not HOST_LABEL.search(node['label']):
                continue
            for candidate_page in cleat_pages & pages.get(article_key(node['article_id']), set()):
                shared[candidate_page].append(node)
        valid = {p: nodes for p, nodes in shared.items()
                 if len({n['article_id'] for n in nodes}) >= 2 and any(n['id'] == target['id'] for n in nodes)}
        if not valid:
            continue
        support = min(valid, key=lambda p: (-len({n['article_id'] for n in valid[p]}), abs(p-page), p))
        questions.append(dict(source=source, original_host=target['id'], candidates=valid[support], support=support))
    return questions


def check_page(page, questions, images, out, backend):
    folder = out / f'p{page:04}'
    folder.mkdir(exist_ok=True)
    completed = folder / 'result.json'
    fingerprint = objecthash(questions)
    if completed.is_file():
        result = read(completed)
        if result.get('question_fingerprint') == fingerprint:
            return result
        raise ValueError('Saved host questions changed; use a fresh check directory')
    support = sorted({q['support'] for q in questions})
    for number in [page, *support]:
        name = f'p{number:04}.png'
        shutil.copyfile(images / name, folder / name)
    receipts = []
    with dspy.context(lm=dspy.LM('openai/gpt-6.1-sol', cache=False),
                      adapter=DrawingAdapter(backend, folder, receipts)):
        answer = dspy.Predict(CheckHosts)(page=page, support_pages=support, questions=questions).answer
    by_source = defaultdict(list)
    for decision in answer.decisions:
        by_source[decision.source].append(decision)
    rows = []
    for q in questions:
        source = q['source']['id']
        choices = by_source[source]
        match = choices[0] if len(choices) == 1 else None
        allowed = {n['id'] for n in q['candidates']}
        resolved = bool(match and match.host in allowed and match.target_evidence.strip()
                        and match.support_evidence.strip())
        rows.append(dict(source=source, source_article=q['source']['article_id'],
                         original_host=q['original_host'], host=match.host if resolved else None,
                         decision=match.model_dump() if match else None, support_page=q['support']))
    result = dict(page=page, decisions=rows, receipts=receipts, question_fingerprint=fingerprint)
    dump(completed, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--images', type=Path, required=True)
    parser.add_argument('--budget-usd', type=float, required=True)
    parser.add_argument('--workers', type=int, choices=range(1,17), default=8)
    args = parser.parse_args()
    load_dotenv()
    raw = read(args.run / 'pilot.json')
    pages = raw['pages']
    if raw.get('errors'):
        parser.error('Complete the page reads before checking hosts')
    index = build_article_index(pages)
    dump(args.run / 'article_occurrence_index.json', index)
    out = args.run / 'host_checks'
    out.mkdir(exist_ok=True)
    budget = Budget(args.run, args.budget_usd, .06)
    backend = OpenAIResponses(args.run, budget, 'gpt-6.1-sol', reasoning_effort='low')
    jobs = {p['page']: questions_for_page(p,index) for p in pages}
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_page,p,q,args.images,out,backend):p for p,q in jobs.items() if q}
        for future in as_completed(futures):
            page = futures[future]
            try:
                results[page] = future.result()
            except Exception as exc:
                errors[page] = f'{type(exc).__name__}: {exc}'
            print(f'{len(results)+len(errors)}/{len(futures)} host-check pages finished',flush=True)
    counts = Counter('unresolved' if row['host'] is None else
                     'confirmed' if row['host'] == row['original_host'] else 'changed'
                     for result in results.values() for row in result['decisions'])
    dump(out / 'report.json', dict(pages=[results[p] for p in sorted(results)],
                                   counts=dict(counts), errors=errors, cost=budget.summary()))
    print(out / 'report.json')
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
