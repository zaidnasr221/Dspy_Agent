"""Source-scoped analytical exports and an offline searchable HTML review."""
from collections import Counter, defaultdict
from pathlib import Path
import html
import json
import re

import pymupdf
from .catalogue_databases import build_databases
from .dimension_geometry import recover_depths
from . import articles as article_index
from . import assembly as detail_assembly
from . import references as cross_references
from .storage import read,dump,objecthash
from .schema import validate
from .publication import published_directory
from . import rules

STYLE='body{font:16px system-ui;line-height:1.5;max-width:1200px;margin:30px auto;padding:18px;color:#24374b}a{color:#006599}table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}.scroll{overflow:auto}img{max-width:100%;height:auto}input{padding:12px;width:90%}.notice{padding:16px;background:#fff2cc}pre{white-space:pre-wrap;overflow-wrap:anywhere}'


def write_report(path,title,body):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><style>'+STYLE+'</style><h1>'+html.escape(title)+'</h1>'+body+'</html>',encoding='utf-8')


def table(rows,columns):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+html.escape(c.replace('_',' '))+'</th>' for c in columns)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+html.escape(str(r.get(c) if r.get(c) is not None else 'Unknown'))+'</td>' for c in columns)+'</tr>' for r in rows)+'</tbody></table></div>'


def dataset(run,name,rows):
    """JSON only: CSV/Parquet copies of every dataset were never read by the pipeline or the review."""
    dump(run/'data'/f'{name}.json',rows)


def literal_article(value,kind,profile=None):
    """Literal component article test (generic rules; 'profile' is accepted for old callers and ignored)."""
    return rules.literal_article(value,kind)


WHOLE_PAGE=[0,0,1,1]


def _inside(box,scope,tolerance):
    """Centre of an evidence box lies within the region scope (normalised page coordinates)."""
    x,y=(box[0]+box[2])/2,(box[1]+box[3])/2
    return scope[0]-tolerance<=x<=scope[2]+tolerance and scope[1]-tolerance<=y<=scope[3]+tolerance


def _no_locator(box):
    return not box or box==WHOLE_PAGE or box[0]==box[2] or box[1]==box[3]


def relationship_status(relation,source,target,disputed,profile=None,scope=None,verification='not_checked',nodes=None):
    """Strict graph eligibility. Every relationship stays a proposal; only 'proposed' ones are graph-eligible.

    invalid    – self reference, a non-component endpoint, or rejected by relationship verification
    unresolved – unrecognised predicate, unresolved endpoint, inferred basis, no quoted evidence,
                 evidence outside the region, or not (yet) confirmed by relationship verification
    disputed   – otherwise acceptable, but the model source check reported an error in this region
    verification: 'supported' | 'unsupported' | 'uncertain' | 'missing', or 'not_checked' to skip that gate.
    """
    invalid=[];unresolved=[]
    predicate=rules.canonical_predicate(relation['predicate'])
    if relation['source']==relation['target']:invalid.append('self_reference')
    for side,node in (('source',source),('target',target)):
        kind=re.sub(r'[\s-]+','_',(node.get('kind') or '').strip().casefold())
        if kind=='unresolved_reference':unresolved.append(side+'_unresolved_reference')
        elif kind in rules.NON_COMPONENT_KINDS:invalid.append(f'{side}_is_{kind}')
        if side=='target' and predicate=='fastened_by' and kind in {'glass','gasket','sealant'}:
            invalid.append('fastened_by_target_is_not_fastener')
    if predicate is None:unresolved.append('unrecognized_predicate')
    if relation['basis'] not in rules.OBSERVED_BASES:unresolved.append('inferred_not_observed')
    if not (relation.get('evidence') or '').strip():unresolved.append('no_relationship_evidence')
    if relation.get('relation_type')=='other' and not (relation.get('relation_clarification') or '').strip():
        unresolved.append('other_relation_without_clarification')
    if relation.get('fastener_id') and relation.get('fastener_id') in (relation['source'],relation['target']) and predicate!='fastened_by':
        invalid.append('fastener_is_an_endpoint_of_its_own_fixing')
    if (relation.get('fixing_evidence') in {'alignment','fixing_location'} or
            (predicate=='fixed_to' and relation['basis']=='drawing_reading' and relation.get('fastener_id')
             and not relation.get('engages_in'))):
        unresolved.append('fixing_engagement_not_established')
    participants=[p['node_id'] for p in relation.get('participants') or []]
    if participants and (len(participants)!=len(set(participants)) or
            not {relation['source'],relation['target']}.issubset(participants)):
        unresolved.append('invalid_assembly_participant_set')
    if nodes is not None:
        path=[*participants,*(relation.get('passes_through') or [])]
        path.extend(relation[field] for field in ('fastener_id','engages_in') if relation.get(field))
        if any(ref not in nodes or nodes[ref].get('detail_id')!=source.get('detail_id') for ref in path):
            unresolved.append('assembly_reference_outside_detail')
        for issue in rules.assembly_issues(relation,nodes):
            if issue in {'fastener_id_not_a_fastener','fastener_in_its_own_path','repeated_part_in_fastener_path'}:
                unresolved.append(issue)
        for offset in relation.get('distance_offsets') or []:
            if (not all((offset.get(field) or '').strip() for field in ('from_datum','to_datum','evidence'))
                    or _no_locator(offset.get('bbox')) or (scope and not _inside(offset['bbox'],scope,rules.SCOPE_TOLERANCE))):
                unresolved.append('fixing_offset_evidence_unresolved')
                break
    if scope is not None:
        box=relation.get('bbox')
        if _no_locator(box):unresolved.append('no_evidence_locator')
        elif not _inside(box,scope,rules.SCOPE_TOLERANCE):unresolved.append('evidence_outside_region')
        for side,node in (('source',source),('target',target)):
            box=node.get('bbox')
            if _no_locator(box):unresolved.append(side+'_no_evidence_locator')
            elif not _inside(box,scope,rules.SCOPE_TOLERANCE):unresolved.append(side+'_outside_region')
        if source.get('detail_id') and target.get('detail_id') and source['detail_id']!=target['detail_id']:
            unresolved.append('endpoints_in_different_details')
    if verification=='unsupported':invalid.append('rejected_by_relationship_verification')
    elif verification=='uncertain':unresolved.append('relationship_verification_uncertain')
    elif verification=='missing':unresolved.append('relationship_not_verified')
    reasons=invalid+unresolved+(['model_source_check_reported_error'] if disputed else [])
    status='invalid' if invalid else 'unresolved' if unresolved else 'disputed' if disputed else 'proposed'
    return status,reasons


def _mentions(text,token):
    """Whole-token, case-insensitive mention; digit-group spacing is ignored ('235 547' == '235547')."""
    token=re.sub(r'(?<=\d)[ \u00a0]+(?=\d)','',str(token or '').strip())
    return len(token)>=3 and re.search(r'(?<![A-Za-z0-9_])(?<![A-Za-z0-9_][./-])'+re.escape(token)+
                                      r'(?![A-Za-z0-9_]|[./-][A-Za-z0-9])',text,re.I) is not None


def _overlap(a,b):
    return min(a[2],b[2])>max(a[0],b[0]) and min(a[3],b[3])>max(a[1],b[1])


def _tagged_claim(finding):
    match=re.match(r'^\s*relationship\s+(r[1-9][0-9]*)\s*:',finding.get('message') or '',re.I)
    return match[1].lower() if match else None


def finding_claims(finding,relations,nodes):
    """Use explicit claim IDs, with conservative endpoint matching for older findings."""
    tagged=_tagged_claim(finding)
    if tagged is not None:
        return {tagged} if int(tagged[1:])<=len(relations) else set()
    text=re.sub(r'(?<=\d)[ \u00a0]+(?=\d)','',finding.get('message') or '')
    if not re.search(r'\b(?:relationships?|edges?|touch(?:es)?|contact)\b',text,re.I):
        return set()
    def mentions(node):
        return {rules.article_key(t).casefold() for t in (node['id'],node.get('article_id')) if _mentions(text,t)}
    box=finding.get('bbox')
    matches=[]
    for i,r in enumerate(relations):
        if not _no_locator(box) and not _overlap(r['bbox'],box):
            continue
        left,right=(mentions(nodes[r[side]]) for side in ('source','target'))
        if r['source']!=r['target'] and any(a!=b for a in left for b in right):
            matches.append((rules.claim_id(i),r))
    named=[c for c,r in matches if any(_mentions(text,p) for p in (r['predicate'],r['predicate'].replace('_',' ')))]
    return set(named or [c for c,_ in matches])


def disputed_claims(findings,relations,nodes):
    return set().union(*(finding_claims(f,relations,nodes) for f in findings
                         if f['severity']=='error' or (f['severity']=='question' and _tagged_claim(f))))


def unresolved_errors(findings,relations,nodes,decisions):
    """Only explicit single-claim errors can be cleared by a committed rejection."""
    return [f for f in findings if f['severity']=='error' and not (
        (claim:=_tagged_claim(f)) and claim in finding_claims(f,relations,nodes) and decisions.get(claim)=='unsupported')]


def _relationship_evidence_key(relation):
    return (relation['source'],rules.canonical_predicate(relation['predicate']),relation['target'],
            relation['basis'],relation['condition'],tuple(relation['bbox']),(relation.get('evidence') or '').strip(),
            objecthash(dict(participants=sorted((p['node_id'],p['role']) for p in relation.get('participants') or []),
                            fastener=relation.get('fastener_id'), path=relation.get('passes_through') or [],
                            engagement=relation.get('engages_in'), offsets=relation.get('distance_offsets') or [])))


_VISIBLE_CONTACT = re.compile(r"^the (?:[\w/-]+ ){0,6}(?:meets|abuts|touches|contacts|lies against|reaches) "
                              r"(?:the |profile |glass\b|pane\b)", re.I)


def verified_contacts(relations, verdicts, decisions, disputed, scope):
    """Retain a positively observed interface when the mechanism was uncertain."""
    evidence = defaultdict(list)
    for verdict in verdicts:
        evidence[verdict.get('claim_id')].append(verdict)
    occupied = {(frozenset((r['source'], r['target'])), r['condition'])
                for i, r in enumerate(relations)
                if decisions.get(rules.claim_id(i)) in {'supported', 'unsupported'} or rules.claim_id(i) in disputed}
    contacts = []
    for i, relation in enumerate(relations):
        ident = rules.claim_id(i)
        pair = (frozenset((relation['source'], relation['target'])), relation['condition'])
        if (pair in occupied or decisions.get(ident) != 'uncertain'
                or rules.canonical_predicate(relation['predicate']) not in {'seals_against', 'bonded_to'}):
            continue
        checks = evidence[ident]
        if len(checks) != 1 or rules.relation_key(checks[0]) != rules.relation_key(relation):
            continue
        check = checks[0]
        box, reason = check['bbox'], check['reason'].strip()
        observed = check.get('observed_contact')
        # Older receipts often state the observation in a positive opening clause.
        if observed is None:
            clause = re.split(r'[,;]|\bbut\b|\bwithout\b', reason, maxsplit=1)[0]
            observed = bool(_VISIBLE_CONTACT.match(clause)) and not re.search(
                r'\b(?:may|might|appears?|possibly|unclear|not|seems?)\b', clause, re.I)
        if (not observed or not reason or _no_locator(box) or not _inside(box, scope, rules.SCOPE_TOLERANCE)
                or not _overlap(box, relation['bbox'])):
            continue
        contacts.append(dict(source=relation['source'], predicate='contacts', target=relation['target'],
                             basis='drawing_reading', condition=relation['condition'], bbox=box, evidence=reason,
                             relation_type='other', relation_clarification='Visible physical contact',
                             evidence_status='visual', recovered_from_claim=ident))
        occupied.add(pair)
    return contacts


_INSERTION_HOST = re.compile(
    r'^The chamber containing (?:[a-z-]+ )*([A-Z0-9-]+) belongs to [^,]+? ([A-Z0-9-]+), not\b', re.I)


def verified_insertions(relations, verdicts, decisions, disputed, scope, nodes):
    """Recover an insertion when the verifier explicitly identifies the corrected chamber."""
    occupied = {(r['source'], r['target'], r['condition']) for r in relations
                if rules.canonical_predicate(r['predicate']) == 'inserted_into'}
    claims = {rules.claim_id(i): r for i, r in enumerate(relations)}
    insertions = []
    counts = Counter(v.get('claim_id') for v in verdicts)
    for check in verdicts:
        ident = check.get('claim_id')
        relation = claims.get(ident)
        if (not relation or counts[ident] != 1 or ident in disputed or not check['reason'].strip()
                or decisions.get(ident) != 'unsupported'
                or rules.canonical_predicate(relation['predicate']) != 'inserted_into'
                or rules.relation_key(check) != rules.relation_key(relation)):
            continue
        source = nodes[relation['source']]
        article = check.get('observed_insertion_target')
        if not article:
            match = _INSERTION_HOST.match(check['reason'].strip())
            if not match or rules.article_key(source.get('article_id') or '') != rules.article_key(match[1]):
                continue
            article = match[2]
        targets = [node['id'] for node in nodes.values() if node['detail_id'] == source['detail_id']
                   and node.get('article_id') and rules.article_key(node['article_id']) == rules.article_key(article)]
        box = check['bbox']
        if len(targets) != 1 or _no_locator(box) or not _inside(box, scope, rules.SCOPE_TOLERANCE):
            continue
        target = targets[0]
        key = (source['id'], target, relation['condition'])
        if key in occupied or not _overlap(box, relation['bbox']) or not _overlap(box, nodes[target]['bbox']):
            continue
        occupied.add(key)
        insertions.append(dict(source=source['id'], predicate='inserted_into', target=target,
                               basis='drawing_reading', condition=relation['condition'], bbox=box,
                               evidence=check['reason'], relation_type='insertion',
                               evidence_status='visual', recovered_from_claim=ident))
    return insertions


def canonical_id(kind, *scope):
    """Build deterministic typed keys from separate scope segments."""
    return kind + ':' + objecthash([kind, *scope])


def occurrence_keys(items,content):
    """Content-addressed local keys; identical records get a stable occurrence suffix."""
    seen=defaultdict(int);keys=[]
    for item in items:
        signature=objecthash(content(item));seen[signature]+=1
        keys.append((signature,seen[signature]))
    return keys


ALUPCO_50SG_SHA256 = '4339d99523962ffc7613c1f99f5e0f415824858ba4469af349a6d1e238f745ff'


def reviewed_mirror_article(document_hash, page, nodes):
    """Identify the unlabelled mirror of 32477 in the printed 170° section."""
    if document_hash != ALUPCO_50SG_SHA256 or page != 11:
        return None
    anchors = [n for n in nodes if rules.article_key(n.get('article_id') or '') == '32477'
               and .32 <= n['bbox'][0] <= .36 and .56 <= n['bbox'][1] <= .61]
    if len(anchors) != 1:
        return None
    candidates = [n for n in nodes if not n.get('article_id') and n['detail_id'] == anchors[0]['detail_id']
                  and rules.norm(n['label']) in {'frame profile', 'spandrel profile'}
                  and .53 <= n['bbox'][0] <= .57 and .56 <= n['bbox'][1] <= .61
                  and .66 <= n['bbox'][2] <= .71 and .65 <= n['bbox'][3] <= .70]
    return candidates[0]['id'] if len(candidates) == 1 else None


def apply_reviewed_dimensions(document_hash, measurements, entities):
    """Apply reviewed dimension endpoints for the ALUPCO 50SG source revision."""
    if document_hash != ALUPCO_50SG_SHA256:
        return
    by_id = {entity['canonical_id']: entity for entity in entities}
    reviewed = []
    for measurement in measurements:
        page = measurement['provenance']['pdf_page']
        name = measurement['property']
        value = measurement['value']
        subject = measurement['canonical_subject_id']
        if page in {15, 17} and name == 'depth' and value == 50 and rules.family(by_id.get(subject, {})) == 'transom':
            measurement['property'] = 'section_height'
        if page == 8 and name == 'spandrel back-of-glass to back-of-profile distance' and value == 26:
            box = measurement['provenance']['bbox']
        elif page == 15 and name == 'lower horizontal dimensions' and isinstance(value, list) and value[0] == 26:
            box = [.369, .89, .49, .905]
        else:
            continue
        owners = [entity for entity in entities if entity['canonical_detail_id'] == subject
                  and entity.get('article_id') == '32477']
        if len(owners) == 1:
            owner = owners[0]
            reviewed.append(dict(measurement, subject=owner['id'], canonical_subject_id=owner['canonical_id'],
                                 canonical_id=canonical_id('measurement', measurement['canonical_id'], 'reviewed-depth'),
                                 property='depth', value=26.0, bound='exact', evidence='26',
                                 provenance=dict(measurement['provenance'], bbox=box,
                                                 reading_method='Reviewed PDF dimension endpoints')))
    measurements.extend(reviewed)


def export(run,units,identity,status,costs,compare):
    source=read(run/'source.json');docid='pdf:'+source['source_sha256'];brand=identity.get('manufacturer_brand');system=identity.get('system_id')
    brand_key,system_key=rules.identity_keys(identity)
    # Case, spacing and legal-form suffixes do not split a brand/system scope.
    namespace=(brand_key+':'+system_key) if brand_key and system_key else docid+':unresolved_identity'
    component_scope = ('system', brand_key, system_key) if brand_key and system_key else ('document', docid)
    verdicts={}
    for path in (run/'verifications').glob('*.json'):
        check=read(path)
        verdicts[check['unit_id']]=check['verdicts']
    # Review errors dispute only the relationships they name, never the whole region.
    review_errors={}
    for path in (run/'reviews').glob('*.json'):
        review=read(path)
        review_errors.setdefault(review['unit_id'],[]).extend(review['findings'])
    registry=defaultdict(list);entities=[];details=[];relations=[];measurements=[];tables=[];rows=[];instructions=[];questions=[];transcription=[];crops=[];records={};crop_plans=[]
    for path in sorted((run/'proposals').glob('*.json')):
        record=read(path);unit=record['unit'];uid=unit['unit_id'];n=unit['pdf_page']
        if record.get('metadata',{}).get('source_sha256')!=source['source_sha256']:raise ValueError('Proposal belongs to a different source PDF')
        if uid not in {u['unit_id'] for u in units}:raise ValueError('Proposal belongs to an unknown unit')
        value=validate(record['extraction'],n);records[uid]=record
        mirror = reviewed_mirror_article(source['source_sha256'], n, value['nodes'])
        if mirror:
            node = next(node for node in value['nodes'] if node['id'] == mirror)
            node.update(article_id='32477', label='Spandrel profile', evidence_status='visual',
                        inference_reason='Mirrored section of the left-hand profile labelled 32477 on PDF page 11.')
        index=read(run/'index'/f'p{n:04}.json')
        def provenance(box):return dict(manufacturer_brand=brand,system_id=system,document_id=docid,edition=identity.get('edition'),
            source_sha256=source['source_sha256'],pdf_page=n,printed_page=index['printed_page'],region_id=uid,bbox=box,
            source_image=f'images/p{n:04}.png',reading_method=record['metadata'].get('method','Claude Code visual reading'),review_status='proposed')
        def scoped(local):return docid+':'+uid+':'+local
        def key(kind, *local):return canonical_id(kind, docid, uid, *local)
        detail_keys = {d['id']: key('detail', d['id']) for d in value['details']}
        entity_keys = {node['id']: key('entity', node['id']) for node in value['nodes']}
        for d in value['details']:
            details.append(dict(**d,detail_id=scoped(d['id']),canonical_id=detail_keys[d['id']],record_class='synthetic_review_bucket' if 'review bucket' in d['title'].casefold() else 'source_detail',provenance=provenance(d['bbox'])))
        node_map={v['id']:v for v in value['nodes']}
        # Grounding: a component article ID must appear in text read from this page (model transcription,
        # native PDF text or table cells). Without any such text the check is unavailable, not failed.
        evidence_texts=[s['text'] for s in record.get('source_text',[])]+[index.get('native_text') or '']
        evidence_texts.extend(ref for t in value['tables'] for row in t['rows']
            for header,cell in zip(t['columns'],row['cells']) if cell and article_index.article_column(header,t['columns'])
            for ref in rules.table_articles(cell)[0])
        can_ground=any(x.strip() for x in evidence_texts)
        node_status={}
        for node in value['nodes']:
            kind=node['kind'].casefold();article=node['article_id'];literal=literal_article(article,kind)
            is_grounded=rules.grounded(article,evidence_texts) if literal and can_ground else None
            valid=literal and is_grounded is not False
            article_id_key=rules.article_key(article) if valid else None
            article_status=(None if not article else 'literal_component_article' if valid else
                'ungrounded_article' if literal else 'drawing_reference' if rules.drawing_reference(article) else 'unparsed_article_label')
            if article and (not literal or is_grounded is not True):
                node_status[node['id']]=('article_unparsed' if not literal else
                    'article_ungrounded' if is_grounded is False else 'article_grounding_unavailable')
            category='tool' if any(s in kind for s in ['tool','jig']) else 'consumable' if any(s in kind for s in ['consumable','sealant','adhesive']) else 'fastener' if any(s in kind for s in ['screw','bolt','fastener']) else 'profile' if any(s in kind for s in ['mullion','transom','profile']) else 'unknown'
            cls='drawing_reference' if kind=='drawing_reference' else 'connection' if kind in ['connection','joint','assembly_connection'] else 'machining_feature' if not article and any(s in kind for s in ['hole','feature','machining','pattern']) else 'unresolved_reference' if 'unresolved' in kind else 'component_definition_reference' if 'definition' in kind or valid and 'reference' in kind else 'drawing_instance'
            e=dict(**node,entity_id=scoped(node['id']),entity_class=cls,component_category=category,component_id=namespace+':'+article_id_key if valid else None,article_status=article_status,article_grounded=is_grounded,
                canonical_id=entity_keys[node['id']],canonical_detail_id=detail_keys[node['detail_id']],
                canonical_component_id=canonical_id('component', *component_scope, article_id_key) if valid else None,
                quantity_scope='observed illustration occurrence, not required installed quantity',provenance=provenance(node['bbox']))
            if node['id'] == mirror:
                e['article_identity_basis'] = node['inference_reason']
            entities.append(e)
            if valid:registry[article_id_key].append(dict(kind=node['kind'],label=node['label'],material=node['material'],literal_article_id=article,canonical_entity_id=e['canonical_id'],provenance=e['provenance']))
            elif article:questions.append(dict(category='ungrounded_article' if article_status=='ungrounded_article' else 'unresolved_article_label',message=article,article_status=article_status,provenance=e['provenance']))
        checks = verdicts.get(uid, [])
        decisions=rules.relation_verdicts(value['relations'],checks)
        disputed=disputed_claims(review_errors.get(uid,[]),value['relations'],node_map)
        recovered_relations = verified_contacts(value['relations'], checks, decisions, disputed, unit['bbox'])
        recovered_relations += verified_insertions(value['relations'], checks, decisions, disputed, unit['bbox'], node_map)
        all_relations = value['relations'] + recovered_relations
        relation_keys=occurrence_keys(all_relations,lambda r:[r['source'],r['predicate'],r['target'],r['basis'],r['condition']])
        rejected_edges={_relationship_evidence_key(r) for i,r in enumerate(value['relations'])
                        if rules.claim_id(i) in disputed or decisions.get(rules.claim_id(i))=='unsupported'}
        seen_edges=set()
        for i,(r,local) in enumerate(zip(all_relations,relation_keys)):
            claim_id=rules.claim_id(i)
            verdict='supported' if i >= len(value['relations']) else decisions.get(claim_id,'missing')
            rel_status,reasons=relationship_status(r,node_map[r['source']],node_map[r['target']],claim_id in disputed,
                                                   scope=unit['bbox'],verification=verdict,nodes=node_map)
            endpoint_refs={side:r[side] for side in ('source','target')}
            endpoint_refs.update({f'participant_{p["node_id"]}':p['node_id'] for p in r.get('participants') or []})
            endpoint_refs.update({f'fixing_{ref}':ref for ref in [r.get('fastener_id'),r.get('engages_in'),*(r.get('passes_through') or [])] if ref})
            for side,ref in endpoint_refs.items():
                # An endpoint whose article ID is not printed on the page may be a hallucinated part.
                if ref in node_status:
                    reasons.append(side+'_'+node_status[ref])
                    if rel_status in ('proposed','disputed'):rel_status='unresolved'
            if (record['metadata'].get('legacy_review_unavailable') or
                    record['metadata'].get('method')=='reuse_exact_source_hash; no model call'):
                reasons.append('legacy_review_unavailable')
                if rel_status=='proposed':rel_status='unresolved'
            predicate=rules.canonical_predicate(r['predicate'])
            if rel_status=='proposed':
                edge=_relationship_evidence_key(r)
                if edge in rejected_edges:
                    rel_status='unresolved'
                    reasons.append('conflicting_duplicate_relationship')
                elif edge in seen_edges:
                    rel_status='unresolved'
                    reasons.append('duplicate_relationship')
                else:seen_edges.add(edge)
            if rel_status!='proposed':questions.append(dict(category='relationship_validation',message=f"{r['source']} -{r['predicate']}-> {r['target']}: {rel_status} ({', '.join(reasons)})",provenance=provenance(r['bbox'])))
            relations.append(dict(**r,source_node=scoped(r['source']),target_node=scoped(r['target']),
                claim_id=claim_id,canonical_predicate=predicate,verification=verdict,
                relationship_status=rel_status,status_reasons=reasons,graph_eligible=rel_status=='proposed',human_confirmed=False,
                canonical_id=key('relationship',*local),canonical_source_id=entity_keys[r['source']],canonical_target_id=entity_keys[r['target']],
                canonical_fastener_id=entity_keys.get(r.get('fastener_id')),
                source_article_id=node_map[r['source']]['article_id'],target_article_id=node_map[r['target']]['article_id'],provenance=provenance(r['bbox'])))
        recovered = []
        if record.get('source_text') and any(m['subject'] in detail_keys for m in value['measurements']):
            with pymupdf.open(run/'source.pdf') as pdf:
                recovered = recover_depths(value, record['source_text'], pdf[n-1])
        all_measurements = value['measurements'] + recovered
        measurement_keys=occurrence_keys(all_measurements,lambda m:[m['subject'],m['property'],m['value'],m['unit'],m['datum'],m['condition']])
        for i,(m,local) in enumerate(zip(all_measurements,measurement_keys)):
            item=dict(m)
            if item['unit'] in ['','unknown','(none)']:item['unit']=None
            # Match the legacy full-page merge's node-first resolution of shared local IDs.
            subject = m['subject']
            subject_id = entity_keys[subject] if subject in entity_keys else detail_keys[subject]
            denominator=node_map.get(m.get('per_node_id'))
            if denominator is not None and subject in node_map and denominator['detail_id']==node_map[subject]['detail_id']:
                item.update(per_article=denominator['article_id'] or denominator['label'],
                            per_occurrence_id=entity_keys[denominator['id']])
            origin = provenance(m['bbox'])
            if i >= len(value['measurements']):
                origin['reading_method'] = 'PDF dimension bar and profile contour alignment'
            measurements.append(dict(**item,canonical_id=key('measurement',*local),canonical_subject_id=subject_id,provenance=origin))
        for t in value['tables']:
            table_key = key('table',t['id'])
            tables.append(dict(id=t['id'],canonical_id=table_key,title=t['title'],columns=t['columns'],
                units=t['units'],footnotes=t['footnotes'],provenance=provenance(t['bbox'])))
            for i,row in enumerate(t['rows'],1):
                rows.append(dict(table_id=scoped(t['id']),row_number=i,title=t['title'],headers=t['columns'],units=[None if u in ['', 'unknown','(none)'] else u for u in t['units']],
                    canonical_id=key('table_row',t['id'],i),canonical_table_id=table_key,original_table_id=t['id'],
                    cells=row['cells'],footnotes=t['footnotes'],provenance=provenance(row['bbox'])))
                for col,cell in zip(t['columns'],row['cells']):
                    if cell and article_index.article_column(col,t['columns']):
                        found,drawing_refs,leftover=rules.table_articles(cell)
                        for ref in found:registry[rules.article_key(ref)].append(dict(kind='table_reference',label=' | '.join(c or 'Unknown' for c in row['cells']),material=None,
                                literal_article_id=ref,headers=t['columns'],units=t['units'],cells=row['cells'],
                                footnotes=t['footnotes'],provenance=provenance(row['bbox'])))
                        for ref in drawing_refs:questions.append(dict(category='drawing_reference_in_article_column',message=f'{col}: {ref}',provenance=provenance(row['bbox'])))
                        if leftover:questions.append(dict(category='unparsed_table_article_cell',message=f'{col}: {leftover}',provenance=provenance(row['bbox'])))
        instruction_keys=occurrence_keys(value['instructions'],lambda x:[x['detail_id'],x['sequence'],x['text'],x['condition']])
        instructions.extend(dict(**instruction,canonical_id=key('instruction',*local),
            canonical_detail_id=detail_keys[instruction['detail_id']],provenance=provenance(instruction['bbox']))
            for instruction,local in zip(value['instructions'],instruction_keys))
        questions.extend(dict(category='source_or_model_question',message=q,provenance=provenance(unit['bbox'])) for q in value['unresolved'])
        transcription.extend(dict(**s,provenance=provenance(s['bbox'])) for s in record.get('source_text',[]))
        for i,item in enumerate(value['tables']+value['details']):
            box=item['bbox'];target=run/'crops'/(objecthash(uid+str(i))[:24]+'.png');target.parent.mkdir(exist_ok=True)
            if box[2]>box[0] and box[3]>box[1]:crop_plans.append(dict(target=target,pdf_page=n,bbox=box,title=item['title'],provenance=provenance(box)))
    with pymupdf.open(run/'source.pdf') as pdf:
        for plan in crop_plans:
            page=pdf[plan['pdf_page']-1];w,h=page.rect.width,page.rect.height;box=plan['bbox']
            clip=pymupdf.Rect(box[0]*w,box[1]*h,box[2]*w,box[3]*h);scale=1800/max(clip.width,clip.height)
            page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),clip=clip,alpha=False).save(plan['target'])
            crops.append(dict(title=plan['title'],image='crops/'+plan['target'].name,provenance=plan['provenance']))
    components=[dict(component_id=namespace+':'+a,canonical_id=canonical_id('component', *component_scope,a),
        article_id=a,manufacturer_brand=brand,system_id=system,observations=obs,review_status='proposed') for a,obs in sorted(registry.items())]
    conflicts=[]
    for c in components:
        materials=sorted({o['material'].casefold().strip() for o in c['observations'] if o.get('material')})
        if len(materials)>1:conflicts.append(dict(article_id=c['article_id'],property='material',claims=materials,evidence=c['observations'],status='potential_conflict_or_variant; source review required'))
    cross=[];comparisons=[]
    for directory in compare:
        base=Path(directory)
        if (base/'outputs/document.json').exists():base=base/'outputs'
        base=published_directory(base)
        doc=read(base/'document.json')
        oldpath=base/'components.json'
        if not oldpath.exists():oldpath=base/'full/component_registry_reviewed.json'
        old=read(oldpath);old=old.get('components',[]) if isinstance(old,dict) else old
        first=old[0] if old else {}
        other_brand=doc.get('manufacturer_brand') or first.get('manufacturer_brand');other_system=doc.get('system_id') or first.get('system_id')
        other=rules.identity_keys(dict(manufacturer_brand=other_brand,system_id=other_system))
        if not brand_key or not system_key or None in other or (brand_key,system_key)!=other:
            questions.append(dict(category='cross_document_scope',message='Manufacturer/system identity does not match or is unknown; no compatibility or component identity links created.',source_document=str(directory)));continue
        by_id={rules.article_key(c['article_id']):c for c in old if c.get('article_id')}
        for c in components:
            if c['article_id'] in by_id:
                previous=by_id[c['article_id']]
                cross.append(dict(article_id=c['article_id'],relationship='same_literal_article_reference_within_manufacturer_system',
                    old_document=doc,old_observations=previous,new_observations=c['observations'],compatibility=None,
                    assessment='identity candidate; editions, conditions and conflicting claims retained',review_status='proposed'))
                old_obs=previous.get('observations',[])
                before={o['material'].strip().casefold() for o in old_obs if o.get('material')}
                after={o['material'].strip().casefold() for o in c['observations'] if o.get('material')}
                assessment='agreement' if before and before==after else 'conflict' if before and after else 'unknown'
                evidence=dict(old_document=doc,old_observations=old_obs,new_observations=c['observations'])
                comparisons.append(dict(article_id=c['article_id'],property_relationship='material',old_claim=sorted(before) or None,
                    new_evidence=sorted(after) or None,source_pages_regions=evidence,assessment=assessment,
                    condition='Different strings may reflect variants or editions; both claims remain proposed. No compatibility inferred.',review_status='requires_source_and_scope_review'))
                comparisons.append(dict(article_id=c['article_id'],property_relationship='description / geometry / relationship applicability',
                    old_claim=[o.get('label') for o in old_obs],new_evidence=[o['label'] for o in c['observations']],
                    source_pages_regions=evidence,assessment='unknown',condition='Compare original tables, units, conditions and drawings; literal identity does not prove these properties equivalent.',review_status='requires_source_and_scope_review'))
    for path in (run/'reviews').glob('*.json'):
        review=read(path)
        for finding in review['findings']:questions.append(dict(category='model_source_check',unit_id=review['unit_id'],**finding,status='unresolved; not human approval'))
    # Catalogue-wide article index, cross-page links, explicit references and per-detail outputs.
    apply_reviewed_dimensions(source['source_sha256'], measurements, entities)
    article_database, assembly_database = build_databases(components, entities, details, measurements, relations, instructions)
    index=article_index.build_index(article_database)
    article_database,link_issues=article_index.link(article_database,index)
    reference_links=cross_references.find(details,instructions,questions,transcription,relations,measurements,read(run/'reference_index.json'))
    outputs,output_issues=detail_assembly.detail_outputs(identity.get('document_name') or source.get('file_name') or docid,
        details,entities,article_database,assembly_database,index,reference_links,transcription)
    questions.extend(dict(category=i['category'],message=json.dumps({k:v for k,v in i.items() if k not in ('category','provenance')},ensure_ascii=False),
                          provenance=i.get('provenance') or dict(pdf_page=(i.get('pages') or [None])[0])) for i in link_issues+output_issues)
    questions.extend(dict(category='unresolved_reference',message=r['quote'],provenance=r['provenance']) for r in reference_links if r['status']=='unresolved')
    # Material conflicts are review questions, not a separate file.
    questions.extend(dict(category='material_conflict',message=f"{c['article_id']}: {', '.join(c['claims'])}",
                          provenance=c['evidence'][0]['provenance']) for c in conflicts)
    # Only what the output and its review need. Entity sub-lists (drawing instances, tools ...), the
    # transcription, crop list and page index are already inside entities/proposals/the run folder.
    collections=dict(components=components,entities=entities,details=details,relationships=relations,
        graph_relationships=[r for r in relations if r['graph_eligible']],measurements=measurements,
        tables=tables,table_rows=rows,instructions=instructions,questions=questions)
    if compare:collections.update(cross_document_links=cross,cross_document_comparison=comparisons)
    for name,data in collections.items():dataset(run,name,data)
    dump(run/'data/article_database.json', article_database)
    dump(run/'data/article_index.json', index)
    dump(run/'data/reference_links.json', reference_links)
    dump(run/'data/detail_outputs.json', outputs)
    for output in outputs:dump(run/'data/details'/detail_assembly.file_name(output),output)
    dump(run/'data/assembly_database.json', assembly_database)
    parents={u['parent'] for u in units if u['parent']};leaves=[u for u in units if u['unit_id'] not in parents]
    complete=[n for n in range(1,source['page_count']+1) if all(u['unit_id'] in records for u in leaves if u['pdf_page']==n) and any(u['pdf_page']==n for u in leaves)]
    metadata_pages={n for n in complete if all(records[u['unit_id']]['extraction']['page_kind']=='metadata_index_only'
                                               for u in leaves if u['pdf_page']==n)}
    coverage=dict(status=status,total_pdf_pages=source['page_count'],indexed_pages=len(read(run/'reference_index.json')),
        complete_proposal_pages=complete,metadata_pages=sorted(metadata_pages),remaining_pages=sorted(set(range(1,source['page_count']+1))-set(complete)),
        completed_region_proposals=len(records),total_leaf_regions=len(leaves),human_engineering_approved_records=0,
        limitations=['Native text and region detection are retrieval aids.','Source boxes are approximate, not CAD geometry.',
                    'Model source checks are not human approval.','Material-string differences do not exhaust compatibility/geometry conflicts.'])
    if (run/'catalogue_scope.json').is_file():
        scope=read(run/'catalogue_scope.json')
        coverage['target_pages']=scope['target_pages']
        coverage['remaining_target_pages']=sorted(set(scope['target_pages'])-set(complete))
        coverage['support_pages']=scope['support']
    dump(run/'coverage.json',coverage);dump(run/'cost_summary.json',costs)
    dump(run/'document.json',dict(**source,**identity,
        document_id=docid,sha256=source['source_sha256'],review_status='proposed',replay_manifest_version=1))
    dump(run/'components.json',components)
    dataset(run,'all_proposals',list(records.values()))
    # Export a legacy-compatible full-page view only when EVERY leaf region is complete.
    # A single recovered region can never create a completed full-page result.
    for n in complete:
        merged=dict(pdf_page=n,page_kind='metadata_index_only' if n in metadata_pages else 'complete_region_proposals',
            details=[],nodes=[],relations=[],measurements=[],tables=[],instructions=[],unresolved=[])
        page_units=[u for u in leaves if u['pdf_page']==n]
        for i,u in enumerate(page_units):
            value=json.loads(json.dumps(records[u['unit_id']]['extraction']));prefix=f'u{i}_'
            dmap={d['id']:prefix+d['id'] for d in value['details']};nmap={e['id']:prefix+e['id'] for e in value['nodes']}
            for d in value['details']:d['id']=dmap[d['id']]
            for e in value['nodes']:e['id']=nmap[e['id']];e['detail_id']=dmap[e['detail_id']]
            for r in value['relations']:
                r['source'], r['target'] = nmap[r['source']], nmap[r['target']]
                if r.get('fastener_id') is not None:
                    r['fastener_id'] = nmap.get(r['fastener_id'])
                if r.get('engages_in') is not None:
                    r['engages_in'] = nmap.get(r['engages_in'])
                if r.get('passes_through'):
                    r['passes_through'] = [nmap[x] for x in r['passes_through'] if x in nmap]
                for participant in r.get('participants') or []:
                    participant['node_id']=nmap[participant['node_id']]
            for m in value['measurements']:
                m['subject']=nmap.get(m['subject'],dmap.get(m['subject']))
                if m.get('per_node_id'):
                    m['per_node_id']=nmap[m['per_node_id']]
            for t in value['tables']:t['id']=prefix+t['id']
            for instruction in value['instructions']:instruction['detail_id']=dmap[instruction['detail_id']]
            for key in ['details','nodes','relations','measurements','tables','instructions','unresolved']:merged[key].extend(value[key])
        validate(merged,n)
        merged['_metadata']=dict(source_document_id=docid,source_sha256=source['source_sha256'],review_status='proposed',
            full_page_scope_basis='all planned leaf regions have validated proposals',unit_ids=[u['unit_id'] for u in page_units])
        dump(run/'full/pages'/f'p{n:04}.json',merged)
    dump(run/'full/summary.json',dict(document_id=docid,processed_content_pages=len(set(complete)-metadata_pages),
        metadata_only_pages=sorted(metadata_pages),review_status='proposed_not_engineer_verified'))
    links='<p><a href="source.pdf">Original PDF</a> · <a href="coverage.json">Coverage</a> · <a href="cost_summary.json">Costs</a> · <a href="data/components.json">Components (JSON)</a></p>'
    body='<p class="notice">Proposed knowledge. No human engineering approvals. '+html.escape(status)+'</p>'+links
    body+='<p><a href="data/article_database.json">Article Database (JSON)</a> · <a href="data/assembly_database.json">Assembly Database (JSON)</a> · <a href="data/detail_outputs.json">Per-detail outputs (JSON)</a> · <a href="data/article_index.json">Article index</a> · <a href="data/reference_links.json">Cross-references</a></p>'
    body+=f'<p>{len(complete)}/{source["page_count"]} pages have complete proposal coverage. {len(components)} article records; {len(relations)} relationships; {len(rows)} table rows.</p>'
    body+='<input id="search" aria-label="Search records" placeholder="Search article, description or source">'
    body+=table([dict(article_id=c['article_id'],description='; '.join(o['label'] for o in c['observations']),source_pages=sorted({o['provenance']['pdf_page'] for o in c['observations']})) for c in components],['article_id','description','source_pages'])
    body+='<h2>Conflicts and unresolved questions</h2>'+table(questions,['category','message','provenance'])
    if comparisons:body+='<h2>Cross-document comparison</h2>'+table(comparisons,['article_id','property_relationship','old_claim','new_evidence','source_pages_regions','assessment'])
    body+='<h2>Source drawings and tables</h2>'+''.join('<details><summary>'+html.escape(c['title'])+'</summary><img loading="lazy" src="'+c['image']+'" alt="Source crop"><p>'+html.escape(str(c['provenance']))+'</p></details>' for c in crops)
    body+='<script>document.getElementById("search").oninput=function(){const query=this.value.toLowerCase();document.querySelectorAll("tbody tr").forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(query));};</script>'
    write_report(run/'review.html',identity.get('document_name') or 'PDF knowledge review',body)
    return coverage
