#!/usr/bin/env python3
"""Prepare a two-minute HUD job after the source IDE conversation reconnects.

Run through the HUD's Python interpreter. This coordinator never calls a model.
The evaluate action reads only lifecycle and token-count metadata from rollouts.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from process_hud.codex_bridge import atomic_json, continuation_owner, endpoints
from process_hud.store import Store


def pause(result):
    started = time.time()
    mono = time.monotonic()
    print('@@HUD '+json.dumps({'current': 0, 'total': 120, 'unit': 's', 'phase': 'Пауза'}), flush=True)
    atomic_json(Path(result), {'started_at': started, 'finished_at': None})
    time.sleep(120)
    final = {'started_at': started, 'finished_at': time.time(),
             'elapsed_seconds': time.monotonic()-mono}
    atomic_json(Path(result), final)
    print('@@HUD '+json.dumps({'current': 120, 'total': 120, 'unit': 's', 'phase': 'Пауза завершена'}), flush=True)
    print('120-second pause finished', flush=True)


def reload_after_turn(thread, window):
    rollouts = list((Path.home()/'.codex/sessions').glob('**/*'+thread+'.jsonl'))
    if len(rollouts) != 1:
        raise ValueError('No unique source rollout')
    with rollouts[0].open() as f:
        turn = None
        for line in f:
            d = json.loads(line)
            p = d.get('payload') or {}
            if d.get('type') == 'event_msg' and p.get('type') == 'task_started':
                turn = p.get('turn_id')
        if not turn:
            raise ValueError('No current turn')
        deadline = time.monotonic()+600
        while time.monotonic() < deadline:
            line = f.readline()
            if not line:
                time.sleep(.25)
                continue
            d = json.loads(line)
            p = d.get('payload') or {}
            if d.get('type') != 'event_msg' or p.get('turn_id') != turn:
                continue
            if p.get('type') == 'turn_aborted':
                raise ValueError('Source turn was interrupted; activation cancelled')
            if p.get('type') == 'task_complete':
                break
        else:
            raise TimeoutError('Source turn did not complete')
        time.sleep(1)
        for line in f:
            d = json.loads(line)
            if d.get('type') == 'event_msg' and (d.get('payload') or {}).get('type') == 'task_started':
                raise ValueError('A new user turn started; activation cancelled')
    title = subprocess.check_output(['xdotool', 'getwindowname', str(window)], text=True).strip()
    if 'Process HUD' not in title or not title.endswith('Visual Studio Code'):
        raise ValueError('Refusing to reload a different editor window')
    subprocess.run(['xdotool', 'windowactivate', '--sync', str(window)], check=True, timeout=10)
    subprocess.run(['xdotool', 'key', '--clearmodifiers', 'ctrl+shift+p'], check=True)
    time.sleep(.5)
    subprocess.run(['xdotool', 'type', '--clearmodifiers', '--delay', '2', 'Developer: Reload Window'], check=True)
    time.sleep(.4)
    subprocess.run(['xdotool', 'key', '--clearmodifiers', 'Return'], check=True)


def prepare(path, thread, window=None):
    manifest = Path(path).absolute()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(manifest, {'state': 'waiting_for_current_turn' if window else 'waiting_for_bridge', 'thread_id': thread,
                           'prepared_at': time.time()})
    if window:
        try:
            reload_after_turn(thread, window)
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
            atomic_json(manifest, {'state': 'activation_failed', 'thread_id': thread, 'error': str(exc)})
            return 1
    deadline = time.monotonic()+600
    owner, lineage = continuation_owner(thread)
    idle_since = None
    idle_instance = None
    while time.monotonic() < deadline:
        matches = [e for e in endpoints() if e['initialized'] and thread in e['threads']
                   and owner in e['threads'] and thread not in e.get('active_turns', {})
                   and owner not in e.get('active_turns', {})]
        if len(matches) == 1:
            if idle_instance != matches[0]['instance']:
                idle_since = time.monotonic()
                idle_instance = matches[0]['instance']
            if time.monotonic()-idle_since >= 2:
                break
        else:
            idle_since = None
            idle_instance = None
        time.sleep(1)
    else:
        atomic_json(manifest, {'state': 'bridge_timeout', 'thread_id': thread})
        return 1
    endpoint = matches[0]
    result = manifest.with_suffix('.pause.json')
    env = dict(os.environ, CODEX_THREAD_ID=thread,
               PROCESS_HUD_CODEX_INSTANCE=endpoint['instance'])
    key = 'codex-idle-120s-'+str(int(time.time()))
    receipt = subprocess.run([str(Path(sys.executable).parent/'process-hud'), 'run',
        '--title', 'HUD · пауза 120 секунд', '--resume-codex', '--key', key, '--',
        sys.executable, str(Path(__file__).resolve()), 'pause', str(result)],
        capture_output=True, text=True, env=env, timeout=15)
    if receipt.returncode:
        atomic_json(manifest, {'state': 'launch_failed', 'error': receipt.stderr[:2000]})
        return 1
    r = json.loads(receipt.stdout)
    atomic_json(manifest, {'state': 'running', 'thread_id': thread, 'task_id': r['id'],
                           'continuation_thread_id': owner, 'lineage': lineage,
                           'receipt': r, 'audit': endpoint['audit'],
                           'pause_result': str(result), 'launched_at': time.time(),
                           'bridge_instance': endpoint['instance']})
    return 0


def evaluate(path):
    manifest = Path(path)
    m = json.loads(manifest.read_text())
    timing = json.loads(Path(m['pause_result']).read_text())
    start, end = timing['started_at'], timing['finished_at']
    if end is None:
        raise ValueError('Pause has not completed')
    events = [json.loads(x) for x in Path(m['audit']).read_text().splitlines() if x]
    window = [e for e in events if e.get('thread') == m['thread_id'] and start <= e['time'] <= end]
    starts = [e for e in window if e['event'] in ('turn/started', 'ide.turn/start', 'hud.turn/start')]
    updates = [e for e in window if e['event'] == 'thread/tokenUsage/updated']
    rollouts = list((Path.home()/'.codex/sessions').glob('**/*'+m['thread_id']+'.jsonl'))
    counts = []
    for rollout in rollouts:
        for line in rollout.open():
            d = json.loads(line)
            p = d.get('payload') or {}
            if d.get('type') == 'event_msg' and p.get('type') == 'token_count':
                info = p.get('info') or {}
                usage = info.get('total_token_usage')
                if usage:
                    stamp = datetime.datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00')).timestamp()
                    counts.append((stamp, usage))
    before = [x for x in counts if x[0] <= start]
    after = [x for x in counts if x[0] <= end]
    baseline = max(before, default=(None, None), key=lambda x: x[0])[1]
    final = max(after, default=(None, None), key=lambda x: x[0])[1]
    report = Store().report(m['task_id'], 8)
    owner = m.get('continuation_thread_id', m['thread_id'])
    owner_window = [e for e in events if e.get('thread') == owner and start <= e['time'] <= end]
    deliveries = [e for e in events if e.get('job') == m['task_id'] and e['event'] == 'delivery.accepted']
    result = {'thread_id': m['thread_id'], 'task_id': m['task_id'], **timing,
              'turn_starts_during_pause': len([e for e in starts if e['event'] == 'turn/started']),
              'turn_submissions_during_pause': len([e for e in starts if e['event'] != 'turn/started']),
              'token_updates_during_pause': len(updates),
              'rollout_token_events_during_pause': len([x for x in counts if start < x[0] <= end]),
              'cumulative_tokens_before': baseline, 'cumulative_tokens_at_end': final,
              'task_state': report['state'], 'exit_code': report['exit_code'],
              'delivery': report.get('codex_delivery'),
              'continuation_thread_id': owner,
              'parent_turn_starts_during_pause': sum(e['event'] == 'turn/started' for e in owner_window),
              'parent_token_updates_during_pause': sum(e['event'] == 'thread/tokenUsage/updated' for e in owner_window),
              'accepted_delivery_targets': sorted({e['thread'] for e in deliveries}),
              'no_model_activity_observed': not starts and not updates and
                  not any(start < x[0] <= end for x in counts) and baseline is not None and baseline == final,
              'scope': 'This conversation only; account-wide quota is not exposed by this measurement.'}
    atomic_json(manifest.with_suffix('.evaluation.json'), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['prepare', 'pause', 'evaluate'])
    p.add_argument('path')
    p.add_argument('--thread')
    p.add_argument('--reload-window', type=int)
    a = p.parse_args()
    if a.action == 'prepare':
        raise SystemExit(prepare(a.path, a.thread, a.reload_window))
    elif a.action == 'pause':
        pause(a.path)
    else:
        evaluate(a.path)
