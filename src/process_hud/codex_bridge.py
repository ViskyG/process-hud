"""Local JSONL relay for the existing Codex App Server. No inference or polling API.

The relay preserves IDE traffic, exposes a private Unix socket, and submits only
completion/attention events of jobs opted in with --resume-codex. Audit files
contain lifecycle/token counters, never prompts, commands, credentials or logs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import selectors
import signal
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import uuid

from .model import TERMINAL
from . import events
from .util import alive, identity, lock_file, private_dir

THREAD_RE = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')


def bridge_home() -> Path:
    return Path(os.environ.get('PROCESS_HUD_CODEX_BRIDGE') or
                Path.home()/'.local/state/process-hud/codex-bridge')


def atomic_json(path, value):
    tmp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f)
    os.replace(tmp, path)


def call(path, request, timeout=20):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall(json.dumps(request).encode()+b'\n')
        with s.makefile('rb') as f:
            data = f.readline(65537)
    if not data or len(data) > 65536:
        raise ValueError('Invalid bridge response')
    return json.loads(data)


def endpoints(home=None):
    result = []
    for p in (Path(home) if home else bridge_home()).glob('*.json'):
        try:
            d = json.loads(p.read_text())
            if alive(d.get('process')) is True:
                result.append(d)
        except (OSError, ValueError, KeyError):
            pass
    return result


def continuation_owner(thread, codex_home=None):
    """Resolve real thread-spawn lineage from Codex's own session metadata.

    A worker's initial final message can leave it retired. Its user-facing root
    remains the owner of the unfinished operation. Never infer this from a path,
    nickname, PID, arbitrary workload output, or an unverified supplied parent ID.
    """
    home = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    lineage = [thread]
    for _ in range(16):
        matches = list((home/'sessions').glob('**/*'+thread+'.jsonl'))
        if not matches and len(lineage) == 1:
            return thread, lineage
        if len(matches) != 1:
            raise ValueError('Cannot verify unique Codex continuation ownership')
        with matches[0].open() as f:
            record = json.loads(f.readline(65537))
        metadata = record.get('payload') or {}
        if record.get('type') != 'session_meta' or metadata.get('id') != thread:
            raise ValueError('Invalid Codex session metadata for continuation')
        source = metadata.get('source')
        if not isinstance(source, dict) or 'subagent' not in source:
            return thread, lineage
        subagent = source['subagent']
        parent = ((subagent.get('thread_spawn') or {}).get('parent_thread_id')
                  if isinstance(subagent, dict) else None)
        if not isinstance(parent, str) or not THREAD_RE.fullmatch(parent) or parent in lineage:
            raise ValueError('Cannot verify subagent continuation owner')
        lineage.append(parent)
        thread = parent
    raise ValueError('Codex continuation lineage exceeds supported depth')


def current_origin():
    thread = os.environ.get('CODEX_THREAD_ID')
    if not thread or not THREAD_RE.fullmatch(thread):
        raise ValueError('--resume-codex requires the current CODEX_THREAD_ID')
    owner, lineage = continuation_owner(thread)
    candidates = [e for e in endpoints() if thread in e.get('threads', [])
                  and owner in e.get('threads', [])]
    preferred = os.environ.get('PROCESS_HUD_CODEX_INSTANCE')
    selected = [e for e in candidates if e['instance'] == preferred] if preferred else []
    if len(selected or candidates) != 1:
        raise ValueError('No unique live Codex bridge for this conversation; no work was launched')
    endpoint = (selected or candidates)[0]
    if not call(endpoint['socket'], {'op': 'status'}).get('initialized'):
        raise ValueError('Codex bridge has not initialized; no work was launched')
    origin = {'thread_id': owner, 'bridge_home': str(bridge_home()),
              'instance': endpoint['instance']}
    if owner != thread:
        origin.update(source_thread_id=thread, parent_thread_id=lineage[1], lineage=lineage)
    return origin


def enqueue(store, job):
    origin = job.get('codex_origin')
    if not origin or job.get('removed_at'):
        return
    try:
        with bridge_db(private_dir(Path(origin['bridge_home']))) as c:
            c.execute('CREATE TABLE IF NOT EXISTS stores (home TEXT PRIMARY KEY)')
            c.execute('INSERT OR IGNORE INTO stores VALUES(?)', (str(store.home),))
        with lock_file(store.task_dir(job['id'])/'codex-dispatch-start.lock'):
            current = store.get(job['id'])
            if alive(current.get('codex_guard')) is not False:
                return
            current = events.sync(store, job['id'])
            if current['finished'] and not events.pending(current):
                return
            from .runtime import environment
            with open(store.task_dir(job['id'])/'codex-dispatch.log', 'ab', buffering=0) as log:
                os.chmod(log.name, 0o600)
                p = subprocess.Popen([sys.executable, '-m', __name__, '--dispatch', str(store.home), job['id']],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=environment(store),
                                     start_new_session=True, close_fds=True)
            store.mutate(job['id'], lambda j: j.update(codex_guard=identity(p.pid)))
    except OSError as exc:
        store.mutate(job['id'], lambda j: j.update(codex_delivery={'state': 'pending', 'error': str(exc)}))


def dispatch(home, job_id):
    from .store import Store
    store = Store(home)
    with lock_file(store.task_dir(job_id)/'codex-dispatch.lock', blocking=False):
        # This independent local guard also survives loss of the workload observer.
        # No model/API polling, no cancellation and no workload replay.
        while True:
            j = events.sync(store, job_id)
            origin = j.get('codex_origin')
            if not origin or j.get('removed_at'):
                return
            waiting = events.pending(j)
            if j['finished'] and not waiting:
                return
            candidates = [e for e in endpoints(origin['bridge_home'])
                          if origin['thread_id'] in e.get('threads', [])]
            preferred = [e for e in candidates if e['instance'] == origin['instance']]
            candidates = preferred or candidates
            for event in waiting:
                result = {'state': 'pending'}
                if time.time()-event['created_at'] >= 86400:
                    result = {'state': 'expired'}
                elif len(candidates) == 1:
                    try:
                        result = call(candidates[0]['socket'], {'op': 'deliver', 'home': str(store.home),
                            'job_id': job_id, 'event_key': event['key']})
                    except (OSError, ValueError, TimeoutError) as exc:
                        result = {'state': 'pending', 'error': str(exc)}
                if result['state'] == 'error':
                    result['state'] = 'pending'
                events.delivered(store, job_id, event['key'], result)
            time.sleep(1)


def bridge_db(home):
    p = home/'deliveries.sqlite3'
    fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    return sqlite3.connect(p, timeout=10)


class Relay:
    def __init__(self, home, command):
        self.home = private_dir(Path(home))
        self.instance = str(os.getpid())+'-'+uuid.uuid4().hex[:8]
        self.socket_path = self.home/(self.instance+'.sock')
        self.metadata_path = self.home/(self.instance+'.json')
        self.audit_path = self.home/(self.instance+'.jsonl')
        self.command = command
        self.initialized = False
        self.threads = set()
        self.active_turns = {}
        self.pending_ui = {}
        self.responses = {}
        self.write_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.delivery_lock = threading.Lock()
        self.stopping = threading.Event()
        self.proc = None
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS stores (home TEXT PRIMARY KEY)')

    def db(self):
        return bridge_db(self.home)

    def maintain(self):
        """Recover native guards after a crash/reload; never launch a workload."""
        from .store import Store
        while not self.stopping.is_set():
            try:
                with self.db() as c:
                    homes = [row[0] for row in c.execute('SELECT home FROM stores')]
                for home in homes:
                    if not (Path(home)/'state.sqlite3').is_file():
                        continue
                    store = Store(home)
                    for job in store.list():
                        origin = job.get('codex_origin') or {}
                        if origin.get('bridge_home') == str(self.home):
                            enqueue(store, job)
            except (OSError, ValueError, sqlite3.Error):
                # Monitoring stays visible in HUD; the next bounded pass retries.
                pass
            self.stopping.wait(5)

    def audit(self, event, **fields):
        with self.state_lock:
            fd = os.open(self.audit_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'a') as f:
                f.write(json.dumps(dict(time=time.time(), event=event, **fields))+'\n')

    def metadata(self):
        with self.state_lock:
            return {'instance': self.instance, 'protocol_version': 2, 'supports_attention': True,
                    'process': identity(os.getpid()),
                    'server_pid': self.proc.pid if self.proc else None,
                    'socket': str(self.socket_path), 'threads': sorted(self.threads),
                    'active_turns': dict(self.active_turns),
                    'initialized': self.initialized, 'audit': str(self.audit_path)}

    def remember(self, thread):
        if isinstance(thread, str) and THREAD_RE.fullmatch(thread):
            with self.state_lock:
                if thread not in self.threads:
                    self.threads.add(thread)
                    atomic_json(self.metadata_path, self.metadata())

    def send(self, line):
        with self.write_lock:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()

    def send_rpc(self, method, params):
        request_id = 'hud:'+uuid.uuid4().hex
        q = queue.Queue(maxsize=1)
        with self.state_lock:
            self.responses[request_id] = q
        try:
            self.send(json.dumps({'id': request_id, 'method': method, 'params': params}).encode()+b'\n')
            return q.get(timeout=15)
        finally:
            with self.state_lock:
                self.responses.pop(request_id, None)

    def from_ide(self, message):
        method, params = message.get('method'), message.get('params') or {}
        if 'id' in message and method:
            with self.state_lock:
                self.pending_ui[str(message['id'])] = (method, params.get('threadId'))
        if method in ('turn/start', 'turn/steer'):
            self.audit('ide.'+method, thread=params.get('threadId'))

    def from_server(self, message):
        request_id = message.get('id')
        with self.state_lock:
            q = self.responses.get(request_id)
            pending = self.pending_ui.pop(str(request_id), None) if request_id is not None else None
        if q is not None and ('result' in message or 'error' in message):
            q.put(message)
            return False
        if isinstance(request_id, str) and request_id.startswith('hud:'):
            return False  # A late internal response must not enter the IDE's request namespace.
        if pending and 'result' in message:
            result = message['result'] or {}
            thread = result.get('thread') or {}
            if isinstance(thread, dict):
                self.remember(thread.get('id'))
            if pending[0] == 'initialize':
                self.initialized = True
                atomic_json(self.metadata_path, self.metadata())
            if pending[0] in ('thread/resume', 'turn/start'):
                self.remember(pending[1])
        method, p = message.get('method'), message.get('params') or {}
        if method == 'thread/started':
            self.remember((p.get('thread') or {}).get('id'))
        if method in ('thread/closed', 'thread/archived', 'thread/deleted'):
            with self.state_lock:
                self.threads.discard(p.get('threadId'))
                self.active_turns.pop(p.get('threadId'), None)
                atomic_json(self.metadata_path, self.metadata())
        if method in ('turn/started', 'turn/completed'):
            self.remember(p.get('threadId'))
            turn = p.get('turn') or {}
            with self.state_lock:
                if method == 'turn/started':
                    self.active_turns[p.get('threadId')] = turn.get('id')
                elif self.active_turns.get(p.get('threadId')) == turn.get('id'):
                    self.active_turns.pop(p.get('threadId'), None)
                atomic_json(self.metadata_path, self.metadata())
            self.audit(method, thread=p.get('threadId'), turn=turn.get('id'), status=turn.get('status'))
        elif method == 'thread/tokenUsage/updated':
            usage = p.get('tokenUsage') or {}
            self.audit(method, thread=p.get('threadId'), total=usage.get('total'), last=usage.get('last'))
        return True

    def deliver(self, home, job_id, event_key='completion'):
        from .store import Store
        store = Store(home)
        j = events.sync(store, job_id)
        origin = j.get('codex_origin') or {}
        thread = origin.get('thread_id')
        if not thread or str(Path(origin.get('bridge_home', '')).resolve()) != str(self.home.resolve()):
            raise ValueError('Job was not authorized for this bridge')
        if j.get('removed_at'):
            return {'state': 'dismissed'}
        selected = j.get('codex_events', {}).get(event_key)
        if selected is None:
            raise ValueError('No eligible completion/attention event')
        if selected['state'] in {'consumed', 'dismissed', 'superseded', 'expired'}:
            return {'state': selected['state'], 'event': selected['event']}
        if not self.initialized or thread not in self.threads:
            return {'state': 'pending', 'reason': 'Source conversation is not loaded'}
        event = selected['event']
        with self.delivery_lock:
            # The user may dismiss/acknowledge a row while another event is sent.
            current = events.sync(store, job_id)
            selected = current.get('codex_events', {}).get(event_key)
            if current['removed_at']:
                return {'state': 'dismissed', 'event': event}
            if selected is None or selected['state'] in {'consumed', 'dismissed', 'superseded', 'expired'}:
                return {'state': selected['state'] if selected else 'superseded', 'event': event}
            j = current
            with self.db() as c:
                c.execute('BEGIN IMMEDIATE')
                previous = c.execute('SELECT state,result FROM events WHERE id=?', (event,)).fetchone()
                if previous:
                    return json.loads(previous[1]) if previous[0] != 'sending' else {'state': 'uncertain', 'event': event}
                c.execute('INSERT INTO events VALUES(?,?,?)', (event, 'sending', '{}'))
            # Only facts are supplied as tool output, with no workload-generated text.
            output = json.dumps({'process_hud_event': event, 'task_id': job_id,
                                 'state': j['state'], 'exit_code': j.get('exit_code'),
                                 'state_home': str(store.home),
                                 'event_kind': selected['kind'],
                                 'finished': j['finished'],
                                 'result_available': j['finished'] and j['state'] in TERMINAL})
            self.audit('hud.turn/start', thread=thread, job=job_id, event_id=event)
            try:
                response = self.send_rpc('turn/start', {'threadId': thread, 'input': [],
                    'toolOutput': {'name': 'process_hud_completion' if selected['kind'] == 'completion'
                                   else 'process_hud_attention', 'namespace': None, 'output': output}})
                if 'error' in response:
                    # A rejected request did not start a turn; retry is safe.
                    with self.db() as c:
                        c.execute('DELETE FROM events WHERE id=?', (event,))
                    return {'state': 'pending', 'reason': 'App Server rejected delivery',
                            'error_code': response['error'].get('code')}
                turn = (response.get('result') or {}).get('turn') or {}
                result = {'state': 'accepted', 'event': event, 'turn_id': turn.get('id')}
            except (OSError, queue.Empty, TimeoutError):
                # Do not blindly resubmit a request whose acknowledgement was lost.
                result = {'state': 'uncertain', 'event': event}
            with self.db() as c:
                c.execute('UPDATE events SET state=?,result=? WHERE id=?',
                          (result['state'], json.dumps(result), event))
            self.audit('delivery.'+result['state'], thread=thread, job=job_id, event_id=event)
            return result

    def run(self):
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(25)
                uid = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                if uid != os.getuid():
                    return
                try:
                    line = self.rfile.readline(65537)
                    if len(line) > 65536:
                        raise ValueError('Request too large')
                    request = json.loads(line)
                    if request.get('op') == 'status':
                        result = relay.metadata()
                    elif request.get('op') == 'deliver':
                        result = relay.deliver(request['home'], request['job_id'],
                                               request.get('event_key', 'completion'))
                    else:
                        raise ValueError('Unsupported bridge operation')
                except (OSError, ValueError, KeyError) as exc:
                    result = {'state': 'error', 'error': str(exc)}
                self.wfile.write(json.dumps(result).encode()+b'\n')

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        env = dict(os.environ, PROCESS_HUD_CODEX_BRIDGE=str(self.home),
                   PROCESS_HUD_CODEX_INSTANCE=self.instance)
        server = Server(str(self.socket_path), Handler)
        os.chmod(self.socket_path, 0o600)
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=None, env=env)
        atomic_json(self.metadata_path, self.metadata())
        self.audit('bridge.started', server_pid=self.proc.pid)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        maintenance_thread = threading.Thread(target=self.maintain, daemon=True)
        maintenance_thread.start()

        def forward_input():
            selector = selectors.DefaultSelector()
            selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
            buffered = b''
            try:
                while not self.stopping.is_set():
                    if not selector.select(.2):
                        continue
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        if buffered:
                            self.send(buffered)
                        break
                    buffered += chunk
                    while b'\n' in buffered:
                        line, buffered = buffered.split(b'\n', 1)
                        try:
                            self.from_ide(json.loads(line))
                        except (ValueError, TypeError, AttributeError):
                            pass
                        self.send(line+b'\n')
            except (BrokenPipeError, OSError):
                pass
            finally:
                selector.close()
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass

        def terminate(signum, _frame):
            self.proc.send_signal(signum)

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        input_thread = threading.Thread(target=forward_input, daemon=True)
        input_thread.start()
        try:
            for line in self.proc.stdout:
                forward = True
                try:
                    forward = self.from_server(json.loads(line))
                except (ValueError, TypeError, AttributeError):
                    pass
                if forward:
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
            return self.proc.wait()
        finally:
            self.stopping.set()
            input_thread.join(timeout=1)
            maintenance_thread.join(timeout=1)
            server.shutdown()
            server.server_close()
            self.metadata_path.unlink(missing_ok=True)
            self.socket_path.unlink(missing_ok=True)
            self.audit('bridge.stopped')


def main():
    args = sys.argv[1:]
    if args[:1] == ['--dispatch']:
        try:
            dispatch(args[1], args[2])
        except BlockingIOError:
            pass
        return 0
    if args == ['--hud-status']:
        print(json.dumps(endpoints(), indent=2))
        return 0
    config_path = Path(os.environ.get('PROCESS_HUD_CODEX_CONFIG') or
                       Path.home()/'.local/share/process-hud/codex-bridge.json')
    config = json.loads(config_path.read_text())
    real_path = Path(config['real_binary'])
    extension_root = config.get('extension_root')
    if extension_root:
        root = Path(extension_root)
        for entry in json.loads((root/'extensions.json').read_text()):
            if entry.get('identifier', {}).get('id') == 'openai.chatgpt':
                candidate = root/entry['relativeLocation']/'bin/linux-x86_64/codex'
                if candidate.is_file():
                    real_path = candidate
    real = str(real_path.resolve())
    if real == str(Path(sys.argv[0]).resolve()):
        raise ValueError('Bridge cannot launch itself')
    # Only relay actual server startup. Other CLI commands preserve exec semantics.
    is_server = ('app-server' in args and not any(x in args for x in
                 ['--help', '-h', '--version', 'generate-json-schema', 'generate-ts', 'daemon', 'proxy']))
    if not is_server:
        os.execv(real, [real, *args])
    return Relay(bridge_home(), [real, *args]).run()


if __name__ == '__main__':
    raise SystemExit(main())
