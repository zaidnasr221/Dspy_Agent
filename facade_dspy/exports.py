"""Source-scoped analytical exports and an offline searchable HTML review."""
from collections import defaultdict
from pathlib import Path
import html
import json
import re
import polars as pl
import pymupdf
from .storage import read,dump,objecthash
from .schema import validate

STYLE='body{font:16px system-ui;line-height:1.5;max-width:1200px;margin:30px auto;padding:18px;color:#24374b}a{color:#006599}table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}.scroll{overflow:auto}img{max-width:100%;height:auto}input{padding:12px;width:90%}.notice{padding:16px;background:#fff2cc}pre{white-space:pre-wrap;overflow-wrap:anywhere}'


def write_report(path,title,body):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><style>'+STYLE+'</style><h1>'+html.escape(title)+'</h1>'+body+'</html>',encoding='utf-8')


def table(rows,columns):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+html.escape(c.replace('_',' '))+'</th>' for c in columns)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+html.escape(str(r.get(c) if r.get(c) is not None else 'Unknown'))+'</td>' for c in columns)+'</tr>' for r in rows)+'</tbody></table></div>'


def dataset(run,name,rows):
    dump(run/'data'/f'{name}.json',rows)
    flat=[{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in r.items()} for r in rows]
    frame=pl.DataFrame(flat,infer_schema_length=None) if flat else pl.DataFrame(schema={'record_id':pl.String})
    frame.write_parquet(run/'data'/f'{name}.parquet');frame.write_csv(run/'data'/f'{name}.csv',null_value='null')


def literal_article(value,kind):
    if not value:return False
    if re.match(r'^(?:geff|K\d{7}$)',value,re.I):return False
    return bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{2,24}|\d{3}[ \u00a0]?\d{3}',value) or re.fullmatch(r'\d{4}',value) and any(x in kind.casefold() for x in ['tool','jig']))


def export(run,units,identity,status,costs,compare):
    source=read(run/'source.json');docid='pdf:'+source['source_sha256'];brand=identity.get('manufacturer_brand');system=identity.get('system_id')
    namespace=(brand.casefold()+':'+system.casefold()) if brand and system else docid+':unresolved_identity'
    registry=defaultdict(list);entities=[];details=[];relations=[];measurements=[];rows=[];instructions=[];questions=[];transcription=[];crops=[];records={};crop_plans=[]
    for path in sorted((run/'proposals').glob('*.json')):
        record=read(path);unit=record['unit'];uid=unit['unit_id'];n=unit['pdf_page']
        if record.get('metadata',{}).get('source_sha256')!=source['source_sha256']:raise ValueError('Proposal belongs to a different source PDF')
        if uid not in {u['unit_id'] for u in units}:raise ValueError('Proposal belongs to an unknown unit')
        value=validate(record['extraction'],n);records[uid]=record
        index=read(run/'index'/f'p{n:04}.json')
        def provenance(box):return dict(manufacturer_brand=brand,system_id=system,document_id=docid,edition=identity.get('edition'),
            source_sha256=source['source_sha256'],pdf_page=n,printed_page=index['printed_page'],region_id=uid,bbox=box,
            source_image=f'images/p{n:04}.png',reading_method=record['metadata'].get('method','Claude Code visual reading'),review_status='proposed')
        def scoped(local):return docid+':'+uid+':'+local
        for d in value['details']:
            details.append(dict(**d,detail_id=scoped(d['id']),record_class='synthetic_review_bucket' if 'review bucket' in d['title'].casefold() else 'source_detail',provenance=provenance(d['bbox'])))
        node_map={v['id']:v for v in value['nodes']}
        for node in value['nodes']:
            kind=node['kind'].casefold();article=node['article_id'];valid=literal_article(article,kind)
            category='tool' if any(s in kind for s in ['tool','jig']) else 'consumable' if any(s in kind for s in ['consumable','sealant','adhesive']) else 'fastener' if any(s in kind for s in ['screw','bolt','fastener']) else 'profile' if any(s in kind for s in ['mullion','transom','profile']) else 'unknown'
            cls='drawing_reference' if kind=='drawing_reference' else 'connection' if kind in ['connection','joint','assembly_connection'] else 'machining_feature' if not article and any(s in kind for s in ['hole','feature','machining','pattern']) else 'unresolved_reference' if 'unresolved' in kind else 'component_definition_reference' if 'definition' in kind or valid and 'reference' in kind else 'drawing_instance'
            e=dict(**node,entity_id=scoped(node['id']),entity_class=cls,component_category=category,component_id=namespace+':'+article if valid else None,
                quantity_scope='observed illustration occurrence, not required installed quantity',provenance=provenance(node['bbox']))
            entities.append(e)
            if valid:registry[article].append(dict(kind=node['kind'],label=node['label'],material=node['material'],provenance=e['provenance']))
            elif article:questions.append(dict(category='unresolved_article_label',message=article,provenance=e['provenance']))
        for r in value['relations']:
            relations.append(dict(**r,source_node=scoped(r['source']),target_node=scoped(r['target']),
                source_article_id=node_map[r['source']]['article_id'],target_article_id=node_map[r['target']]['article_id'],provenance=provenance(r['bbox'])))
        for m in value['measurements']:
            item=dict(m)
            if item['unit'] in ['','unknown','(none)']:item['unit']=None
            measurements.append(dict(**item,provenance=provenance(m['bbox'])))
        for t in value['tables']:
            for i,row in enumerate(t['rows'],1):
                rows.append(dict(table_id=scoped(t['id']),row_number=i,title=t['title'],headers=t['columns'],units=[None if u in ['', 'unknown','(none)'] else u for u in t['units']],
                    cells=row['cells'],footnotes=t['footnotes'],provenance=provenance(row['bbox'])))
                for col,cell in zip(t['columns'],row['cells']):
                    if cell and re.search(r'Art\.?[- ]?(?:Nr|No)|article|reference',col,re.I) and not re.search('drawing',col,re.I):
                        for ref in re.findall(r'\b(?:[A-Z]{1,4}\d{3,5}|\d{3}[ \u00a0]?\d{3})\b',cell):
                            if literal_article(ref,'table_reference'):registry[ref].append(dict(kind='table_reference',label=' | '.join(c or 'Unknown' for c in row['cells']),material=None,
                                headers=t['columns'],units=t['units'],footnotes=t['footnotes'],provenance=provenance(row['bbox'])))
        instructions.extend(dict(**i,provenance=provenance(i['bbox'])) for i in value['instructions'])
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
    components=[dict(component_id=namespace+':'+a,article_id=a,manufacturer_brand=brand,system_id=system,observations=obs,review_status='proposed') for a,obs in sorted(registry.items())]
    conflicts=[]
    for c in components:
        materials=sorted({o['material'].casefold().strip() for o in c['observations'] if o.get('material')})
        if len(materials)>1:conflicts.append(dict(article_id=c['article_id'],property='material',claims=materials,evidence=c['observations'],status='potential_conflict_or_variant; source review required'))
    cross=[];comparisons=[]
    for directory in compare:
        base=Path(directory)
        if (base/'outputs/document.json').exists():base=base/'outputs'
        doc=read(base/'document.json')
        oldpath=base/'components.json'
        if not oldpath.exists():oldpath=base/'full/component_registry_reviewed.json'
        old=read(oldpath);old=old.get('components',[]) if isinstance(old,dict) else old
        first=old[0] if old else {}
        other_brand=doc.get('manufacturer_brand') or first.get('manufacturer_brand');other_system=doc.get('system_id') or first.get('system_id')
        if not brand or not system or not other_brand or not other_system or brand.casefold()!=other_brand.casefold() or system.casefold()!=other_system.casefold():
            questions.append(dict(category='cross_document_scope',message='Manufacturer/system identity does not match or is unknown; no compatibility or component identity links created.',source_document=str(directory)));continue
        by_id={c['article_id']:c for c in old}
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
    collections=dict(components=components,entities=entities,details=details,relationships=relations,measurements=measurements,
        table_rows=rows,instructions=instructions,questions=questions,conflicts=conflicts,cross_document_links=cross,cross_document_comparison=comparisons,
        source_text=transcription,source_crops=crops,reference_index=read(run/'reference_index.json'))
    for name,predicate in [('drawing_instances',lambda e:e['entity_class']=='drawing_instance'),('connections',lambda e:e['entity_class']=='connection'),
                           ('machining_features',lambda e:e['entity_class']=='machining_feature'),('tools',lambda e:e['component_category']=='tool'),
                           ('consumables',lambda e:e['component_category']=='consumable')]:collections[name]=[e for e in entities if predicate(e)]
    for name,data in collections.items():dataset(run,name,data)
    parents={u['parent'] for u in units if u['parent']};leaves=[u for u in units if u['unit_id'] not in parents]
    metadata_pages={r['unit']['pdf_page'] for r in records.values() if r['extraction']['page_kind']=='metadata_index_only'}
    complete=[n for n in range(1,source['page_count']+1) if all(u['unit_id'] in records for u in leaves if u['pdf_page']==n) and any(u['pdf_page']==n for u in leaves)]
    coverage=dict(status=status,total_pdf_pages=source['page_count'],indexed_pages=len(read(run/'reference_index.json')),
        complete_proposal_pages=complete,metadata_pages=sorted(metadata_pages),remaining_pages=sorted(set(range(1,source['page_count']+1))-set(complete)),
        completed_region_proposals=len(records),total_leaf_regions=len(leaves),human_engineering_approved_records=0,
        limitations=['Native text and region detection are retrieval aids.','Source boxes are approximate, not CAD geometry.',
                    'Model source checks are not human approval.','Material-string differences do not exhaust compatibility/geometry conflicts.'])
    dump(run/'coverage.json',coverage);dump(run/'cost_summary.json',costs)
    dump(run/'document.json',dict(**source,**identity,document_id=docid,sha256=source['source_sha256'],review_status='proposed'))
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
            for r in value['relations']:r['source']=nmap[r['source']];r['target']=nmap[r['target']]
            for m in value['measurements']:m['subject']=nmap.get(m['subject'],dmap.get(m['subject']))
            for t in value['tables']:t['id']=prefix+t['id']
            for instruction in value['instructions']:instruction['detail_id']=dmap[instruction['detail_id']]
            for key in ['details','nodes','relations','measurements','tables','instructions','unresolved']:merged[key].extend(value[key])
        validate(merged,n)
        merged['_metadata']=dict(source_document_id=docid,source_sha256=source['source_sha256'],review_status='proposed',
            full_page_scope_basis='all planned leaf regions have validated proposals',unit_ids=[u['unit_id'] for u in page_units])
        dump(run/'full/pages'/f'p{n:04}.json',merged)
    dump(run/'full/summary.json',dict(document_id=docid,processed_content_pages=len(set(complete)-metadata_pages),
        metadata_only_pages=sorted(metadata_pages),review_status='proposed_not_engineer_verified'))
    links='<p><a href="source.pdf">Original PDF</a> · <a href="coverage.json">Coverage</a> · <a href="cost_summary.json">Costs</a> · <a href="data/components.json">JSON</a> · <a href="data/components.parquet">Parquet</a> · <a href="data/components.csv">CSV</a></p>'
    body='<p class="notice">Proposed knowledge. No human engineering approvals. '+html.escape(status)+'</p>'+links
    body+=f'<p>{len(complete)}/{source["page_count"]} pages have complete proposal coverage. {len(components)} article records; {len(relations)} relationships; {len(rows)} table rows.</p>'
    body+='<input id="search" aria-label="Search records" placeholder="Search article, description or source">'
    body+=table([dict(article_id=c['article_id'],description='; '.join(o['label'] for o in c['observations']),source_pages=sorted({o['provenance']['pdf_page'] for o in c['observations']})) for c in components],['article_id','description','source_pages'])
    body+='<h2>Conflicts and unresolved questions</h2>'+table(questions,['category','message','provenance'])
    if comparisons:body+='<h2>Cross-document comparison</h2>'+table(comparisons,['article_id','property_relationship','old_claim','new_evidence','source_pages_regions','assessment'])
    body+='<h2>Source drawings and tables</h2>'+''.join('<details><summary>'+html.escape(c['title'])+'</summary><img loading="lazy" src="'+c['image']+'" alt="Source crop"><p>'+html.escape(str(c['provenance']))+'</p></details>' for c in crops)
    body+='<script>document.getElementById("search").oninput=function(){const query=this.value.toLowerCase();document.querySelectorAll("tbody tr").forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(query));};</script>'
    write_report(run/'review.html',identity.get('document_name') or 'PDF knowledge review',body)
    return coverage
