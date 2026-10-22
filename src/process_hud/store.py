from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from .model import STATES, TERMINAL, progress, public_progress
from .util import ID_RE, alive, clean, private_dir, tail

DEFAULTS = {'visible': True, 'width': 294, 'opacity': .24, 'notify': True,
            'sound': True, 'log_bytes': 2*1024*1024, 'poll_seconds': 2.0}


class Store:
    def __init__(self, home: str | Path | None = None):
        self.home = private_dir(Path(home or os.environ.get('PROCESS_HUD_HOME') or
                                    Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))/'process-hud'))
        self.logs = private_dir(self.home/'jobs')
        self.db = self.home/'state.sqlite3'
        fd = os.open(self.db, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd); self.db.chmod(0o600)
        with self.connect() as c:
            if c.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
                raise RuntimeError('Unsupported database version; do not overwrite it')
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, idem TEXT UNIQUE, fingerprint TEXT NOT NULL,
                data TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, attempted REAL NOT NULL);
                PRAGMA user_version=1;''')

    @contextlib.contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=8)
        try:
            c.execute('PRAGMA busy_timeout=8000')
            yield c
            c.commit()
        except BaseException:
            c.rollback(); raise
        finally:
            c.close()

    def task_dir(self, job_id: str) -> Path:
        if not ID_RE.fullmatch(job_id):
            raise ValueError('Expected full 32-character task ID')
        return private_dir(self.logs/job_id)

    def create(self, title: str, backend: str, target: dict | None = None,
               *, key: str | None = None, task_id: str | None = None,
               codex_origin: dict | None = None) -> tuple[dict, bool]:
        title = clean(title, 160).replace('\n', ' ').strip()
        if not title:
            raise ValueError('Task title is empty')
        target = target or {}
        spec = {'title': title, 'backend': backend, 'target': target}
        if codex_origin:
            spec['codex_origin'] = codex_origin
        fingerprint_spec = spec
        if codex_origin:
            # A reconnect changes transport identity, not the authorized operation.
            fingerprint_spec = dict(spec, codex_origin={k: v for k, v in codex_origin.items()
                                                      if k != 'instance'})
        fingerprint = hashlib.sha256(json.dumps(fingerprint_spec, sort_keys=True).encode()).hexdigest()
        task_id = task_id or uuid.uuid4().hex
        if not ID_RE.fullmatch(task_id):
            raise ValueError('Invalid task ID')
        now = time.time()
        job = dict(spec, id=task_id, state='starting', observation='live', finished=False,
                   created_at=now, updated_at=now, started_at=None, ended_at=None,
                   exit_code=None, worker=None, process=None, progress={}, detail='',
                   removed_at=None, capabilities={})
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            matches = c.execute('SELECT data,fingerprint FROM tasks WHERE id=? OR idem=?',
                                (task_id, key)).fetchall()
            if len(matches) > 1:
                raise ValueError('Task ID and idempotency key refer to different tasks')
            if matches:
                existing = matches[0]
                if existing[1] != fingerprint:
                    raise ValueError('Idempotency key/task ID already has different launch parameters')
                return json.loads(existing[0]), False
            c.execute('INSERT INTO tasks(id,idem,fingerprint,data) VALUES(?,?,?,?)',
                      (task_id, key, fingerprint, json.dumps(job)))
        self.task_dir(task_id)
        return job, True

    def get(self, job_id: str) -> dict:
        with self.connect() as c:
            r = c.execute('SELECT data FROM tasks WHERE id=?', (job_id,)).fetchone()
        if not r:
            raise KeyError(f'Unknown task ID: {job_id}')
        return json.loads(r[0])

    def list(self, include_removed=False) -> list[dict]:
        with self.connect() as c:
            query = 'SELECT data FROM tasks' + ('' if include_removed else ' WHERE removed=0')
            return sorted((self.view(json.loads(r[0])) for r in c.execute(query)), key=lambda x: (x['created_at'], x['id']))

    @staticmethod
    def view(job):
        if (not job['finished'] and job['backend'] != 'external' and
                time.time()-job['created_at'] > 4 and alive(job.get('worker')) is False):
            return dict(job, state='unknown', observation='lost',
                        detail='Observer is no longer running; use process-hud recover. Workload will NOT be relaunched.')
        return job

    def mutate(self, job_id, fn):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT data FROM tasks WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            job = json.loads(row[0]); fn(job)
            job['updated_at'] = time.time()
            c.execute('UPDATE tasks SET data=?,removed=? WHERE id=?',
                      (json.dumps(job, allow_nan=False), int(job['removed_at'] is not None), job_id))
            return job

    def update(self, job_id, **values):
        def change(j):
            if 'state' in values and values['state'] not in STATES:
                raise ValueError('Invalid state')
            # A final receipt cannot be downgraded by a late progress or heartbeat.
            if j['finished']:
                return
            j.update(values)
        return self.mutate(job_id, change)

    @staticmethod
    def _apply_progress(job, event):
        if job['finished']:
            raise ValueError('Cannot update progress after final receipt')
        updated = progress(job['progress'], event)
        request = job.get('attention_request')
        if 'attention' in event:
            reason = event['attention']
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError('attention must be a nonempty reason string')
            reason = clean(reason, 1000)
            if (request or {}).get('reason') != reason:
                request = {'id': uuid.uuid4().hex, 'reason': reason, 'created_at': time.time()}
        # Commit the event only after all of its fields have been validated.
        job['progress'] = updated
        if request is not None:
            job['attention_request'] = request

    def set_progress(self, job_id, event):
        return self.mutate(job_id, lambda job: self._apply_progress(job, event))

    def set_progress_batch(self, job_id, events):
        """Apply stdout events in order in one transaction; malformed lines are data."""
        def change(job):
            if job['finished']:
                raise ValueError('Cannot update progress after final receipt')
            for event in events:
                try:
                    self._apply_progress(job, event)
                except (ValueError, TypeError):
                    continue
        return self.mutate(job_id, change)

    def finish(self, job_id, state, *, code=None, detail='', evidence=''):
        if state not in TERMINAL | {'unknown'}:
            raise ValueError('Invalid terminal outcome')
        return self.update(job_id, state=state, observation='complete' if state in TERMINAL else 'lost',
                           finished=True, exit_code=code, detail=clean(detail),
                           evidence=clean(evidence), ended_at=time.time())

    def remove(self, job_id):
        return self.mutate(job_id, lambda j: j.update(removed_at=j['removed_at'] or time.time()))

    def restore(self, job_id):
        def change(j):
            j['removed_at'] = None
            for event in j.get('codex_events', {}).values():
                if event['state'] == 'dismissed':
                    event['state'] = 'pending'
            if j.get('codex_delivery', {}).get('state') == 'dismissed':
                j['codex_delivery'] = {'state': 'pending'}
        return self.mutate(job_id, change)

    def acknowledge(self, job_id, event_id):
        def change(j):
            event = next((e for e in j.get('codex_events', {}).values()
                          if e['event'] == event_id), None)
            if event is None:
                raise ValueError('Unknown event for this task')
            event.update(state='consumed', consumed_at=time.time())
            request = j.get('attention_request') or {}
            if event['key'] == 'attention:'+request.get('id', ''):
                j['attention_request'] = None
        return self.mutate(job_id, change)

    def setting(self, key, default=None):
        with self.connect() as c:
            row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else DEFAULTS.get(key, default)

    def configure(self, **values):
        with self.connect() as c:
            c.executemany('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          [(k, json.dumps(v, allow_nan=False)) for k, v in values.items()])

    def claim_notification(self, job_id) -> bool:
        with self.connect() as c:
            return c.execute('INSERT OR IGNORE INTO notifications VALUES(?,?)', (job_id, time.time())).rowcount == 1

    def report(self, job_id, lines=40):
        j = self.view(self.get(job_id))
        j['progress'] = public_progress(j['progress'])
        j['log_path'] = str(self.task_dir(job_id)/'output.log')
        j['log_tail'] = tail(Path(j['log_path']), lines)
        j['schema_version'] = 1
        return j
