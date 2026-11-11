"""Explicit cross-references ('see next page', 'refer page 12', 'detail B', 'section 1').

Only references printed in the source (instructions, unresolved notes, transcribed text, drawing
references, evidence quotes) are followed; nothing is linked by proximity or by guess. A reference
that points to a page or detail present in the document is 'resolved'; otherwise 'unresolved'.
"""
import re

RELATIVE = re.compile(r'\b(?:see|refer(?:\s+to)?|continued\s+on|cont\.?\s+on)\s+(?:the\s+)?'
                      r'(?P<where>next|following|previous|preceding)\s+(?:page|sheet)', re.I)
PAGE = re.compile(r'\b(?:see|refer(?:\s+to)?|continued\s+on)\s+(?:page|p\.|pg\.?|sheet)\s*(?P<page>\d{1,4})\b', re.I)
DETAIL = re.compile(r'\b(?:see|refer(?:\s+to)?)\s+(?:the\s+)?(?P<kind>detail|section|view)\s*[-:]?\s*'
                    r'(?P<name>[A-Z0-9][A-Za-z0-9.\-]{0,12})\b', re.I)


def _texts(details, instructions, questions, source_text, relations, measurements):
    for d in details:
        yield d['provenance'], d['canonical_id'], d['title']
        for ref in d.get('drawing_refs') or []:
            yield d['provenance'], d['canonical_id'], ref
    for i in instructions:
        yield i['provenance'], i['canonical_detail_id'], i['text']
    for item in relations:
        yield item['provenance'], None, item.get('evidence') or ''
    for item in measurements:
        yield item['provenance'], None, item.get('evidence') or ''
    for q in questions:
        if q.get('category') == 'source_or_model_question':
            yield q['provenance'], None, q['message']
    for s in source_text:
        yield s['provenance'], None, s['text']


def find(details, instructions, questions, source_text, relations, measurements, reference_index):
    """All explicit references with their resolution. reference_index maps pdf pages to printed pages."""
    pdf_pages = {p['pdf_page'] for p in reference_index}
    printed = {}
    for p in reference_index:
        if p.get('printed_page') is not None:
            printed.setdefault(str(p['printed_page']), p['pdf_page'])
    names = {}
    for d in details:
        for token in re.findall(r'[A-Za-z0-9.\-]+', d['title'] or ''):
            names.setdefault(token.casefold(), set()).add((d['provenance']['pdf_page'], d['canonical_id']))
    links, seen = [], set()
    for provenance, detail_id, text in _texts(details, instructions, questions, source_text, relations, measurements):
        if not text:
            continue
        page = provenance['pdf_page']
        found = []
        for m in RELATIVE.finditer(text):
            step = 1 if m['where'].lower() in ('next', 'following') else -1
            target = page + step
            found.append((m.group(0), 'page', [target] if target in pdf_pages else []))
        for m in PAGE.finditer(text):
            number = m['page']
            target = printed.get(number) or (int(number) if int(number) in pdf_pages and not printed else None)
            found.append((m.group(0).strip(), 'page', [target] if target else []))
        for m in DETAIL.finditer(text):
            hits = sorted(p for p, _ in names.get(m['name'].casefold(), set()) if p != page)
            found.append((m.group(0), m['kind'].lower(), hits))
        for quote, kind, targets in found:
            key = (page, detail_id, quote.casefold())
            if key in seen:
                continue
            seen.add(key)
            links.append(dict(from_page=page, from_detail=detail_id, quote=quote, kind=kind,
                              target_pages=targets, status='resolved' if targets else 'unresolved',
                              provenance=provenance))
    return links
