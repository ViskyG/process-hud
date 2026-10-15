import json
import os
from pathlib import Path
import signal
import subprocess
import sys
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
