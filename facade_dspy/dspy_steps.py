"""DSPy steps of the classification process that fill gaps left in validated detail outputs.

Each step is a typed dspy.Signature whose output uses the deliverable vocabulary, wrapped in
dspy.BestOfN with a deterministic reward (no model grades itself):

- ClassifyDrawing      step 6: view type of a detail whose drawing type is unknown;
- ClassifyFWR          step 8: bar / piece / parameter for an article the knowledge file cannot type;
- ExtractPieceSpacing  step 9: edge_spacing / spacing of a PIECE, only from printed values;
- ClassifyRelation     Assembly DB: relation of a relation left as 'other', with fastener only for 'fixed'.

A step's answer is accepted only when its reward is 1 (every value printed in the quoted evidence,
vocabulary respected, required clarification present). Accepted answers are marked
evidence_status 'visual' with the step name in inference_reason; rejected ones change nothing.

    python -m facade_dspy.dspy_steps <run-or-snapshot> --out <new folder> --budget-usd 1
"""
import argparse
import re
import shutil
from pathlib import Path
from typing import Literal

import dspy
from pydantic import BaseModel, Field

from . import deliverables, rules
from .schema import validate_output
from .storage import dump, read

RELATIONS = ('clamped', 'bonded', 'fixed', 'supported', 'movement', 'insertion', 'other')
VIEWS = ('profile_2d', 'assembly_section_2d', 'assembly_isometric', 'assembly_orthographic', 'table')
NUMBER = re.compile(r'(?<![\d.])\d+(?:\.\d+)?')


# --- typed outputs -------------------------------------------------------------------------------

class DrawingDecision(BaseModel):
    view_type: Literal['profile_2d', 'assembly_section_2d', 'assembly_isometric', 'assembly_orthographic', 'table']
    reason: str


class FWRDecision(BaseModel):
    article_type: Literal['bar', 'piece', 'parameter']
    reason: str


class EdgeReading(BaseModel):
    printed_value: float
    unit: str
    rail_end: Literal['x0', 'x1'] | None = None
    measured_from: Literal['rail_end', 'dimension_line']
    offset: float | None = Field(default=None, description='Printed dimension-line offset; positive inward.')


class SpacingReading(BaseModel):
    value: float
    unit: str
    bound: Literal['exact', 'minimum', 'maximum', 'interval'] | None = None


class PieceSpacing(BaseModel):
    edge_spacing: EdgeReading | None = None
    spacing: SpacingReading | None = None
    evidence: str = Field(description='Printed text or dimension labels quoted exactly; empty if nothing is printed.')


class RelationDecision(BaseModel):
    relation: Literal['clamped', 'bonded', 'fixed', 'supported', 'movement', 'insertion', 'other']
    relation_clarification: str | None = None
    fastener_type: str | None = Field(default=None, description="Only for 'fixed': the fastener shown, e.g. screw.")
    evidence: str


# --- signatures -------------------------------------------------------------------------------------

class ClassifyDrawing(dspy.Signature):
    """Decide what the catalogue drawing in source.png shows: profile_2d (cross-section of ONE article),
    assembly_section_2d (section through several articles put together), assembly_isometric (3D view of
    an assembly), assembly_orthographic (elevation/plan of an assembly) or table. Judge only the image."""
    detail: dict = dspy.InputField()
    decision: DrawingDecision = dspy.OutputField()


class ClassifyFWR(dspy.Signature):
    """Classify how this curtain-wall article is measured and supplied (FWR type).
    bar: linear profile cut to length (mullion, transom, pressure plate, cover).
    piece: item placed at intervals (clip, bracket, setting block, spacer, toggle, screw).
    parameter: quantified by a length or perimeter, not per piece (gasket, sealant).
    Use only the printed name, description and drawing; give the reason."""
    article: dict = dspy.InputField()
    decision: FWRDecision = dspy.OutputField()


class ExtractPieceSpacing(dspy.Signature):
    """Read how this PIECE is distributed along its rail, only from values printed on the page.
    edge_spacing: from a rail end (x0 or x1) to the centre of the nearest piece; if the printed limit is
    measured to a dimension line, give that line and its printed offset. spacing: centre-to-centre between
    neighbouring pieces on the same rail, with max./min. as bound. Quote the printed text in evidence.
    Never measure from the image and never use general product knowledge; leave unknown fields null."""
    piece: dict = dspy.InputField()
    reading: PieceSpacing = dspy.OutputField()


class ClassifyRelation(dspy.Signature):
    """Classify how SOURCE is assembled with TARGET in the drawing: clamped (held by compression),
    bonded (adhesive or structural silicone), fixed (mechanical fastener; name the fastener type),
    supported (rests on / carries), movement (allowed to move), insertion (sits in a groove or clips in),
    or other with a clarification. Use only what the drawing shows; quote or describe the evidence."""
    claim: dict = dspy.InputField()
    decision: RelationDecision = dspy.OutputField()


OUTPUTS = {ClassifyDrawing: ('decision', DrawingDecision), ClassifyFWR: ('decision', FWRDecision),
           ExtractPieceSpacing: ('reading', PieceSpacing), ClassifyRelation: ('decision', RelationDecision)}


# --- rewards (deterministic) ----------------------------------------------------------------------

def drawing_reward(args, pred):
    return float(pred.decision.view_type in VIEWS and bool(pred.decision.reason.strip()))


def fwr_reward(args, pred):
    return float(bool(pred.decision.reason.strip()))


def _printed(value, text):
    # Signs are directions (inward/outward), not printed characters: compare magnitudes.
    return any(abs(float(n) - abs(float(value))) < 1e-6 for n in NUMBER.findall(text or ''))


def spacing_reward(args, pred):
    """1 only when every number is printed in the quoted evidence and that quote is on the page."""
    reading, page_text = pred.reading, args['piece'].get('page_text') or ''
    if reading.edge_spacing is None and reading.spacing is None:
        return 0.0
    quote = reading.evidence.strip()
    if not quote or (page_text and rules.norm(quote) not in rules.norm(page_text)):
        return 0.0
    edge = reading.edge_spacing
    values = [v for v in (edge and edge.printed_value, edge and edge.offset, reading.spacing and reading.spacing.value)
              if v is not None]
    if not all(_printed(v, quote) for v in values):
        return 0.0
    if edge and edge.measured_from == 'dimension_line' and edge.offset is None:
        return 0.0
    return 1.0


def relation_reward(args, pred):
    decision = pred.decision
    if decision.relation == 'other' and not (decision.relation_clarification or '').strip():
        return 0.0
    if decision.fastener_type and decision.relation != 'fixed':
        return 0.0
    return float(bool(decision.evidence.strip()))


# --- adapter: route every call through the budgeted backend -----------------------------------------

class BackendAdapter(dspy.JSONAdapter):
    """Sends the signature's instructions, typed inputs and output schema to the project's budgeted
    vision backend (one call per attempt), so DSPy modules run on the same receipts and budget."""

    def __init__(self, backend=None, folder=None):
        super().__init__()
        self.backend, self.folder, self.receipts = backend, folder, []

    def __call__(self, lm, lm_kwargs, signature, demos, inputs):
        name, model = next((field, out) for sig, (field, out) in OUTPUTS.items()
                           if set(sig.output_fields) == set(signature.output_fields) == {field}
                           and sig.instructions == signature.instructions)
        attempt = self.folder / f"attempt-{len(self.receipts):02}"
        attempt.mkdir(parents=True, exist_ok=False)
        for image in self.folder.glob('*.png'):
            shutil.copy2(image, attempt / image.name)
        dump(attempt / 'inputs.json', {k: v for k, v in inputs.items()})
        reply = self.backend.execute('review', signature.instructions, model.model_json_schema(), attempt, lambda: None)
        self.receipts.append(dict(folder=str(attempt), raw_response=reply.get('raw_response')))
        return [{name: model.model_validate(reply['output'])}]


def run_step(signature, reward, backend, folder, n=2, **inputs):
    """BestOfN over the backend; returns (output or None, receipts). None unless the reward is 1."""
    adapter = BackendAdapter(backend, folder)
    field = OUTPUTS[signature][0]
    with dspy.context(lm=dspy.LM('openai/catalogue-backend', cache=False), adapter=adapter):
        module = dspy.BestOfN(module=dspy.Predict(signature), N=n, reward_fn=reward, threshold=1.0)
        try:
            prediction = module(**inputs)
        except Exception as error:  # every attempt failed to parse or the backend refused
            return None, adapter.receipts + [dict(error=type(error).__name__ + ': ' + str(error)[:200])]
    accepted = reward(inputs, prediction) == 1.0
    return (getattr(prediction, field) if accepted else None), adapter.receipts


# --- applying the steps to detail outputs -------------------------------------------------------------

def _evidence(page, text):
    return [dict(page=page, kind='detail', bbox=None, text=text)]


def _mark(target, step, reason):
    target['evidence_status'] = 'visual'
    target['inference_reason'] = f'{step}: {reason}'.strip()


def complete(outputs, backend, workdir, images, page_text=None, max_calls=50):
    """Fill gaps in validated detail outputs. images: {page: Path to page PNG}. Returns (outputs, log)."""
    page_text = page_text or {}
    log, calls = [], 0

    def folder_for(name, page):
        folder = workdir / name
        folder.mkdir(parents=True, exist_ok=False)
        if page in images:
            shutil.copy2(images[page], folder / 'source.png')
        return folder

    for output in outputs:
        page, did = output['page'], output['detail_id']
        tag = re.sub(r'[^A-Za-z0-9]+', '_', did.split(':')[-1])[:24]
        if output.get('view_type') is None and calls < max_calls:
            calls += 1
            decision, receipts = run_step(ClassifyDrawing, drawing_reward, backend, folder_for(f'{tag}_drawing', page),
                                          detail=dict(title=output.get('title'), page=page))
            log.append(dict(step='ClassifyDrawing', detail_id=did, accepted=decision is not None, receipts=receipts))
            if decision:
                output['view_type'] = decision.view_type
        for i, article in enumerate(output['article_database']):
            name = article['profile_id'] or article['label']
            if article['article_type'] is None and calls < max_calls:
                calls += 1
                decision, receipts = run_step(ClassifyFWR, fwr_reward, backend, folder_for(f'{tag}_fwr_{i}', page),
                                              article=dict(profile_id=article['profile_id'], label=article['label'],
                                                           role=article['profile_role'], other=article['other']))
                log.append(dict(step='ClassifyFWR', detail_id=did, article=name, accepted=decision is not None, receipts=receipts))
                if decision:
                    article['article_type'] = decision.article_type
                    article.setdefault('other', {})['fwr_reason'] = dict(value=decision.reason, evidence=[])
            if (article['article_type'] == 'piece' and not article.get('edge_spacing') and not article.get('spacing')
                    and calls < max_calls):
                calls += 1
                reading, receipts = run_step(ExtractPieceSpacing, spacing_reward, backend, folder_for(f'{tag}_spacing_{i}', page),
                                             piece=dict(profile_id=article['profile_id'], label=article['label'],
                                                        role=article['profile_role'], page_text=page_text.get(page, '')))
                log.append(dict(step='ExtractPieceSpacing', detail_id=did, article=name, accepted=reading is not None, receipts=receipts))
                if reading:
                    edge = reading.edge_spacing
                    if edge:
                        offset = edge.offset if edge.measured_from == 'dimension_line' else 0.0
                        article['edge_spacing'] = dict(
                            value=edge.printed_value + (offset or 0.0), unit=edge.unit, printed_value=edge.printed_value,
                            offset_applied='none' if not offset else 'added' if offset > 0 else 'subtracted',
                            edge_reference=dict(rail_end=edge.rail_end, measured_from=edge.measured_from,
                                                offset=edge.offset, offset_unit=edge.unit if edge.offset is not None else None),
                            evidence=_evidence(page, reading.evidence))
                    if reading.spacing:
                        article['spacing'] = dict(value=reading.spacing.value, unit=reading.spacing.unit,
                                                  bound=reading.spacing.bound, evidence=_evidence(page, reading.evidence))
                    article['missing_information'] = [m for m in article['missing_information'] if m not in ('edge_spacing', 'spacing')]
                    _mark(article, 'ExtractPieceSpacing', 'values printed on the page')
        for j, item in enumerate(output['assembly_database']):
            if item['relation'] != 'other' or calls >= max_calls:
                continue
            calls += 1
            decision, receipts = run_step(ClassifyRelation, relation_reward, backend, folder_for(f'{tag}_relation_{j}', page),
                                          claim=dict(participants=item['participants'], clarification=item.get('relation_clarification'),
                                                     evidence=[e.get('text') for e in item['evidence']]))
            log.append(dict(step='ClassifyRelation', detail_id=did, assembly_id=item['assembly_id'],
                            accepted=decision is not None, receipts=receipts))
            if decision and decision.relation != 'other':
                item['relation'], item['relation_clarification'] = decision.relation, None
                if decision.relation == 'fixed' and decision.fastener_type and not item.get('fastener_assigned'):
                    item['fastener_assigned'] = dict(article_id=None, type=decision.fastener_type)
                    item['missing_information'].append('fastener article and path not identified')
                item['evidence'] = item['evidence'] + _evidence(page, decision.evidence)
                _mark(item, 'ClassifyRelation', decision.evidence)
    return [validate_output(o) for o in outputs], log


def main():
    from dotenv import load_dotenv

    from .budget import Budget
    from .openai_api import OpenAIResponses
    from .publication import published_directory

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--budget-usd', type=float, required=True)
    parser.add_argument('--max-calls', type=int, default=50)
    args = parser.parse_args()
    load_dotenv()
    source = published_directory(args.run)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    outputs = read(source / 'data/detail_outputs.json')
    images = {int(p.stem[1:]): p for p in (source / 'images').glob('p*.png')}
    # The page's own transcription (Stage 1 source_text), used to check that quoted evidence is printed.
    text = {}
    for path in (source / 'proposals').glob('*.json'):
        record = read(path)
        page = record.get('unit', {}).get('pdf_page')
        text.setdefault(page, []).extend(item.get('text', '') for item in record.get('source_text', []))
    page_text = {page: '\n'.join(lines) for page, lines in text.items()}
    backend = OpenAIResponses(out, Budget(out, args.budget_usd, .1), 'gpt-6.1-sol')
    completed, log = complete(outputs, backend, out / 'steps', images, page_text, args.max_calls)
    catalogue = completed[0]['catalogue'] if completed else source.name
    article_db, assembly_db = deliverables.build(catalogue, completed)
    dump(out / 'data/detail_outputs.json', completed)
    dump(out / 'data/article_db.json', article_db)
    dump(out / 'data/assembly_db.json', assembly_db)
    dump(out / 'dspy_steps_log.json', log)
    print(dict(calls=len(log), accepted=sum(item['accepted'] for item in log), out=str(out)))


if __name__ == '__main__':
    main()
