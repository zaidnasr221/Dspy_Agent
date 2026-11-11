"""Full-page DSPy pilot with explicit article dependencies and structured observations."""
import argparse
import shutil
from pathlib import Path
from typing import Literal

import dspy
from dotenv import load_dotenv
from pydantic import BaseModel

from .budget import Budget
from .openai_api import OpenAIResponses
from .publication import published_directory
from .rules import article_key
from .storage import dump, read


class Part(BaseModel):
    id: str
    label: str
    article_id: str | None
    location: str


class Interface(BaseModel):
    source: str
    target: str
    relation: Literal['contact', 'insertion', 'bond', 'support', 'fixing', 'clamping']
    evidence: str


class Lookup(BaseModel):
    article_id: str
    field: Literal['depth', 'width', 'height', 'thickness']
    question: str


class Measurement(BaseModel):
    occurrence: str
    field: str
    value: float
    unit: str | None
    source_page: int
    evidence: str


class Observations(BaseModel):
    parts: list[Part]
    interfaces: list[Interface]
    measurements: list[Measurement]
    lookups: list[Lookup]


class Resolution(BaseModel):
    measurements: list[Measurement]
    unresolved: list[str]


class ReadDrawing(dspy.Signature):
    """Read the whole original page. Record separate part occurrences with positional descriptions.
    First identify drawn interfaces, then classify only mechanisms the drawing shows. Record a plain
    contact if the mechanism is unclear. Sweep both sides of each joint and each repeated assembly
    occurrence. Examine gaskets, grooves, filler beads, pane edges, cleats and fasteners. Keep distinct
    counterpart occurrences separate. Quote or describe visible geometry for each claim. For measurements
    require clear endpoints and owner. Use occurrence "section" for dimensions spanning multiple parts.
    Request an indexed lookup for dimensions that are absent locally
    for a numbered profile. Use the physical PDF page number and no bounding boxes.
    """
    page: int = dspy.InputField()
    observations: Observations = dspy.OutputField()


class ResolveDrawingDependencies(dspy.Signature):
    """Resolve the requested facts from the numbered source and support page images and article index.
    Check printed dimension and both endpoints on the support drawing before linking it to the same
    article and variant on the target drawing. Match each occurrence separately. Share a printed
    dimension across matching occurrences only when the drawing supports both. Cite the actual physical
    evidence page. Leave unsupported values unresolved. Return only new measurements for existing IDs.
    """
    page: int = dspy.InputField()
    observations: Observations = dspy.InputField()
    dependencies: dict = dspy.InputField()
    resolution: Resolution = dspy.OutputField()


class DrawingAdapter(dspy.JSONAdapter):
    """Run typed DSPy predictors on the existing budgeted image transport."""
    def __init__(self, backend, folder, receipts):
        super().__init__()
        self.backend, self.folder, self.receipts = backend, folder, receipts

    def __call__(self, lm, lm_kwargs, signature, demos, inputs):
        name, field = next(iter(signature.output_fields.items()))
        schema = field.annotation
        dump(self.folder / 'inputs.json', {k: v.model_dump() if isinstance(v, BaseModel) else v
                                          for k, v in inputs.items()})
        receipt = self.backend.execute('review', signature.instructions, schema.model_json_schema(),
                                       self.folder, lambda: None)
        self.receipts.append(receipt['raw_response'])
        return [{name: schema.model_validate(receipt['output'])}]


def find_dependencies(observations, index, page):
    """Retrieve only exact-article, portable facts requested by the reader."""
    article_ids = {article_key(part.article_id) for part in observations.parts if part.article_id}
    dependencies = {}
    for lookup in observations.lookups:
        key = article_key(lookup.article_id)
        if key not in article_ids:
            continue
        entry = index.get(key, {})
        facts = {(f['page'], str(f['value']), f.get('unit')) for f in entry.get('properties', {}).get(lookup.field, [])
                 if f.get('portable') and f.get('page') and f['page'] != page and f.get('value') is not None}
        if facts:
            dependencies.setdefault(key, {})[lookup.field] = [
                dict(page=p, value=v, unit=u) for p, v, u in sorted(facts, key=lambda item: (item[0], item[1], item[2] or ''))]
    return dependencies


def run_page(page, source, out, backend, index):
    folder = out / f'page-{page}'
    read_folder = folder / 'read'
    read_folder.mkdir(parents=True, exist_ok=True)
    image = source / 'images' / f'p{page:04}.png'
    shutil.copyfile(image, read_folder / image.name)
    receipts = []
    lm = dspy.LM('openai/gpt-6.1-sol', cache=False)
    checkpoint = folder / 'observations.json'
    if checkpoint.exists():
        observations = Observations.model_validate(read(checkpoint))
        receipts = read(read_folder / 'receipts.json')
    else:
        with dspy.context(lm=lm, adapter=DrawingAdapter(backend, read_folder, receipts)):
            observations = dspy.Predict(ReadDrawing)(page=page).observations
        dump(read_folder / 'receipts.json', receipts)
        dump(checkpoint, observations.model_dump())
    parts = {p.id: p for p in observations.parts}
    if len(parts) != len(observations.parts) or any(
            edge.source == edge.target or edge.source not in parts or edge.target not in parts or not edge.evidence.strip()
            for edge in observations.interfaces):
        raise ValueError(f'Invalid relationship occurrence on page {page}')
    dependencies = find_dependencies(observations, index, page)
    dump(folder / 'dependencies.json', dependencies)
    section_measurements = []
    local = []
    unresolved = []
    for measurement in observations.measurements:
        if measurement.source_page != page or not measurement.evidence.strip():
            unresolved.append(f'{measurement.field}: missing local evidence')
        elif measurement.occurrence in parts:
            local.append(measurement)
        elif measurement.occurrence == 'section' or any(
                word in measurement.occurrence.casefold() for word in ('assembly', 'joint')):
            section_measurements.append(measurement.model_dump())
        else:
            unresolved.append(f'{measurement.field}: unknown occurrence {measurement.occurrence}')
    observations.measurements = local
    resolution = Resolution(measurements=[], unresolved=[])
    if dependencies:
        support = {fact['page'] for fields in dependencies.values() for facts in fields.values() for fact in facts}
        support = sorted(support, key=lambda n: (abs(n-page), n))[:3]
        resolve_folder = folder / 'resolve'
        resolve_folder.mkdir(exist_ok=True)
        for number in [page, *support]:
            name = f'p{number:04}.png'
            shutil.copyfile(source / 'images' / name, resolve_folder / name)
        with dspy.context(lm=lm, adapter=DrawingAdapter(backend, resolve_folder, receipts)):
            resolution = dspy.Predict(ResolveDrawingDependencies)(page=page, observations=observations,
                                                                  dependencies=dependencies).resolution
        checked = []
        for m in resolution.measurements:
            node = parts.get(m.occurrence)
            facts = dependencies.get(article_key(node.article_id), {}).get(m.field, []) if node and node.article_id else []
            if m.source_page in support and m.evidence.strip() and any(
                    f['page'] == m.source_page and str(m.value) == f['value'] and m.unit == f['unit'] for f in facts):
                checked.append(m)
            else:
                resolution.unresolved.append(f'{m.occurrence}: {m.field} was not supported by an indexed source')
        resolution.measurements = checked
    result = dict(page=page, observations=observations.model_dump(), dependencies=dependencies,
                  section_measurements=section_measurements,
                  resolution=resolution.model_dump(), unresolved_local=unresolved, receipts=receipts)
    dump(folder / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalogue', type=Path)
    parser.add_argument('--pages', type=int, nargs='+', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--budget-usd', type=float, required=True)
    parser.add_argument('--reasoning-effort', choices=('low', 'medium'), default='low')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    load_dotenv()
    source = published_directory(args.catalogue)
    index = read(source / 'data/article_index.json')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=args.resume)
    budget = Budget(out, args.budget_usd, .06)
    backend = OpenAIResponses(out, budget, 'gpt-6.1-sol', reasoning_effort=args.reasoning_effort)
    pages = []
    for page in args.pages:
        completed = out / f'page-{page}' / 'result.json'
        pages.append(read(completed) if args.resume and completed.is_file() else run_page(page, source, out, backend, index))
        dump(out / 'pilot.json', dict(pages=pages, cost=budget.summary()))
    print(out / 'pilot.json')


if __name__ == '__main__':
    main()
