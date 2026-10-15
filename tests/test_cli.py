import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from process_hud.cli import main, parser
from process_hud.runtime import ensure_ui


def test_external_producer_lifecycle(store,capsys):
    pre=['--home',str(store.home)]
    assert main(pre+['register','--title','external','--no-ui'])==0
    jid=json.loads(capsys.readouterr().out)['id']
    assert main(pre+['progress',jid,'--current','2','--total','4'])==0
    capsys.readouterr()
    assert main(pre+['finish',jid,'--state','succeeded','--exit-code','0'])==0
    capsys.readouterr()
    assert store.report(jid)['progress']['percent']==50
    assert main(pre+['remove',jid])==0
    assert json.loads(capsys.readouterr().out)['workload_signalled'] is False
    assert store.list()==[]
    assert main(pre+['restore',jid])==0
    capsys.readouterr()
    assert len(store.list())==1


def test_no_workload_control_commands():
    p=parser()
    with pytest.raises(SystemExit):
        p.parse_args(['stop','arbitrary'])


def test_cannot_falsify_managed_success(store,capsys):
    j,_=store.create('managed','managed')
    assert main(['--home',str(store.home),'finish',j['id'],'--state','succeeded'])==2
    assert 'Only external' in capsys.readouterr().err
    assert not store.get(j['id'])['finished']


def test_ui_ensure_does_not_unhide(store,monkeypatch):
    store.configure(visible=False)
    for k in ('DISPLAY','WAYLAND_DISPLAY','QT_QPA_PLATFORM'):
        monkeypatch.delenv(k,raising=False)
    assert ensure_ui(store)['running'] is False
    assert store.setting('visible') is False


def test_bad_config_is_rejected(store,capsys):
    assert main(['--home',str(store.home),'config','--opacity','9'])==2
    assert store.setting('opacity')==.24


def test_skill_cli_examples_parse():
    p=parser()
    cases=[['run','--title','DWH','--key','k','--','python','-m','pytest'],
           ['attach','--title','T','--pid','10'],
           ['attach-docker','--title','T','--container','c'],
           ['attach-k8s','--title','T','--context','ctx','--namespace','n','--job','j'],
           ['run-ssh','--title','T','--host','h','--remote-bin','/bin/hud','--','python3','test.py'],
           ['attach-ssh','--title','T','--host','h','--remote-id','a'*32],
           ['report','a'*32,'--tail','40'],['remove','a'*32],['purge','a'*32,'--yes']]
    for args in cases:
        assert p.parse_args(args).cmd==args[0]


def test_attention_cli_is_opt_in_and_validated(store, capsys):
    prefix = ['--home', str(store.home)]
    assert main(prefix+['register', '--title', 'bad deadline', '--attention-after', '1', '--no-ui']) == 2
    assert 'requires --resume-codex' in capsys.readouterr().err
    assert not store.list()
    job, _ = store.create('producer', 'external')
    assert main(prefix+['attention', job['id'], '--message', 'need input']) == 0
    assert store.get(job['id'])['attention_request']['reason'] == 'need input'
    assert not store.get(job['id'])['finished']
