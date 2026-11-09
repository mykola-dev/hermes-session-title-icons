"""Bounded durable queue containing IDs and counts, never titles/messages/tokens.

This SQLite file belongs ONLY to this plugin, not Hermes SessionDB.
"""
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4


class Queue:
    def __init__(self, directory, *, capacity=10000):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / 'jobs.sqlite3'
        self.capacity = capacity
        db = self._connect()
        try:
            with db:
                db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, caller TEXT NOT NULL,
                    session_id TEXT, limit_n INTEGER, due REAL NOT NULL,
                    state TEXT NOT NULL, result TEXT)''')
        finally:
            db.close()
        os.chmod(self.path, 0o600)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=0.25)

    def _insert(self, job_id, kind, caller, sid, limit, due):
        db = self._connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM jobs WHERE id=?', (job_id,)).fetchone():
                    return False
                if db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= self.capacity:
                    raise RuntimeError('queue_capacity')
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,NULL)',
                           (job_id, kind, caller, sid, limit, due, 'pending'))
            return True
        finally:
            db.close()

    def schedule(self, sid, delay, *, now=None):
        if not isinstance(sid, str) or not 0 < len(sid) <= 256:
            raise ValueError('invalid_session_id')
        return self._insert('auto:' + sid, 'auto', '', sid, None,
                            (time.time() if now is None else now) + delay)

    def batch(self, caller, limit):
        if not caller or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid_batch')
        job_id = 'batch:' + uuid4().hex
        self._insert(job_id, 'batch', caller, None, limit, time.time())
        return job_id

    def claim(self, *, now=None):
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.row_factory = sqlite3.Row
            with db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT * FROM jobs WHERE state='pending' AND due<=? ORDER BY due,id LIMIT 1",
                                 (now,)).fetchone()
                if row is None:
                    return None
                db.execute("UPDATE jobs SET state='running' WHERE id=?", (row['id'],))
            return dict(row)
        finally:
            db.close()

    def finish(self, job_id, result):
        db = self._connect()
        try:
            with db:
                db.execute("UPDATE jobs SET state='done',result=? WHERE id=? AND state='running'",
                           (json.dumps(result, separators=(',', ':')), job_id))
        finally:
            db.close()

    def recover(self):
        # Never replay an uncertain model/write after process death. Keep the
        # deduplication row so subsequent hooks cannot silently rearm it.
        db = self._connect()
        try:
            with db:
                db.execute("UPDATE jobs SET state='done',result=? WHERE state='running'",
                           (json.dumps({'error': 'interrupted_outcome_unknown'}),))
        finally:
            db.close()

    def status(self, job_id, caller):
        db = self._connect()
        try:
            row = db.execute('SELECT state,result FROM jobs WHERE id=? AND caller=? AND kind=?',
                             (job_id, caller, 'batch')).fetchone()
            return None if row is None else {'state': row[0], 'result': json.loads(row[1]) if row[1] else None}
        finally:
            db.close()
