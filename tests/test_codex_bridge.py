"""Exercise the actual relay transport against a model-free fake App Server."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

from process_hud.codex_bridge import Relay, call, continuation_owner, current_origin
from process_hud.store import Store


@pytest.fixture
def relay(tmp_path):
    fake = tmp_path/'fake-codex'
    fake.write_text('''#!/usr/bin/env python3
import json,sys
def send(x):
 print(json.dumps(x),flush=True)
for line in sys.stdin:
 m=json.loads(line);method=m.get('method');p=m.get('params',{})
 if method=='initialize':send({'id':m['id'],'result':{'userAgent':'fake/1'}})
 elif method=='thread/resume':send({'id':m['id'],'result':{'thread':{'id':p['threadId']}}})
 elif method=='turn/start':
  t={'id':'turn-fake','status':'inProgress'}
  send({'method':'turn/started','params':{'threadId':p['threadId'],'turn':t}})
  send({'id':m['id'],'result':{'turn':t}})
  send({'method':'thread/tokenUsage/updated','params':{'threadId':p['threadId'],'tokenUsage':{'total':{'totalTokens':11},'last':{'totalTokens':11}}}})
  send({'method':'turn/completed','params':{'threadId':p['threadId'],'turn':dict(t,status='completed')}})
 elif 'id' in m:send({'id':m['id'],'result':{'echo':p}})
''')
    fake.chmod(0o700)
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'real_binary': str(fake)}))
    # Unix domain socket paths are bounded; pytest tmp paths may be long.
    import tempfile
    home = Path(tempfile.mkdtemp(prefix='hud-bridge-test-'))
    env = dict(os.environ, PROCESS_HUD_CODEX_BRIDGE=str(home),
               PROCESS_HUD_CODEX_CONFIG=str(config), PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    p = subprocess.Popen([sys.executable, '-m', 'process_hud.codex_bridge', 'app-server'],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, bufsize=0)

    def send(value):
        p.stdin.write(json.dumps(value).encode()+b'\n');p.stdin.flush()

    def receive():
        assert select.select([p.stdout], [], [], 5)[0], 'Relay did not forward a response'
        return json.loads(p.stdout.readline())

    send({'id': 'ide:1', 'method': 'initialize', 'params': {}})
    assert receive()['id'] == 'ide:1'
    send({'method': 'initialized'})
    send({'id': 'ide:2', 'method': 'thread/resume', 'params': {'threadId': 'thread-source'}})
    assert receive()['result']['thread']['id'] == 'thread-source'
    deadline = time.monotonic()+5
    metadata = None
    while time.monotonic() < deadline:
        for path in home.glob('*.json'):
            metadata = json.loads(path.read_text())
        if metadata and 'thread-source' in metadata['threads']:
            break
        time.sleep(.01)
    assert metadata
    yield home, metadata, send, receive
    p.stdin.close()
    p.wait(timeout=5)
    assert p.returncode == 0, p.stderr.read().decode()
    import shutil
    shutil.rmtree(home)


def terminal_job(store, home, state='succeeded', thread='thread-source'):
    j, _ = store.create('background', 'external', codex_origin={
        'thread_id': thread, 'bridge_home': str(home), 'instance': 'original'})
    return store.finish(j['id'], state, code=0 if state == 'succeeded' else 9)


def test_relay_preserves_ide_traffic_and_deduplicates_completion(relay, store):
    home, metadata, send, receive = relay
    payload = {'literal': 'keep spaces, unicode: пауза', 'nested': [None, True, 3]}
    send({'id': 'ide:3', 'method': 'echo', 'params': payload})
    assert receive() == {'id': 'ide:3', 'result': {'echo': payload}}
    j = terminal_job(store, home)
    request = {'op': 'deliver', 'home': str(store.home), 'job_id': j['id']}
    first = call(metadata['socket'], request)
    assert first['state'] == 'accepted'
    assert call(metadata['socket'], request) == first
    # Actual server notifications reach the IDE; internal responses do not.
    events = [receive() for _ in range(3)]
    assert [x['method'] for x in events] == ['turn/started', 'thread/tokenUsage/updated', 'turn/completed']
    audit = [json.loads(x) for x in Path(metadata['audit']).read_text().splitlines()]
    assert len([x for x in audit if x['event'] == 'hud.turn/start']) == 1
    assert all(x.get('thread') == 'thread-source' for x in audit if x['event'] == 'turn/started')
    assert payload['literal'] not in Path(metadata['audit']).read_text()


@pytest.mark.parametrize('state', ['succeeded', 'failed', 'cancelled'])
def test_terminal_states_are_delivered(relay, store, state):
    home, m, _, _ = relay
    j = terminal_job(store, home, state)
    assert call(m['socket'], {'op': 'deliver', 'home': str(store.home), 'job_id': j['id']})['state'] == 'accepted'


def test_unrelated_running_unknown_and_removed_jobs_never_start_turn(relay, store):
    home, m, _, _ = relay
    foreign = terminal_job(store, home, thread='another-conversation')
    assert call(m['socket'], {'op': 'deliver', 'home': str(store.home), 'job_id': foreign['id']})['state'] == 'pending'
    unknown = terminal_job(store, home, state='unknown')
    assert call(m['socket'], {'op': 'deliver', 'home': str(store.home), 'job_id': unknown['id']})['state'] == 'error'
    running, _ = store.create('running', 'external', codex_origin={'thread_id': 'thread-source', 'bridge_home': str(home)})
    assert call(m['socket'], {'op': 'deliver', 'home': str(store.home), 'job_id': running['id']})['state'] == 'error'
    removed = terminal_job(store, home)
    store.remove(removed['id'])
    assert call(m['socket'], {'op': 'deliver', 'home': str(store.home), 'job_id': removed['id']})['state'] == 'dismissed'
    audit = Path(m['audit']).read_text()
    assert 'hud.turn/start' not in audit


def test_missing_bridge_is_rejected_before_launch(monkeypatch, tmp_path):
    monkeypatch.setenv('CODEX_THREAD_ID', 'thread-source')
    monkeypatch.setenv('PROCESS_HUD_CODEX_BRIDGE', str(tmp_path/'missing'))
    with pytest.raises(ValueError, match='No unique live'):
        current_origin()


def session_metadata(home, thread, parent=None):
    folder = home/'sessions/2026/10/15'
    folder.mkdir(parents=True, exist_ok=True)
    source = 'vscode' if parent is None else {'subagent': {'thread_spawn': {'parent_thread_id': parent}}}
    (folder/('rollout-'+thread+'.jsonl')).write_text(json.dumps({
        'type': 'session_meta', 'payload': {'id': thread, 'source': source}})+'\n')


def test_worker_continuation_uses_verified_root_owner(tmp_path, monkeypatch):
    import process_hud.codex_bridge as bridge
    session_metadata(tmp_path, 'root')
    session_metadata(tmp_path, 'worker', 'root')
    session_metadata(tmp_path, 'nested', 'worker')
    assert continuation_owner('nested', tmp_path) == ('root', ['nested', 'worker', 'root'])
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    monkeypatch.setenv('CODEX_THREAD_ID', 'nested')
    monkeypatch.setenv('PROCESS_HUD_CODEX_INSTANCE', 'test-relay')
    monkeypatch.setattr(bridge, 'endpoints', lambda: [{'instance': 'test-relay',
        'socket': '/unused', 'threads': ['root', 'worker', 'nested']}])
    monkeypatch.setattr(bridge, 'call', lambda *a: {'initialized': True})
    origin = current_origin()
    assert origin['thread_id'] == 'root'
    assert origin['source_thread_id'] == 'nested'
    assert origin['parent_thread_id'] == 'worker'


def test_cyclic_or_missing_subagent_parent_is_rejected(tmp_path):
    session_metadata(tmp_path, 'worker', 'absent')
    with pytest.raises(ValueError, match='unique'):
        continuation_owner('worker', tmp_path)
    session_metadata(tmp_path, 'absent', 'worker')
    with pytest.raises(ValueError, match='owner'):
        continuation_owner('worker', tmp_path)


def test_ambiguous_acknowledgement_is_not_retried(store, tmp_path, monkeypatch):
    import queue
    relay = Relay(tmp_path/'bridge', [])
    relay.initialized = True
    relay.threads.add('thread-source')
    j = terminal_job(store, relay.home)
    calls = []
    def lost(*args):
        calls.append(args)
        raise queue.Empty
    monkeypatch.setattr(relay, 'send_rpc', lost)
    assert relay.deliver(store.home, j['id'])['state'] == 'uncertain'
    assert relay.deliver(store.home, j['id'])['state'] == 'uncertain'
    assert len(calls) == 1


def test_rejected_rpc_can_be_retried_without_duplicate_acceptance(store, tmp_path, monkeypatch):
    relay = Relay(tmp_path/'bridge', [])
    relay.initialized = True
    relay.threads.add('thread-source')
    j = terminal_job(store, relay.home)
    calls = []
    def rpc(*args):
        calls.append(args)
        return {'error': {'code': -1}} if len(calls) == 1 else {'result': {'turn': {'id': 'accepted'}}}
    monkeypatch.setattr(relay, 'send_rpc', rpc)
    assert relay.deliver(store.home, j['id'])['state'] == 'pending'
    assert relay.deliver(store.home, j['id'])['state'] == 'accepted'
    assert relay.deliver(store.home, j['id'])['state'] == 'accepted'
    assert len(calls) == 2


def test_managed_completion_dispatches_without_a_model(relay, store):
    from process_hud.runtime import launch
    home, metadata, _, _ = relay
    origin = {'thread_id': 'thread-source', 'bridge_home': str(home), 'instance': metadata['instance']}
    job = launch(store, 'managed completion', [sys.executable, '-c', 'print("result retained")'],
                 ui=False, codex_origin=origin)
    deadline = time.monotonic()+8
    while time.monotonic() < deadline:
        final = store.get(job['id'])
        if final.get('codex_delivery', {}).get('state') == 'accepted':
            break
        time.sleep(.05)
    assert final['state'] == 'succeeded'
    assert final['codex_delivery']['state'] == 'accepted'
    assert not final['removed_at']
    store.remove(job['id'])
    assert 'result retained' in store.report(job['id'])['log_tail']


def test_independent_guard_reports_observer_death_without_replaying_work(relay, store, tmp_path):
    import signal
    from process_hud.runtime import launch
    home, metadata, _, _ = relay
    started, done = tmp_path/'started', tmp_path/'done'
    script = ('import time; from pathlib import Path; Path('+repr(str(started))+').write_text("once");'
              'time.sleep(1.5); Path('+repr(str(done))+').write_text("done")')
    origin = {'thread_id': 'thread-source', 'bridge_home': str(home), 'instance': metadata['instance']}
    job = launch(store, 'observer loss', [sys.executable, '-c', script], ui=False, codex_origin=origin)
    deadline = time.monotonic()+5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert started.read_text() == 'once'
    os.kill(store.get(job['id'])['worker']['pid'], signal.SIGKILL)
    store.mutate(job['id'], lambda j: j.update(created_at=time.time()-10))
    while time.monotonic() < deadline:
        event = store.get(job['id']).get('codex_events', {}).get('observer_lost', {})
        if event.get('state') == 'accepted' and done.exists():
            break
        time.sleep(.05)
    assert event['state'] == 'accepted'
    assert not store.get(job['id'])['finished']
    assert done.read_text() == 'done' and started.read_text() == 'once'


def test_dispatcher_waits_through_a_missing_owner_then_delivers(relay, store):
    from process_hud.codex_bridge import enqueue
    home, metadata, send, receive = relay
    job = terminal_job(store, home, state='failed', thread='later-owner')
    enqueue(store, job)
    time.sleep(.15)
    assert store.get(job['id']).get('codex_delivery', {}).get('state') != 'accepted'
    send({'id': 'load-later', 'method': 'thread/resume', 'params': {'threadId': 'later-owner'}})
    assert receive()['id'] == 'load-later'
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        final = store.get(job['id'])
        if final.get('codex_delivery', {}).get('state') == 'accepted':
            break
        time.sleep(.03)
    assert final['codex_delivery']['state'] == 'accepted'
