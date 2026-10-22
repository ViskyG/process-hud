from __future__ import annotations
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import psutil
from . import adapters
from .model import TERMINAL
from .store import Store
from .util import RotatingLog, alive, clean, identity, lock_file, tail

CALLER_PYTHONPATH = '_PROCESS_HUD_CALLER_PYTHONPATH'

PERCENT = re.compile(r'(?<![\d.\-])(\d{1,3}(?:\.\d+)?)\s*%')


def environment(store: Store) -> dict:
    """Bootstrap a HUD worker without changing the eventual workload's import path."""
    env = os.environ.copy()
    env['PROCESS_HUD_HOME'] = str(store.home)
    if CALLER_PYTHONPATH not in env:
        env[CALLER_PYTHONPATH] = json.dumps(env.get('PYTHONPATH'))
    source = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = source + (os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    return env


def workload_environment(store: Store, job_id: str) -> dict:
    """Do not inject HUD's site-packages into a target project's Python environment."""
    env = os.environ.copy()
    original = env.pop(CALLER_PYTHONPATH, None)
    if original is not None:
        value = json.loads(original)
        if value is None:
            env.pop('PYTHONPATH', None)
        elif isinstance(value, str):
            env['PYTHONPATH'] = value
        else:
            raise ValueError('Invalid saved caller PYTHONPATH')
    env['PROCESS_HUD_HOME'] = str(store.home)
    env['PROCESS_HUD_TASK_ID'] = job_id
    env['PYTHONUNBUFFERED'] = '1'
    return env


def spawn_worker(store: Store, job_id: str, *, recover: bool = False) -> dict:
    with lock_file(store.home/'launch.lock'):
        job = store.get(job_id)
        if (job['finished'] or
                (alive(job.get('worker')) is not False and not job.get('worker_stopping'))):
            return job
        if job['backend'] == 'external':
            return job
        # A restored row can hand off from an observer already committed to
        # detaching. The replacement waits for its predecessor's lifetime lock.
        store.update(job_id, worker_ready=False, worker_stopping=False)
        args = [sys.executable, '-m', 'process_hud', '--home', str(store.home), '_worker', job_id]
        if recover:
            args += ['--recover']
        worker_log = store.task_dir(job_id)/'worker.log'
        if worker_log.exists() and worker_log.stat().st_size > 65536:
            os.replace(worker_log, worker_log.with_suffix('.log.1'))
        with open(os.devnull, 'rb') as null, open(worker_log, 'ab', buffering=0) as err:
            os.chmod(err.name, 0o600)
            p = subprocess.Popen(args, stdin=null, stdout=err, stderr=err,
                                 env=environment(store), start_new_session=True, close_fds=True)
        try:
            ref = identity(p.pid)
        except psutil.NoSuchProcess:
            ref = None
        store.update(job_id, worker=ref)
        # A bounded launch acknowledgement, not background/model polling.
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            job = store.get(job_id)
            if job.get('worker_ready') or job['finished']:
                break
            if p.poll() is not None:
                break
            time.sleep(.03)
        if not job.get('worker_ready') and not job['finished']:
            store.update(job_id, state='unknown', observation='lost',
                         detail='Worker launch unconfirmed; inspect worker.log. Do not blindly relaunch workload.')
        return store.get(job_id)


def launch(store: Store, title: str, argv: list[str], *, cwd=None, key=None, task_id=None,
           parser='none', ui=True, codex_origin=None) -> dict:
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(a, str) or '\0' in a for a in argv):
        raise ValueError('Provide a valid command after --')
    if len(json.dumps(argv).encode()) > 48000:
        raise ValueError('Command arguments too large')
    workdir = str(Path(cwd or os.getcwd()).expanduser().resolve())
    if not Path(workdir).is_dir():
        raise ValueError('Working directory does not exist')
    # Keep the executable's symlink path: Python venvs use it to locate pyvenv.cfg.
    search_path = os.pathsep.join(os.path.abspath(os.path.join(workdir, entry))
                                  for entry in os.get_exec_path())
    executable = (shutil.which(argv[0], path=search_path) if '/' not in argv[0]
                  else os.path.abspath(os.path.join(workdir, os.path.expanduser(argv[0]))))
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f'Executable not found/not executable: {argv[0]}')
    argv = [executable, *argv[1:]]
    job, created = store.create(title, 'managed', {'argv': argv, 'cwd': workdir, 'parser': parser},
                                key=key, task_id=task_id, codex_origin=codex_origin)
    if created:
        job = spawn_worker(store, job['id'])
    from .codex_bridge import enqueue
    enqueue(store, job)
    if ui:
        job = dict(job, ui=ensure_ui(store))
    return job


def attach(store: Store, title: str, backend: str, target: dict, *, key=None, ui=True, codex_origin=None) -> dict:
    if backend == 'docker' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', target.get('container','')):
        raise ValueError('Invalid Docker container name/ID')
    if backend == 'kubernetes':
        for field in ('name', 'namespace'):
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', target.get(field,'')):
                raise ValueError('Invalid Kubernetes '+field)
    if backend == 'pid':
        target = dict(target, identity=identity(int(target['pid'])))
        if target.get('log_file'):
            target['log_file'] = str(Path(target['log_file']).expanduser().resolve())
    # Pin backend identity immediately where the backend is reachable.
    if backend in ('docker', 'kubernetes'):
        initial = adapters.observe({'backend': backend, 'target': target})
        if initial.target:
            target = initial.target
    job, created = store.create(title, backend, target, key=key, codex_origin=codex_origin)
    if created:
        job = spawn_worker(store, job['id'])
    from .codex_bridge import enqueue
    enqueue(store, job)
    if ui:
        job = dict(job, ui=ensure_ui(store))
    return job


def run_ssh(store, title, target, argv, *, cwd=None, key=None, ui=True, codex_origin=None):
    # Validate before creating a record. Task UUID also names the remote idempotent launch.
    adapters.ssh_argv(target)
    if not argv:
        raise ValueError('Provide a remote command after --')
    spec = dict(target, argv=argv, cwd=cwd)
    job, created = store.create(title, 'ssh', spec, key=key, codex_origin=codex_origin)
    if created:
        target = dict(spec, remote_id=job['id'])
        store.update(job['id'], target=target)
        try:
            adapters.remote_rpc(target, {'op': 'run', 'id': job['id'], 'title': title, 'argv': argv, 'cwd': cwd})
        except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
            store.update(job['id'], state='unknown', observation='disconnected',
                         detail='Remote submit acknowledgement missing: '+clean(exc)+'. Only reads will be retried.')
        job = spawn_worker(store, job['id'])
    from .codex_bridge import enqueue
    enqueue(store, job)
    if ui:
        job = dict(job, ui=ensure_ui(store))
    return job


def ensure_ui(store: Store, *, show=False):
    if show:
        store.configure(visible=True)
    if alive(store.setting('ui_identity')) is True:
        return {'running': True, 'visible': store.setting('visible')}
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY') or os.environ.get('QT_QPA_PLATFORM')):
        return {'running': False, 'reason': 'No graphical session; job is still monitored'}
    if importlib.util.find_spec('PySide6') is None:
        return {'running': False, 'reason': 'PySide6 unavailable; install process-hud[ui]'}
    # Concurrent UI launches serialize here and in gui.run's lifetime lock.
    with lock_file(store.home/'ui-start.lock'):
        if alive(store.setting('ui_identity')) is True:
            return {'running': True, 'visible': store.setting('visible')}
        with open(store.home/'ui.log', 'wb') as log:
            os.chmod(log.name, 0o600)
            p = subprocess.Popen([sys.executable, '-m', 'process_hud', '--home', str(store.home), '_ui'],
                env=environment(store), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            if alive(store.setting('ui_identity')) is True:
                return {'running': True, 'visible': store.setting('visible')}
            if p.poll() is not None:
                break
            time.sleep(.05)
        return {'running': False, 'reason': 'UI startup unconfirmed; see '+str(store.home/'ui.log')}


def reconcile(store):
    """Recover observers only. Never re-execute an interrupted managed job."""
    for j in store.list():
        recover_job(store, j['id'])


def recover_job(store, job_id, *, restored=False):
    """Restore observation/delivery of one row, never replay its command."""
    job = store.get(job_id)
    if job['removed_at']:
        return
    if (not job['finished'] and job['backend'] != 'external' and
            (restored or time.time()-job['created_at'] > 4) and
            (alive(job.get('worker')) is False or job.get('worker_stopping'))):
        spawn_worker(store, job['id'], recover=True)
    from .codex_bridge import enqueue
    enqueue(store, store.get(job_id))


def _subreaper():
    if sys.platform != 'linux':
        raise RuntimeError('Managed execution requires Linux')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'Unable to become child subreaper')


def managed(store, job):
    _subreaper()
    jid, t = job['id'], job['target']
    if job.get('started_at'):
        raise RuntimeError('Refusing to execute the same job twice')
    # Persist the launch attempt BEFORE spawning. A crash here is unknown, not a retry.
    capabilities = {'liveness': True, 'logs': True, 'exit_code': True,
                    'progress': True, 'descendants': True}
    store.update(jid, started_at=time.time(), capabilities=capabilities)
    env = workload_environment(store, jid)
    log = RotatingLog(store.task_dir(jid)/'output.log', int(store.setting('log_bytes')))
    try:
        p = subprocess.Popen(t['argv'], cwd=t['cwd'], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                             start_new_session=True, close_fds=True)
    except OSError as exc:
        log.close()
        store.finish(jid, 'failed', detail=str(exc), evidence='exec failed before workload start')
        return
    try:
        process_ref = identity(p.pid)
    except psutil.NoSuchProcess:
        process_ref = None
    def observed_update(**values):
        try:
            store.update(jid, **values)
        except (OSError, sqlite3.Error):
            # A transient state-store failure cannot justify closing a running
            # workload's stdout. Final persistence is attempted after reaping.
            pass

    observed_update(process=process_ref, state='running')
    os.set_blocking(p.stdout.fileno(), False)
    sel = selectors.DefaultSelector(); sel.register(p.stdout, selectors.EVENT_READ)
    buffer = b''; pending = []; last_flush = 0.0; last_heartbeat = 0.0
    main_code = None; adopted_failure = None; log_error = ''

    def flush_progress():
        nonlocal pending, last_flush
        if pending:
            try:
                store.set_progress_batch(jid, pending)
            except (ValueError, OSError, sqlite3.Error):
                pass
            pending = []
            last_flush = time.monotonic()

    def line(data):
        s = clean(data.decode('utf8', 'replace'), 65536).strip()
        if s.startswith('@@HUD '):
            try:
                obj = json.loads(s[6:])
                if not isinstance(obj, dict):
                    return
                # Output can report progress/attention, NEVER terminal status or commands.
                event = {k: v for k, v in obj.items() if k in
                         {'current','total','percent','phase','unit','message','eta_seconds','indeterminate','attention'}}
                event['source'] = 'structured_stdout'
                pending.append(event)
            except (ValueError, TypeError):
                pass
        elif t.get('parser') == 'percent':
            matches = PERCENT.findall(s)
            if matches:
                pending.append(dict(percent=float(matches[-1]), source='opt_in_percent_parser'))
        # Retain event order and validation semantics without a database write
        # per line or an unbounded in-memory queue under noisy workloads.
        if len(pending) >= 256:
            flush_progress()

    try:
        while True:
            for key, _ in sel.select(.1):
                data = os.read(key.fd, 65536)
                if data:
                    if not log_error:
                        try:
                            log.write(data)
                        except OSError as exc:
                            # Losing log storage must not close the workload's pipe or
                            # abandon wait status. Continue draining and supervising.
                            log_error = 'Log storage unavailable: '+clean(exc, 500)
                            capabilities['logs'] = False
                            observed_update(capabilities=capabilities, detail=log_error)
                    buffer += data.replace(b'\r', b'\n')
                    while b'\n' in buffer:
                        entry, buffer = buffer.split(b'\n', 1)
                        line(entry)
                    if len(buffer) > 65536:
                        buffer = buffer[-65536:]
                else:
                    sel.unregister(key.fileobj)
                    if buffer:
                        line(buffer); buffer = b''
            now = time.monotonic()
            if pending and now-last_flush >= .2:
                flush_progress()
            if now-last_heartbeat >= 2:
                observed_update(last_observed_at=time.time())
                last_heartbeat = now
            main_code = p.poll()
            no_children = False
            if main_code is not None:
                while True:
                    try:
                        pid, status = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        no_children = True; break
                    if pid == 0:
                        break
                    code = os.waitstatus_to_exitcode(status)
                    if code != 0:
                        adopted_failure = code
            if no_children and not sel.get_map():
                break
        flush_progress()
        code = main_code if main_code else (adopted_failure or 0)
        state = 'succeeded' if code == 0 else ('cancelled' if code < 0 else 'failed')
        observed_update(process=process_ref, capabilities=capabilities)
        store.finish(jid, state, code=code,
                     detail='; '.join(x for x in (
                         'An adopted descendant failed' if adopted_failure and not main_code else '',
                         log_error) if x),
                     evidence='Direct wait status plus all adopted descendants reaped')
    finally:
        sel.close(); p.stdout.close()
        try:
            log.close()
        except OSError:
            pass


def _write_log_snapshot(store, job_id, content):
    path = store.task_dir(job_id)/'output.log'
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(clean(content, 32768))


def watch(store, job):
    jid = job['id']; polls = 0
    while True:
        job = store.get(jid)
        if job['finished']:
            return
        if job['removed_at']:
            # Observation-only work is detached; actual workload is untouched.
            def detach(current):
                if current['removed_at'] and not current['finished']:
                    current.update(state='unknown', observation='lost', worker_stopping=True,
                                   detail='Monitoring detached by record removal')
            detached = store.mutate(jid, detach)
            if detached.get('worker_stopping') or detached['finished']:
                return
            continue  # Undo won the transaction; keep observing this same workload.
        try:
            o = adapters.observe(job, with_logs=(polls % 5 == 0))
            changes = {'state': o.state, 'observation': o.availability, 'detail': o.detail,
                       'capabilities': o.capabilities, 'last_observed_at': time.time()}
            if o.target:
                changes['target'] = o.target
            store.update(jid, **changes)
            if o.progress:
                try:
                    if job['backend'] == 'ssh':
                        # Preserve remote measurement timestamp; do not re-estimate it from polling rate.
                        store.update(jid, progress=o.progress)
                    else:
                        store.set_progress(jid, o.progress)
                except (ValueError, TypeError):
                    pass
            if o.log is not None:
                try:
                    _write_log_snapshot(store, jid, o.log)
                except (OSError, ValueError) as exc:
                    o.detail += ' Log snapshot unavailable: '+clean(exc, 500)
            if (job['backend'] == 'pid' and job['target'].get('log_file') and
                    (polls % 5 == 0 or o.finished)):
                try:
                    content = tail(Path(job['target']['log_file']))
                    _write_log_snapshot(store, jid, content)
                except (OSError, ValueError) as exc:
                    o.detail += ' Log snapshot unavailable: '+clean(exc, 500)
            if o.finished:
                # Capture one final bounded log snapshot; failure does not undo terminal evidence.
                if job['backend'] not in ('pid', 'managed') and polls % 5:
                    try:
                        final = adapters.observe(store.get(jid), with_logs=True)
                        if final.log is not None:
                            _write_log_snapshot(store, jid, final.log)
                    except (OSError, ValueError, RuntimeError, TimeoutError, KeyError) as exc:
                        o.detail += ' Final log snapshot unavailable: '+clean(exc, 500)
                store.finish(jid, o.state, code=o.code, detail=o.detail, evidence=o.evidence)
                return
            if o.detail != changes['detail']:
                store.update(jid, detail=o.detail)
        except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, IndexError) as exc:
            store.update(jid, state='unknown', observation='disconnected', detail=clean(exc, 1000))
        polls += 1
        time.sleep(max(.2, float(store.setting('poll_seconds'))))


def worker(store, job_id, recover=False):
    try:
        with lock_file(store.task_dir(job_id)/'worker.lock', blocking=recover):
            job = store.get(job_id)
            if job['finished']:
                return
            store.update(job_id, worker=identity(os.getpid()), worker_ready=True, worker_stopping=False)
            if job['backend'] == 'managed' and not recover and not job.get('started_at'):
                managed(store, job)
            else:
                watch(store, job)
            final = store.get(job_id)
            if final['finished']:
                from .notify import completed
                completed(store, final)
    except BlockingIOError:
        return
    except BaseException as exc:
        traceback.print_exc()
        store.update(job_id, state='unknown', observation='lost', detail='Observer failed: '+clean(exc))
