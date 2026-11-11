"""Catalogue-wide article index and cross-page linking.

Article facts are spread over pages: a detail prints only a callout ('235918'), while a table or a
profile sheet elsewhere gives its name, size, weight or variant ('235 918-14.5'). Linking is
deterministic (no model): IDs are normalised, a base ID is linked to a variant only when the detail
itself prints the variant's size, and every linked value keeps its source page.
"""
import re
from collections import defaultdict

from . import rules

VARIANT = re.compile(r'^(?P<base>.+?)-(?P<size>\d+(?:\.\d+)?)$')
INSTALLATION_FIELDS = {'edge_spacing', 'spacing', 'quantity'}
INSTALLATION_TERMS = {'spacing', 'pitch', 'offset', 'quantity', 'qty', 'count', 'fixing', 'installation'}
PORTABLE_MEASUREMENTS = {'depth', 'width', 'height', 'thickness', 'weight', 'weight_per_metre',
                         'mass_per_metre', 'cross_sectional_area', 'second_moment_of_area'}
STANDARD_FIELDS = INSTALLATION_FIELDS | {'depth', 'material', 'finish', 'color'}
SIZE_VALUE = r'(\d+(?:[.,]\d+)?)\s*\(?\s*mm\b'


def _field_name(text):
    return rules.norm(text).replace(' ', '_')


def article_column(header, headers):
    """A bare Number column identifies articles in an item description/material schedule."""
    return (rules.article_column(header)
            or rules.norm(header) == 'number' and {'description','material'} <= {rules.norm(h) for h in headers})


def property_cells(table):
    headers = [cell['header'] for cell in table['fields']]
    return (cell for cell in table['fields'] if not article_column(cell['header'], headers))


def _facts(record, field):
    if field in {'material', 'finish', 'color'}:
        return record.get(field + '_observations') or []
    if field in STANDARD_FIELDS:
        return record.get(field) or []
    facts = [m for m in record.get('measurements', []) if _field_name(m['property']) == field]
    facts += [dict(value=cell['value'], unit=cell.get('unit'), provenance=table['provenance'],
                   source_header=cell['header'], footnotes=table.get('footnotes', []))
              for table in record.get('table_observations', []) for cell in property_cells(table)
              if _field_name(cell['header']) == field
              and cell['value'] is not None]
    return facts


def _own_other_fields(record):
    fields = {_field_name(m['property']) for m in record.get('measurements', [])}
    fields.update(_field_name(cell['header']) for table in record.get('table_observations', [])
                  for cell in property_cells(table))
    return fields - STANDARD_FIELDS - {'c/c_spacing', 'centre-to-centre_spacing', 'qty'}


def article_base(value):
    """('235918', '14.5') for '235 918-14.5'; ('32374', None) for '32374'."""
    key = rules.article_key(value) if value else None
    if not key:
        return None, None
    match = VARIANT.match(key)
    return (match['base'], match['size']) if match else (key, None)


def _pages(record):
    pages = {o['provenance']['pdf_page'] for o in record.get('occurrences', [])}
    pages |= {t['provenance']['pdf_page'] for t in record.get('table_observations', [])}
    return sorted(pages)


def build_index(article_database):
    """Index keyed by normalised article ID: names, every field value with its page, pages seen, variants."""
    index = {}
    variants = defaultdict(list)
    for record in article_database:
        key = rules.article_key(record['profile_id']) if record.get('profile_id') else None
        if not key:
            continue
        entry = index.setdefault(key, dict(profile_id=record['profile_id'], names=[], properties={},
                                           seen_on=[], variants=[], record_ids=[], conflicts=[]))
        entry['record_ids'].append(record['record_id'])
        names = [o['label'] for o in record.get('occurrences', [])]
        names += [f['value'] for t in record.get('table_observations', []) for f in property_cells(t)
                  if f['value']]
        entry['names'] = list(dict.fromkeys(entry['names'] + [n for n in names if n]))
        entry['seen_on'] = sorted(set(entry['seen_on']) | set(_pages(record)))
        for field in sorted(STANDARD_FIELDS | _own_other_fields(record)):
            for fact in _facts(record, field):
                portable = not INSTALLATION_TERMS.intersection(re.findall(r'[a-z]+', field)) and (
                    field in STANDARD_FIELDS or field in PORTABLE_MEASUREMENTS or fact.get('source_header'))
                entry['properties'].setdefault(field, []).append(dict(
                    fact, page=(fact.get('provenance') or {}).get('pdf_page'), portable=bool(portable)))
        base, size = article_base(record['profile_id'])
        if size:
            variants[base].append(dict(key=key, size=size))
    for base, items in variants.items():
        if base in index:
            index[base]['variants'] = sorted({i['key'] for i in items})
    for entry in index.values():
        for field, facts in entry['properties'].items():
            values = {(str(f['value']), f.get('unit')) for f in facts if f['value'] is not None}
            if len(values) > 1:
                entry['conflicts'].append(dict(field=field, claims=sorted(values, key=lambda v:(v[0],v[1] or ''))))
    return index


def _printed_sizes(record):
    article = rules.article_key(record['profile_id'])
    pattern = re.compile(r'(?<![\w.-])' + re.escape(article) + r'(?:\s*[:=]?\s*\(\s*|\s*[:=]?\s+)'
                         + SIZE_VALUE, re.I)
    texts = [re.sub(r'(?<=\d)[ \u00a0]+(?=\d{3}(?![\d.,]))', '', o.get(field) or '')
             for o in record.get('occurrences', []) for field in ('label', 'callout')]
    sizes = {float(size.replace(',', '.')) for text in texts for size in pattern.findall(text)}
    if not sizes:
        for occurrence in record.get('occurrences', []):
            if occurrence.get('article_id') and rules.article_key(occurrence['article_id']) == article:
                sizes.update(float(value.replace(',', '.')) for field in ('callout', 'label')
                             for value in re.findall(SIZE_VALUE, occurrence.get(field) or '', re.I))
    return sizes


def link(article_database, index):
    """Attach cross-page facts to every article record. A base ID printed in a detail is linked to one
    variant only when the detail prints that variant's size; otherwise all variants are candidates and
    the link stays unresolved. Returns (records, issues)."""
    issues = []
    for record in article_database:
        record.setdefault('linked', [])
        record.setdefault('link_status', 'no_article_id')
        if not record.get('profile_id'):
            continue
        key = rules.article_key(record['profile_id'])
        entry = index.get(key)
        own = set(entry['record_ids']) if entry else set()
        other_pages = [p for p in (entry['seen_on'] if entry else []) if p not in _pages(record)]
        record['link_status'] = 'linked' if other_pages else 'single_source'
        if entry and other_pages:
            record['linked'].append(dict(article_key=key, pages=other_pages, record_ids=sorted(own - {record['record_id']})))
        base, size = article_base(record['profile_id'])
        candidates = (index.get(base) or {}).get('variants', []) if not size else []
        if candidates:
            sizes_by_occurrence = [_printed_sizes(dict(record, occurrences=[occurrence]))
                                   for occurrence in record.get('occurrences', [])]
            sizes = set.intersection(*sizes_by_occurrence) if sizes_by_occurrence else set()
            chosen = [c for c in candidates if article_base(c)[1] and float(article_base(c)[1]) in sizes]
            if len(chosen) == 1:
                target = index[chosen[0]]
                record['linked'].append(dict(article_key=chosen[0], pages=target['seen_on'], basis='printed_size',
                                             record_ids=target['record_ids']))
                record['link_status'] = 'linked_variant'
            else:
                record['link_status'] = 'variant_unresolved'
                record['variant_candidates'] = candidates
                issues.append(dict(category='article_variant_unresolved', article_id=record['profile_id'],
                                   candidates=candidates, pages=_pages(record)))
    return article_database, issues


def for_occurrence(record, occurrence, index):
    """Resolve a base article's variant using only this drawing's evidence."""
    if not record.get('profile_id'):
        return record
    scoped = dict(record, occurrences=[occurrence], linked=[])
    scoped.pop('variant_candidates', None)
    link([scoped], index)
    tables = scoped.get('table_observations') or []
    dimensions = [_table_sizes(table) for table in tables]
    occurrence_sizes = [(o, _printed_sizes(dict(record, occurrences=[o]))) for o in record.get('occurrences', [])]
    variant_sizes = [*dimensions, *(sizes for _, sizes in occurrence_sizes)]
    if len({frozenset(sizes) for sizes in variant_sizes if sizes}) <= 1:
        return scoped
    printed = _printed_sizes(scoped)
    keep = [table for table, sizes in zip(tables, dimensions) if not sizes or len(printed)==1 and sizes==printed]
    excluded = [table['provenance'] for table in tables if table not in keep]
    scoped['table_observations'] = keep
    excluded_owners = {o['canonical_id'] for o, sizes in occurrence_sizes if o.get('canonical_id')
                       and o is not occurrence and sizes != printed}
    scoped['_excluded_variant_owners'] = excluded_owners
    for field in ('depth', 'edge_spacing', 'spacing', 'quantity', 'measurements',
                  'material_observations', 'finish_observations', 'color_observations'):
        scoped[field] = [fact for fact in scoped.get(field, []) if fact.get('provenance') not in excluded
                         and (fact.get('canonical_subject_id') or fact.get('occurrence_id')) not in excluded_owners]
    if len(printed) != 1 or not any(sizes == printed for sizes in variant_sizes):
        scoped['link_status'] = 'variant_unresolved'
        scoped['variant_candidates'] = [f'{record["profile_id"]} {size:g} mm'
                                        for size in sorted(set().union(*variant_sizes))]
    return scoped


def _table_sizes(table):
    dimensions = set()
    for cell in table['fields']:
        header = _field_name(cell['header'])
        value = str(cell.get('value') or '')
        if header not in {'shape','shape_annotation','description','size','depth','profile_depth','article_depth'}:
            continue
        if rules.norm(cell.get('unit') or '') == 'mm' and re.fullmatch(r'\d+(?:[.,]\d+)?', value.strip()):
            value += ' mm'
        dimensions.update(float(match.replace(',', '.')) for match in re.findall(SIZE_VALUE, value, re.I))
    return dimensions


def linked_facts(record, index, field):
    """Facts for one field from the record itself, then from linked records on other pages (with pages)."""
    facts = list(_facts(record, field))
    if field in INSTALLATION_FIELDS:
        return facts
    for link in record.get('linked', []):
        entry = index.get(link['article_key'])
        if entry:
            facts += [dict(f, linked_from=link['article_key']) for f in entry['properties'].get(field, [])
                      if f.get('page') in link['pages'] and f.get('portable')
                      and (f.get('canonical_subject_id') or f.get('occurrence_id'))
                      not in record.get('_excluded_variant_owners', ())]
    return facts


def other_fields(record, index):
    fields = _own_other_fields(record)
    for link in record.get('linked', []):
        fields.update(field for field,facts in index[link['article_key']]['properties'].items()
                      if any(f.get('portable') and f.get('page') in link['pages'] for f in facts))
    return sorted(fields - STANDARD_FIELDS)
