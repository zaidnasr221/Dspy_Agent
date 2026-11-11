"""Publish complete evidence snapshots from durable queue commits."""
from pathlib import Path
import os
import shutil
import tempfile
import uuid

from .storage import dump, filehash, objecthash, read, safe_path


def published_directory(run):
    """Pin the current generation, with a fallback for historical bundles."""
    run = Path(run).resolve()
    pointer = run / 'publication.json'
    if not pointer.exists():
        return run
    snapshot = safe_path(run, read(pointer)['snapshot'])
    if snapshot.parent != (run / 'snapshots') or not (snapshot / 'document.json').is_file():
        raise ValueError('Invalid publication pointer')
    return snapshot


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def mirror_legacy_paths(snapshot, run, previous):
    """Keep legacy root paths available for callers that predate snapshots."""
    for name in ('coverage.json', 'cost_summary.json', 'document.json', 'components.json', 'review.html'):
        mirror_file(snapshot / name, run / name)
    for name in ('data', 'full', 'crops'):
        for source in (snapshot / name).rglob('*'):
            if source.is_file():
                mirror_file(source, run / source.relative_to(snapshot))
    if previous != run.resolve():
        for old in (previous / 'full/pages').glob('*.json'):
            relative = old.relative_to(previous)
            if not (snapshot / relative).exists():
                (run / relative).unlink(missing_ok=True)


def mirror_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as stream, tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
        shutil.copyfileobj(stream, temp)
    try:
        os.replace(temp.name, target)
    finally:
        Path(temp.name).unlink(missing_ok=True)


def publish(run, queue, units, identity, status, costs, compare):
    """Stage outputs under the controller's run lock, then advance one pointer."""
    from .exports import export

    previous = published_directory(run)
    snapshot = run / 'snapshots' / uuid.uuid4().hex
    snapshot.mkdir(parents=True)
    for name in ('source.json', 'source.pdf', 'reference_index.json'):
        copy_file(run / name, snapshot / name)
    for page in read(snapshot / 'reference_index.json'):
        number = page['pdf_page']
        for name in (f'index/p{number:04}.json', f'images/p{number:04}.png'):
            copy_file(run / name, snapshot / name)
    parents = {unit['parent'] for unit in units if unit['parent']}
    for unit in units:
        uid = unit['unit_id']
        if uid in parents:
            continue
        filename = objecthash(uid)[:24] + '.json'
        for stage, directory in (('extract', 'proposals'), ('source_review', 'reviews'), ('relation_check', 'verifications')):
            relative = Path(directory) / filename
            record = queue.committed_json(uid + ':' + stage, run / relative)
            if record is None:
                continue
            if stage == 'extract' and record['unit'] != unit:
                raise ValueError(f'Committed proposal scope changed for {uid}')
            if stage in ('source_review', 'relation_check') and record['unit_id'] != uid:
                raise ValueError(f'Committed review scope changed for {uid}')
            dump(snapshot / relative, record)
            original = record.get('metadata', {}).get('original_proposal')
            if original:
                source = safe_path(run, original)
                copy_file(source, snapshot / source.relative_to(run))
            receipt = record.get('metadata', {}).get('tool_receipt', {}) if stage == 'extract' else record.get('tool_receipt', {})
            for response in (receipt, record.get('feedback_receipt', {})):
                for field in ('raw_response', 'read_validation'):
                    if response.get(field):
                        raw = safe_path(run, response[field])
                        copy_file(raw, snapshot / raw.relative_to(run))
    for name in ('model_selection.json', 'cost_ledger.json', 'units.json', 'pilot_validation.json', 'catalogue_scope.json'):
        if (run / name).is_file():
            copy_file(run / name, snapshot / name)
    coverage = export(snapshot, units, identity, status, costs, compare)
    files = [snapshot / name for name in ('document.json', 'units.json')
             if (snapshot / name).is_file()]
    for directory in ('proposals', 'reviews', 'verifications', 'imported_originals', 'raw_responses'):
        files.extend(p for p in (snapshot / directory).rglob('*') if p.is_file())
    dump(snapshot / 'replay_manifest.json', dict(version=1,
        files={p.relative_to(snapshot).as_posix(): filehash(p) for p in sorted(files)}))
    dump(run / 'publication.json', {'version': 1, 'snapshot': snapshot.relative_to(run).as_posix()})
    try:
        mirror_legacy_paths(snapshot, run, previous)
    except OSError as exc:
        print(f'[publication] Snapshot published. Legacy path refresh failed: {exc}', flush=True)
    return coverage, snapshot
