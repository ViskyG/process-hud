from __future__ import annotations
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import stat
import time
import psutil

ID_RE = re.compile(r"^[a-f0-9]{32}$")
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def clean(value: object, limit: int = 2000) -> str:
    text = ANSI.sub("", str(value))
    return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) <= 159))[:limit]


def boot_id() -> str:
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return str(psutil.boot_time())


def identity(pid: int) -> dict:
    p = psutil.Process(pid)
    return {'pid': p.pid, 'created': p.create_time(), 'boot': boot_id()}


def alive(ref: dict | None) -> bool | None:
    """None means permission/observation failure, never proven death."""
    if not ref or ref.get('boot') != boot_id():
        return False
    try:
        p = psutil.Process(int(ref['pid']))
        return (p.create_time() == ref['created'] and p.is_running()
                and p.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD))
    except (psutil.NoSuchProcess, KeyError, ValueError):
        return False
    except psutil.AccessDenied:
        return None


def private_dir(path: Path) -> Path:
    path = path.expanduser().absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError(f'Unsafe state directory: {path}')
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def lock_file(path: Path, blocking: bool = True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield fd
    finally:
        os.close(fd)


def tool(argv: list[str], *, timeout: float = 12, input_text: str | None = None,
         max_bytes: int = 1024 * 1024) -> tuple[int, str, str]:
    """Bound helper output and runtime; kill only the helper, not its workload."""
    payload = input_text.encode() if input_text is not None else b''
    if len(payload) > 64000:
        raise ValueError('Helper input exceeds 64 KB')
    with subprocess.Popen(argv, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as p:
        # Multiplex bounded RPC input with both output streams to avoid pipe deadlocks.
        if input_text is not None:
            os.set_blocking(p.stdin.fileno(), False)
        sel = selectors.DefaultSelector()
        output = {'out': bytearray(), 'err': bytearray()}
        for label, stream in [('out', p.stdout), ('err', p.stderr)]:
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_READ, label)
        if p.stdin is not None:
            sel.register(p.stdin, selectors.EVENT_WRITE, 'in')
        deadline = time.monotonic() + timeout
        sent = 0
        try:
            while sel.get_map():
                if time.monotonic() > deadline:
                    raise TimeoutError(f'Helper timed out: {argv[0]}')
                for key, _ in sel.select(min(.2, max(.01, deadline-time.monotonic()))):
                    if key.data == 'in':
                        try:
                            sent += os.write(key.fd, payload[sent:sent+4096])
                        except BrokenPipeError:
                            sent = len(payload)
                        if sent == len(payload):
                            sel.unregister(key.fileobj); key.fileobj.close()
                        continue
                    data = os.read(key.fd, 65536)
                    if not data:
                        sel.unregister(key.fileobj)
                    else:
                        output[key.data].extend(data)
                        if sum(map(len, output.values())) > max_bytes:
                            raise ValueError('Helper response exceeds size limit')
            try:
                p.wait(timeout=max(.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f'Helper timed out: {argv[0]}') from exc
            return p.returncode, output['out'].decode('utf8', 'replace'), output['err'].decode('utf8', 'replace')
        finally:
            sel.close()
            if p.poll() is None:
                p.kill(); p.wait()


def json_tool(argv: list[str], **kwargs):
    code, out, err = tool(argv, **kwargs)
    if code:
        raise RuntimeError(clean(err or out or f'exit {code}', 1200))
    return json.loads(out)


class RotatingLog:
    """Bounded bytes, preserving the latest output even without newline characters."""
    def __init__(self, path: Path, limit: int = 2 * 1024 * 1024, backups: int = 2):
        self.path, self.limit, self.backups = path, max(limit, 1024), max(backups, 1)
        self.file = None
        self._open()

    def _open(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, 'ab', buffering=0)
        self.size = self.path.stat().st_size

    def write(self, data: bytes):
        while data:
            if self.size >= self.limit:
                self.file.close()
                for n in range(self.backups, 0, -1):
                    old = self.path if n == 1 else Path(str(self.path)+f'.{n-1}')
                    new = Path(str(self.path)+f'.{n}')
                    if old.exists():
                        os.replace(old, new)
                self._open()
            chunk, data = data[:self.limit-self.size], data[self.limit-self.size:]
            self.file.write(chunk); self.size += len(chunk)

    def close(self):
        self.file.close()


def tail(path: Path, lines: int = 60, limit: int = 32768) -> str:
    if not 0 <= lines <= 1000:
        raise ValueError('tail lines must be 0..1000')
    if lines == 0:
        return ''
    chunks = []
    remaining = limit
    for candidate in [path, Path(str(path)+'.1'), Path(str(path)+'.2')]:
        try:
            fd = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise ValueError('Log source must be a regular file')
                f.seek(0, 2); f.seek(max(0, f.tell()-remaining))
                data = f.read(remaining)
            chunks.insert(0, data); remaining -= len(data)
            if remaining <= 0:
                break
        except FileNotFoundError:
            pass
    text = clean(b''.join(chunks).decode('utf8', 'replace'), limit)
    return '\n'.join(text.splitlines()[-lines:])
