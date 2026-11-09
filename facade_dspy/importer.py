"""Replay published regions and their committed reviews, with a legacy page fallback."""
from .storage import dump,read,objecthash,filehash,safe_path
from .schema import validate, VisualReview
from .publication import copy_file


def snapshot_plan(folder):
    manifest_path=folder/'replay_manifest.json'
    if manifest_path.is_file():
        manifest=read(manifest_path)
        if manifest.get('version')!=1 or not {'units.json','document.json'}<=manifest['files'].keys():
            raise ValueError('Invalid replay manifest')
        files={p.relative_to(folder).as_posix() for directory in ('proposals','reviews','imported_originals','raw_responses')
               for p in (folder/directory).rglob('*') if p.is_file()}
        if not files<=manifest['files'].keys():
            raise ValueError('Replay evidence is outside the snapshot manifest')
        for ref,sha in manifest['files'].items():
            path=safe_path(folder,ref)
            if not path.is_file() or filehash(path)!=sha:
                raise ValueError(f'Snapshot replay integrity check failed: {ref}')
    else:
        if (folder/'manufacturer_profile.json').is_file() or read(folder/'document.json').get('replay_manifest_version'):
            raise ValueError('Published replay manifest is missing')
        if folder.parent.name!='snapshots' or not (folder/'units.json').is_file():
            return None
    units=read(folder/'units.json')
    ids={u['unit_id'] for u in units}
    if len(ids)!=len(units):
        raise ValueError('Duplicate replay unit IDs')
    parents={u['parent'] for u in units if u['parent']}
    names={objecthash(uid)[:24]+'.json' for uid in ids-parents}
    proposals={p.name for p in (folder/'proposals').glob('*.json')}
    reviews={p.name for p in (folder/'reviews').glob('*.json')}
    if not reviews<=proposals<=names:
        raise ValueError('Snapshot review or proposal is missing its source region')
    return [dict(u,imported=objecthash(u['unit_id'])[:24]+'.json' in proposals) for u in units]


def copy_evidence(source,target):
    if target.exists():
        if filehash(source)!=filehash(target):
            raise ValueError(f'Replay evidence changed: {target.name}; choose a fresh output directory')
    else:
        copy_file(source,target)
    return target


def copy_receipt(folder,run,receipt):
    return [copy_evidence(safe_path(folder,receipt[key]),safe_path(run,receipt[key]))
            for key in ('raw_response','read_validation') if receipt.get(key)]


def import_regions(run,folder,source,profile,queue,units):
    from .program import result_path
    for unit in units:
        if not unit['imported']:
            continue
        uid=unit['unit_id'];jid=uid+':extract'
        target=result_path(run,unit)
        original=folder/'proposals'/target.name
        record=read(original)
        if (dict(record['unit'],imported=True)!=unit or
                record['metadata']['source_sha256']!=source['source_sha256']):
            raise ValueError(f'Replay proposal provenance changed for {uid}')
        validate(record['extraction'],unit['pdf_page'],profile['drawing_reference_patterns'])
        archive=copy_evidence(original,run/'imported_originals'/'regions'/target.name)
        metadata=record['metadata']
        evidence=copy_receipt(folder,run,metadata.get('tool_receipt',{}))
        metadata.update(original_proposal=archive.relative_to(run).as_posix(),
                        original_proposal_sha256=filehash(archive),replayed_from_snapshot=True)
        record['unit']=unit
        if queue.committed_json(jid,target) is None:
            def save(spec):
                dump(target,record)
                return {'outputs':[target,archive,*evidence],'new_api_cost_usd':0,'method':'replay_snapshot_region'}
            queue.run(jid,save)
        review_source=folder/'reviews'/target.name
        if not review_source.is_file():
            continue
        review=read(review_source)
        fields={key:review[key] for key in VisualReview.model_fields}
        if VisualReview.model_validate(fields).unit_id!=uid:
            raise ValueError(f'Replay review belongs to another region: {uid}')
        review_archive=copy_evidence(review_source,run/'imported_originals'/'reviews'/target.name)
        review_evidence=copy_receipt(folder,run,review.get('tool_receipt',{}))
        review_target=run/'reviews'/target.name
        review_job=uid+':source_review'
        queue.register(review_job,'selected_source_verification',uid,[target,review_archive,*review_evidence],[jid],
                       {'review_status':'replayed_model_check_not_human_approval'})
        if queue.committed_json(review_job,review_target) is None:
            def save_review(spec):
                dump(review_target,review)
                return {'outputs':[review_target,review_archive,*review_evidence],'new_api_cost_usd':0,
                        'method':'replay_snapshot_review'}
            queue.run(review_job,save_review)


def import_pages(run,bundle,source,identity,queue,units,profile):
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
            value=validate({k:v for k,v in value.items() if k!='_metadata'},n,profile['drawing_reference_patterns'])
            archived=copy_evidence(original,run/'imported_originals'/f'p{n:04}.json')
            record=dict(unit=unit,extraction=value,source_text=[],metadata=dict(source_sha256=source['source_sha256'],
                identity=identity,review_status='reused_proposal_not_newly_verified',original_proposal='imported_originals/'+archived.name,
                original_proposal_sha256=filehash(archived),method='reuse_exact_source_hash; no model call',
                legacy_review_unavailable=True))
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
