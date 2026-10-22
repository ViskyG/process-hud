"""Read-only observers. They never stop/restart/delete a workload."""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import re
import shlex
import time
from .model import number, progress as validate_progress
from .util import alive, clean, json_tool, tool


@dataclass
class Observation:
    state: str = 'running'
    availability: str = 'live'
    finished: bool = False
    code: int | None = None
    detail: str = ''
    evidence: str = ''
    progress: dict | None = None
    target: dict | None = None
    log: str | None = None
    capabilities: dict = field(default_factory=dict)


def docker_snapshot(obj: dict, target: dict) -> Observation:
    exact = obj['Id']
    pinned = target.get('container_id')
    if pinned and exact != pinned:
        return Observation('unknown', 'lost', True, detail='Container identity changed')
    t = dict(target, container_id=exact)
    s = obj['State']
    caps = {'liveness': True, 'logs': True, 'exit_code': True, 'progress': False}
    if s.get('Restarting'):
        return Observation(detail='Docker is restarting the container', target=t, capabilities=caps)
    if s.get('Paused'):
        return Observation('paused', target=t, capabilities=caps)
    if s.get('Running'):
        return Observation(target=t, capabilities=caps)
    if s.get('Status') in ('exited', 'dead'):
        code = s.get('ExitCode')
        if type(code) is not int:
            return Observation('unknown', 'lost', True, detail='Container exited without exit evidence', target=t)
        return Observation('succeeded' if code == 0 and not s.get('OOMKilled') else 'failed',
                           'complete', True, code, clean(s.get('Error') or ('OOM killed' if s.get('OOMKilled') else '')),
                           'Docker State.ExitCode', target=t, capabilities=caps)
    return Observation('starting', detail='Container has not started', target=t, capabilities=caps)


def kubernetes_snapshot(obj: dict, target: dict) -> Observation:
    uid = obj['metadata']['uid']
    if target.get('uid') and uid != target['uid']:
        return Observation('unknown', 'lost', True, detail='Kubernetes Job UID changed; not adopting replacement')
    t = dict(target, uid=uid)
    status, spec = obj.get('status', {}), obj.get('spec', {})
    caps = {'liveness': True, 'logs': True, 'exit_code': False, 'progress': bool(spec.get('completions'))}
    for cond in status.get('conditions', []):
        if cond.get('status') == 'True' and cond.get('type') in ('Complete', 'Failed'):
            return Observation('succeeded' if cond['type'] == 'Complete' else 'failed', 'complete', True,
                               detail=clean(cond.get('message', '')), evidence='Kubernetes Job '+cond['type'],
                               target=t, capabilities=caps)
    if spec.get('suspend'):
        return Observation('paused', target=t, capabilities=caps)
    p = None
    total, done = spec.get('completions'), status.get('succeeded', 0)
    if isinstance(total, int) and total > 0 and isinstance(done, int) and done <= total:
        # Completion-count fraction, not a universal duration estimate.
        p = {'current': done, 'total': total, 'unit': 'pods', 'source': 'kubernetes.completions',
             'phase': 'completed pods', 'message': 'Pod completion count; work durations may differ'}
    return Observation(progress=p, target=t, capabilities=caps)


def ssh_argv(target: dict) -> list[str]:
    host = target['host']
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:\-]*', host):
        raise ValueError('Use a simple SSH config host alias or user@hostname')
    executable = target.get('remote_bin', 'process-hud')
    if not isinstance(executable, str) or '\n' in executable or not executable:
        raise ValueError('Invalid remote executable')
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
            '-o', 'StrictHostKeyChecking=yes', '--', host, shlex.join([executable, '_rpc'])]


def remote_rpc(target: dict, request: dict) -> dict:
    result = json_tool(ssh_argv(target), timeout=15, input_text=json.dumps(request))
    if not isinstance(result, dict):
        raise ValueError('Remote response must be an object')
    if result.get('error'):
        raise RuntimeError(clean(result['error']))
    return result


def observe(job: dict, *, with_logs: bool = False) -> Observation:
    b, t = job['backend'], job['target']
    if b in ('pid', 'managed'):
        ref = t.get('identity') if b == 'pid' else job.get('process')
        live = alive(ref)
        if live is True:
            return Observation(capabilities={'liveness': True, 'logs': bool(t.get('log_file')), 'exit_code': False},
                               detail='Attached process: final exit code is not owned by this observer')
        if live is None:
            return Observation('unknown', 'disconnected', detail='Cannot read process identity')
        return Observation('unknown', 'lost', True,
                           detail='Process no longer observed; successful completion is NOT established',
                           evidence='PID + creation time + boot identity')
    if b == 'docker':
        argv = ['docker'] + (['--context', t['context']] if t.get('context') else [])
        data = json_tool(argv + ['inspect', '--type', 'container', t.get('container_id', t['container'])])
        o = docker_snapshot(data[0], t)
        if with_logs and not (o.finished and o.state == 'unknown'):
            try:
                code, out, err = tool(argv + ['logs', '--timestamps', '--tail', '120', o.target['container_id']])
                # docker logs writes container stderr to its own stderr.
                if code == 0:
                    o.log = clean(out + ('\n'+err if err else ''), 32768)
                else:
                    o.detail += ' Logs unavailable: '+clean(err, 200)
            except (OSError, ValueError, TimeoutError):
                o.detail += ' Logs unavailable; backend state was still observed.'
        return o
    if b == 'kubernetes':
        argv = ['kubectl', '--request-timeout=8s']
        if t.get('context'):
            argv += ['--context', t['context']]
        argv += ['--namespace', t['namespace']]
        data = json_tool(argv + ['get', 'job', t['name'], '-o', 'json'])
        o = kubernetes_snapshot(data, t)
        if with_logs and not (o.finished and o.state == 'unknown'):
            # Pin log selection by the verified Job UID, not merely a potentially reused name.
            try:
                code, out, err = tool(argv + ['logs', '-l', 'controller-uid='+o.target['uid'],
                                            '--all-containers=true', '--prefix=true', '--tail=100', '--max-log-requests=5'])
                if code == 0:
                    o.log = clean(out, 32768)
                else:
                    o.detail += ' Logs unavailable: '+clean(err, 200)
            except (OSError, ValueError, TimeoutError):
                o.detail += ' Logs unavailable; Job state was still observed.'
        return o
    if b == 'ssh':
        result = remote_rpc(t, {'op': 'report', 'id': t['remote_id'], 'tail': 80 if with_logs else 0})
        if (result.get('id') != t['remote_id'] or type(result.get('schema_version')) is not int
                or result['schema_version'] != 1):
            raise ValueError('Remote identity/protocol mismatch')
        state = result['state']
        if not isinstance(state, str) or state not in {'starting', 'running', 'paused', 'unknown', 'succeeded', 'failed', 'cancelled'}:
            raise ValueError('Invalid remote state')
        if type(result.get('finished')) is not bool:
            raise ValueError('Remote finished must be a boolean')
        code = result.get('exit_code')
        if code is not None and type(code) is not int:
            raise ValueError('Invalid remote exit code')
        if state in ('succeeded', 'failed', 'cancelled') and not result['finished']:
            raise ValueError('Remote terminal state without terminal receipt')
        if result['finished'] and state not in ('succeeded', 'failed', 'cancelled', 'unknown'):
            raise ValueError('Remote finished receipt has a nonterminal state')
        if state == 'succeeded' and code not in (None, 0):
            raise ValueError('Remote success conflicts with exit code')
        if result.get('observation') not in ('live','disconnected','lost','complete'):
            raise ValueError('Invalid remote observation')
        p = result.get('progress') or None
        if p:
            if not isinstance(p, dict):
                raise ValueError('Invalid remote progress')
            timestamp = number(p.get('updated_at', time.time()), 'updated_at')
            measured = number(p.get('measurement_at', timestamp), 'measurement_at')
            eta_source = p.get('eta_source', 'reported')
            if eta_source not in ('reported', 'rate_estimate'):
                raise ValueError('Invalid remote ETA source')
            p = validate_progress({}, p, timestamp)
            p['measurement_at'] = measured
            if p.get('eta_seconds') is not None:
                p['eta_source'] = eta_source
            if max(timestamp, measured) > time.time()+60:
                p['eta_seconds'] = None  # Unreconciled remote clock; no fake countdown.
        return Observation(state, result.get('observation', 'live'), bool(result.get('finished')),
                           result.get('exit_code'), clean(result.get('detail', '')),
                           'Remote report: '+clean(result.get('evidence', '')), p,
                           log=result.get('log_tail') if with_logs else None,
                           capabilities=result.get('capabilities', {}))
    raise ValueError(f'Unsupported observer backend: {b}')
