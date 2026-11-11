"""Retry saved region extractions with DSPy Refine and original PDF evidence."""
import argparse
import shutil
from pathlib import Path

import dspy
from dotenv import load_dotenv
from pydantic import BaseModel

from . import rules
from .budget import Budget
from .compare_batch import difference, page_records
from .evidence_feedback import EvidenceResponse, VerifyDrawingEvidence, normalize_checks
from .exports import export, relationship_status
from .openai_api import OpenAIResponses
from .program import READING_RULES, result_path
from .publication import published_directory
from .schema import Identity, Measurement, Relation, validate
from .source import prepare_images
from .storage import dump, read


class RelationEdit(BaseModel):
    replace_index: int | None
    claim: Relation


class MeasurementEdit(BaseModel):
    replace_index: int | None
    claim: Measurement


class Edits(BaseModel):
    relations: list[RelationEdit]
    measurements: list[MeasurementEdit]


class Advice(BaseModel):
    discussion: str
    advice: dict[str, str]


class RecoverClaims(dspy.Signature):
    """Recover omitted claims and correct evidence gaps in a catalogue drawing."""
    proposal: dict = dspy.InputField()
    evidence_feedback: list[dict] = dspy.InputField()
    edits: Edits = dspy.OutputField()


class RegionExtraction(dspy.Module):
    def __init__(self):
        super().__init__()
        self.extract = dspy.Predict(RecoverClaims)

    def forward(self, proposal, evidence_feedback):
        return self.extract(proposal=proposal, evidence_feedback=evidence_feedback)


class ResponsesAdapter(dspy.JSONAdapter):
    """Keep both Refine's advice and the visual retry on the budgeted backend."""
    def __init__(self, backend=None, folder=None):
        # Refine constructs a no-argument subclass to inject the retry hint.
        super().__init__()
        self.backend = backend
        self.folder = folder
        self.receipt = None

    def __call__(self, lm, lm_kwargs, signature, demos, inputs):
        if 'edits' not in signature.output_fields:
            folder = self.folder / 'advice'
            folder.mkdir(exist_ok=True)
            dump(folder / 'inputs.json', inputs)
            result = self.backend.execute('review', signature.instructions, Advice.model_json_schema(), folder, lambda: None)
            return [Advice.model_validate(result['output']).model_dump()]
        if lm.kwargs.get('rollout_id', 0) == 0:
            return [dict(edits=Edits(relations=[], measurements=[]))]
        dump(self.folder / 'retry_inputs.json', inputs)
        self.receipt = self.backend.execute(
            'extraction', READING_RULES + '\nReturn only edits to the saved proposal. '
            'Use zero-based replace_index to correct a claim or null to add a missing claim. '
            'Use existing node IDs. Scan the entire drawing for omitted relationships and printed dimensions. '
            'Do not duplicate an existing claim. Use the numbered support drawings to resolve article identity '
            'and the endpoints of measurements. The main drawing supplies each edited claim locator. '
            'Leave uncertain dimensions at section scope. State both endpoints and the owning article in measurement evidence.',
            Edits.model_json_schema(), self.folder, lambda: None)
        return [dict(edits=Edits.model_validate(self.receipt['output']))]


def reward(args, prediction):
    """The saved proposal scores zero. Only independently supported edits improve it."""
    return float(prediction.get('supported_edits', 0))


def edit_candidates(proposal, edits, unit):
    nodes = {node['id']: node for node in proposal['nodes']}
    candidates = []
    for field in ('relations', 'measurements'):
        for edit in getattr(edits, field):
            index, claim = edit.replace_index, edit.claim.model_dump()
            old = proposal[field]
            if index is not None and not 0 <= index < len(old):
                continue
            if not (claim.get('evidence') or '').strip() or claim in old:
                continue
            if field == 'relations':
                if claim['source'] not in nodes or claim['target'] not in nodes:
                    continue
                if relationship_status(claim, nodes[claim['source']], nodes[claim['target']], False,
                                       scope=unit['bbox'], nodes=nodes)[0] != 'proposed':
                    continue
                kind = 'fixing' if rules.canonical_predicate(claim['predicate']) == 'fixed_to' else 'relationship'
                context = dict(claim, source_part=nodes[claim['source']], target_part=nodes[claim['target']],
                               path_parts=[nodes[key] for key in claim['passes_through'] or [] if key in nodes],
                               fastener_assigned=dict(passes_through=claim['passes_through']))
            else:
                if claim['subject'] not in nodes or claim['value'] is None:
                    continue
                kind = 'measurement'
                context = dict(claim, owner=nodes[claim['subject']])
            candidate = dict(proposal, **{field: [*old, claim]})
            try:
                validate(candidate, unit['pdf_page'])
            except ValueError:
                continue
            candidates.append(dict(claim_id=str(len(candidates)), kind=kind, claim=context,
                                   field=field, replace_index=index, value=claim))
    return candidates


def verify_edits(backend, folder, candidates, pages):
    if not candidates:
        return []
    check = folder / 'check'
    check.mkdir(exist_ok=True)
    for path in folder.glob('*.png'):
        shutil.copy2(path, check / path.name)
    dump(check / 'claims.json', candidates)
    page = read(folder / 'scope.json')['pdf_page']
    receipt = backend.execute(
        'relation_check', VerifyDrawingEvidence.instructions +
        f' source.png and focus.png show physical PDF page {page}. '
        'Each support_pNNNN.png is physical page NNNN. Use normalized full-page boxes. '
        'For relationship check source, target, mechanism. For fixing also check fastener_path, including all layers '
        'and final engagement. For measurement check value, endpoints, owner. Mark each visible, missing or contradicted. '
        'Require both extension-line endpoints and clear ownership. A nearby article callout alone does not establish ownership. '
        'Each verdict must include a reason and physical source page and box, or null if unavailable.',
        EvidenceResponse.model_json_schema(), check, lambda: None)
    verdicts = normalize_checks(candidates, receipt['output'], pages)
    dump(check / 'verdicts.json', dict(verdicts=verdicts, receipt=receipt['raw_response']))
    return [item for item, verdict in zip(candidates, verdicts)
            if set(verdict['checks'].values()) == {'visible'}]


def apply_edits(proposal, verification, candidates):
    updated = dict(proposal, relations=list(proposal['relations']), measurements=list(proposal['measurements']))
    verdicts = {item['claim_id']: item for item in verification['verdicts']}
    touched = set()
    for item in candidates:
        field, index, claim = item['field'], item['replace_index'], item['value']
        if (field, index) in touched or claim in updated[field]:
            continue
        if index is None:
            index = len(updated[field])
            updated[field].append(claim)
        else:
            touched.add((field, index))
            updated[field][index] = claim
        if field == 'relations':
            claim_id = rules.claim_id(index)
            verdicts[claim_id] = dict(claim_id=claim_id, verdict='supported', bbox=claim['bbox'],
                                      reason='Refine evidence checks passed',
                                      **{key: claim[key] for key in ('source', 'predicate', 'target')})
    return updated, dict(verification, verdicts=list(verdicts.values()))


def refine_region(backend, folder, record, feedback, pages):
    proposal = record['extraction']
    adapter = ResponsesAdapter(backend, folder)
    receipts = []

    def evaluate(args, prediction):
        if adapter.receipt is None:
            return reward(args, prediction)
        candidates = edit_candidates(proposal, prediction.edits, record['unit'])
        supported = verify_edits(backend, folder, candidates, pages)
        prediction.supported_edits = len(supported)
        prediction.accepted = supported
        receipts.append(dict(proposed=len(candidates), supported=len(supported),
                             extraction=adapter.receipt['raw_response']))
        return reward(args, prediction)

    with dspy.context(lm=dspy.LM('openai/gpt-6.1-sol', cache=False, rollout_id=0), adapter=adapter):
        refined = dspy.Refine(module=RegionExtraction(), N=2, reward_fn=evaluate, threshold=1)
        best = refined(proposal=proposal, evidence_feedback=feedback)
    if not receipts:
        raise RuntimeError(f"Refine did not complete the visual retry for {record['unit']['unit_id']}")
    return best.get('accepted', []), receipts[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('feedback', type=Path)
    parser.add_argument('--pages', type=int, nargs='+', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--budget-usd', type=float, required=True)
    args = parser.parse_args()
    load_dotenv()
    source, baseline = (published_directory(path) for path in (args.run, args.baseline))
    if read(source / 'document.json')['sha256'] != read(baseline / 'document.json')['sha256']:
        parser.error('Both runs must use the same PDF')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    budget = Budget(out, args.budget_usd, .1)
    backend = OpenAIResponses(out, budget, 'gpt-6.1-sol')
    for name in ('source.json', 'source.pdf', 'reference_index.json'):
        shutil.copy2(source / name, out / name)
    shutil.copytree(source / 'index', out / 'index')
    (out / 'images').mkdir()
    (out / 'verifications').mkdir()
    (out / 'reviews').mkdir()
    support = read(source / 'coverage.json').get('support_pages', {})
    audit = read(args.feedback)['pages']
    units = [unit for unit in read(source / 'units.json') if unit['pdf_page'] in args.pages
             and result_path(source, unit).is_file()]
    if {unit['pdf_page'] for unit in units} != set(args.pages):
        parser.error('A requested page has no saved extraction')
    result = {}
    for unit in units:
        page = unit['pdf_page']
        path = result_path(source, unit)
        record = read(path)
        folder = out / 'refine_inputs' / path.stem
        prepare_images(source, unit, folder)
        pages = {page}
        shutil.copy2(source / 'images' / f'p{page:04}.png', out / 'images' / f'p{page:04}.png')
        verdicts = audit[str(page)]['verdicts']
        feedback = [item for item in verdicts if item['missing_evidence_count']
                    or 'contradicted' in item['checks'].values()]
        support_pages = {item['pdf_page'] for item in support.get(str(page), [])}
        support_pages.update(item['source_page'] for item in feedback if item['source_page'])
        for number in sorted(support_pages - pages):
            image = source / 'images' / f'p{number:04}.png'
            if image.is_file():
                shutil.copy2(image, folder / f'support_p{number:04}.png')
                pages.add(number)
        verification_path = source / 'verifications' / path.name
        verification = read(verification_path) if verification_path.is_file() else dict(unit_id=unit['unit_id'], verdicts=[])
        accepted, summary = refine_region(backend, folder, record, feedback, pages)
        updated, verification = apply_edits(record['extraction'], verification, accepted)
        record['extraction'] = validate(updated, page)
        dump(result_path(out, unit), record)
        dump(out / 'verifications' / path.name, verification)
        review = source / 'reviews' / path.name
        if review.is_file():
            shutil.copy2(review, out / 'reviews' / review.name)
        result[unit['unit_id']] = dict(summary, accepted=accepted)
        dump(out / 'refine_progress.json', result)
    finish(out, source, baseline, units, args.pages, result, budget.summary())


def finish(out, source, baseline, units, pages, result, costs):
    document = read(source / 'document.json')
    identity = Identity.model_validate({key: document[key] for key in Identity.model_fields}).model_dump()
    # Preserve the saved catalogue context so cross-page links and dimensions
    # still resolve when only a few target pages were re-extracted.
    refined_units = {unit['unit_id'] for unit in units}
    all_units = read(source / 'units.json')
    for unit in all_units:
        original = result_path(source, unit)
        if unit['unit_id'] in refined_units or not original.is_file():
            continue
        shutil.copy2(original, result_path(out, unit))
        for name in ('reviews', 'verifications'):
            artifact = source / name / original.name
            if artifact.is_file():
                shutil.copy2(artifact, out / name / artifact.name)
        image = f"p{unit['pdf_page']:04}.png"
        if not (out / 'images' / image).is_file():
            shutil.copy2(source / 'images' / image, out / 'images' / image)
    export(out, all_units, identity, 'refine_completed', costs, [])
    base, saved, refined = (read(path / 'data/detail_outputs.json') for path in (baseline, source, out))
    comparisons = {}
    for page in pages:
        reference, before, after = (page_records(rows, page) for rows in (base, saved, refined))
        comparisons[str(page)] = {key: dict(before=difference(reference[key], before[key]),
                                           after=difference(reference[key], after[key]))
                                  for key in ('assemblies', 'measurements')}
    dump(out / 'report.json', dict(pages=comparisons, refinements=result, cost=costs))
    print(out / 'report.json')


if __name__ == '__main__':
    main()
