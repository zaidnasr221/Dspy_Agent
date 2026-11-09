"""Generic engineering rules for catalogue extraction. One rule set for every manufacturer.

Covers literal article IDs, dimensions, article columns in tables, drawing references,
the controlled relationship vocabulary, and the identity keys used to scope components.
Model output is never trusted for these decisions; this module applies them afterwards.
"""
import re
from collections import Counter, defaultdict

# Drawing/sheet numbers are not article IDs (legacy catalogue conventions; harmless elsewhere).
DRAWING_REFERENCE_PATTERNS = (r'K\d{7}', r'geff[fpc]?\d*')

ARTICLE_PATTERNS = (
    r'[A-Za-z][A-Za-z0-9_.-]{2,24}',                                    # EM070, ALU-50-123
    r'\d{1,3}[A-Za-z]{1,4}\d{1,6}',                                     # 50SG02
    r'[A-Za-z][A-Za-z0-9_.-]*(?:[  ][A-Za-z0-9_.-]+){1,3}',        # ALU 1234
    r'\d{3}[  ]?\d{3}(?:-\d+(?:\.\d+)?)?',                         # 235 918, 235 918-11.5
    r'\d{5,10}(?:-\d+(?:\.\d+)?)?',                                     # 32476, 235918-20.5
    r'\d{2,}(?:[.\-/]\d+){1,4}(?:[.\-][A-Za-z0-9-]+)?',                # 030.6400.--
)
TOOL_ARTICLE_PATTERNS = (r'\d{4}',)
TOOL_KIND_KEYWORDS = ('tool', 'jig')

# Ranges first, so '5 - 7 mm' is masked whole rather than leaving '5'.
MEASUREMENT_PATTERNS = (
    r'\d+(?:[.,]\d+)?\s*[-–]\s*\d+(?:[.,]\d+)?\s*(?:mm|cm|m)',
    r'\d+(?:[.,]\d+)?\s*(?:mm|cm|m|kg|°|deg)',
    r'\d{1,3}[.,]\d{1,2}',
)

ARTICLE_COLUMN_PATTERNS = (r'Art\.?[- ]?(?:Nr|No)', r'article', r'reference', r'part\s*(?:no|number)',
                           r'item\s*(?:no|code)', r'code')
ARTICLE_COLUMN_EXCLUDE_PATTERNS = (r'drawing',)

# Most complete pattern first: a variant ID is never split into an ID plus a 'dimension'.
TABLE_ARTICLE_TOKEN_PATTERNS = (
    r'\d{3}[  ]?\d{3}-\d+(?:\.\d+)?',
    r'\d{5,10}-\d+(?:\.\d+)?',
    r'\d{3,}[.\-/]\d{3,}(?:[.\-/][0-9A-Za-z-]+)*',
    r'\d{1,3}[A-Za-z]{1,4}\d{1,6}',
    r'[A-Z]{1,4}\d{3,8}',
    r'\d{3}[  ]?\d{3}',
    r'\d{5,10}',
)

# Things that are never components and never relationship endpoints.
NON_COMPONENT_KINDS = frozenset({'drawing_reference', 'unresolved_reference', 'dimension', 'measurement',
                                 'annotation', 'label', 'position', 'step'})

# Controlled relationship vocabulary: definition (shown to the model) and accepted synonyms.
PREDICATES = {
    'fastened_by': ('SOURCE is held by fastener TARGET (screw, bolt, rivet, anchor).',
                    ('fixed_with', 'screwed_with', 'secured_by', 'fastened_with')),
    'fixed_to': ('SOURCE is mechanically attached to TARGET.',
                 ('attached_to', 'mounted_on', 'mounted_to', 'screwed_to', 'secured_to')),
    'inserted_into': ('SOURCE sits in a groove, chamber or slot of TARGET.',
                      ('fits_into', 'installed_in', 'inserted_in', 'slides_into', 'located_in')),
    'clipped_into': ('SOURCE snaps or clips onto/into TARGET.', ('clipped_to', 'snapped_into', 'clips_into')),
    'seals_against': ('Gasket/seal SOURCE presses against TARGET to seal.', ('sealed_against',)),
    'supports': ('SOURCE carries the load of TARGET (setting block, bracket).', ('carries', 'bears')),
    'supported_by': ('SOURCE rests on TARGET.', ('rests_on', 'sits_on', 'carried_by')),
    'bonded_to': ('SOURCE is glued or structurally bonded to TARGET.', ('glued_to', 'structurally_bonded_to')),
    'isolates': ('Thermal/electrical isolator SOURCE separates TARGET from another part.',
                 ('thermally_separates', 'separates')),
    'spaces': ('Spacer SOURCE keeps TARGET panes or parts apart.', ('spacer_for',)),
    'alternative_to': ('SOURCE may be used instead of TARGET (explicitly stated).',
                       ('alternative_of', 'replaces_as_alternative')),
}
OBSERVED_BASES = ('explicit_text', 'drawing_reading')
SCOPE_TOLERANCE = 0.02
LEGAL_SUFFIXES = r'(?:llc|l\.l\.c\.|ltd\.?|limited|inc\.?|co\.?|company|corp\.?|gmbh|ag|s\.a\.|w\.l\.l\.?|plc)'


def _bounded(pattern):
    """Not inside a longer token, and not the start of a decimal or variant continuation."""
    return r'(?<![A-Za-z0-9.\-])(?:' + pattern + r')(?![A-Za-z0-9]|[.\-/]\d)'


def _fullmatch_any(patterns, value, flags=0):
    return any(re.fullmatch(p, value, flags) for p in patterns)


def norm(text):
    return re.sub(r'\s+', ' ', str(text).replace(' ', ' ')).strip().casefold()


def identity_keys(identity):
    """Brand/system keys for component scope: case, spacing and legal-form suffixes do not split a scope."""
    identity = identity or {}
    brand, system = identity.get('manufacturer_brand'), identity.get('system_id')
    brand_key = re.sub(r'(?:[ ,]+' + LEGAL_SUFFIXES + r')+$', '', norm(brand)).strip() if brand else None
    system_key = re.sub(r'\s+', '', norm(system)) if system else None
    return brand_key or None, system_key or None


def drawing_reference(value):
    return bool(value) and any(re.match(p + r'(?:\b|$)', value.strip(), re.I) for p in DRAWING_REFERENCE_PATTERNS)


def article_key(value):
    """Registry key for a literal article ID; callers keep the literal text separately.

    Outer whitespace removed, inner spacing normalised, and spaces between digit groups
    dropped, so '235 918', '235 918' and '235918' are one component.
    """
    text = re.sub(r'[  ]+', ' ', str(value).strip())
    return re.sub(r'(?<=\d) (?=\d)', '', text)


def measurement(value):
    return bool(value) and _fullmatch_any(MEASUREMENT_PATTERNS, value.strip(), re.I)


def contains_measurement(value):
    """A free-standing dimension inside the text ('Isolator 11.4mm'); a variant suffix ('-11.5') is not one."""
    return any(re.search(r'(?<![A-Za-z0-9.\-])(?:' + p + r')(?![A-Za-z0-9])', value, re.I)
               for p in MEASUREMENT_PATTERNS)


def literal_article(value, kind=''):
    """True when the value is a literal component article ID (not a dimension, drawing ref or word)."""
    if not value or not value.strip():
        return False
    text = re.sub(r'[  ]+', ' ', value.strip())
    if (kind or '').strip().casefold() in NON_COMPONENT_KINDS:
        return False
    if not re.search(r'\d', text) or drawing_reference(text) or measurement(text) or contains_measurement(text):
        return False
    if any(k in (kind or '').casefold() for k in TOOL_KIND_KEYWORDS) and _fullmatch_any(TOOL_ARTICLE_PATTERNS, text):
        return True
    return _fullmatch_any(ARTICLE_PATTERNS, text)


def article_column(header):
    header = header or ''
    return (any(re.search(p, header, re.I) for p in ARTICLE_COLUMN_PATTERNS)
            and not any(re.search(p, header, re.I) for p in ARTICLE_COLUMN_EXCLUDE_PATTERNS))


def table_articles(cell):
    """Literal IDs in an article-column cell, drawing references found there, and unmatched text with digits."""
    text = (cell or '').strip()
    if not text:
        return [], [], ''
    if not re.search(r'\s', text) and literal_article(text, 'table_reference'):
        return [re.sub(r'[  ]+', ' ', text)], [], ''
    found, drawings, rest = [], [], text
    for pattern in DRAWING_REFERENCE_PATTERNS:
        bounded = r'(?<![A-Za-z0-9])(?:' + pattern + r')(?![A-Za-z0-9])'
        for match in re.finditer(bounded, rest, re.I):
            if match.group(0) not in drawings:
                drawings.append(match.group(0))
        rest = re.sub(bounded, ' ', rest, flags=re.I)
    for pattern in TABLE_ARTICLE_TOKEN_PATTERNS:
        def take(match):
            token = re.sub(r'[  ]+', ' ', match.group(0).strip())
            if literal_article(token, 'table_reference'):
                if token not in found:
                    found.append(token)
                return ' '
            return match.group(0)
        rest = re.sub(_bounded(pattern), take, rest)
    for pattern in MEASUREMENT_PATTERNS:
        rest = re.sub(r'(?<![A-Za-z0-9.])(?:' + pattern + r')(?![A-Za-z0-9])', ' ', rest, flags=re.I)
    leftover = re.sub(r'[\s,;/|+&()-]+', ' ', rest).strip()
    return found, drawings, leftover if re.search(r'\d', leftover) else ''


def canonical_predicate(predicate):
    text = re.sub(r'[\s-]+', '_', str(predicate or '').strip().casefold())
    for canonical, (_, synonyms) in PREDICATES.items():
        if text == canonical or text in synonyms:
            return canonical
    return None


def predicate_guide():
    """Prompt text: allowed predicates with direction and meaning."""
    return '\n'.join(f'- {name}: {definition}' for name, (definition, _) in PREDICATES.items())


def grounding_text(value):
    """Comparable form for checking that an ID appears in transcribed text (spacing/case-insensitive)."""
    return re.sub(r'[\s ]+', '', str(value or '')).casefold()


def grounded(article, texts):
    """Match a complete printed token while allowing spaces within an article ID."""
    needle=grounding_text(article)
    if not needle:return False
    token=r'[ \t\u00a0]*'.join(map(re.escape,needle))
    pattern=re.compile(r'(?<![A-Za-z0-9])(?<![A-Za-z0-9][./-])'+token+
                       r'(?![A-Za-z0-9]|[./-][A-Za-z0-9])',re.I)
    return any(pattern.search(t) for t in texts)


def relation_key(relation):
    return relation['source'],relation['predicate'],relation['target']


def claim_id(index):
    return f'r{index+1}'


def relation_verdicts(relations, verdicts):
    """Bind decisions to claim IDs. An ambiguous legacy triple cannot confer support."""
    keys={claim_id(i):relation_key(r) for i,r in enumerate(relations)}
    counts=Counter(keys.values())
    legacy={key:ident for ident,key in keys.items() if counts[key]==1}
    decisions=defaultdict(set)
    for verdict in verdicts:
        key=relation_key(verdict)
        ident=verdict.get('claim_id')
        if ident is None:
            ident=legacy.get(key)
        if keys.get(ident)==key:
            decisions[ident].add(verdict['verdict'])
    return {ident:'unsupported' if 'unsupported' in values else
            'supported' if values=={'supported'} else 'uncertain'
            for ident,values in decisions.items()}
