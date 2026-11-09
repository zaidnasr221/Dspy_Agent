"""Representation repairs only. Original model JSON is always retained separately."""
import json
import re


def normalize(parsed):
    data=json.loads(json.dumps(parsed))
    notes=[]
    extras=[]
    shapes={
      'detail':{'id':None,'title':'Unclassified detail','drawing_refs':[],'bbox':[0,0,1,1]},
      'node':{'id':None,'detail_id':None,'article_id':None,'kind':'unclassified','label':'Unlabelled item',
              'material':None,'quantity':None,'callout':None,'bbox':[0,0,1,1]},
      'relation':{'source':None,'predicate':'unresolved_relation','target':None,'basis':'inference','condition':None,'bbox':[0,0,1,1],'evidence':None},
      'measurement':{'subject':None,'property':'unclassified','value':None,'unit':None,'datum':None,'condition':None,'bbox':[0,0,1,1]},
      'table':{'id':None,'title':'Unclassified table','columns':[],'units':[],'rows':[],'footnotes':[],'bbox':[0,0,1,1]},
      'instruction':{'detail_id':None,'sequence':None,'text':'Unclassified instruction','condition':None,'article_refs':[],'bbox':[0,0,1,1]},
      'row':{'cells':[],'bbox':[0,0,1,1]}}
    def shape(obj,kind,path):
        if not isinstance(obj,dict):
            extras.append({'path':path,'unstructured_value':obj});obj={}
        defaults=shapes[kind]
        for key in list(obj):
            if key not in defaults:
                extras.append({'path':path,'field':key,'value':obj.pop(key)})
        for key,default in defaults.items():
            if key not in obj:
                obj[key]=json.loads(json.dumps(default))
                notes.append(f'{path}.{key}: missing field represented as unknown/empty; no value inferred.')
        if obj.get('bbox') is None:
            obj['bbox']=[0,0,1,1];notes.append(f'{path}: unknown region uses whole-page locator.')
        return obj
    lists={'details':'detail','nodes':'node','relations':'relation','measurements':'measurement',
           'tables':'table','instructions':'instruction'}
    for key,kind in lists.items():
        data[key]=[shape(obj,kind,f'{key}[{i}]') for i,obj in enumerate(data.get(key) or [])]
    data.setdefault('unresolved',[])
    data.setdefault('page_kind','unclassified')
    allowed={'pdf_page','page_kind','unresolved'}|set(lists)
    for key in list(data):
        if key not in allowed:extras.append({'path':'page','field':key,'value':data.pop(key)})
    for t in data['tables']:
        t['rows']=[shape(row,'row',f'table {t["id"]} row {i+1}') for i,row in enumerate(t['rows'])]
        width=max([len(t['columns'])]+[len(row['cells']) for row in t['rows']])
        if len(t['columns'])!=width or any(len(row['cells'])!=width for row in t['rows']):
            notes.append(f'Table {t["id"]}: inconsistent widths retained with unknown columns/cells; alignment requires review.')
        t['columns']+=['Unlabelled column '+str(i+1) for i in range(len(t['columns']),width)]
        t['units']=(t['units']+[None]*width)[:width]
        for row in t['rows']:
            row['cells']=[None if x is None else str(x) for x in row['cells']]+[None]*(width-len(row['cells']))
    for node in data['nodes']:
        if node['article_id'] is not None:node['article_id']=str(node['article_id'])
        if node['callout'] is not None:node['callout']=str(node['callout'])
        if node['quantity'] is not None and not isinstance(node['quantity'],(int,float)):
            extras.append({'path':node['id'],'field':'quantity','value':node['quantity']});node['quantity']=None
            notes.append(f'{node["id"]}: nonnumeric quantity retained in unmapped source fields, not guessed.')
    for r in data['relations']:
        if r['basis'] not in ['explicit_text','drawing_reading','inference']:
            extras.append({'path':'relation','field':'basis','value':r['basis']});r['basis']='inference'
    for m in data['measurements']:
        v=m['value']
        if isinstance(v,dict) or (isinstance(v,list) and not all(isinstance(x,(int,float)) for x in v)):
            m['value']=json.dumps(v,ensure_ascii=False)
            notes.append(f'Measurement {m["property"]}: structured value preserved as literal JSON text.')
    dids={d['id'] for d in data['details']}
    unassigned='unassigned_references'
    def detail(ref,box):
        if ref in dids:return ref
        matches=[x for x in dids if str(x).casefold()==str(ref).casefold()]
        if len(matches)==1:return matches[0]
        if unassigned not in dids:
            data['details'].append({'id':unassigned,'title':'Unassigned source references — review bucket, not a separate drawing',
                                   'drawing_refs':[],'bbox':[0,0,1,1]})
            dids.add(unassigned)
        return unassigned
    for node in data['nodes']:node['detail_id']=detail(node['detail_id'],node['bbox'])
    descriptions={}
    def resolve(ref,box,allow_detail=False):
        ids={x['id'] for x in data['nodes']}
        if ref in ids or (allow_detail and ref in dids):return ref
        text=str(ref or 'Unspecified source entity')
        direct=[x for x in data['nodes'] if text.casefold() in [str(x['id']).casefold(),
                    str(x.get('article_id') or '').casefold(),x['label'].casefold()]]
        if len(direct)==1:return direct[0]['id']
        if text in descriptions:return descriptions[text]
        ident=f'anonymous_reference_{len(descriptions)+1}'
        while ident in ids:ident+='x'
        named_dids=[d for d in dids if d!=unassigned]
        explicit=[d for d in named_dids if re.search(r'(?<!\w)'+re.escape(d)+r'(?!\w)',text,re.I)]
        target=explicit[0] if len(explicit)==1 else named_dids[0] if len(named_dids)==1 else detail(None,box)
        data['nodes'].append({'id':ident,'detail_id':target,'article_id':None,'kind':'unresolved_reference',
            'label':text,'material':None,'quantity':None,'callout':None,'bbox':box})
        descriptions[text]=ident
        notes.append(f'Anonymous reference {text!r} retains an unresolved measurement/relation subject; no part identity inferred.')
        return ident
    for r in data['relations']:
        r['source']=resolve(r['source'],r['bbox']);r['target']=resolve(r['target'],r['bbox'])
    for m in data['measurements']:m['subject']=resolve(m['subject'],m['bbox'],True)
    for ins in data['instructions']:ins['detail_id']=detail(ins['detail_id'],ins['bbox'])
    if notes:data['unresolved'].append('Representation review: '+' '.join(notes))
    if extras:data['unresolved'].append('Additional model fields preserved in metadata; review unmapped fields before engineering use.')
    return data,{'notes':notes,'unmapped_fields':extras}
