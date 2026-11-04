"""Import only complete saved page proposals from the exact same PDF hash."""
from pathlib import Path
import shutil
from .storage import dump,read,objecthash,filehash
from .schema import validate


def import_pages(run,bundle,source,identity,queue,units):
    from .program import result_path
    folder,doc,pages,metadata=bundle;imported=set()
    for unit in units:
        if not unit['imported']:continue
        n=unit['pdf_page'];jid=unit['unit_id']+':extract'
        state=queue.db.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0]
        if state=='completed':imported.add(n);continue
        if n in pages:
            original=pages[n];value=read(original)
            if value.get('_metadata',{}).get('source_document_id') not in [None,doc.get('document_id',doc.get('source_document_id'))]:
                raise ValueError('Imported page provenance differs from its PDF manifest')
            value=validate({k:v for k,v in value.items() if k!='_metadata'},n)
            archived=run/'imported_originals'/f'p{n:04}.json';archived.parent.mkdir(exist_ok=True);shutil.copy2(original,archived)
            record=dict(unit=unit,extraction=value,source_text=[],metadata=dict(source_sha256=source['source_sha256'],
                identity=identity,review_status='reused_proposal_not_newly_verified',original_proposal='imported_originals/'+archived.name,
                original_proposal_sha256=filehash(archived),method='reuse_exact_source_hash; no model call'))
        elif n in metadata:
            value=dict(pdf_page=n,page_kind='metadata_index_only',details=[],nodes=[],relations=[],measurements=[],tables=[],instructions=[],unresolved=[])
            record=dict(unit=unit,extraction=value,source_text=[],metadata=dict(source_sha256=source['source_sha256'],identity=identity,
                review_status='existing_metadata_classification',method='reuse matching document metadata; no model call'))
        else:raise ValueError('An imported unit has no matching full-page source record')
        target=result_path(run,unit)
        def save(spec,value=record,path=target):
            dump(path,value);outputs=[path]
            if value['metadata'].get('original_proposal'):outputs.append(run/value['metadata']['original_proposal'])
            return {'outputs':outputs,'new_api_cost_usd':0,'method':'reuse_saved_page_proposal'}
        queue.run(jid,save);imported.add(n)
    return imported
