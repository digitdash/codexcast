"""OS collection ownership and verified desktop process identities (macOS/Linux)."""
import fcntl
import os
from pathlib import Path
import subprocess


class CollectorBusy(OSError):
    pass


class CollectionLock:
    # Lock by database even across different Codex homes: mixing accounts in one
    # history must never create two simultaneous writers.
    def __init__(self, db):
        self.path = Path(str(Path(db).expanduser().resolve())+'.collector.lock')
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd);self.fd = None
            raise CollectorBusy('This database already has a collector; stop or attach to it') from None
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd);self.fd = None


def process_identity(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        raw = subprocess.check_output(['ps', '-p', str(pid), '-o', 'stat=,lstart=,comm='],
                                      text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
        parts = raw.split(None, 6)
        if len(parts) != 7 or parts[0].startswith('Z'):
            return None
        return {'pid': pid, 'started': ' '.join(parts[1:6]), 'command': parts[6]}
    except (OSError, subprocess.SubprocessError):
        return None


def identity_alive(identity):
    return isinstance(identity, dict) and process_identity(identity.get('pid')) == identity


def desktop_owner():
    """Only attach the native desktop ancestor; a provider subprocess is not one."""
    pid = os.getpid()
    for _ in range(16):
        identity = process_identity(pid)
        if identity is None:
            return None
        if identity['command'].endswith(('/ChatGPT.app/Contents/MacOS/ChatGPT',
                                         '/Codex.app/Contents/MacOS/Codex')):
            return identity
        try:
            pid = int(subprocess.check_output(['ps', '-p', str(pid), '-o', 'ppid='],
                                             text=True, stderr=subprocess.DEVNULL, timeout=2).strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if pid <= 1:
            return None
    return None
