"""Recover profile depths from dimension bars and matching PDF contours."""
import re

from . import rules


def axis_lines(page):
    """Group dimension strokes and black contours by their longitudinal axis."""
    dimensions, contours = [[], []], [[], []]
    width, height = page.rect.width, page.rect.height
    for drawing in page.get_drawings():
        color = drawing.get('color')
        if not color:
            continue
        red, green, blue = color[:3]
        if red > .9 and .3 < green < .8 and abs(green - blue) < .05:
            lines = dimensions
        elif max(red, green, blue) < .15:
            lines = contours
        else:
            continue
        for item in drawing['items']:
            if item[0] != 'l':
                continue
            _, a, b = item
            x0, y0, x1, y1 = a.x / width, a.y / height, b.x / width, b.y / height
            for axis, (start, end, cross, other) in enumerate(((x0, x1, y0, y1), (y0, y1, x0, x1))):
                if abs(cross - other) < .0005 and abs(start - end) > .012:
                    lines[axis].append((*sorted((start, end)), cross))
    return dimensions, contours


def dimension_bars(box, dimensions):
    for axis, lines in enumerate(dimensions):
        low, high = box[axis], box[axis + 2]
        along = (low + high) / 2
        transverse = (box[1 - axis] + box[3 - axis]) / 2
        near = [(a, b, c) for a, b, c in lines if abs(c - transverse) < .008]
        for start, end, cross in near:
            if not (start < low and along - .032 < end < along):
                continue
            for next_start, stop, next_cross in near:
                if along < next_start < along + .032 and stop > high and abs(cross - next_cross) < .0006:
                    yield axis, start, stop, cross


def profile_owners(axis, start, stop, cross, contours, profiles, glazing):
    for a, b, transverse in contours[axis]:
        if abs(a - start) > .009 or abs(b - stop) > .009:
            continue
        for node in profiles:
            box = node['bbox']
            if (a < box[axis] - .012 or b > box[axis + 2] + .012
                    or min(abs(transverse - box[1 - axis]), abs(transverse - box[3 - axis])) > .012):
                continue
            nearby = [glass['bbox'] for glass in glazing if glass['detail_id'] == node['detail_id']
                      and all(glass['bbox'][j] <= box[j + 2] + .12 and box[j] <= glass['bbox'][j + 2] + .12
                              for j in (0, 1))]
            panes = [g for g in nearby if g[3 - axis] - g[1 - axis] > 2 * (g[axis + 2] - g[axis])]
            # An interval between two glazing faces can coincide with a profile
            # edge, but is a glazing cavity dimension rather than profile depth.
            if panes and (all(g[axis + 2] <= a + .008 for g in panes)
                          or all(g[axis] >= b - .008 for g in panes)):
                yield node['id'], abs(transverse - cross)


def recover_depths(value, source_text, page):
    """Require a read scalar, dimension strokes, a unique contour, and a glazing direction."""
    sections = {d['id'] for d in value['details'] if d.get('view_type') == 'assembly_section_2d'}
    loose = [m for m in value['measurements'] if m['subject'] in sections
             and isinstance(m['value'], (list, float, int)) and not m.get('condition')]
    profiles = [n for n in value['nodes'] if n['detail_id'] in sections and n['kind'] == 'profile'
                and n.get('article_id') and rules.family(n) != 'spacer']
    if not loose or not profiles:
        return []
    glazing = [n for n in value['nodes'] if rules.family(n) in {'glass', 'infill'} or n['kind'] in {'glass', 'infill'}]
    if not glazing:
        return []
    dimensions, contours = axis_lines(page)
    existing = {m['subject'] for m in value['measurements'] if rules.norm(m['property']) == 'depth'}
    resolved = {}
    for span in source_text:
        text = span['text'].strip()
        if not re.fullmatch(r'\d+(?:[.,]\d+)?', text):
            continue
        number = float(text.replace(',', '.'))
        box = span['bbox']
        x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        candidates = [m for m in loose if number in (m['value'] if isinstance(m['value'], list) else [m['value']])
                      and m['bbox'][0] <= x <= m['bbox'][2] and m['bbox'][1] <= y <= m['bbox'][3]]
        if len(candidates) != 1:
            continue
        source = candidates[0]
        owners = {}
        local = [n for n in profiles if n['detail_id'] == source['subject']]
        for axis, start, stop, cross in dimension_bars(box, dimensions):
            for owner, distance in profile_owners(axis, start, stop, cross, contours, local, glazing):
                owners[owner] = min(owners.get(owner, 1), distance)
        ranked = sorted(owners, key=owners.get)
        if not ranked or (len(ranked) > 1 and owners[ranked[1]] - owners[ranked[0]] < .025):
            continue
        owner = ranked[0]
        if owner not in existing:
            resolved.setdefault(owner, {})[number] = dict(source, subject=owner, property='depth', value=number,
                                                           bbox=box, evidence=text, datum=None, bound='exact')
    return [next(iter(facts.values())) for facts in resolved.values() if len(facts) == 1]
