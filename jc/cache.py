"""Exact request caching: task, model, all batch content and prompt participate."""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


class Cache:
    def __init__(self, path: Path, ttl: int):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError('Cache cannot be a symlink')
        self.db = sqlite3.connect(path, timeout=5)
        os.chmod(path, 0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL)')
        self.ttl = ttl

    @staticmethod
    def key(body):
        return hashlib.sha256(('request-v2\n' + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()

    def get(self, body):
        row = self.db.execute('SELECT value, created FROM responses WHERE key=?', (self.key(body),)).fetchone()
        if row and self.ttl and time.time() - row[1] < self.ttl:
            try:
                return json.loads(row[0])
            except (ValueError, TypeError):
                return None
        return None

    def put(self, body, scores):
        self.db.execute('INSERT OR REPLACE INTO responses VALUES (?, ?, ?)', (self.key(body), json.dumps(scores), time.time()))
        self.db.commit()

    def close(self):
        self.db.close()
