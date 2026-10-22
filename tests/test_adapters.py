import json
import os
from pathlib import Path
import sys
import time
import pytest
from process_hud.adapters import docker_snapshot, kubernetes_snapshot, observe, ssh_argv
from process_hud.runtime import attach, run_ssh
from process_hud.store import Store
from process_hud.util import tool
from conftest import await_finished


def docker(state, code=0, **extra):
    return {'Id':'abc123', 'State':{'Status':state,'Running':state=='running','ExitCode':code,**extra}}

@pytest.mark.parametrize('data,state,finished',[
    (docker('running'),'running',False),(docker('exited'),'succeeded',True),
    (docker('exited',5),'failed',True),(docker('running',Paused=True),'paused',False),
    (docker('exited',OOMKilled=True),'failed',True),(docker('exited',Restarting=True),'running',False),
])
def test_docker_states(data,state,finished):
    o=docker_snapshot(data,{'container':'name'})
    assert (o.state,o.finished)==(state,finished)
    assert o.target['container_id']=='abc123'


def test_docker_identity_change():
    o=docker_snapshot(docker('running'),{'container':'name','container_id':'not-the-same'})
    assert o.state=='unknown' and o.finished


def job(conditions=None, **status):
    return {'metadata':{'uid':'uuid1'},'spec':{'completions':10},
            'status':{'conditions':conditions or [],**status}}

@pytest.mark.parametrize('cond,expected', [('Complete','succeeded'),('Failed','failed'),('FailureTarget','running')])
def test_k8s_terminal_conditions(cond,expected):
    o=kubernetes_snapshot(job([{'type':cond,'status':'True'}]),{'name':'x'})
    assert o.state==expected


def test_k8s_failed_pod_is_not_failed_job():
    o=kubernetes_snapshot(job(failed=1,active=1,succeeded=3),{'name':'x'})
    assert o.state=='running' and o.progress['current']==3


def test_k8s_uid_change():
    assert kubernetes_snapshot(job(),{'uid':'other'}).state=='unknown'


@pytest.mark.parametrize('host',['-oProxyCommand=evil','x;evil','name\nother','$(evil)'])
def test_no_ssh_option_or_shell_injection(host):
    with pytest.raises(ValueError):
        ssh_argv({'host':host})


def test_ssh_argv_quoting():
    a=ssh_argv({'host':'user@server','remote_bin':'/home/user/a b/process-hud'})
    assert 'StrictHostKeyChecking=yes' in a
    assert a[-1]=="'/home/user/a b/process-hud' _rpc"


def script(bin_dir,name,body):
    path=bin_dir/name
    path.write_text('#!'+sys.executable+'\n'+body)
    path.chmod(0o700)
    return path


def test_docker_executable_adapter(store,tmp_path,monkeypatch):
    bins=tmp_path/'bin';bins.mkdir()
    state=tmp_path/'docker.json';state.write_text(json.dumps([docker('running')]))
    script(bins,'docker',f'import sys;from pathlib import Path\nif "inspect" in sys.argv: print(Path({str(state)!r}).read_text())\nelse: print("fixture container log")\n')
    monkeypatch.setenv('PATH',str(bins)+os.pathsep+os.environ.get('PATH',''))
    j=attach(store,'docker','docker',{'container':'example'},ui=False)
    state.write_text(json.dumps([docker('exited',7)]))
    final=await_finished(store,j['id'])
    assert final['state']=='failed' and final['exit_code']==7
    assert 'fixture container log' in store.report(j['id'])['log_tail']


def test_k8s_executable_adapter(store,tmp_path,monkeypatch):
    bins=tmp_path/'bin';bins.mkdir()
    state=tmp_path/'k8s.json';state.write_text(json.dumps(job(active=1)))
    script(bins,'kubectl',f'import sys;from pathlib import Path\nif "get" in sys.argv: print(Path({str(state)!r}).read_text())\nelse: print("fixture pod log")\n')
    monkeypatch.setenv('PATH',str(bins)+os.pathsep+os.environ.get('PATH',''))
    j=attach(store,'k8s','kubernetes',{'name':'job','namespace':'default','context':'test-context'},ui=False)
    state.write_text(json.dumps(job([{'type':'Complete','status':'True'}],succeeded=10)))
    assert await_finished(store,j['id'])['state']=='succeeded'


def test_remote_rpc_over_local_ssh_fixture(store,tmp_path,monkeypatch):
    """Actual remote CLI + supervisor; only SSH transport is substituted, explicitly."""
    monkeypatch.setenv('PYTHONPATH',str(Path(__file__).resolve().parents[1]/'src'))
    remote=Store(tmp_path/'remote');remote.configure(notify=False,sound=False,poll_seconds=.2)
    bins=tmp_path/'bin';bins.mkdir()
    script(bins,'ssh',f'import subprocess,sys\nr=subprocess.run([sys.executable,"-m","process_hud","--home",{str(remote.home)!r},"_rpc"],input=sys.stdin.buffer.read(),capture_output=True)\nsys.stdout.buffer.write(r.stdout);sys.stderr.buffer.write(r.stderr);raise SystemExit(r.returncode)\n')
    monkeypatch.setenv('PATH',str(bins)+os.pathsep+os.environ.get('PATH',''))
    j=run_ssh(store,'remote',{'host':'fixture','remote_bin':'process-hud'},
              [sys.executable,'-c','print("remote real command")'],ui=False)
    final=await_finished(store,j['id'],timeout=15)
    assert final['state']=='succeeded'
    assert 'remote real command' in store.report(j['id'])['log_tail']
    assert len(remote.list())==1


def test_helper_bounded_output_and_timeout():
    with pytest.raises(ValueError,match='size limit'):
        tool([sys.executable,'-c','print("x"*100000)'],max_bytes=1024)
    with pytest.raises(TimeoutError):
        tool([sys.executable,'-c','import time;time.sleep(3)'],timeout=.15)


def test_malformed_remote_progress_rejected(monkeypatch):
    from process_hud import adapters
    monkeypatch.setattr(adapters,'remote_rpc',lambda *a:{'schema_version':1,'id':'a'*32,'state':'running',
        'finished':False,'observation':'live','progress':{'percent':'wrong'}})
    with pytest.raises(ValueError):
        observe({'backend':'ssh','target':{'remote_id':'a'*32}})


def test_log_failure_does_not_erase_docker_terminal_evidence(monkeypatch):
    from process_hud import adapters
    monkeypatch.setattr(adapters,'json_tool',lambda *a,**k:[docker('exited',0)])
    def fail(*a,**k): raise TimeoutError('logs blocked')
    monkeypatch.setattr(adapters,'tool',fail)
    o=adapters.observe({'backend':'docker','target':{'container':'c'}},with_logs=True)
    assert o.finished and o.state=='succeeded'
    assert 'Logs unavailable' in o.detail


def test_docker_boolean_exit_code_is_not_success_evidence():
    observation = docker_snapshot(docker('exited', False), {'container': 'fixture'})
    assert observation.state == 'unknown' and observation.code is None


@pytest.mark.parametrize('changes', [
    {'state': 'running', 'finished': True},
    {'state': 'succeeded', 'finished': True, 'exit_code': 7},
    {'schema_version': True},
    {'state': []},
])
def test_remote_inconsistent_receipt_is_rejected(monkeypatch, changes):
    from process_hud import adapters
    result = {'schema_version': 1, 'id': 'a'*32, 'state': 'running',
              'finished': False, 'observation': 'live'} | changes
    monkeypatch.setattr(adapters, 'remote_rpc', lambda *a: result)
    with pytest.raises(ValueError):
        observe({'backend': 'ssh', 'target': {'remote_id': 'a'*32}})


def test_remote_progress_preserves_measurement_age_and_eta_source(monkeypatch):
    from process_hud import adapters
    from process_hud.model import public_progress
    now = time.time()
    measurement = now-40
    result = {'schema_version': 1, 'id': 'a'*32, 'state': 'running',
              'finished': False, 'observation': 'live', 'progress': {
                  'percent': 50, 'eta_seconds': 10, 'eta_source': 'rate_estimate',
                  'updated_at': now, 'measurement_at': measurement}}
    monkeypatch.setattr(adapters, 'remote_rpc', lambda *a: result)
    observation = observe({'backend': 'ssh', 'target': {'remote_id': 'a'*32}})
    assert observation.progress['measurement_at'] == measurement
    assert observation.progress['eta_source'] == 'rate_estimate'
    assert public_progress(observation.progress)['eta_seconds'] is None
