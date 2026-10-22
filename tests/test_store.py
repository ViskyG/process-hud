import concurrent.futures
import os
import pytest
from process_hud.store import Store
from process_hud.util import RotatingLog, tail


def test_idempotence_and_conflict(store):
    j, new = store.create('one','external',key='same')
    same, new2 = store.create('one','external',key='same')
    assert new and not new2 and same['id'] == j['id']
    with pytest.raises(ValueError):
        store.create('different','external',key='same')


def test_codex_reconnect_does_not_change_operation_identity(store):
    origin = {'thread_id': 'same-thread', 'bridge_home': '/tmp/bridge', 'instance': 'first'}
    j, _ = store.create('one', 'external', key='reconnect', codex_origin=origin)
    same, created = store.create('one', 'external', key='reconnect',
                                 codex_origin=dict(origin, instance='second'))
    assert not created and same['id'] == j['id']


def test_concurrent_creation_and_progress(store):
    def task(n):
        s=Store(store.home)
        j,_ = s.create('task '+str(n),'external')
        s.set_progress(j['id'],{'current':n,'total':30})
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(task,range(20)))
    assert len(store.list()) == 20


def test_tombstone_does_not_resurrect(store):
    j,_=store.create('task','external')
    store.remove(j['id'])
    store.set_progress(j['id'],{'percent':100})
    store.finish(j['id'],'succeeded',code=0)
    assert store.list() == []
    assert store.get(j['id'])['finished']
    store.restore(j['id'])
    assert store.list()[0]['state'] == 'succeeded'


def test_terminal_cannot_be_downgraded(store):
    j,_=store.create('task','external')
    store.finish(j['id'],'failed',code=5)
    store.update(j['id'],state='running')
    assert store.get(j['id'])['state']=='failed'
    with pytest.raises(ValueError):
        store.set_progress(j['id'],{'percent':50})


def test_notify_once(store):
    j,_=store.create('task','external')
    assert store.claim_notification(j['id'])
    assert not store.claim_notification(j['id'])


def test_private_permissions(store):
    assert store.home.stat().st_mode & 0o777 == 0o700
    assert store.db.stat().st_mode & 0o777 == 0o600


def test_bounded_log(tmp_path):
    path=tmp_path/'output.log'
    r=RotatingLog(path,1024,2)
    r.write(b'a'*7000+b'\nLAST\n'); r.close()
    assert sum(p.stat().st_size for p in tmp_path.iterdir())<=3*1024
    assert tail(path,1)=='LAST'


def test_invalid_task_path(store):
    with pytest.raises(ValueError):
        store.task_dir('../../elsewhere')


def test_task_id_and_key_cannot_alias_different_records(store):
    first, _ = store.create('same spec', 'external', key='first')
    second, _ = store.create('same spec', 'external', key='second')
    with pytest.raises(ValueError, match='different tasks'):
        store.create('same spec', 'external', task_id=first['id'], key='second')
    assert len(store.list()) == 2
    same, created = store.create('same spec', 'external', task_id=second['id'], key='second')
    assert not created and same['id'] == second['id']


def test_progress_batch_preserves_order_and_skips_invalid_events_atomically(store):
    job, _ = store.create('producer', 'external')
    result = store.set_progress_batch(job['id'], [
        {'current': 20, 'total': 100, 'phase': 'first'},
        {'percent': 40},
        {'percent': 99, 'attention': ''},
        {'phase': 'next', 'current': 3},
        {'attention': 'needs decision'},
        {'percent': float('nan')},
    ])
    assert result['progress']['current'] == 3
    assert result['progress']['percent'] is None
    assert 'total' not in result['progress']
    assert result['attention_request']['reason'] == 'needs decision'
    request_id = result['attention_request']['id']
    result = store.set_progress_batch(job['id'], [{'attention': 'needs decision'}])
    assert result['attention_request']['id'] == request_id
    store.finish(job['id'], 'succeeded', code=0)
    with pytest.raises(ValueError, match='final receipt'):
        store.set_progress_batch(job['id'], [{'percent': 5}])
    assert store.get(job['id'])['state'] == 'succeeded'


def test_invalid_attention_does_not_partially_apply_batch_progress(store):
    job, _ = store.create('producer', 'external')
    result = store.set_progress_batch(job['id'], [
        {'percent': 10}, {'percent': 99, 'attention': ''},
    ])
    assert result['progress']['percent'] == 10
    assert 'attention_request' not in result
