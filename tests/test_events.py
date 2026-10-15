import json
import time
from process_hud import events
from process_hud.codex_bridge import Relay


def owner_job(store, **origin):
    job, _ = store.create('attention', 'external', codex_origin={
        'thread_id': 'root', 'bridge_home': str(store.home/'bridge'), **origin})
    return store.update(job['id'], state='running', started_at=time.time())


def test_unknown_outcome_is_attention_not_a_fabricated_result(store, monkeypatch):
    job = owner_job(store)
    store.finish(job['id'], 'unknown', detail='PID disappeared')
    relay = Relay(store.home/'bridge', []); relay.initialized = True; relay.threads.add('root')
    sent = []
    monkeypatch.setattr(relay, 'send_rpc', lambda m, p: sent.append(p) or {'result': {'turn': {'id': 'one'}}})
    assert relay.deliver(store.home, job['id'], 'outcome_unknown')['state'] == 'accepted'
    output = json.loads(sent[0]['toolOutput']['output'])
    assert output['state'] == 'unknown' and output['result_available'] is False
    assert sent[0]['toolOutput']['name'] == 'process_hud_attention'
    assert relay.deliver(store.home, job['id'], 'outcome_unknown')['state'] == 'accepted'
    assert len(sent) == 1


def test_connection_grace_recovers_without_a_false_alert(store):
    job = owner_job(store)
    store.update(job['id'], state='unknown', observation='disconnected')
    first = events.sync(store, job['id'], 100)
    assert not events.pending(first)
    assert not events.pending(events.sync(store, job['id'], 129))
    warned = events.sync(store, job['id'], 130)
    assert [e['kind'] for e in events.pending(warned)] == ['connection_lost']
    store.update(job['id'], state='running', observation='live')
    recovered = events.sync(store, job['id'], 131)
    assert not events.pending(recovered)
    assert recovered['codex_events']['connection_lost']['state'] == 'superseded'
    assert not recovered['codex_health']
    store.update(job['id'], state='unknown', observation='disconnected')
    assert not events.pending(events.sync(store, job['id'], 200))
    retried = events.sync(store, job['id'], 231)
    assert [e['kind'] for e in events.pending(retried)] == ['connection_lost']


def test_deadline_and_explicit_input_request_never_finish_or_stop_work(store):
    job = owner_job(store, attention_after=60)
    started = job['started_at']
    assert not events.pending(events.sync(store, job['id'], started+59))
    warned = events.sync(store, job['id'], started+60)
    assert warned['state'] == 'running' and not warned['finished']
    assert [e['kind'] for e in events.pending(warned)] == ['deadline']
    store.set_progress(job['id'], {'attention': 'need a decision'})
    requested = events.sync(store, job['id'], started+61)
    request = next(e for e in events.pending(requested) if e['kind'] == 'needs_attention')
    store.acknowledge(job['id'], request['event'])
    assert store.get(job['id'])['attention_request'] is None
    assert not store.get(job['id'])['removed_at']
    store.finish(job['id'], 'succeeded', code=0)
    final = events.sync(store, job['id'])
    assert [e['kind'] for e in events.pending(final)] == ['completion']


def test_remove_dismisses_and_undo_rearms_only_undelivered_events(store):
    job = owner_job(store)
    store.finish(job['id'], 'failed', code=7)
    ready = events.sync(store, job['id'])
    event = ready['codex_events']['completion']['event']
    store.remove(job['id'])
    assert not events.pending(events.sync(store, job['id']))
    store.restore(job['id'])
    assert events.pending(events.sync(store, job['id']))[0]['event'] == event
    events.delivered(store, job['id'], 'completion', {'state': 'accepted', 'event': event})
    store.remove(job['id']); events.sync(store, job['id']); store.restore(job['id'])
    assert not events.pending(events.sync(store, job['id']))


def test_observer_death_is_reported_even_when_the_workload_state_is_stale(store):
    job, _ = store.create('lost observer', 'managed', codex_origin={'thread_id': 'root'})
    store.update(job['id'], state='running', created_at=time.time()-10)
    result = events.sync(store, job['id'])
    assert [e['kind'] for e in events.pending(result)] == ['observer_lost']
    assert not result['finished']


def test_relay_restart_preserves_acceptance_and_waits_for_actual_owner(store, monkeypatch):
    job = owner_job(store); store.finish(job['id'], 'failed', code=7)
    home = store.home/'bridge'
    first = Relay(home, []); first.initialized = True
    assert first.deliver(store.home, job['id'])['state'] == 'pending'
    first.threads.add('root')
    calls = []
    rpc = lambda m, p: calls.append(p) or {'result': {'turn': {'id': 'accepted'}}}
    monkeypatch.setattr(first, 'send_rpc', rpc)
    assert first.deliver(store.home, job['id'])['state'] == 'accepted'
    second = Relay(home, []); second.initialized = True; second.threads.add('root')
    monkeypatch.setattr(second, 'send_rpc', rpc)
    assert second.deliver(store.home, job['id'])['state'] == 'accepted'
    assert len(calls) == 1


def test_new_relay_recovers_registered_native_guards_only(store, monkeypatch):
    import process_hud.codex_bridge as bridge
    job = owner_job(store)
    relay = Relay(store.home/'bridge', [])
    with relay.db() as c:
        c.execute('INSERT INTO stores VALUES(?)', (str(store.home),))
    calls = []
    def recover(s, j):
        calls.append((str(s.home), j['id']))
        relay.stopping.set()
    monkeypatch.setattr(bridge, 'enqueue', recover)
    relay.maintain()
    assert calls == [(str(store.home), job['id'])]
