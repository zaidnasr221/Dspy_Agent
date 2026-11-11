"""Trace missing screw paths from tight PDF crops and existing part occurrences."""
import argparse
import shutil
from pathlib import Path

import pymupdf
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from . import rules
from .budget import Budget
from .publication import published_directory
from .storage import dump, objecthash, read


class Trace(BaseModel):
    bearing_part: str | None
    crossed_parts: list[str]
    engagement_part: str | None
    uncertain: bool
    evidence: str
    locator: list[float] = Field(min_length=4, max_length=4)


PROMPT = (
    'Trace only the indicated fastener from head to tip in crop.png. Use only existing node IDs. '
    'Use page_context.png to follow the printed callout leaders to their actual parts. Follow each contour '
    'from its leader endpoint to the fastener before deciding which part the shaft crosses. '
    'Record the part under the head, all visibly crossed parts in order (including the bearing part if '
    'the shaft crosses it), and the final threaded host separately. Exclude the host from crossed_parts. '
    'Distinguish crossing from adjacency using the contours. Set uncertain if any crossing or the host '
    'cannot be identified. The proposed endpoints come from the existing fixing; use them as search hints '
    'and verify their roles from the shaft and contours. Candidate boxes are approximate full-page locators. '
    'Explain the geometric evidence and give a tight normalized crop-local locator.'
)


def overlaps(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def trace_empty_paths(value, pdf_path, output, backend, heartbeat):
    """Fill only paths with a complete, grounded trace. Return an audit record."""
    nodes = {node['id']: node for node in value['nodes']}
    traces = []
    for index, relation in enumerate(value['relations']):
        fastener = nodes.get(relation.get('fastener_id'))
        if (rules.canonical_predicate(relation['predicate']) != 'fixed_to'
                or relation.get('passes_through') or not fastener or not relation.get('engages_in')
                or relation.get('fixing_evidence') != 'engagement'):
            continue
        x0, y0, x1, y1 = relation['bbox']
        if not (0 < x1 - x0 <= .35 and 0 < y1 - y0 <= .25):
            continue
        box = [max(0, x0 - .022), max(0, y0 - .04), min(1, x1 + .022), min(1, y1 + .04)]
        local = {key: dict(label=node['label'], kind=node['kind'], bbox=node['bbox']) for key, node in nodes.items()
                 if node['detail_id'] == fastener['detail_id'] and overlaps(node['bbox'], box)}
        if not {relation['source'], relation['engages_in'], fastener['id']} <= local.keys():
            continue
        folder = output / rules.claim_id(index)
        folder.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(pdf_path) as pdf:
            page = pdf[value['pdf_page'] - 1]
            w, h = page.rect.width, page.rect.height
            clip = pymupdf.Rect(box[0]*w, box[1]*h, box[2]*w, box[3]*h)
            scale = min(12, 1650 / max(clip.width, clip.height))
            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False).save(folder / 'crop.png')
            context_scale = 2200 / max(w, h)
            page.get_pixmap(matrix=pymupdf.Matrix(context_scale, context_scale), alpha=False).save(folder / 'page_context.png')
        dump(folder / 'nodes.json', dict(fastener=fastener['id'],
                                        proposed_bearing=relation['source'], proposed_host=relation['engages_in'],
                                        candidates=local, crop_full_page_box=box))
        receipt = backend.execute('review', PROMPT, Trace.model_json_schema(), folder, heartbeat)
        trace = Trace.model_validate(receipt['output'])
        path = trace.crossed_parts
        a, b, c, d = trace.locator
        accepted = (
            not trace.uncertain and trace.bearing_part == relation['source']
            and trace.engagement_part == relation['engages_in'] and bool(path)
            and set(path) <= local.keys() - {fastener['id'], trace.engagement_part}
            and len(path) == len(set(path)) and bool(trace.evidence.strip())
            and 0 <= a < c <= 1 and 0 <= b < d <= 1
        )
        result = dict(claim_id=rules.claim_id(index), accepted=accepted, trace=trace.model_dump(),
                      crop_full_page_box=box, receipt=receipt['raw_response'],
                      artifacts=[(folder / name).relative_to(pdf_path.parent).as_posix()
                                 for name in ('crop.png', 'page_context.png', 'nodes.json')],
                      reported_cost_usd=0 if receipt.get('cache_hit') else receipt.get('reported_cost_usd') or 0)
        if accepted:
            relation['passes_through'] = path
        traces.append(result)
    return traces


def main():
    from .openai_api import OpenAIResponses
    from .program import result_path
    from .refine_extraction import export_with_context
    from .schema import validate

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--pages', type=int, nargs='+', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--budget-usd', type=float, required=True)
    args = parser.parse_args()
    load_dotenv()
    source = published_directory(args.run)
    units = [unit for unit in read(source / 'units.json')
             if unit['pdf_page'] in args.pages and result_path(source, unit).is_file()]
    if {unit['pdf_page'] for unit in units} != set(args.pages):
        parser.error('A requested page has no saved proposal')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    budget = Budget(out, args.budget_usd, .05)
    backend = OpenAIResponses(out, budget, 'gpt-6.1-sol')
    for name in ('source.json', 'source.pdf', 'reference_index.json'):
        shutil.copy2(source / name, out / name)
    shutil.copytree(source / 'index', out / 'index')
    for name in ('images', 'reviews', 'verifications'):
        (out / name).mkdir()
    audits = {}
    for unit in units:
        original = result_path(source, unit)
        record = read(original)
        page = unit['pdf_page']
        folder = out / 'fixing_traces' / objecthash(unit['unit_id'])[:20]
        traces = trace_empty_paths(record['extraction'], out / 'source.pdf', folder, backend, lambda: None)
        record['extraction'] = validate(record['extraction'], page)
        record['metadata']['fixing_traces'] = traces
        dump(result_path(out, unit), record)
        for name in ('reviews', 'verifications'):
            artifact = source / name / original.name
            if artifact.is_file():
                shutil.copy2(artifact, out / name / artifact.name)
        image = f'p{page:04}.png'
        shutil.copy2(source / 'images' / image, out / 'images' / image)
        audits[unit['unit_id']] = traces
        dump(out / 'trace_report.json', dict(units=audits, cost=budget.summary()))
    export_with_context(out, source, units, budget.summary(), 'fixing_trace_completed')
    print(out / 'trace_report.json')


if __name__ == '__main__':
    main()
