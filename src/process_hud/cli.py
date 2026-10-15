from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
from . import __version__
from .model import TERMINAL, summary
from .store import DEFAULTS, Store
from .util import alive, clean, identity, tail


def parser():
    p = argparse.ArgumentParser(description='Model-free background job monitor. No workload stop/restart commands.')
    p.add_argument('--version', action='version', version=__version__)
    p.add_argument('--home', help='Private state directory (also PROCESS_HUD_HOME)')
    s = p.add_subparsers(dest='cmd', required=True)
    for name in ('run', 'run-ssh'):
        q = s.add_parser(name, help='Launch once and return immediately with task ID')
        q.add_argument('--title', required=True)
        q.add_argument('--cwd')
        q.add_argument('--key', help='Stable idempotency key; reuse only for this exact operation')
        q.add_argument('--no-ui', action='store_true')
        q.add_argument('--id-only', action='store_true')
        if name == 'run':
            q.add_argument('--parser', choices=['none', 'percent'], default='none')
        else:
            q.add_argument('--host', required=True)
            q.add_argument('--remote-bin', default='process-hud')
        q.add_argument('command', nargs=argparse.REMAINDER)
    q = s.add_parser('attach', help='Observe an existing local PID; exit status may be unavailable')
    q.add_argument('--pid', type=int, required=True)
    q.add_argument('--log-file')
    for name in ('attach-docker', 'attach-k8s', 'attach-ssh'):
        q = s.add_parser(name)
        if name == 'attach-docker':
            q.add_argument('--container', required=True)
            q.add_argument('--context')
        elif name == 'attach-k8s':
            q.add_argument('--job', required=True)
            q.add_argument('--namespace', default='default')
            q.add_argument('--context', required=True, help='Pin kubectl context; never follow current-context changes')
        else:
            q.add_argument('--host', required=True)
            q.add_argument('--remote-id', required=True)
            q.add_argument('--remote-bin', default='process-hud')
    for name in ('attach', 'attach-docker', 'attach-k8s', 'attach-ssh'):
        q = s.choices[name]
        q.add_argument('--title', required=True)
        q.add_argument('--key')
        q.add_argument('--no-ui', action='store_true')
    q = s.add_parser('register', help='Register an external producer; it must report completion explicitly')
    q.add_argument('--title', required=True); q.add_argument('--source', default='external')
    q.add_argument('--key'); q.add_argument('--no-ui', action='store_true')
    for name in ('run', 'run-ssh', 'attach', 'attach-docker', 'attach-k8s', 'attach-ssh', 'register'):
        q = s.choices[name]
        q.add_argument('--resume-codex', action='store_true',
                       help='Deliver completion/attention to the verified Codex owner')
        q.add_argument('--attention-after', type=float, metavar='SECONDS',
                       help='One attention event after this duration; never stops work (requires --resume-codex)')
    q = s.add_parser('progress')
    q.add_argument('id', nargs='?', default=os.environ.get('PROCESS_HUD_TASK_ID'))
    for field in ('current', 'total', 'percent', 'eta-seconds'):
        q.add_argument('--'+field, type=float)
    for field in ('phase', 'unit', 'message'):
        q.add_argument('--'+field)
    q.add_argument('--indeterminate', action='store_true')
    q = s.add_parser('finish', help='Terminal evidence from an external producer only')
    q.add_argument('id'); q.add_argument('--state', choices=sorted(TERMINAL), required=True)
    q.add_argument('--exit-code', type=int); q.add_argument('--message', default='')
    q = s.add_parser('attention', help='Explicit producer request for a decision; does not finish or stop work')
    q.add_argument('id', nargs='?', default=os.environ.get('PROCESS_HUD_TASK_ID'))
    q.add_argument('--message', required=True)
    q = s.add_parser('acknowledge', help='Mark a delivered attention event as consumed; task stays visible')
    q.add_argument('id'); q.add_argument('--event', required=True)
    q = s.add_parser('list'); q.add_argument('--all', action='store_true'); q.add_argument('--text', action='store_true')
    for name in ('report', 'logs'):
        q = s.add_parser(name); q.add_argument('id'); q.add_argument('--tail', type=int, default=40)
    for name in ('remove', 'restore', 'purge'):
        q = s.add_parser(name); q.add_argument('id')
        if name == 'purge':
            q.add_argument('--yes', action='store_true', required=True)
    q = s.add_parser('ui'); q.add_argument('action', choices=['ensure', 'show', 'hide', 'quit'], default='ensure', nargs='?')
    q = s.add_parser('config')
    q.add_argument('--notify', choices=['on', 'off']); q.add_argument('--sound', choices=['on', 'off'])
    q.add_argument('--opacity', type=float); q.add_argument('--width', type=int)
    q.add_argument('--poll-seconds', type=float); q.add_argument('--log-mib', type=int)
    s.add_parser('doctor'); s.add_parser('recover', help='Recover observers, never rerun work')
    q = s.add_parser('_worker'); q.add_argument('id'); q.add_argument('--recover', action='store_true')
    s.add_parser('_ui'); s.add_parser('_rpc')
    return p


def compact(j):
    return {k: j[k] for k in ('id','title','backend','state','finished','ui') if k in j}


def main(argv=None):
    os.umask(0o077)
    a = parser().parse_args(argv)
    try:
        st = Store(a.home)
        from . import runtime
        origin = None
        if hasattr(a, 'resume_codex'):
            if a.attention_after is not None:
                from .model import number
                number(a.attention_after, 'attention-after', minimum=.1, maximum=365*86400)
                if not a.resume_codex:
                    raise ValueError('--attention-after requires --resume-codex')
            if a.resume_codex:
                from .codex_bridge import current_origin
                origin = current_origin()
                if a.attention_after is not None:
                    origin['attention_after'] = a.attention_after
        if a.cmd == '_worker':
            runtime.worker(st, a.id, a.recover); return 0
        if a.cmd == '_ui':
            from .gui import run
            return run(st)
        if a.cmd == '_rpc':
            # SSH-authenticated stdio only. No listening socket or remotely accessible HTTP API.
            text = sys.stdin.read(64001)
            if len(text.encode()) > 64000:
                raise ValueError('RPC request too large')
            r = json.loads(text)
            if not isinstance(r, dict):
                raise ValueError('RPC request must be a JSON object')
            if r['op'] == 'run':
                result = compact(runtime.launch(st, r['title'], r['argv'], cwd=r.get('cwd'),
                                                 task_id=r['id'], ui=False))
            elif r['op'] == 'report':
                runtime.reconcile(st)
                result = st.report(r['id'], int(r.get('tail', 0)))
            else:
                raise ValueError('Unsupported RPC operation')
        elif a.cmd in ('run', 'run-ssh'):
            command = a.command[1:] if a.command[:1] == ['--'] else a.command
            if a.cmd == 'run':
                j = runtime.launch(st, a.title, command, cwd=a.cwd, key=a.key, parser=a.parser,
                                   ui=not a.no_ui, codex_origin=origin)
            else:
                j = runtime.run_ssh(st, a.title, {'host': a.host, 'remote_bin': a.remote_bin}, command,
                                    cwd=a.cwd, key=a.key, ui=not a.no_ui, codex_origin=origin)
            if a.id_only:
                print(j['id']); return 0
            result = compact(j)
        elif a.cmd.startswith('attach'):
            if a.cmd == 'attach':
                b, t = 'pid', {'pid': a.pid, 'log_file': a.log_file}
            elif a.cmd == 'attach-docker':
                b, t = 'docker', {'container': a.container, 'context': a.context}
            elif a.cmd == 'attach-k8s':
                b, t = 'kubernetes', {'name': a.job, 'namespace': a.namespace, 'context': a.context}
            else:
                b, t = 'ssh', {'host': a.host, 'remote_bin': a.remote_bin, 'remote_id': a.remote_id}
                from .adapters import ssh_argv
                from .util import ID_RE
                ssh_argv(t)
                if not ID_RE.fullmatch(a.remote_id):
                    raise ValueError('Invalid remote task ID')
            result = compact(runtime.attach(st, a.title, b, t, key=a.key, ui=not a.no_ui, codex_origin=origin))
        elif a.cmd == 'register':
            j, created = st.create(a.title, 'external', {'source': clean(a.source)}, key=a.key,
                                   codex_origin=origin)
            if created:
                j = st.update(j['id'], state='running', started_at=time.time(),
                              capabilities={'progress': True, 'terminal_evidence': 'producer_report'})
            from .codex_bridge import enqueue
            enqueue(st, j)
            if not a.no_ui:
                j['ui'] = runtime.ensure_ui(st)
            result = compact(j)
        elif a.cmd == 'progress':
            if not a.id:
                raise ValueError('Task ID missing (argument or PROCESS_HUD_TASK_ID)')
            event = {f: getattr(a, f) for f in ('current','total','percent','eta_seconds','phase','unit','message')
                     if getattr(a, f) is not None}
            if a.indeterminate:
                event['indeterminate'] = True
            result = st.set_progress(a.id, event)['progress']
        elif a.cmd == 'finish':
            if st.get(a.id)['backend'] != 'external':
                raise ValueError('Only external producer records may be manually finished')
            if a.state == 'succeeded' and a.exit_code not in (None, 0):
                raise ValueError('Non-zero exit code cannot claim success')
            result = compact(st.finish(a.id, a.state, code=a.exit_code, detail=a.message,
                                       evidence='Explicit external producer report'))
            from .notify import completed
            completed(st, st.get(a.id))
        elif a.cmd == 'attention':
            if not a.id:
                raise ValueError('Task ID missing')
            st.set_progress(a.id, {'attention': a.message, 'message': a.message})
            result = {'id': a.id, 'attention_requested': True, 'workload_signalled': False}
        elif a.cmd == 'acknowledge':
            st.acknowledge(a.id, a.event)
            result = {'id': a.id, 'event': a.event, 'consumed': True}
        elif a.cmd == 'list':
            result = [compact(j) | {'summary': summary(j), 'removed_at': j['removed_at']} for j in st.list(a.all)]
            if a.text:
                for j in result:
                    print(f"{j['id']}  {j['title']}  {j['summary']}")
                return 0
        elif a.cmd == 'report':
            result = st.report(a.id, a.tail)
        elif a.cmd == 'logs':
            print(st.report(a.id, a.tail)['log_tail']); return 0
        elif a.cmd in ('remove', 'restore'):
            result = compact(getattr(st, a.cmd)(a.id)) | {'record_action': a.cmd, 'workload_signalled': False}
            if a.cmd == 'restore':
                runtime.recover_job(st, a.id, restored=True)
        elif a.cmd == 'purge':
            j = st.get(a.id)
            if not j['removed_at'] or alive(j.get('worker')) is not False:
                raise ValueError('Remove record first and wait for its observer to exit before purging logs')
            # Retain tiny tombstone/idempotency evidence; delete only known local log paths.
            for path in st.task_dir(a.id).glob('*.log*'):
                path.unlink()
            result = {'id': a.id, 'logs_purged': True, 'tombstone_retained': True}
        elif a.cmd == 'ui':
            if a.action in ('show', 'ensure'):
                result = runtime.ensure_ui(st, show=a.action == 'show')
            elif a.action == 'hide':
                st.configure(visible=False); result = {'visible': False, 'workloads_untouched': True}
            else:
                st.configure(ui_quit=time.time()); result = {'ui_quit_requested': True, 'workloads_untouched': True}
        elif a.cmd == 'recover':
            runtime.reconcile(st); result = {'observer_reconciliation': True, 'workloads_relaunched': False}
        elif a.cmd == 'config':
            values = {}
            for k in ('notify', 'sound'):
                if getattr(a, k) is not None:
                    values[k] = getattr(a, k) == 'on'
            if a.opacity is not None:
                if not .05 <= a.opacity <= .95:
                    raise ValueError('opacity must be .05..95')
                values['opacity'] = a.opacity
            if a.width is not None:
                if not 215 <= a.width <= 800:
                    raise ValueError('width must be 215..800')
                values['width'] = a.width
            if a.poll_seconds is not None:
                if not .2 <= a.poll_seconds <= 60:
                    raise ValueError('poll seconds must be .2..60')
                values['poll_seconds'] = a.poll_seconds
            if a.log_mib is not None:
                if not 1 <= a.log_mib <= 128:
                    raise ValueError('log MiB must be 1..128')
                values['log_bytes'] = a.log_mib*1024*1024
            st.configure(**values)
            result = {k: st.setting(k) for k in DEFAULTS}
        elif a.cmd == 'doctor':
            result = {'version': __version__, 'python': sys.version.split()[0], 'platform': sys.platform,
                'state_home': str(st.home), 'display': os.environ.get('DISPLAY'),
                'session_type': os.environ.get('XDG_SESSION_TYPE'),
                'pyside6': importlib.util.find_spec('PySide6') is not None,
                'tools': {k: shutil.which(k) for k in ('ssh','docker','kubectl','notify-send','gsettings','gdbus','canberra-gtk-play')},
                'ui_alive': alive(st.setting('ui_identity')),
                'dead_observers': [j['id'] for j in st.list() if not j['finished'] and j['backend'] != 'external'
                                   and alive(j['worker']) is False],
                'note': 'Read-only diagnostic. No login autostart, workload restart, or quota measurement.'}
        else:
            raise ValueError('Unknown command')
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (ValueError, RuntimeError, KeyError, OSError, TimeoutError, ImportError) as exc:
        # Machine-readable errors on stderr; no secrets from environment dumped.
        print(json.dumps({'error': clean(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
