"""Recheck saved relationship and measurement claims against their PDF evidence."""
import argparse
import shutil
from pathlib import Path
from typing import Literal

import dspy
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, computed_field

from .budget import Budget, BudgetStopped
from .compare_batch import difference, page_records
from .openai_api import OpenAIResponses
from .publication import published_directory
from .storage import dump, objecthash, read

State = Literal['visible', 'missing', 'contradicted']
CHECKS = {
    'relationship': {'source', 'target', 'mechanism'},
    'fixing': {'source', 'target', 'mechanism', 'fastener_path'},
    'measurement': {'value', 'endpoints', 'owner'},
}


class EvidenceCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    claim_id: str
    checks: dict[str, State]
    reason: str
    source_page: int | None
    bbox: list[float] | None = Field(min_length=4, max_length=4)

    @computed_field
    @property
    def missing_evidence_count(self) -> int:
        return sum(state == 'missing' for state in self.checks.values())


class EvidenceResponse(BaseModel):
    checks: list[EvidenceCheck]


class VerifyDrawingEvidence(dspy.Signature):
    """Verify each numbered claim using the actual PDF drawings."""
    claims: list[dict] = dspy.InputField()
    feedback: list[dict] = dspy.InputField()
    checks: list[EvidenceCheck] = dspy.OutputField(
        desc='One verdict per claim with evidence checks and source locator. The count is computed from the checks.')


def normalize_checks(claims, response, allowed_pages):
    by_id = {}
    duplicates = set()
    for check in EvidenceResponse.model_validate(response).checks:
        if check.claim_id in by_id:
            duplicates.add(check.claim_id)
        by_id[check.claim_id] = check
    results = []
    for claim in claims:
        claim_id = claim['claim_id']
        required = CHECKS[claim['kind']]
        check = by_id.get(claim_id)
        if check is None or claim_id in duplicates:
            check = EvidenceCheck(claim_id=claim_id, checks={}, reason='No unique verdict',
                                  source_page=None, bbox=None)
        box = check.bbox
        has_locator = bool(box and check.reason.strip() and check.source_page in allowed_pages
                           and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)
        check.checks = {key: check.checks.get(key, 'missing') if has_locator else 'missing'
                        for key in sorted(required)}
        if claim['kind'] == 'fixing':
            fixing = claim['claim'].get('fastener_assigned') or {}
            if not (fixing.get('path_occurrence_ids') or fixing.get('passes_through')):
                if check.checks['fastener_path'] == 'visible':
                    check.checks['fastener_path'] = 'missing'
        results.append(check.model_dump())
    return results


def claim_rows(details):
    for detail in details:
        for index, assembly in enumerate(detail['assembly_database']):
            yield dict(claim_id=f"{detail['detail_id']}:r{index}",
                       kind='fixing' if assembly['relation']=='fixed' else 'relationship',
                       claim=assembly)
        for index, article in enumerate(detail['article_database']):
            for field in ('depth', 'spacing', 'edge_spacing', 'quantity'):
                fact = article.get(field)
                if fact and fact.get('value') is not None:
                    yield dict(claim_id=f"{detail['detail_id']}:m{index}:{field}", kind='measurement',
                               claim=dict(field=field, fact=fact, owner=article['label'],
                                          article_id=article['profile_id']))


def accepted_details(details, verdicts):
    supported = {v['claim_id'] for v in verdicts if set(v['checks'].values()) == {'visible'}}
    filtered = []
    for detail in details:
        ident = detail['detail_id']
        assemblies = [row for i,row in enumerate(detail['assembly_database']) if f'{ident}:r{i}' in supported]
        articles = []
        for i,article in enumerate(detail['article_database']):
            articles.append({key:value for key,value in article.items()
                             if key not in ('depth','spacing','edge_spacing','quantity')
                             or f'{ident}:m{i}:{key}' in supported})
        filtered.append(dict(detail, article_database=articles, assembly_database=assemblies))
    return filtered


class EvidenceAudit(dspy.Module):
    signature = VerifyDrawingEvidence

    def __init__(self, backend, snapshot, out):
        super().__init__()
        self.backend = backend
        self.snapshot = snapshot
        self.out = out

    def forward(self, page, claims, support_pages):
        # Include pages cited by exported facts, plus the saved support plan.
        pages = set(support_pages) | {page}
        for item in claims:
            content = item['claim']
            refs = content.get('evidence', []) if item['kind'] != 'measurement' else content['fact'].get('evidence', [])
            pages.update(ref['page'] for ref in refs if ref.get('page'))
        pages = {n for n in pages if (self.snapshot/'images'/f'p{n:04}.png').is_file()}
        prompt = (
            f'{self.signature.instructions} Check only the required keys for its kind. '
            'Relationship: source, target, mechanism. Fixing: those three plus fastener_path. '
            'Measurement: value, endpoints, owner. Use visible only when clearly evidenced; '
            'use missing when ambiguous and contradicted when the drawing disproves it. '
            'Cite a physical source page and normalized bounding box. Use null if unavailable. '
            'Do not infer an article dimension from a section dimension. For a fixing, an empty path is missing. '
            'Use feedback to recheck only the supplied claims. Required output is JSON.'
        )
        current = claims
        feedback = []
        result = {}
        attempts = []
        for attempt in (1, 2):
            if not current:
                break
            folder = self.out/f'page-{page}'/f'attempt-{attempt}'
            checkpoint = folder/'checkpoint.json'
            fingerprint = objecthash(dict(claims=current, feedback=feedback, pages=sorted(pages)))
            if checkpoint.exists():
                receipt = read(checkpoint)
                if receipt['fingerprint'] != fingerprint:
                    raise ValueError('Audit inputs changed. Use a fresh output directory.')
                checks = receipt['checks']
                attempts.append(receipt)
            else:
                folder.mkdir(parents=True, exist_ok=True)
                for number in sorted(pages):
                    name = f'p{number:04}.png'
                    shutil.copyfile(self.snapshot/'images'/name, folder/name)
                dump(folder/'claims.json', current)
                if feedback:
                    dump(folder/'feedback.json', feedback)
                try:
                    response = self.backend.execute('relation_check', prompt, EvidenceResponse.model_json_schema(),
                                                    folder, lambda: None)
                except BudgetStopped:
                    return dict(attempts=attempts, verdicts=list(result.values()), budget_stopped=True)
                checks = normalize_checks(current, response['output'], pages)
                receipt = dict(attempt=attempt, receipt=response['raw_response'], checks=checks, fingerprint=fingerprint)
                dump(checkpoint, receipt)
                attempts.append(receipt)
            result.update({check['claim_id']:check for check in checks})
            feedback = [check for check in checks if check['missing_evidence_count'] > 0]
            retry_ids = {check['claim_id'] for check in feedback}
            current = [claim for claim in current if claim['claim_id'] in retry_ids]
        return dict(attempts=attempts, verdicts=list(result.values()), budget_stopped=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('--pages', type=int, nargs='+', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--budget-usd', type=float, required=True)
    args = parser.parse_args()
    load_dotenv()
    snapshot, baseline = (published_directory(p) for p in (args.run, args.baseline))
    if read(snapshot/'document.json')['sha256'] != read(baseline/'document.json')['sha256']:
        parser.error('Saved runs must refer to the same PDF')
    details, reference = (read(p/'data/detail_outputs.json') for p in (snapshot, baseline))
    plan = read(snapshot/'coverage.json').get('support_pages', {})
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    budget = Budget(out, args.budget_usd, .1)
    audit = EvidenceAudit(OpenAIResponses(out, budget, 'gpt-6.1-sol'), snapshot, out)
    report = {}
    for page in args.pages:
        selected = [row for row in details if row['page']==page]
        result = audit(page=page, claims=list(claim_rows(selected)),
                       support_pages=[item['pdf_page'] for item in plan.get(str(page), [])])
        base = page_records(reference, page)
        saved = page_records(selected, page)
        checked = page_records(accepted_details(selected, result['verdicts']), page)
        report[str(page)] = dict(result,
            saved_comparison={key:difference(base[key], saved[key]) for key in ('assemblies','measurements')},
            supported_comparison={key:difference(base[key], checked[key]) for key in ('assemblies','measurements')})
        dump(out/'report.json', dict(pages=report, cost=budget.summary()))
    print(out/'report.json')


if __name__ == '__main__':
    main()
