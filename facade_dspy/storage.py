"""Atomic UTF-8 output and content hashes; no provider or document globals."""
from pathlib import Path
import hashlib
import json
import os
import tempfile
from time import sleep


def dump(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    data=json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False) as f:
        f.write(data);temp=Path(f.name)
    try:
        for attempt in range(8):
            try:
                os.replace(temp,path)
                return
            except PermissionError:
                if os.name != 'nt' or attempt == 7:
                    raise
                # A concurrent Windows reader can briefly prevent replacement.
                sleep(.02 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def filehash(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def objecthash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def safe_path(root,relative):
    root=Path(root).resolve();path=(root/relative).resolve()
    if not path.is_relative_to(root):raise ValueError('Path leaves its document workspace')
    return path
