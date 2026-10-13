"""One local timer-driven collector; MCP instances communicate over a private socket."""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid
from .collection import collect, statistics
from .estimator import EstimatorConfig
from .lifecycle import CollectionLock, CollectorBusy, identity_alive, desktop_owner
from .plugin_state import Baselines, validate_session
from .provider import CodexProvider, ProviderError
from .storage import DEFAULT_DB, Store
from .tokens import TokenReader


@dataclass
class CollectorConfig:
    db: Path = DEFAULT_DB
    codex_bin: str = 'codex'
    codex_home: Path = Path.home()/'.codex'
    interval: float = 30
    runtime_root: Path | None = None

    def __post_init__(self):
        self.db = Path(self.db).expanduser().resolve()
        self.codex_home = Path(self.codex_home).expanduser().resolve()
        if isinstance(self.interval, bool) or not isinstance(self.interval, (float, int)) or not math.isfinite(self.interval) or self.interval <= 0:
            raise ValueError('Invalid collection interval')
        key = hashlib.sha256((str(self.db)+'\0'+str(self.codex_home)).encode()).hexdigest()[:12]
        base = Path(self.runtime_root) if self.runtime_root else DEFAULT_DB.parent/'runtime'
        self.root = base/key
        self.socket = self.root/'c.sock'
        if len(os.fsencode(self.socket)) >= 104:
            raise ValueError('Collector socket path is too long; use a shorter runtime_root')

    def wire(self):
        return {'db': str(self.db), 'codex_bin': self.codex_bin, 'codex_home': str(self.codex_home),
                'interval': self.interval, 'runtime_root': str(self.root.parent)}


def log_event(config, event, **fields):
    config.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.root.chmod(0o700)
    fd = os.open(config.root/'events.jsonl', os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps({'event': event, 'timestamp': time.time(), 'pid': os.getpid(), **fields},
                                allow_nan=False)+'\n').encode())
    finally:
        os.close(fd)


def collector_health(latest, error, now, interval):
    age = max(0, now-latest) if latest is not None else None
    state = 'error' if error else 'starting' if latest is None else 'stale' if age > max(120, 3*interval) else 'running'
    return state, age


def _read_message(connection):
    data = b''
    while b'\n' not in data:
        part = connection.recv(4096)
        if not part:
            raise ConnectionError('Collector connection closed')
        data += part
        if len(data) > 65536:
            raise ValueError('Collector message is too large')
    result = json.loads(data.split(b'\n', 1)[0])
    if not isinstance(result, dict):
        raise ValueError('Invalid collector message')
    return result


class CollectorClient:
    def __init__(self, config, *, owner=None):
        self.config = config
        self.owner = owner
        self.child = None

    def request(self, command, **fields):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(45)
            connection.connect(str(self.config.socket))
            connection.sendall((json.dumps({'command': command, **fields})+'\n').encode())
            response = _read_message(connection)
        if 'request_error' in response:
            raise ValueError(response['request_error'])
        return response

    def start(self, *, cause='explicit'):
        if not identity_alive(self.owner):
            raise ValueError('Start requires a live Codex desktop client')
        self.config.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Serialize launch attempts independently of the lock held by the daemon.
        import fcntl
        fd = os.open(self.config.root/'startup.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                return self.request('attach', owner=self.owner)
            except (FileNotFoundError, ConnectionError):
                pass
            with CollectionLock(self.config.db):
                pass
            # A bundled launcher modifies sys.path, which subprocesses do not
            # inherit. Pin the child to this same packaged source, independent
            # of its working directory or editable development installations.
            env = dict(os.environ, CODEX_HOME=str(self.config.codex_home),
                       PYTHONPATH=str(Path(__file__).resolve().parent.parent))
            if self.child is not None:
                self.child.poll()
            self.child = subprocess.Popen([sys.executable, '-m', 'codex_usage.collector',
                                           json.dumps(self.config.wire()), json.dumps(self.owner), cause],
                                          env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic()+45
            while time.monotonic()<deadline:
                try:
                    return self.request('attach', owner=self.owner)
                except (FileNotFoundError, ConnectionError):
                    if self.child.poll() is not None:
                        raise CollectorBusy('Collector could not start; inspect local lifecycle evidence')
                    time.sleep(.05)
            raise TimeoutError('Collector startup timed out')
        finally:
            os.close(fd)

    def stats(self, session_id):
        validate_session(session_id)
        try:
            return self.request('stats', session_id=session_id)
        except (FileNotFoundError, ConnectionError):
            response = {'state': 'stopped', 'collector_id': None, 'successful_polls': 0,
                    'latest_sample_timestamp': None, 'sample_age_seconds': None,
                    'message': 'Mention Codex Usage and request start'}
            if self.config.db.exists():
                with Store(self.config.db, read_only=True) as store:
                    latest = store.connection.execute('SELECT MAX(timestamp) FROM samples').fetchone()[0]
                    baseline_path = self.config.root/'baselines.sqlite3'
                    cutoff = None
                    if baseline_path.exists():
                        with Baselines(baseline_path, read_only=True) as baselines:
                            cutoff = baselines.since(session_id)
                    response.update(latest_sample_timestamp=latest,
                                    sample_age_seconds=max(0, time.time()-latest) if latest is not None else None,
                                    sample_count=store.connection.execute('SELECT COUNT(*) FROM samples').fetchone()[0],
                                    calculation_since=cutoff, scope='saved account telemetry; collector stopped')
                    if latest is not None:
                        current = [s for s in store.load(since=latest) if s.timestamp == latest]
                        config = EstimatorConfig(max_gap_seconds=max(120, 3*self.config.interval))
                        response.update(statistics(store, current, config, since=cutoff))
            return response

    def reset(self, session_id):
        if session_id is not None:
            validate_session(session_id)
        return self.request('reset', session_id=session_id)

    def stop(self):
        try:
            response = self.request('stop')
        except (FileNotFoundError, ConnectionError):
            response = {'state': 'stopped'}
        if self.child is not None:
            self.child.wait(timeout=25)
        return response


def run(config, owner, cause):
    os.umask(0o077)
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    provider = None
    collector_id = str(uuid.uuid4())
    owners = [owner]
    polls = failures = 0
    last_error = None
    latest = None
    with CollectionLock(config.db), Store(config.db) as store, Baselines(config.root/'baselines.sqlite3') as baselines:
        latest = store.connection.execute('SELECT MAX(timestamp) FROM samples').fetchone()[0]
        config.socket.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(config.socket));config.socket.chmod(0o600)
            server.listen(16);server.settimeout(.1)
            reader = TokenReader(config.codex_home/'sessions')
            estimator = EstimatorConfig(max_gap_seconds=max(120, 3*config.interval))
            next_poll = next_owner_check = 0
            log_event(config, 'collector_start', collector_id=collector_id, cause=cause, owner_pid=owner['pid'])

            def status():
                state, age = collector_health(latest, last_error, time.time(), config.interval)
                process = getattr(provider, '_process', None)
                return {'state': state, 'collector_id': collector_id, 'collector_pid': os.getpid(),
                        'provider_pid': process.pid if process else None, 'startup_cause': cause,
                        'successful_polls': polls, 'latest_sample_timestamp': latest,
                        'sample_age_seconds': age, 'error': last_error, 'interval_seconds': config.interval,
                        'sample_count': store.connection.execute('SELECT COUNT(*) FROM samples').fetchone()[0]}

            try:
                while running:
                    now = time.monotonic()
                    if now >= next_owner_check:
                        owners[:] = [p for p in owners if identity_alive(p)]
                        next_owner_check = now+1
                        if not owners:
                            log_event(config, 'desktop_exit', collector_id=collector_id)
                            break
                    if now >= next_poll:
                        try:
                            if provider is None:
                                provider = CodexProvider(config.codex_bin, timeout=20)
                            samples = collect(provider, store, reader)
                            latest = max(s.timestamp for s in samples)
                            polls += 1;failures = 0;last_error = None
                            log_event(config, 'poll_success', collector_id=collector_id, poll=polls,
                                      sample_timestamp=latest, windows=len(samples))
                            delay = config.interval
                        except (ProviderError, OSError) as error:
                            failures += 1
                            if provider is not None:
                                provider.close();provider = None
                            # Provider messages are already sanitized. Never log raw RPC or stderr.
                            last_error = str(error)[:120] if isinstance(error, ProviderError) else 'Local collection failed'
                            log_event(config, 'poll_error', collector_id=collector_id, error=last_error)
                            delay = min(300, config.interval*2**min(failures-1, 8))
                        next_poll = time.monotonic()+delay
                    try:
                        connection, _ = server.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(2)
                        try:
                            request = _read_message(connection)
                            command = request.get('command')
                            if command == 'attach':
                                attached = request.get('owner')
                                if not identity_alive(attached):
                                    raise ValueError('Desktop client has exited')
                                if attached not in owners: owners.append(attached)
                                response = status()
                            elif command == 'stats':
                                session = validate_session(request.get('session_id'))
                                cutoff = baselines.since(session)
                                response = status()
                                response.update(calculation_since=cutoff, scope='account telemetry; session calculation baseline')
                                if latest is not None:
                                    current = [s for s in store.load(since=latest) if s.timestamp == latest]
                                    response.update(statistics(store, current, estimator, since=cutoff))
                                log_event(config, 'stats', collector_id=collector_id)
                            elif command == 'reset':
                                session = request.get('session_id')
                                stamp = time.time();baselines.reset(session, stamp)
                                response = {'reset_scope': 'all' if session is None else 'current',
                                            'calculation_since': stamp, 'history_preserved': True}
                                log_event(config, 'reset_all' if session is None else 'reset_current', collector_id=collector_id)
                            elif command == 'stop':
                                running = False;response = {'state': 'stopped', 'collector_id': collector_id}
                            else:
                                raise ValueError('Unknown collector command')
                        except (ValueError, TypeError) as error:
                            response = {'request_error': str(error)[:120]}
                        except OSError:
                            continue
                        try:
                            connection.sendall((json.dumps(response, allow_nan=False)+'\n').encode())
                        except OSError:
                            # One abandoned local request must not stop sampling.
                            pass
            finally:
                if provider is not None: provider.close()
                config.socket.unlink(missing_ok=True)
                log_event(config, 'collector_stop', collector_id=collector_id)


if __name__ == '__main__':
    run(CollectorConfig(**json.loads(sys.argv[1])), json.loads(sys.argv[2]), sys.argv[3])
