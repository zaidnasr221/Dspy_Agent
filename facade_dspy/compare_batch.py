"""Compare two saved catalogue runs page by page."""
import argparse
import re
from collections import Counter
from pathlib import Path

from .publication import published_directory
from .rules import article_key, norm
from .storage import dump, read

MEASUREMENTS = ('depth', 'spacing', 'edge_spacing', 'quantity')
SCORE_FIELDS = ('articles', 'assemblies', 'relationships', 'fixings', 'fixing_paths', 'measurements')
LABEL_ALIASES = {
    'backer rod': 'backing rod',
    'aluminum bent sheet': 'aluminium bent sheet',
    'silicone sealant on backing rod': 'silicone sealant',
}


def part_name(label):
    label = re.sub(r' \([a-z]+[0-9]+\)$', '', norm(label))
    return LABEL_ALIASES.get(label, label)


def page_records(details, page):
    rows = [detail for detail in details if detail['page'] == page]
    articles, assemblies, measurements = Counter(), Counter(), Counter()
    relationships, fixings, fixing_paths = Counter(), Counter(), Counter()
    for detail in rows:
        occurrences = {}
        for article in detail['article_database']:
            literal = article.get('profile_id')
            identity = article_key(literal) if literal else part_name(article['label'])
            occurrences[article['occurrence_id']] = identity
            if literal:
                articles[identity] += 1
            for field in MEASUREMENTS:
                fact = article.get(field) or {}
                value = fact.get('value')
                if value is not None:
                    number = tuple(value) if isinstance(value, list) else value
                    measurements[(identity, field, number, fact.get('unit'))] += 1
        for assembly in detail['assembly_database']:
            participants = tuple(sorted(
                (part['role'], occurrences.get(part.get('occurrence_id'), part_name(part['article'])))
                for part in assembly['participants']
            ))
            fixing = assembly.get('fastener_assigned') or {}
            path = fixing.get('path_occurrence_ids') or fixing.get('passes_through') or []
            host = fixing.get('engages_in_occurrence_id') or fixing.get('engages_in')
            fastener = (fixing.get('type'), tuple(occurrences.get(part, part_name(part)) for part in path),
                        occurrences.get(host, part_name(host)) if host else None)
            assemblies[(assembly['relation'], participants, fastener)] += 1
            if assembly['relation'] == 'fixed':
                fixing_key = (participants, fastener[0], fastener[2])
                fixings[fixing_key] += 1
                if fastener[1]:
                    fixing_paths[(fixing_key, fastener[1])] += 1
            else:
                relationships[(assembly['relation'], participants)] += 1
    systems = sorted(detail['system']['type'] for detail in rows if detail['system']['type'])
    return dict(details=len(rows), systems=systems, articles=articles, assemblies=assemblies,
                relationships=relationships, fixings=fixings, fixing_paths=fixing_paths, measurements=measurements)


def difference(before, after):
    return dict(before=before.total(), after=after.total(), shared=(before & after).total(),
                baseline_only=list((before-after).items()), rerun_only=list((after-before).items()))


def compare_runs(baseline, rerun, pages):
    left, right = (published_directory(path) for path in (baseline, rerun))
    if read(left/'document.json')['sha256'] != read(right/'document.json')['sha256']:
        raise ValueError('The runs must refer to the same source PDF')
    before, after = (read(folder/'data/detail_outputs.json') for folder in (left, right))
    coverage = [set(read(folder/'coverage.json')['complete_proposal_pages']) for folder in (left, right)]
    results = []
    for page in pages:
        old, new = (page_records(data, page) for data in (before, after))
        results.append(dict(page=page, baseline_complete=page in coverage[0], rerun_complete=page in coverage[1],
                            details=[old['details'], new['details']], systems=[old['systems'], new['systems']],
                            **{key:difference(old[key], new[key]) for key in SCORE_FIELDS}))
    return dict(baseline_snapshot=str(left), rerun_snapshot=str(right), pages=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('rerun', type=Path)
    parser.add_argument('--pages', default='10-19')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    from .cli import page_numbers

    try:
        pages = page_numbers(args.pages)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    result = compare_runs(args.baseline, args.rerun, pages)
    dump(args.out, result)
    print(args.out.resolve())


if __name__ == '__main__':
    main()
