"""Manufacturer profiles: identity aliases and literal-ID rules kept outside the code.

Profiles are JSON files in facade_dspy/manufacturers/ plus any folder named by the
FACADE_MANUFACTURER_DIR environment variable. Each profile inherits every key it
does not define from default.json. A brand/system that matches no alias keeps the
previous behaviour (case-folded literal), so existing canonical IDs are unchanged.
"""
from functools import lru_cache
from pathlib import Path
import json
import os
import re
from .storage import dump, filehash, objecthash, read

BUILTIN = Path(__file__).with_name('manufacturers')
REGEX_KEYS = ('drawing_reference_patterns', 'article_patterns', 'tool_article_patterns', 'measurement_patterns',
              'article_column_patterns', 'article_column_exclude_patterns', 'table_article_token_patterns')


def norm(text):
    """Alias comparison form: case-folded, non-breaking spaces as spaces, whitespace collapsed."""
    return re.sub(r'\s+', ' ', str(text).replace(' ', ' ')).strip().casefold()


def _folders():
    folders = [BUILTIN]
    extra = os.environ.get('FACADE_MANUFACTURER_DIR')
    if extra:
        folders += [Path(p) for p in extra.split(os.pathsep) if p]
    return tuple(folders)


@lru_cache(maxsize=8)
def _load(folders, fingerprints=()):
    default = None
    profiles = {}
    for folder in folders:
        for path in sorted(Path(folder).glob('*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            ident = data.get('id')
            if not ident or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', ident):
                raise ValueError(f'Manufacturer profile {path} needs a lowercase "id"')
            if type(data.get('version', 1)) is not int or data.get('version', 1) < 1:
                raise ValueError(f'Manufacturer profile {path} needs a positive integer version')
            for key in REGEX_KEYS:
                for pattern in data.get(key, []):
                    re.compile(pattern)
            if ident == 'default':
                default = data
            elif ident in profiles:
                raise ValueError(f'Duplicate manufacturer profile id: {ident}')
            else:
                profiles[ident] = data
    if default is None:
        raise ValueError('default.json manufacturer profile is missing')
    merged = {k: {**default, **v} for k, v in profiles.items()}
    owners = {}
    for ident, profile in merged.items():
        for alias in [ident, *profile.get('brand_aliases', [])]:
            key = norm(alias)
            if owners.setdefault(key, ident) != ident:
                raise ValueError(f'Brand alias {alias!r} is claimed by {owners[key]} and {ident}')
        systems = {}
        for canonical, aliases in profile.get('system_aliases', {}).items():
            for alias in (canonical, *aliases):
                if systems.setdefault(norm(alias), canonical) != canonical:
                    raise ValueError(f'System alias {alias!r} is ambiguous in profile {ident}')
    return default, merged, owners


def _load_current(folders):
    fingerprints=tuple((path,filehash(path)) for folder in folders for path in sorted(folder.glob('*.json')))
    return _load(folders,fingerprints)


def profiles():
    return _load_current(_folders())


def resolve(identity, *, builtin_only=False):
    """Return the profile and canonical brand/system scope keys for a document identity."""
    default, merged, owners = _load_current((BUILTIN,)) if builtin_only else profiles()
    identity = identity or {}
    brand = identity.get('manufacturer_brand')
    system = identity.get('system_id')
    match = None
    for candidate in (brand, identity.get('legal_manufacturer')):
        if candidate and norm(candidate) in owners:
            match = owners[norm(candidate)]
            break
    profile = merged[match] if match else default
    brand_key = match if match else (brand.casefold() if brand else None)
    system_key = system.casefold() if system else None
    if system and match:
        for canonical, aliases in profile.get('system_aliases', {}).items():
            if norm(system) in {norm(canonical), *map(norm, aliases)}:
                system_key = norm(canonical)
                break
    return dict(profile=profile, profile_id=profile['id'], brand_key=brand_key, system_key=system_key,
                brand_matched=bool(match))


PROFILE_FILE = 'manufacturer_profile.json'


def read_pin(folder, source_sha):
    path = folder / PROFILE_FILE
    if not path.is_file():
        return None
    pin = read(path)
    if (pin.get('version') != 1 or pin.get('source_sha256') != source_sha
            or not isinstance(pin.get('scope'), dict)
            or pin.get('scope_sha256') != objecthash(pin['scope'])
            or not isinstance(pin['scope'].get('profile'),dict)
            or pin.get('profile_version') != pin['scope']['profile'].get('version',1)
            or pin.get('profile_sha256') != objecthash(pin['scope']['profile'])):
        raise ValueError('Manufacturer profile pin failed integrity or source validation')
    return pin


def pin_scope(run, source_sha, identity, *, inherited=None, legacy=False):
    existing = read_pin(run, source_sha)
    if existing is not None:
        if inherited is not None and existing != inherited:
            raise ValueError('Manufacturer profile differs from the saved source; use a fresh replay directory')
        return existing
    if inherited is None:
        scope = resolve(identity, builtin_only=legacy)
        inherited = dict(version=1, profile_version=scope['profile'].get('version',1), source_sha256=source_sha, scope=scope,
                         profile_sha256=objecthash(scope['profile']), scope_sha256=objecthash(scope),
                         origin='legacy_builtin_baseline' if legacy else 'resolved_for_run')
    dump(run / PROFILE_FILE, inherited)
    return inherited


def _any(patterns, value, flags=re.I):
    return any(re.fullmatch(p, value, flags) for p in patterns)


def drawing_reference(value, profile):
    return bool(value) and any(re.match(p + r'(?:\b|$)', value.strip(), re.I) for p in profile['drawing_reference_patterns'])


def article_key(value, profile=None):
    """Registry key for a literal article ID. The literal text is kept separately by callers.

    Outer whitespace is removed and inner spacing normalised. Profiles that print the same
    article with and without digit-group spaces (ALUPCO: '235 918' / '235918') opt in to
    removing spaces between digit groups, so both spellings share one component.
    """
    text = re.sub(r'[ \u00a0]+', ' ', str(value).strip())
    if profile and profile.get('article_key_ignore_digit_group_spaces'):
        text = re.sub(r'(?<=\d) (?=\d)', '', text)
    return text


def measurement(value, profile):
    return bool(value) and _any(profile.get('measurement_patterns', []), value.strip())


def contains_measurement(value, profile):
    """A free-standing dimension inside the text ('Isolator 11.4mm'); a variant suffix ('-11.5') is not one."""
    return any(re.search(r'(?<![A-Za-z0-9.\-])(?:' + p + r')(?![A-Za-z0-9])', value)
               for p in profile.get('measurement_patterns', []))


def literal_article(value, kind, profile):
    if not value or not value.strip():
        return False
    text = article_key(value)
    if (kind or '').strip().casefold() in {'drawing_reference','unresolved_reference','dimension','measurement',
                                          'annotation','label','position','step'}:
        return False
    if profile.get('article_requires_digit') and not re.search(r'\d',text):
        return False
    if drawing_reference(text, profile) or measurement(text, profile) or contains_measurement(text, profile):
        return False
    tool = any(k in (kind or '').casefold() for k in profile['tool_kind_keywords'])
    if tool and _any(profile['tool_article_patterns'], text, 0):
        return True
    if not _any(profile['article_patterns'], text, 0):
        return False
    if ' ' in text and profile.get('article_requires_digit_when_spaced', True) and not re.search(r'\d', text):
        return False
    return True


def article_column(header, profile):
    header = header or ''
    return any(re.search(p, header, re.I) for p in profile['article_column_patterns']) and not any(
        re.search(p, header, re.I) for p in profile['article_column_exclude_patterns'])


def _bounded(pattern):
    # Not inside a longer token, and not the start of a decimal/variant continuation.
    return r'(?<![A-Za-z0-9.\-])(?:' + pattern + r')(?![A-Za-z0-9]|[.\-/]\d)'


def table_articles(cell, profile):
    """Literal IDs in an article-column cell, drawing references found there, and unmatched text.

    Patterns are tried in profile order and each match is removed before the next pattern,
    so a complete variant ID ('235 918-11.5') is never split into '235 918' plus '11.5'.
    Dimensions ('11.4mm', '20.5', '5 - 7 mm') are masked and never become components.
    """
    text = (cell or '').strip()
    if not text:
        return [], [], ''
    if not re.search(r'\s', text) and re.search(r'\d', text) and literal_article(text, 'table_reference', profile):
        return [article_key(text)], [], ''
    found = []
    drawings = []
    rest = text
    for pattern in profile['drawing_reference_patterns']:
        for match in re.finditer(r'(?<![A-Za-z0-9])(?:' + pattern + r')(?![A-Za-z0-9])', rest, re.I):
            if match.group(0) not in drawings:drawings.append(match.group(0))
        rest = re.sub(r'(?<![A-Za-z0-9])(?:' + pattern + r')(?![A-Za-z0-9])', ' ', rest, flags=re.I)
    for pattern in profile['table_article_token_patterns']:
        def take(match):
            token = article_key(match.group(0))
            if literal_article(token, 'table_reference', profile):
                if token not in found:found.append(token)
                return ' '
            return match.group(0)
        rest = re.sub(_bounded(pattern), take, rest)
    for pattern in profile.get('measurement_patterns', []):
        rest = re.sub(r'(?<![A-Za-z0-9.])(?:' + pattern + r')(?![A-Za-z0-9])', ' ', rest)
    leftover = re.sub(r'[\s,;/|+&()-]+', ' ', rest).strip()
    return found, drawings, leftover if re.search(r'\d', leftover) else ''


def canonical_predicate(predicate, profile):
    """Controlled predicate for a literal model predicate, or None when it is not recognised."""
    text = re.sub(r'[\s-]+', '_', str(predicate or '').strip().casefold())
    for canonical, synonyms in profile.get('relationship_predicates', {}).items():
        if text == canonical or text in synonyms:
            return canonical
    return None
