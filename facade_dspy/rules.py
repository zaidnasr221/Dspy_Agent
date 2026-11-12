"""Generic engineering rules for catalogue extraction. One rule set for every manufacturer.

Covers literal article IDs, dimensions, article columns in tables, drawing references,
the controlled relationship vocabulary, and the identity keys used to scope components.
Model output is never trusted for these decisions; this module applies them afterwards.
"""
import json
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).with_name('knowledge')


@lru_cache(maxsize=None)
def knowledge(name):
    """Generic domain knowledge kept as data (facade_dspy/knowledge/<name>.json), never as code."""
    return json.loads((KNOWLEDGE_DIR / f'{name}.json').read_text(encoding='utf-8'))


# Drawing/sheet numbers are not article IDs. Conventions live in knowledge/catalogue_conventions.json.
DRAWING_REFERENCE_PATTERNS = tuple(knowledge('catalogue_conventions')['drawing_reference_patterns'])

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
    'clamped_by': ('SOURCE is held by the visible or specified compressive action of TARGET.', ('clamped',)),
    'moves_relative_to': ('SOURCE is explicitly allowed to move relative to TARGET in service.', ('movement',)),
    'other': ('An explicitly shown or specified relationship not covered above. Explain exactly what connects the parts in evidence.', ()),
    'fastened_by': ('SOURCE is held by fastener TARGET (screw, bolt, rivet, anchor).',
                    ('fixed_with', 'screwed_with', 'secured_by', 'fastened_with')),
    'fixed_to': ('SOURCE is mechanically attached to TARGET.',
                 ('attached_to', 'mounted_on', 'mounted_to', 'screwed_to', 'secured_to')),
    'inserted_into': ('SOURCE sits in a groove, chamber or slot of TARGET.',
                      ('fits_into', 'installed_in', 'inserted_in', 'slides_into', 'located_in')),
    'clipped_into': ('SOURCE snaps or clips onto/into TARGET.', ('clipped_to', 'snapped_into', 'clips_into')),
    'seals_against': ('Gasket/seal SOURCE presses against TARGET to seal.', ('sealed_against',)),
    'contacts': ('SOURCE and TARGET visibly touch along an interface; no function or attachment is implied.', ('touches',)),
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


# ---------------------------------------------------------------------------------------------
# System type (SG / CC / semi-SG) and part families, from knowledge/system_profiles.json.

def _profiles():
    return knowledge('system_profiles')


def family(node):
    """Generic family of a part: the model's profile_role when given, else a whole-word synonym match
    on its kind/label. Returns None when nothing identifies it (e.g. a bare 'silicone')."""
    families = _profiles()['families']
    role = (node.get('profile_role') or '').strip()
    if role in families:
        return role
    best = None
    for field in ('kind', 'label'):
        text = ' ' + re.sub(r'[_\s]+', ' ', norm(node.get(field) or '')) + ' '
        for name, spec in families.items():
            for synonym in spec['synonyms']:
                if f' {synonym} ' in text and (best is None or len(synonym) > best[1]):
                    best = (name, len(synonym))
        if best:
            return best[0]
    return None


def default_article_type(name):
    spec = _profiles()['families'].get(name or '')
    return spec['article_type'] if spec else None


# Step 9 of the classification process: only pieces are placed at intervals along a rail.
PIECE_ONLY_FIELDS = ('edge_spacing', 'spacing')


def view_feeds(view_type):
    """Step 6 routing: 'article' for a single-article profile drawing or table (Article DB only),
    'assembly' for a view of articles put together (Assembly DB); None when the view is unknown."""
    return _profiles()['view_routing'].get(view_type or '')


def is_fastener(node):
    return family(node) in _profiles()['fastener_families']


def retention_system(relation, nodes):
    """Classify a glazing retention claim from its mechanism and named participants."""
    ids = {relation['source'], relation['target'], *(p['node_id'] for p in relation.get('participants') or [])}
    families = {family(nodes[i]) for i in ids if i in nodes}
    if not families.intersection({'glass', 'infill'}):
        return None
    predicate = canonical_predicate(relation['predicate'])
    if predicate == 'clamped_by' and 'pressure_plate' in families:
        return 'CC'
    if ((predicate == 'bonded_to' and 'structural_silicone' in families)
            or (predicate == 'clamped_by' and 'toggle' in families)):
        return 'SG'
    return None


def detail_system(nodes, relations=()):
    """Classify glazing from eligible, evidence-backed retention claims in this detail."""
    by_id = {n['id']: n for n in nodes if 'id' in n}
    matches = [(retention_system(r, by_id), r) for r in relations
               if r.get('graph_eligible') and (r.get('evidence') or '').strip()]
    matches = [(kind, r) for kind, r in matches if kind]
    kinds = {kind for kind, _ in matches}
    kind = _profiles()['semi_type'] if len(kinds) > 1 else next(iter(kinds), None)
    status = 'unresolved' if not matches else (
        'explicit' if {k for k, r in matches if r['basis'] == 'explicit_text'} == kinds else 'visual')
    return dict(type=kind, evidence=[f"{k}: {r['evidence']}" for k, r in matches], evidence_status=status)


def system_issues(declared, nodes, relations=()):
    """Compare the model's system_type with the type computed from the detail's parts (review only)."""
    computed = detail_system(nodes, relations)
    issues = []
    if declared and computed['type'] and declared != computed['type']:
        issues.append(dict(issue='system_type_mismatch', declared=declared, computed=computed['type'],
                           evidence=computed['evidence']))
    if declared and not computed['type']:
        issues.append(dict(issue='system_type_without_retention_evidence', declared=declared))
    return computed, issues


def system_part_of(nodes):
    """Which system part a set of participants belongs to, from system-only families; None if mixed/unknown."""
    only = _profiles()['system_only_families']
    parts = {name for node in nodes for name, families in only.items() if family(node) in families}
    return next(iter(parts)) if len(parts) == 1 else None


def assembly_issues(relation, nodes):
    """Structural checks on one fixing/assembly relation; returns issue names (review, never deletion)."""
    issues = []
    path = [*(relation.get('passes_through') or []), *([relation['engages_in']] if relation.get('engages_in') else [])]
    fastener = relation.get('fastener_id')
    if relation.get('relation_type') == 'fixed':
        endpoints = [nodes.get(relation.get('source')), nodes.get(relation.get('target'))]
        if not fastener and not any(n and is_fastener(n) for n in endpoints):
            issues.append('fixed_without_fastener')
    if fastener and fastener in nodes and not is_fastener(nodes[fastener]):
        issues.append('fastener_id_not_a_fastener')
    if fastener and fastener in path:
        issues.append('fastener_in_its_own_path')
    if len(path) != len(set(path)):
        issues.append('repeated_part_in_fastener_path')
    part = relation.get('system_part')
    members = [nodes[i] for i in (relation.get('source'), relation.get('target'), *path) if i in nodes]
    implied = system_part_of(members)
    if part and implied and part != implied:
        issues.append('system_part_mismatch')
    return issues


def axis_of(nodes):
    """'vertical' / 'horizontal' when the parts touch frame members of exactly one axis, else None."""
    axes = {axis for node in nodes for axis, families in _profiles()['axes'].items()
            if not axis.startswith('_') and family(node) in families}
    return next(iter(axes)) if len(axes) == 1 else None


def semi_axes(parts_by_axis):
    """Per-axis system of a semi-SG detail from {axis: {system parts seen on that axis}}.
    Returns (per_axis, issues): an axis carrying both CC and SG parts is flagged, never resolved."""
    per_axis, issues = {}, []
    for axis, parts in parts_by_axis.items():
        if len(parts) == 1:
            per_axis[axis] = next(iter(parts))
        elif len(parts) > 1:
            issues.append(f'semi_sg_both_systems_on_{axis}_axis')
    if len(per_axis) == 2 and len(set(per_axis.values())) == 1:
        issues.append('semi_sg_same_system_on_both_axes')
    return per_axis, issues
