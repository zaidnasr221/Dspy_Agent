"""Download once, bind evidence to the PDF hash, and prepare local reference units."""
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from urllib.request import Request,urlopen
import hashlib
import os
import re
import shutil
import tempfile
import traceback
import pymupdf
from .storage import dump,read,filehash

INDEX_VERSION='native-images-regions-1'
REFERENCE=re.compile(r'\b(?:geff[fpc]\d+|K\d{7}|[A-Z]{1,4}\d{3,8}|\d{3}[ \u00a0]?\d{3})\b')


def acquire(pdf_url,out,max_bytes=300_000_000):
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    parsed=urlsplit(pdf_url)
    if parsed.scheme not in ['http','https','file'] or parsed.username or parsed.password:
        raise ValueError('Use an HTTP(S) PDF URL, or file:// for local/replay work; no embedded credentials')
    public_url=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))
    with tempfile.NamedTemporaryFile(dir=out,suffix='.pdf.part',delete=False) as temp:
        partial=Path(temp.name)
        try:
            with urlopen(Request(pdf_url,headers={'User-Agent':'Facade-DSPy/0.1'}),timeout=45) as stream:
                length=stream.headers.get('Content-Length')
                if length and int(length)>max_bytes:raise ValueError('PDF exceeds the download size limit')
                total=0
                while chunk:=stream.read(1024*1024):
                    total+=len(chunk)
                    if total>max_bytes:raise ValueError('PDF exceeds the download size limit')
                    temp.write(chunk)
            temp.flush()
            with partial.open('rb') as check:
                if b'%PDF-' not in check.read(1024):raise ValueError('The URL did not return a PDF')
        except BaseException:
            temp.close();partial.unlink(missing_ok=True);raise
    sha=filehash(partial);run=out/sha[:20];run.mkdir(exist_ok=True)
    target=run/'source.pdf'
    if target.exists():
        if filehash(target)!=sha:partial.unlink();raise ValueError('Stored PDF hash mismatch')
        partial.unlink()
    else:os.replace(partial,target)
    with pymupdf.open(target) as pdf:
        if pdf.needs_pass:raise ValueError('A password-protected PDF requires an unlocked source copy')
        count=len(pdf)
        if count<1 or count>2000:raise ValueError('PDF page count is outside the supported limit (1–2000)')
        metadata=pdf.metadata
    path=run/'source.json'
    if not path.exists():dump(path,dict(source_sha256=sha,source_url=public_url,page_count=count,pdf_metadata=metadata))
    elif read(path)['source_sha256']!=sha:raise ValueError('Existing workspace belongs to a different PDF')
    return run


def index(run,queue):
    source=read(run/'source.json');pages=[]
    with pymupdf.open(run/'source.pdf') as pdf:
        for number,page in enumerate(pdf,1):
            print(f'[local_index] page {number}: native text and image',flush=True)
            cache=run/'index'/f'p{number:04}.json';image=run/'images'/f'p{number:04}.png'
            if cache.exists():
                record=read(cache)
                if record['source_sha256']!=source['source_sha256'] or record['index_version']!=INDEX_VERSION:
                    raise ValueError('Index configuration/source changed; create a new workspace')
                if not image.exists() or filehash(image)!=record['image_sha256']:raise ValueError('Source image changed or missing')
            else:
                w,h=page.rect.width,page.rect.height;groups=defaultdict(list)
                for word in page.get_text('words'):groups[word[5],word[6]].append(word)
                lines=[]
                for group in groups.values():
                    group.sort(key=lambda a:a[0])
                    lines.append(dict(text=' '.join(a[4] for a in group),bbox=[max(0,min(a[0] for a in group)/w),max(0,min(a[1] for a in group)/h),min(1,max(a[2] for a in group)/w),min(1,max(a[3] for a in group)/h)]))
                text=page.get_text();image.parent.mkdir(exist_ok=True)
                page.get_pixmap(matrix=pymupdf.Matrix(2000/max(w,h),2000/max(w,h)),alpha=False).save(image)
                footers=[a['text'] for a in lines if a['bbox'][1]>.94]
                labels=re.findall(r'\bD-\d+\b',' '.join(footers)) or [t.strip() for t in footers if re.fullmatch(r'\d{1,3}',t.strip())]
                tables=[]
                print(f'[local_index] page {number}: table candidates (native characters: {len(text)})',flush=True)
                try:
                    for t in page.find_tables(strategy='text',paths=[]).tables:
                        if t.row_count>=3 and t.col_count>=2:
                            tables.append(dict(bbox=[t.bbox[0]/w,t.bbox[1]/h,t.bbox[2]/w,t.bbox[3]/h],rows=t.row_count,columns=t.col_count))
                except Exception as exc:
                    traceback.print_exc()
                    tables=[{'triage_error':type(exc).__name__}]
                # The body fallback preserves coverage outside any heuristic table boundaries.
                refs=sorted(set(REFERENCE.findall(text)))
                regions=[dict(kind='table_candidate',boundary_status='unverified_text_alignment',**t) for t in tables]
                if any(re.match(r'K\d{7}|geff',r,re.I) for r in refs):regions.append(dict(kind='drawing_candidate',bbox=[.015,.05,.985,.94],boundary_status='coarse_unverified'))
                regions.append(dict(kind='full_page_coverage_fallback',bbox=[0,0,1,1],boundary_status='coverage_only'))
                record=dict(source_sha256=source['source_sha256'],index_version=INDEX_VERSION,pdf_page=number,
                    printed_page=labels[-1] if labels else None,native_text=text,native_lines=lines,
                    reference_candidates=refs,table_candidates=tables,region_candidates=regions,
                    image_sha256=filehash(image),image=f'images/{image.name}',
                    ocr_status='Not performed by local indexing; visual reading is tracked by region jobs')
                dump(cache,record)
            jid=f'p{number:04}:index'
            queue.register(jid,'local_index',str(number),[cache,image,run/'source.json',Path(__file__)])
            state=queue.db.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0]
            if state=='pending':queue.run(jid,lambda spec,c=cache,i=image:{'outputs':[c,i],'new_api_cost_usd':0})
            elif state=='stale':raise ValueError('Index checkpoint is stale; inspect the saved evidence before continuing')
            pages.append(record)
            print(f'[local_index] page {number}: checkpoint completed',flush=True)
    dump(run/'reference_index.json',pages)
    return pages


def band_split(tables,low=.3,high=.7):
    """Midline unless it crosses a detected table; then the nearest table edge inside [low, high]."""
    spans=[(t['bbox'][1],t['bbox'][3]) for t in tables if isinstance(t,dict) and len(t.get('bbox') or [])==4]
    inside=lambda y:any(a<y<b for a,b in spans)
    if not inside(.5):return .5
    edges=sorted({round(y,4) for a,b in spans for y in (a,b) if low<=y<=high and not inside(y)},key=lambda y:abs(y-.5))
    return edges[0] if edges else .5


def make_units(run,pages,imported_pages=()):
    path=run/'units.json'
    if path.exists():
        existing=read(path)
        prior={u['pdf_page'] for u in existing if u['imported']}
        if prior==set(imported_pages):return existing
        if list((run/'proposals').glob('*.json')):raise ValueError('Reuse selection changes an existing extraction plan; choose a new output directory')
    units=[]
    for p in pages:
        n=p['pdf_page']
        if n in imported_pages:
            units.append(dict(unit_id=f'p{n:04}:page',pdf_page=n,bbox=[0,0,1,1],depth=0,parent=None,imported=True));continue
        # ponytail: two non-overlapping bands, with full-page/header context supplied to the reader.
        # More precise regions can replace this planner without changing the queue/data contracts.
        cut=band_split(p.get('table_candidates') or [])
        for label,bbox in [('top',[0,0,1,cut]),('bottom',[0,cut,1,1])]:
            units.append(dict(unit_id=f'p{n:04}:{label}',pdf_page=n,bbox=bbox,depth=0,parent=None,imported=False))
    dump(path,units);return units


def split_unit(unit):
    if unit['depth']>=2:return []
    x0,y0,x1,y1=unit['bbox'];mid=(y0+y1)/2
    return [dict(unit_id=unit['unit_id']+f':{i}',pdf_page=unit['pdf_page'],bbox=box,depth=unit['depth']+1,parent=unit['unit_id'],imported=False)
            for i,box in enumerate([[x0,y0,x1,mid],[x0,mid,x1,y1]],1)]


def prepare_images(run,unit,folder):
    folder.mkdir(parents=True,exist_ok=True)
    n=unit['pdf_page'];shutil.copy2(run/'images'/f'p{n:04}.png',folder/'source.png')
    with pymupdf.open(run/'source.pdf') as pdf:
        page=pdf[n-1];w,h=page.rect.width,page.rect.height;b=unit['bbox']
        clip=pymupdf.Rect(b[0]*w,b[1]*h,b[2]*w,b[3]*h)
        # Zoom the crop itself (longest side ~2200 px) so small callouts gain resolution; never below full-page scale.
        scale=max(2200/max(w,h),min(2200/max(clip.width,clip.height,1e-6),4*2200/max(w,h)))
        page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),clip=clip,alpha=False).save(folder/'focus.png')
    record=read(run/'index'/f'p{n:04}.json')
    (folder/'native.txt').write_text(record['native_text'],encoding='utf-8')
    dump(folder/'scope.json',unit)
