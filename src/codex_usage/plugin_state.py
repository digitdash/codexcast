"""Plugin calculation baselines, separate from immutable telemetry history."""
import math
import os
from pathlib import Path
import re
import sqlite3


def validate_session(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', session_id):
        raise ValueError('A verified Codex thread ID is required')
    return session_id


class Baselines:
    def __init__(self, path, *, read_only=False):
        path = Path(path)
        if read_only:
            self.connection = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.connection = sqlite3.connect(path)
        self.connection.execute('CREATE TABLE IF NOT EXISTS baselines (scope TEXT PRIMARY KEY, since REAL NOT NULL)')
        self.connection.commit()

    def since(self, session_id):
        validate_session(session_id)
        row = self.connection.execute('SELECT MAX(since) FROM baselines WHERE scope IN (?,?)',
                                      ('all', 'session:'+session_id)).fetchone()
        return row[0]

    def reset(self, session_id, timestamp):
        if isinstance(timestamp, bool) or not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp):
            raise ValueError('Invalid reset timestamp')
        scope = 'all' if session_id is None else 'session:'+validate_session(session_id)
        with self.connection:
            self.connection.execute('INSERT INTO baselines VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET since=MAX(since,excluded.since)',
                                    (scope, timestamp))

    def close(self):
        self.connection.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
