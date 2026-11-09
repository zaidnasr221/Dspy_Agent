"""Existing durable SQLite queue, with portable run/code path references."""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,sqlite3,time,traceback,uuid
import polars as pl
from .storage import dump
from .checkpoint_compat import compatible_code
QUEUE_VERSION='2.0-portable'
WORKER_VERSION='dspy-claude-code-1'
def stamp():return datetime.now(timezone.utc).isoformat()
def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def compatible_spec(old,new):
    old_refs,new_refs=old['inputs'].keys(),new['inputs'].keys()
    added=new_refs-old_refs
    if old_refs-new_refs or (added and not (old['stage']=='region_extraction' and
            added=={'code:manufacturers.py','run:manufacturer_profile.json'})):
        return False
    if any(not compatible_code(ref,sha,new['inputs'][ref]) for ref,sha in old['inputs'].items()):
        return False
    if not compatible_code('code:queue.py',old['worker_code_sha256'],new['worker_code_sha256']):
        return False
    fields=old.keys()-{'inputs','worker_code_sha256'}
    return fields==new.keys()-{'inputs','worker_code_sha256'} and all(old[k]==new[k] for k in fields)


class WorkQueue:
    def ref(self,path):
        path=Path(path).resolve()
        if path.is_relative_to(self.workspace):return 'run:'+path.relative_to(self.workspace).as_posix()
        code=Path(__file__).parent.resolve()
        if path.is_relative_to(code):return 'code:'+path.relative_to(code).as_posix()
        raise ValueError('Copy evidence into the document workspace before registering it')

    def path(self,ref):
        kind,relative=ref.split(':',1)
        if kind not in ['run','code']:raise ValueError('Historical absolute checkpoint path; import evidence into a new run instead')
        from .storage import safe_path
        return safe_path(self.workspace if kind=='run' else Path(__file__).parent,relative)

    def snapshot(self):
        rows=[dict(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY id')]
        counts={}
        for r in rows:counts.setdefault(r['stage'],{});counts[r['stage']][r['status']]=counts[r['stage']].get(r['status'],0)+1
        dump(self.root/'queue.json',{'jobs':rows,'by_stage':counts})
        if rows:pl.DataFrame(rows,infer_schema_length=None).write_parquet(self.root/'jobs.parquet')
        for table in ['attempts','events']:
            records=[dict(r) for r in self.db.execute(f'SELECT * FROM {table}')]
            dump(self.root/(table+'.json'),records)
            if records:pl.DataFrame(records,infer_schema_length=None).write_parquet(self.root/(table+'.parquet'))
        dump(self.root/'progress.json',{'by_stage':counts,'engineering_approval_count':0,
            'meaning':'A completed extraction job contains a proposal, not engineering approval.'})
        return counts

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.workspace = self.root.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'queue.sqlite', timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, stage TEXT NOT NULL, subject TEXT NOT NULL,
          status TEXT NOT NULL, spec TEXT NOT NULL, spec_hash TEXT NOT NULL,
          lease_until REAL, active_attempt TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dependencies(
          job_id TEXT NOT NULL REFERENCES jobs(id), dependency_id TEXT NOT NULL REFERENCES jobs(id),
          PRIMARY KEY(job_id,dependency_id));
        CREATE TABLE IF NOT EXISTS attempts(
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), ordinal INTEGER NOT NULL,
          status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
          checkpoint TEXT, error TEXT, UNIQUE(job_id,ordinal));
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT REFERENCES jobs(id),
          attempt_id TEXT, at TEXT NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def event(self, job, event, data=None, attempt=None):
        self.db.execute('INSERT INTO events(job_id,attempt_id,at,event,data) VALUES(?,?,?,?,?)',
                        (job, attempt, stamp(), event, json.dumps(data or {})))

    def register(self, job_id, stage, subject, inputs, dependencies=(), params=None):
        spec = {'version': QUEUE_VERSION, 'worker_version': WORKER_VERSION,
                'worker_code_sha256': digest(Path(__file__)),
                'stage': stage, 'subject': subject,
                'inputs': {self.ref(p): digest(p) for p in inputs},
                'dependencies': sorted(dependencies), 'params': params or {}}
        encoded = json.dumps(spec, sort_keys=True)
        hashed = hashlib.sha256(encoded.encode()).hexdigest()
        with self.transaction():
            old = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if old and old['status'] == 'running':
                raise RuntimeError(f'Cannot change running job {job_id}')
            if old and old['spec_hash'] == hashed:
                return
            if old and compatible_spec(json.loads(old['spec']),spec):
                if old['status'] not in ('completed','needs_review'):
                    self.db.execute('UPDATE jobs SET spec=?,spec_hash=?,updated_at=? WHERE id=?',
                                    (encoded,hashed,stamp(),job_id))
                    self.event(job_id,'compatible_worker_update')
                return
            for dep in dependencies:
                if dep == job_id or not self.db.execute('SELECT 1 FROM jobs WHERE id=?', (dep,)).fetchone():
                    raise ValueError(f'Missing or self dependency: {dep}')
                # Updating an existing job must not introduce a dependency cycle.
                if dep in self.descendants(job_id):
                    raise ValueError('Dependency cycle')
            if old:
                self._invalidate(job_id, 'input or configuration changed')
                self.db.execute('UPDATE jobs SET spec=?,spec_hash=?,stage=?,subject=?,updated_at=? WHERE id=?',
                                (encoded, hashed, stage, subject, stamp(), job_id))
                self.db.execute('DELETE FROM dependencies WHERE job_id=?', (job_id,))
            else:
                self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,NULL,NULL,?)',
                                (job_id, stage, subject, 'pending', encoded, hashed, stamp()))
                self.event(job_id, 'registered')
            self.db.executemany('INSERT INTO dependencies VALUES(?,?)', [(job_id, d) for d in dependencies])

    def descendants(self, job_id):
        return {r[0] for r in self.db.execute('''WITH RECURSIVE children(id) AS (
          SELECT job_id FROM dependencies WHERE dependency_id=? UNION
          SELECT d.job_id FROM dependencies d JOIN children c ON d.dependency_id=c.id)
          SELECT id FROM children''', (job_id,))}

    def _invalidate(self, job_id, reason):
        affected = {job_id} | self.descendants(job_id)
        if any(self.db.execute('SELECT status FROM jobs WHERE id=?', (j,)).fetchone()['status'] == 'running'
               for j in affected):
            raise RuntimeError('Cannot invalidate a running job or its inputs')
        for j in affected:
            self.db.execute("UPDATE jobs SET status='stale',updated_at=? WHERE id=?", (stamp(), j))
            self.event(j, 'invalidated', {'reason': reason, 'root_job': job_id})

    def invalidate(self, job_id, reason):
        with self.transaction():
            if not self.db.execute('SELECT 1 FROM jobs WHERE id=?', (job_id,)).fetchone():
                raise KeyError(job_id)
            self._invalidate(job_id, reason)

    def retry(self, job_id):
        with self.transaction():
            row = self.db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or row['status'] not in ('failed', 'stale'):
                raise ValueError('Retry only failed/stale jobs; invalidate completed jobs first')
            self.db.execute("UPDATE jobs SET status='pending',updated_at=? WHERE id=?", (stamp(), job_id))
            self.event(job_id, 'retry_requested')

    def claim(self, job_id, lease_seconds=300):
        with self.transaction():
            job = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job or job['status'] != 'pending':
                raise ValueError(f'Job is not pending: {job_id}')
            spec = json.loads(job['spec'])
            for dep in spec['dependencies']:
                if self.db.execute('SELECT status FROM jobs WHERE id=?', (dep,)).fetchone()['status'] != 'completed':
                    raise ValueError(f'Unfinished dependency: {dep}')
            for p, sha in spec['inputs'].items():
                if not self.path(p).exists() or digest(self.path(p)) != sha:
                    raise ValueError(f'Input changed; reseed before running: {p}')
            ordinal = self.db.execute('SELECT COUNT(*) FROM attempts WHERE job_id=?', (job_id,)).fetchone()[0] + 1
            attempt = str(uuid.uuid4())
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,NULL,NULL,NULL)',
                            (attempt, job_id, ordinal, 'running', stamp()))
            self.db.execute("UPDATE jobs SET status='running',active_attempt=?,lease_until=?,updated_at=? WHERE id=?",
                            (attempt, time.time()+lease_seconds, stamp(), job_id))
            self.event(job_id, 'claimed', {'ordinal': ordinal}, attempt)
            return dict(job), spec, attempt, ordinal

    def heartbeat(self, job_id, attempt, lease_seconds=300):
        with self.transaction():
            n = self.db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND active_attempt=? AND status='running'",
                                (time.time()+lease_seconds, job_id, attempt)).rowcount
            if n != 1:
                raise ValueError('Lease no longer belongs to this attempt')

    def recover(self):
        with self.transaction():
            expired = self.db.execute("SELECT id,active_attempt FROM jobs WHERE status='running' AND lease_until<?", (time.time(),)).fetchall()
            for job in expired:
                self.db.execute("UPDATE attempts SET status='interrupted',finished_at=?,error=? WHERE id=?",
                                (stamp(), 'Worker lease expired; explicit retry required', job['active_attempt']))
                self.db.execute("UPDATE jobs SET status='failed',lease_until=NULL,active_attempt=NULL,updated_at=? WHERE id=?",
                                (stamp(), job['id']))
                self.event(job['id'], 'interrupted', attempt=job['active_attempt'])
        return len(expired)

    def run(self, job_id, handler):
        job, spec, attempt, ordinal = self.claim(job_id)
        folder = self.root / 'checkpoints' / hashlib.sha256(job_id.encode()).hexdigest()[:16] / f'attempt-{ordinal:03}'
        folder.mkdir(parents=True, exist_ok=True)
        try:
            result = handler(spec)
            if result.get('status', 'completed') not in ('completed', 'needs_review'):
                raise ValueError('Invalid handler completion state')
            files = {self.ref(p): digest(p) for p in result.get('outputs', [])}
            if any(not self.path(p).exists() or digest(self.path(p))!=sha for p,sha in spec['inputs'].items()):
                raise RuntimeError('Input changed during execution; output not committed')
            dependency_outputs = {}
            for dep in spec['dependencies']:
                dep_row = self.db.execute("SELECT checkpoint FROM attempts WHERE job_id=? AND status='completed' ORDER BY ordinal DESC LIMIT 1", (dep,)).fetchone()
                if dep_row:
                    dependency_outputs[dep] = digest(self.path(dep_row['checkpoint']))
            receipt = {'job_id': job_id, 'attempt_id': attempt, 'ordinal': ordinal,
                       'spec': spec, 'dependency_checkpoint_hashes': dependency_outputs,
                       'outputs': files, 'result': {k: v for k, v in result.items() if k != 'outputs'},
                       'completed_at': stamp(), 'new_api_cost_usd': result.get('new_api_cost_usd',0.0)}
            checkpoint = folder / 'checkpoint.json'
            dump(checkpoint, receipt)
            with self.transaction():
                active = self.db.execute('SELECT active_attempt,status FROM jobs WHERE id=?', (job_id,)).fetchone()
                if active['active_attempt'] != attempt or active['status'] != 'running':
                    raise RuntimeError('Worker lost its lease; checkpoint retained but not committed')
                status = result.get('status', 'completed')
                self.db.execute('UPDATE attempts SET status=?,finished_at=?,checkpoint=? WHERE id=?',
                                (status, stamp(), self.ref(checkpoint), attempt))
                self.db.execute('UPDATE jobs SET status=?,active_attempt=NULL,lease_until=NULL,updated_at=? WHERE id=?',
                                (status, stamp(), job_id))
                self.event(job_id, status, {'checkpoint': self.ref(checkpoint)}, attempt)
        except Exception as exc:
            err = {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}
            dump(folder / 'error.json', err)
            with self.transaction():
                self.db.execute("UPDATE attempts SET status='failed',finished_at=?,error=? WHERE id=? AND status='running'",
                                (stamp(), json.dumps(err), attempt))
                self.db.execute("UPDATE jobs SET status='failed',active_attempt=NULL,lease_until=NULL,updated_at=? WHERE id=? AND active_attempt=?",
                                (stamp(), job_id, attempt))
                self.event(job_id, 'failed', err, attempt)
            raise

    def committed_json(self, job_id, path):
        """Read the exact artifact covered by the latest successful commit."""
        row = self.db.execute("""
            SELECT j.spec, a.id AS attempt_id, a.checkpoint
            FROM jobs j JOIN attempts a ON a.job_id=j.id
            WHERE j.id=? AND j.status='completed' AND a.status='completed'
            ORDER BY a.ordinal DESC LIMIT 1
        """, (job_id,)).fetchone()
        if row is None:
            return None
        receipt = json.loads(self.path(row['checkpoint']).read_text(encoding='utf-8'))
        if (receipt['job_id'] != job_id or receipt['attempt_id'] != row['attempt_id']
                or receipt['spec'] != json.loads(row['spec'])):
            raise ValueError(f'Checkpoint identity changed for {job_id}')
        expected = receipt['outputs'].get(self.ref(path))
        if expected is None:
            raise ValueError(f'Committed job {job_id} does not cover {path.name}')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f'Committed output changed for {job_id}')
        return json.loads(content)

    def audit(self, *, preserve=False):
        changed = []
        for job in self.db.execute("SELECT * FROM jobs WHERE status IN ('completed','needs_review')").fetchall():
            spec = json.loads(job['spec'])
            bad = (not compatible_code('code:queue.py',spec['worker_code_sha256'],digest(Path(__file__)))
                   or any(not self.path(p).exists() or not compatible_code(p,sha,digest(self.path(p)))
                          for p,sha in spec['inputs'].items()))
            row = self.db.execute('SELECT id,checkpoint FROM attempts WHERE job_id=? ORDER BY ordinal DESC LIMIT 1', (job['id'],)).fetchone()
            if not row or not row['checkpoint'] or not self.path(row['checkpoint']).exists():
                bad = True
            else:
                receipt = json.loads(self.path(row['checkpoint']).read_text(encoding='utf-8'))
                bad |= (receipt.get('job_id')!=job['id'] or receipt.get('attempt_id')!=row['id']
                        or receipt.get('spec')!=spec)
                bad |= any(not self.path(p).exists() or digest(self.path(p)) != sha for p, sha in receipt['outputs'].items())
                for dep,expected in receipt.get('dependency_checkpoint_hashes',{}).items():
                    dependency=self.db.execute(
                        "SELECT checkpoint FROM attempts WHERE job_id=? AND status='completed' ORDER BY ordinal DESC LIMIT 1",
                        (dep,)).fetchone()
                    bad |= not dependency or not self.path(dependency['checkpoint']).is_file() or digest(self.path(dependency['checkpoint']))!=expected
            if bad:
                changed.append(job['id'])
        if not preserve:
            for j in changed:
                self.invalidate(j, 'input/output integrity audit failed')
        return changed
