"""Allowlisted telemetry only; no raw Codex payloads enter the database."""
import csv
from dataclasses import asdict, fields
import json
import os
from pathlib import Path
import sqlite3
from .models import UsageSample

DEFAULT_DB = Path.home() / 'Library/Application Support/codex-usage-estimator/usage.sqlite3'
COLUMNS = [f.name for f in fields(UsageSample)]


class Store:
    def __init__(self, path=DEFAULT_DB, *, read_only=False):
        self.path = Path(path).expanduser()
        if read_only:
            self.connection = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True)
            self.connection.row_factory = sqlite3.Row
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Set permissions before sqlite opens the file; never store credentials here.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('''CREATE TABLE IF NOT EXISTS samples (
            timestamp REAL NOT NULL, used_percent REAL NOT NULL,
            limit_id TEXT NOT NULL, window TEXT NOT NULL, window_minutes INTEGER,
            resets_at REAL, model TEXT, banked_reset_count INTEGER, limit_name TEXT,
            PRIMARY KEY (limit_id, window, timestamp))''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS token_quota_samples (
            timestamp REAL NOT NULL, used_percent REAL NOT NULL,
            limit_id TEXT NOT NULL, window TEXT NOT NULL, window_minutes INTEGER,
            resets_at REAL, model TEXT, banked_reset_count INTEGER, limit_name TEXT,
            PRIMARY KEY (limit_id, window, timestamp))''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS token_events (
            event_id TEXT PRIMARY KEY, timestamp REAL NOT NULL, started_at REAL NOT NULL,
            tokens INTEGER NOT NULL, source TEXT NOT NULL)''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS credit_samples (
            timestamp REAL PRIMARY KEY, balance TEXT NOT NULL)''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS token_credit_details (
            event_id TEXT PRIMARY KEY, model TEXT, service_tier TEXT,
            input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
            cache_write_input_tokens INTEGER)''')
        # Account polls stay separate from possibly delayed rollout balance snapshots.
        self.connection.execute('''CREATE TABLE IF NOT EXISTS account_credit_samples (
            timestamp REAL PRIMARY KEY, balance TEXT NOT NULL)''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS token_response_boundaries (
            event_id TEXT PRIMARY KEY, response_started_at REAL NOT NULL)''')
        self.connection.execute('CREATE INDEX IF NOT EXISTS token_event_time ON token_events(timestamp)')
        self.connection.execute('CREATE INDEX IF NOT EXISTS token_quota_time ON token_quota_samples(timestamp)')
        self.connection.commit()

    def add_token_quotas(self, samples):
        with self.connection:
            self.connection.executemany(
                f'INSERT OR IGNORE INTO token_quota_samples ({",".join(COLUMNS)}) VALUES ({",".join("?" for _ in COLUMNS)})',
                [tuple(asdict(s)[c] for c in COLUMNS) for s in samples])

    def load_token_quotas(self, *, since):
        return [UsageSample(**dict(r)) for r in self.connection.execute(
            'SELECT * FROM token_quota_samples WHERE timestamp>=? ORDER BY timestamp,limit_id,window', (since,))]

    def add_token_events(self, events):
        with self.connection:
            self.connection.executemany('INSERT OR IGNORE INTO token_events VALUES (?,?,?,?,?)',
                [(e.event_id,e.timestamp,e.started_at,e.tokens,e.source) for e in events])
            self.connection.executemany('''INSERT INTO token_response_boundaries VALUES (?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                response_started_at=MIN(response_started_at,excluded.response_started_at)''',
                [(e.event_id,e.response_started_at) for e in events if e.response_started_at is not None])
            self.connection.executemany('''INSERT INTO token_credit_details VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                model=COALESCE(excluded.model,model), service_tier=COALESCE(excluded.service_tier,service_tier),
                input_tokens=COALESCE(excluded.input_tokens,input_tokens),
                cached_input_tokens=COALESCE(excluded.cached_input_tokens,cached_input_tokens),
                output_tokens=COALESCE(excluded.output_tokens,output_tokens),
                cache_write_input_tokens=COALESCE(excluded.cache_write_input_tokens,cache_write_input_tokens)''',
                [(e.event_id,e.model,e.service_tier,e.input_tokens,e.cached_input_tokens,
                  e.output_tokens,e.cache_write_input_tokens) for e in events if e.model is not None])

    def load_token_events(self, *, since=None):
        from .tokens import TokenEvent
        query='''SELECT e.*, d.model, d.service_tier, d.input_tokens, d.cached_input_tokens,
                 d.output_tokens, d.cache_write_input_tokens, b.response_started_at FROM token_events e
                 LEFT JOIN token_credit_details d ON e.event_id=d.event_id
                 LEFT JOIN token_response_boundaries b ON e.event_id=b.event_id'''
        args=[]
        if since is not None:
            query+=' WHERE timestamp>=?';args.append(since)
        return [TokenEvent(**dict(r)) for r in self.connection.execute(query+' ORDER BY timestamp,e.event_id',args)]

    def add_account_credits(self, readings):
        with self.connection:
            self.connection.executemany('INSERT OR IGNORE INTO account_credit_samples VALUES (?,?)',readings)

    def load_account_credits(self, *, since):
        return [(r['timestamp'],r['balance']) for r in self.connection.execute(
            'SELECT * FROM account_credit_samples WHERE timestamp>=? ORDER BY timestamp',(since,))]

    def add_credits(self, readings):
        with self.connection:
            self.connection.executemany('INSERT OR IGNORE INTO credit_samples VALUES (?,?)',readings)

    def load_credits(self, *, since):
        return [(r['timestamp'],r['balance']) for r in self.connection.execute(
            'SELECT * FROM credit_samples WHERE timestamp>=? ORDER BY timestamp',(since,))]

    def add(self, samples):
        with self.connection:
            self.connection.executemany(
                f'INSERT OR IGNORE INTO samples ({",".join(COLUMNS)}) VALUES ({",".join("?" for _ in COLUMNS)})',
                [tuple(asdict(s)[c] for c in COLUMNS) for s in samples])

    def load(self, *, limit_id=None, window=None, since=None, limit=None):
        clauses, values = [], []
        for column, value in [('limit_id', limit_id), ('window', window)]:
            if value is not None:
                clauses.append(f'{column} = ?')
                values.append(value)
        if since is not None:
            clauses.append('timestamp >= ?')
            values.append(since)
        query = 'SELECT * FROM samples'
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY timestamp DESC, limit_id, window'
        if limit is not None:
            query += ' LIMIT ?'
            values.append(limit)
        rows = [UsageSample(**dict(r)) for r in self.connection.execute(query, values)]
        return list(reversed(rows))

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def export_samples(samples, output, format):
    rows = [dict(asdict(s), remaining_percent=s.remaining_percent) for s in samples]
    if format == 'json':
        json.dump(rows, output, indent=2, allow_nan=False)
        output.write('\n')
    elif format == 'csv':
        writer = csv.DictWriter(output, fieldnames=COLUMNS + ['remaining_percent'])
        writer.writeheader()
        writer.writerows(rows)
    else:
        raise ValueError('Export format must be json or csv')
