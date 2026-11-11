"""Recover relationships absent from saved PDF section proposals in one batch."""
import argparse
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from . import rules
from .budget import Budget, BudgetStopped
from .exports import _overlap, relationship_status
from .openai_api import OpenAIResponses
from .program import result_path
from .publication import published_directory
from .refine_extraction import export_with_context
from .schema import Relation, validate
from .storage import dump, read


class Addition(BaseModel):
    region: str
    source: str
    target: str
    predicate: str
    bbox: list[float] = Field(min_length=4, max_length=4)
    evidence: str
    fastener_id: str | None = None
    path: list[str] = Field(default_factory=list)


class Discovery(BaseModel):
    additions: list[Addition]


class Verdict(BaseModel):
    candidate: int
    supported: bool
    reason: str
    contact_only: bool = False
    corrected_target: str | None = None


class Check(BaseModel):
    verdicts: list[Verdict]


DISCOVER = (
    'Find omitted relationships in each numbered physical PDF page. Each region has existing part IDs, '
    'approximate full-page boxes, and the claims already made. Return only genuinely missing claims, up to '
    'five per page. Start with every orphan_fastener: follow the screw shaft and identify bearing part, '
    'crossed parts in order, and threaded host. For fixed_to use source=bearing part, target=host, '
    'fastener_id and path (crossed node IDs, excluding host). Omit if the path or host is ambiguous. '
    'Then trace each section interface on both sides, including cleats in chambers, structural silicone '
    'interfaces, gasket retention and spacer seats. Use inserted_into, bonded_to, supported_by, supports, '
    'clamped_by or contacts (visible touch without an established function). Count each drawn occurrence '
    'separately. Skip duplicates and incidental adjacency. Do not introduce nodes or copy relationships '
    'across drawings. Return tight bboxes normalized to the full page and a short visual cue for each.'
)

VERIFY = (
    'Independently check each candidate against the corresponding physical PDF page image and region '
    'part IDs and boxes. Determine that the exact source and target occurrences participate in the '
    'geometry shown. The crop manifest gives each image rectangle in full-page coordinates. '
    'described mechanism at the stated locator. Reject uncertain, duplicated, or inferred claims. '
    'For a fixing verify shaft, threaded host and every crossed part in the proposed order. For contacts '
    'verify touching contours. Return a verdict for each numbered candidate and a visual reason of at most 15 words.'
)

TYPES = {
    'fixed_to': 'fixed', 'inserted_into': 'insertion', 'bonded_to': 'bonded',
    'supported_by': 'supported', 'supports': 'supported', 'clamped_by': 'clamped', 'contacts': 'other',
}


def edge_key(relation):
    predicate = rules.canonical_predicate(relation['predicate'])
    source, target = relation['source'], relation['target']
    if predicate == 'contacts':
        source, target = sorted((source, target))
    elif predicate == 'supports':
        source, target, predicate = target, source, 'supported_by'
    return source, predicate, target, relation.get('fastener_id')


def region_inputs(record):
    value = record['extraction']
    used = {r.get('fastener_id') for r in value['relations']}
    used.update(r['target'] for r in value['relations'] if rules.canonical_predicate(r['predicate']) == 'fastened_by')
    return dict(page=record['unit']['pdf_page'],
                nodes={n['id']: [n['label'], n['article_id'], n['bbox'], n['detail_id']] for n in value['nodes']},
                existing=[list(edge_key(r)) for r in value['relations']],
                orphan_fasteners=[n['id'] for n in value['nodes']
                                  if rules.family(n) in {'screw', 'fastener', 'bolt', 'rivet'} and n['id'] not in used])


def proposals(additions, records):
    accepted = []
    seen = {uid: {edge_key(r) for r in rec['extraction']['relations']} for uid, rec in records.items()}
    for add in additions:
        record = records.get(add.region)
        predicate = rules.canonical_predicate(add.predicate)
        if not record or predicate not in TYPES or not add.evidence.strip():
            continue
        unit, value = record['unit'], record['extraction']
        nodes = {n['id']: n for n in value['nodes']}
        if add.source not in nodes or add.target not in nodes:
            continue
        fixing = predicate == 'fixed_to'
        if fixing:
            fastener = nodes.get(add.fastener_id)
            if (not fastener or rules.family(fastener) not in {'screw', 'bolt', 'rivet', 'fastener'}
                    or not add.path or add.path[0] != add.source or add.target in add.path
                    or add.fastener_id not in region_inputs(record)['orphan_fasteners']
                    or not _overlap(add.bbox, fastener['bbox'])):
                continue
        elif add.fastener_id or add.path:
            continue
        relation = Relation(source=add.source, target=add.target, predicate=predicate, basis='drawing_reading',
                            condition=None, bbox=add.bbox, evidence=add.evidence, relation_type=TYPES[predicate],
                            relation_clarification='Visible physical contact' if predicate == 'contacts' else None,
                            fastener_id=add.fastener_id, passes_through=add.path or None,
                            engages_in=add.target if fixing else None, fixing_evidence='engagement' if fixing else None,
                            evidence_status='visual').model_dump()
        key = edge_key(relation)
        if key in seen[add.region] or relationship_status(relation, nodes[add.source], nodes[add.target], False,
                                                         scope=unit['bbox'], nodes=nodes)[0] != 'proposed':
            continue
        try:
            validate(dict(value, relations=[*value['relations'], relation]), unit['pdf_page'])
        except ValueError:
            continue
        seen[add.region].add(key)
        accepted.append(dict(candidate=len(accepted), region=add.region, page=unit['pdf_page'], relation=relation))
    return accepted


def apply_checked(record, verification, candidates, checks):
    relations = record['extraction']['relations']
    verdicts = verification['verdicts']
    nodes = {n['id']: n for n in record['extraction']['nodes']}
    seen = {edge_key(r) for r in relations}
    by_candidate = defaultdict(list)
    for check in checks:
        by_candidate[check.candidate].append(check)
    approved = [(c, by_candidate[c['candidate']][0]) for c in candidates
                if len(by_candidate[c['candidate']]) == 1 and by_candidate[c['candidate']][0].supported
                and by_candidate[c['candidate']][0].reason.strip()]
    fixing_counts = Counter(c['relation']['fastener_id'] for c, _ in approved if c['relation']['fastener_id'])
    for candidate, check in approved:
        relation = dict(candidate['relation'])
        if fixing_counts[relation['fastener_id']] > 1:
            continue
        if check.contact_only and relation['predicate'] != 'fixed_to':
            relation.update(predicate='contacts', relation_type='other',
                            relation_clarification='Visible physical contact', evidence=check.reason)
        if check.corrected_target and relation['predicate'] == 'inserted_into':
            relation.update(target=check.corrected_target, evidence=check.reason)
        if relation['target'] not in nodes or edge_key(relation) in seen:
            continue
        if relationship_status(relation, nodes[relation['source']], nodes[relation['target']], False,
                               scope=record['unit']['bbox'], nodes=nodes)[0] != 'proposed':
            continue
        seen.add(edge_key(relation))
        ident = rules.claim_id(len(relations))
        relations.append(relation)
        verdicts.append(dict(claim_id=ident, verdict='supported', reason=check.reason,
                             **{k: relation[k] for k in ('source', 'predicate', 'target', 'bbox')}))
        candidate['exported_claim'] = ident
    validate(record['extraction'], record['unit']['pdf_page'])


def orphan_inputs(pdf_path, folder, context):
    folder.mkdir(exist_ok=True)
    manifest = {}
    with pymupdf.open(pdf_path) as pdf:
        for uid, region in context.items():
            page = pdf[region['page'] - 1]
            w, h = page.rect.width, page.rect.height
            for screw in region['orphan_fasteners']:
                x0, y0, x1, y1 = region['nodes'][screw][2]
                box = [max(0, x0-.055), max(0, y0-.048), min(1, x1+.055), min(1, y1+.048)]
                clip = pymupdf.Rect(box[0]*w, box[1]*h, box[2]*w, box[3]*h)
                scale = min(10, 1050 / max(clip.width, clip.height))
                name = f"p{region['page']:04}-{len(manifest)}.png"
                page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False).save(folder / name)
                nodes = {key: item for key, item in region['nodes'].items()
                         if item[3] == region['nodes'][screw][3] and all(
                             item[2][i] < box[i+2] and box[i] < item[2][i+2] for i in (0, 1))}
                manifest[name] = dict(region=uid, fastener=screw, crop_full_page_box=box, nodes=nodes)
    dump(folder / 'orphan_manifest.json', manifest)
    return manifest


def discover_orphans(source, out, context, candidates, records, backend):
    linked = {(c['region'], c['relation']['fastener_id']) for c in candidates}
    missing = {uid: dict(region, orphan_fasteners=[
        screw for screw in region['orphan_fasteners'] if (uid, screw) not in linked])
               for uid, region in context.items()}
    if not any(region['orphan_fasteners'] for region in missing.values()):
        return None
    folder = out / 'orphan_inputs'
    orphan_inputs(source / 'source.pdf', folder, missing)
    receipt = backend.execute(
        'coverage_check',
        'Trace only the orphan screw indicated for each named crop. The manifest gives the crop box '
        'in full-page coordinates and nearby node IDs with labels and boxes. Use the shaft, head and '
        'threaded receiver to identify the exact bearing part and host. Include every part crossed '
        'in order, excluding the host. Distinguish crossing from adjacency. Return only fixed_to '
        'claims with region ID, fastener ID, ordered path and tight full-page bbox. Omit ambiguous traces.',
        Discovery.model_json_schema(), folder, lambda: None)
    additions = [a for a in Discovery.model_validate(receipt['output']).additions
                 if a.predicate == 'fixed_to' and a.region in missing
                 and a.fastener_id in missing[a.region]['orphan_fasteners']]
    candidates.extend(dict(c, candidate=i) for i, c in enumerate(proposals(additions, records), len(candidates)))
    return receipt['raw_response']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--pages', nargs='+', type=int, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--budget-usd', type=float, required=True)
    parser.add_argument('--resume', action='store_true', help='Reuse completed discovery after a budget stop')
    parser.add_argument('--trace-orphans', action='store_true', help='Resume with a single close-crop batch for unlinked screws')
    parser.add_argument('--reviewed', type=Path, help='Apply explicitly reviewed candidate verdicts to a saved discovery batch')
    args = parser.parse_args()
    if args.reviewed and (not args.resume or args.trace_orphans):
        parser.error('Reviewed verdicts require a resumed discovery without orphan tracing')
    load_dotenv()
    source = published_directory(args.run)
    units = [u for u in read(source / 'units.json')
             if u['pdf_page'] in args.pages and result_path(source, u).is_file()]
    if {u['pdf_page'] for u in units} != set(args.pages):
        parser.error('A requested page has no saved region proposal')
    out = args.out.resolve()
    records = {u['unit_id']: read(result_path(source, u)) for u in units}
    folder = out / 'discovery'
    context = {uid: region_inputs(record) for uid, record in records.items()}
    budget = Budget(out, args.budget_usd, .05)
    backend = None if args.reviewed else OpenAIResponses(out, budget, 'gpt-6.1-sol')
    report = out / 'coverage_pass.json'
    orphan_receipt = None
    verification_receipt = None
    machine_checks = []
    if args.resume:
        checkpoint = read(report)
        if ((not checkpoint.get('budget_stopped') and not args.reviewed) or context != read(folder / 'regions.json')
                or read(out / 'source.json')['source_sha256'] != read(source / 'source.json')['source_sha256']):
            parser.error('Resume requires unchanged input and a saved budget stop')
        discovery = checkpoint['discovery']
        candidates = checkpoint['candidates']
        orphan_receipt = checkpoint.get('orphan_discovery')
        verification_receipt = checkpoint.get('verification')
        machine_checks = checkpoint.get('machine_checks', checkpoint.get('checks', []))
        for c in candidates:
            c.pop('exported_claim', None)
    else:
        out.mkdir(parents=True, exist_ok=False)
        for name in ('source.json', 'source.pdf', 'reference_index.json'):
            shutil.copy2(source / name, out / name)
        for name in ('index', 'proposals', 'reviews', 'verifications', 'images', 'fixing_traces', 'raw_responses'):
            if (source / name).is_dir():
                shutil.copytree(source / name, out / name)
        folder.mkdir()
        dump(folder / 'regions.json', context)
        with pymupdf.open(source / 'source.pdf') as pdf:
            for page in sorted(set(args.pages)):
                drawing = pdf[page - 1]
                scale = 1250 / max(drawing.rect.width, drawing.rect.height)
                drawing.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(folder / f'p{page:04}.png')
        first = backend.execute('review', DISCOVER, Discovery.model_json_schema(), folder, lambda: None)
        discovery = first['raw_response']
        candidates = proposals(Discovery.model_validate(first['output']).additions, records)
        dump(report, dict(discovery=discovery, candidates=candidates, cost=budget.summary()))
    stopped = False
    checks = []
    if args.trace_orphans:
        if not args.resume or orphan_receipt:
            parser.error('Orphan tracing requires a resumed discovery with no previous orphan receipt')
    if not orphan_receipt and not args.reviewed:
        try:
            orphan_receipt = discover_orphans(source, out, context, candidates, records, backend)
        except BudgetStopped:
            stopped = True
        dump(report, dict(discovery=discovery, orphan_discovery=orphan_receipt, candidates=candidates,
                          budget_stopped=True, cost=budget.summary()))
    if args.trace_orphans:
        stopped = True
    elif args.reviewed:
        if not args.resume:
            parser.error('Reviewed verdicts require a saved discovery batch')
        overrides = Check.model_validate(read(args.reviewed)).verdicts
        if len({v.candidate for v in overrides}) != len(overrides) or any(
                not 0 <= v.candidate < len(candidates) for v in overrides):
            parser.error('Reviewed verdicts must identify unique candidates in this batch')
        combined = {v['candidate']: Verdict.model_validate(v) for v in machine_checks}
        combined.update({v.candidate: v for v in overrides})
        checks = list(combined.values())
    elif candidates and not stopped:
        check = out / 'verification_inputs'
        check.mkdir(exist_ok=True)
        compact = []
        for c in candidates:
            r = c['relation']
            nodes = context[c['region']]['nodes']
            ids = {r['source'], r['target'], r['fastener_id'], *(r['passes_through'] or [])} - {None}
            compact.append(dict(candidate=c['candidate'], page=c['page'], predicate=r['predicate'],
                                box=r['bbox'], source=r['source'], target=r['target'],
                                fastener=r['fastener_id'], path=r['passes_through'],
                                parts={key:nodes[key][:3] for key in sorted(ids)}))
        (check / 'regions.json').unlink(missing_ok=True)
        dump(check / 'candidates.json', compact)
        crops = {}
        with pymupdf.open(source / 'source.pdf') as pdf:
            for page in sorted({c['page'] for c in candidates}):
                boxes = [c['box'] for c in compact if c['page'] == page]
                box = [max(0, min(b[i] for b in boxes) - .045) if i < 2
                       else min(1, max(b[i] for b in boxes) + .045) for i in range(4)]
                drawing = pdf[page - 1]
                w, h = drawing.rect.width, drawing.rect.height
                clip = pymupdf.Rect(box[0]*w, box[1]*h, box[2]*w, box[3]*h)
                scale = min(9, 850 / max(clip.width, clip.height))
                name = f'p{page:04}.png'
                drawing.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False).save(check / name)
                crops[name] = box
        dump(check / 'crops.json', crops)
        try:
            reply = backend.execute('coverage_check', VERIFY, Check.model_json_schema(), check, lambda: None)
            verification_receipt = reply['raw_response']
            checks = Check.model_validate(reply['output']).verdicts
            for check in checks:
                check.contact_only = False
                check.corrected_target = None
            machine_checks = [v.model_dump() for v in checks]
        except BudgetStopped:
            stopped = True
    for uid, record in records.items():
        path = result_path(out, record['unit'])
        check_path = out / 'verifications' / path.name
        original_check = source / 'verifications' / path.name
        verification = read(original_check) if original_check.is_file() else dict(unit_id=uid, verdicts=[])
        apply_checked(record, verification, [c for c in candidates if c['region'] == uid], checks)
        dump(path, record)
        dump(check_path, verification)
    export_with_context(out, source, units, budget.summary(), 'relationship_coverage_replayed')
    dump(report, dict(discovery=discovery, orphan_discovery=orphan_receipt,
         verification=verification_receipt, budget_stopped=stopped, machine_checks=machine_checks,
         reviewed_verdicts=str(args.reviewed.resolve()) if args.reviewed else None,
         candidates=candidates, checks=[v.model_dump() for v in checks], cost=budget.summary()))
    print(report)


if __name__ == '__main__':
    main()
