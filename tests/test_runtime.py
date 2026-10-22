import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import pytest
from process_hud.runtime import attach, environment, launch, reconcile
from process_hud.util import alive
from conftest import await_finished


def test_real_success(store):
    j=launch(store,'success',[sys.executable,'-c','print("HELLO")'],ui=False)
    assert j['target']['argv'][0] == sys.executable
    final=await_finished(store,j['id'])
    assert final['state']=='succeeded' and final['exit_code']==0
    assert store.report(j['id'])['log_tail']=='HELLO'


def test_failure(store):
    j=launch(store,'failure',[sys.executable,'-c','import sys; print("error"); sys.exit(7)'],ui=False)
    assert await_finished(store,j['id'])['exit_code']==7


def test_external_signal_is_an_interruption_with_real_wait_status(store):
    j = launch(store, 'signal', [sys.executable, '-c',
        'import os,signal; os.kill(os.getpid(), signal.SIGTERM)'], ui=False)
    final = await_finished(store, j['id'])
    assert final['state'] == 'cancelled' and final['exit_code'] == -signal.SIGTERM


def test_exec_error_after_validation_is_a_failed_receipt(store, tmp_path):
    executable = tmp_path/'missing-interpreter'
    executable.write_text('#!/no/such/interpreter\n')
    executable.chmod(0o700)
    j = launch(store, 'exec failure', [str(executable)], ui=False)
    final = await_finished(store, j['id'])
    assert final['state'] == 'failed' and final['exit_code'] is None
    assert final['evidence'] == 'exec failed before workload start'


def test_detached_from_cli_and_closed_stdin(store,tmp_path):
    marker=tmp_path/'done'
    script='import time; from pathlib import Path; time.sleep(1.2); Path('+repr(str(marker))+').write_text("done")'
    r=subprocess.run([sys.executable,'-m','process_hud','--home',str(store.home),'run',
        '--title','detached','--no-ui','--',sys.executable,'-c',script],env=environment(store),
        capture_output=True,text=True,timeout=6)
    assert r.returncode==0, r.stderr
    jid=json.loads(r.stdout)['id']
    assert await_finished(store,jid)['state']=='succeeded'
    assert marker.read_text()=='done'


def test_surviving_child_and_child_failure(store,tmp_path):
    marker=tmp_path/'child_done'
    child='import time,sys;from pathlib import Path;time.sleep(.8);Path('+repr(str(marker))+').write_text("yes");sys.exit(9)'
    parent='import subprocess,sys;subprocess.Popen([sys.executable,"-c",'+repr(child)+'])'
    j=launch(store,'orphan-child',[sys.executable,'-c',parent],ui=False)
    final=await_finished(store,j['id'])
    assert marker.exists()
    assert final['state']=='failed' and final['exit_code']==9


def test_removed_running_job_finishes(store,tmp_path):
    marker=tmp_path/'done'
    j=launch(store,'remove-me',[sys.executable,'-c','import time;from pathlib import Path;time.sleep(.7);Path('+repr(str(marker))+').touch()'],ui=False)
    store.remove(j['id'])
    final=await_finished(store,j['id'])
    assert marker.exists() and final['state']=='succeeded'
    assert store.list()==[]


def test_tagged_progress_and_terminal_injection_ignored(store):
    script='import time;print(\'@@HUD {"current": 0, "total": 10}\',flush=True);print(\'@@HUD {"current": 5, "state":"succeeded"}\',flush=True);time.sleep(.4);raise SystemExit(4)'
    j=launch(store,'progress',[sys.executable,'-c',script],ui=False)
    final=await_finished(store,j['id'])
    assert final['state']=='failed'
    assert final['progress']['percent']==50


def test_tagged_attention_does_not_claim_failure_or_completion(store):
    script = 'print(\'@@HUD {"attention":"need a decision", "state":"failed"}\', flush=True)'
    job = launch(store, 'explicit attention', [sys.executable, '-c', script], ui=False)
    final = await_finished(store, job['id'])
    assert final['state'] == 'succeeded'
    assert final['attention_request']['reason'] == 'need a decision'


def test_percent_parser_is_opt_in(store):
    j=launch(store,'not-parsed',[sys.executable,'-c','print("83%")'],ui=False)
    assert await_finished(store,j['id'])['progress']=={}
    j=launch(store,'parsed',[sys.executable,'-c','print("83%")'],parser='percent',ui=False)
    assert await_finished(store,j['id'])['progress']['percent']==83


def test_pid_quiet_then_unknown(store):
    with subprocess.Popen([sys.executable,'-c','import time;time.sleep(1.2)']) as p:
        j=attach(store,'attached','pid',{'pid':p.pid},ui=False)
        time.sleep(.3)
        assert store.get(j['id'])['state']=='running'
        p.wait()
    final=await_finished(store,j['id'])
    assert final['state']=='unknown' and final['exit_code'] is None


def test_early_restore_restarts_only_a_detached_pid_observer(store):
    from process_hud.runtime import recover_job
    with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)']) as p:
        job = attach(store, 'PID undo', 'pid', {'pid': p.pid}, ui=False)
        old_worker = job['worker']
        store.remove(job['id'])
        end = time.monotonic()+2
        while alive(old_worker) is True and time.monotonic() < end:
            time.sleep(.02)
        assert alive(old_worker) is False
        assert time.time()-job['created_at'] < 4
        store.restore(job['id']); recover_job(store, job['id'], restored=True)
        new = store.get(job['id'])
        assert new['worker'] != old_worker and new['target']['identity'] == job['target']['identity']
        assert p.poll() is None
        p.wait()
    assert await_finished(store, job['id'])['state'] == 'unknown'


def test_idempotent_run_only_once(store,tmp_path):
    path=tmp_path/'counter'
    args=[sys.executable,'-c','from pathlib import Path; p=Path('+repr(str(path))+'); p.write_text(p.read_text()+"x" if p.exists() else "x")']
    a=launch(store,'once',args,key='one-key',ui=False)
    b=launch(store,'once',args,key='one-key',ui=False)
    assert a['id']==b['id']
    await_finished(store,a['id'])
    assert path.read_text()=='x'


def test_invalid_exec_never_creates_task(store):
    with pytest.raises(ValueError):
        launch(store,'bad',['/no/such/binary'],ui=False)
    assert store.list()==[]


def test_no_shell_interpolation(store,tmp_path):
    command='echo SHOULD_NOT_EXECUTE'
    j=launch(store,'literal',[sys.executable,'-c','import sys;print(sys.argv[1])',command],ui=False)
    await_finished(store,j['id'])
    assert store.report(j['id'])['log_tail']==command


def test_crashed_supervisor_never_reexecutes(store,tmp_path):
    marker=tmp_path/'counter'
    script='import time;from pathlib import Path;p=Path('+repr(str(marker))+');p.write_text("once");time.sleep(1.4)'
    j=launch(store,'lost-owner',[sys.executable,'-c',script],ui=False)
    end=time.monotonic()+3
    while not marker.exists() and time.monotonic()<end:
        time.sleep(.04)
    j=store.get(j['id'])
    os.kill(j['worker']['pid'],signal.SIGKILL)
    time.sleep(.1)
    store.mutate(j['id'],lambda x:x.update(created_at=time.time()-10))
    reconcile(store)
    final=await_finished(store,j['id'])
    assert final['state']=='unknown'
    assert marker.read_text()=='once'


@pytest.mark.parametrize('original', [None, '/tmp/a project/python:/tmp/another'])
def test_workload_pythonpath_not_contaminated(store, monkeypatch, original):
    monkeypatch.delenv('_PROCESS_HUD_CALLER_PYTHONPATH', raising=False)
    if original is None:
        monkeypatch.delenv('PYTHONPATH', raising=False)
    else:
        monkeypatch.setenv('PYTHONPATH', original)
    script = 'import os,json;print(json.dumps({"path":os.getenv("PYTHONPATH"),"internal":os.getenv("_PROCESS_HUD_CALLER_PYTHONPATH")}))'
    j = launch(store, 'isolated-pythonpath', [sys.executable, '-c', script], ui=False)
    assert await_finished(store, j['id'])['state'] == 'succeeded'
    result = json.loads(store.report(j['id'])['log_tail'])
    assert result == {'path': original, 'internal': None}


def test_relative_path_search_uses_workload_cwd(store, tmp_path, monkeypatch):
    caller = tmp_path/'caller'; caller.mkdir()
    workdir = tmp_path/'project'; (workdir/'bin').mkdir(parents=True)
    executable = workdir/'bin'/'project-command'
    executable.write_text('#!'+sys.executable+'\nprint("project executable")\n')
    executable.chmod(0o700)
    monkeypatch.chdir(caller)
    monkeypatch.setenv('PATH', 'bin')
    job = launch(store, 'relative PATH', ['project-command'], cwd=workdir, ui=False)
    assert await_finished(store, job['id'])['state'] == 'succeeded'
    assert store.report(job['id'])['log_tail'] == 'project executable'


def test_pid_final_log_snapshot_is_not_delayed_until_next_poll(store, tmp_path, monkeypatch):
    from process_hud import runtime
    from process_hud.adapters import Observation
    source = tmp_path/'producer.log'
    source.write_text('initial output\n')
    job, _ = store.create('PID final output', 'pid', {'log_file': str(source)})
    calls = []
    def observe(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            return Observation()
        source.write_text('initial output\nfinal output\n')
        return Observation('unknown', 'lost', True, evidence='PID disappeared')
    monkeypatch.setattr(runtime.adapters, 'observe', observe)
    runtime.watch(store, job)
    assert store.report(job['id'])['log_tail'].endswith('final output')
    assert store.get(job['id'])['state'] == 'unknown'


def test_snapshot_write_failure_preserves_observed_terminal_receipt(store, monkeypatch):
    from process_hud import runtime
    from process_hud.adapters import Observation
    job, _ = store.create('backend receipt', 'docker', {'container': 'fixture'})
    observations = iter([Observation('succeeded', 'complete', True, 0,
                                     evidence='backend exit status', log='last output')])
    monkeypatch.setattr(runtime.adapters, 'observe', lambda *a, **kw: next(observations))
    real_open = runtime.os.open
    def denied(path, *args, **kwargs):
        if Path(path).name == 'output.log':
            raise OSError('fixture log disk full')
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(runtime.os, 'open', denied)
    runtime.watch(store, job)
    final = store.get(job['id'])
    assert final['finished'] and final['state'] == 'succeeded' and final['exit_code'] == 0
    assert 'log' in final['detail'].lower()


@pytest.mark.parametrize('metadata_failure', [False, True])
def test_managed_log_failure_keeps_draining_and_supervising(store, tmp_path, monkeypatch, metadata_failure):
    from process_hud import runtime
    marker = tmp_path/'workload-completed'
    job, _ = store.create('disk full', 'managed', {'argv': [sys.executable, '-c',
        'import sys; from pathlib import Path; sys.stdout.write("x"*200000); '
        'sys.stdout.flush(); Path('+repr(str(marker))+').touch()'], 'cwd': str(tmp_path)})
    class FailedLog:
        def __init__(self, *args): pass
        def write(self, data): raise OSError('fixture disk full')
        def close(self): pass
    monkeypatch.setattr(runtime, 'RotatingLog', FailedLog)
    monkeypatch.setattr(runtime, '_subreaper', lambda: None)
    update = store.update
    failed_updates = []
    def transient_failure(job_id, **values):
        if (metadata_failure and values.get('capabilities', {}).get('logs') is False
                and not failed_updates):
            failed_updates.append(None)
            raise sqlite3.OperationalError('fixture metadata temporarily unavailable')
        return update(job_id, **values)
    monkeypatch.setattr(store, 'update', transient_failure)
    runtime.managed(store, job)
    final = store.get(job['id'])
    assert marker.exists()
    assert final['finished'] and final['state'] == 'succeeded' and final['exit_code'] == 0
    assert final['capabilities']['logs'] is False
    assert 'fixture disk full' in final['detail']


@pytest.mark.parametrize('events, expected', [
    ([{'current': 2, 'total': 4}, {'percent': 75}], {'percent': 75}),
    ([{'phase': 'one', 'current': 2, 'total': 4}, {'phase': 'two'}], {'phase': 'two', 'percent': None}),
    ([{'indeterminate': True}, {'percent': 35}], {'percent': 35}),
    ([{'percent': 25}, {'percent': 200}], {'percent': 25}),
])
def test_stdout_progress_batch_respects_sequential_updates(store, events, expected):
    output = '\n'.join('@@HUD '+json.dumps(event) for event in events)
    job = launch(store, 'batched progress', [sys.executable, '-c', 'print('+repr(output)+')'], ui=False)
    final = await_finished(store, job['id'])
    assert final['state'] == 'succeeded'
    for key, value in expected.items():
        assert final['progress'].get(key) == value
    if events[-1].get('percent') == 75:
        assert 'current' not in final['progress']


def test_undo_between_removed_read_and_detach_keeps_observing(store, monkeypatch):
    from process_hud import runtime
    from process_hud.adapters import Observation
    job, _ = store.create('restore read race', 'pid')
    store.remove(job['id'])
    read = store.get
    intercepted = []
    def undo_after_read(job_id):
        result = read(job_id)
        if not intercepted:
            intercepted.append(None)
            store.restore(job_id)
        return result
    monkeypatch.setattr(store, 'get', undo_after_read)
    monkeypatch.setattr(runtime.adapters, 'observe',
                        lambda *a, **kw: Observation('unknown', 'lost', True))
    runtime.watch(store, job)
    assert read(job['id'])['finished']


def test_restore_hands_off_from_observer_committed_to_detach(store, tmp_path, monkeypatch):
    from process_hud import runtime
    from process_hud.util import identity
    detaching = threading.Event(); release = threading.Event()
    mutate = store.mutate
    intercepted = []
    def paused_detach(job_id, fn):
        result = mutate(job_id, fn)
        if fn.__name__ == 'detach' and result.get('worker_stopping') and not intercepted:
            intercepted.append(None)
            detaching.set()
            assert release.wait(4), 'fixture detach barrier was not released'
        return result
    monkeypatch.setattr(store, 'mutate', paused_detach)
    done = tmp_path/'fixture-done'
    script = ('import time; from pathlib import Path; end=time.monotonic()+5\n'
              'while not Path('+repr(str(done))+').exists() and time.monotonic()<end: time.sleep(.01)')
    with subprocess.Popen([sys.executable, '-c', script]) as p:
        target = {'pid': p.pid, 'identity': identity(p.pid)}
        job, _ = store.create('restore exit race', 'pid', target)
        store.remove(job['id'])
        previous = threading.Thread(target=runtime.worker, args=(store, job['id']))
        previous.start()
        try:
            assert detaching.wait(2)
            old_worker = store.get(job['id'])['worker']
            store.restore(job['id'])
            timer = threading.Timer(.1, release.set); timer.start()
            runtime.recover_job(store, job['id'], restored=True)
            replacement = store.get(job['id'])
            assert replacement['worker'] != old_worker
            assert replacement['target'] == target and replacement['worker_ready']
            assert p.poll() is None
        finally:
            release.set(); previous.join(3); done.touch()
        p.wait()
    assert await_finished(store, job['id'])['state'] == 'unknown'
