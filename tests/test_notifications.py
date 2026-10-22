from process_hud import notify
import json


def test_no_notification_when_disabled(store,monkeypatch):
    j,_=store.create('t','external');j=store.finish(j['id'],'succeeded',code=0)
    calls=[];monkeypatch.setattr(notify,'tool',lambda *a,**k:calls.append(a))
    notify.completed(store,j)
    assert calls==[]


def test_gnome_quiet_mode_no_fallback_sound(store,monkeypatch):
    store.configure(notify=True,sound=True)
    j,_=store.create('t','external');j=store.finish(j['id'],'succeeded',code=0)
    calls=[]
    monkeypatch.setattr(notify.shutil,'which',lambda k:'/bin/'+k)
    def tool(args,**kwargs):
        calls.append(args)
        if args[0]=='gdbus': return 0,"(['body'],)",''
        if args[0]=='gsettings': return 0,'false',''
        return 0,'',''
    monkeypatch.setattr(notify,'tool',tool)
    notify.completed(store,j)
    assert any(c[0]=='gdbus' and 'org.freedesktop.Notifications.Notify' in c for c in calls)
    assert not any(c[0]=='canberra-gtk-play' for c in calls)
    count=len(calls);notify.completed(store,j)
    assert len(calls)==count


def test_sound_hint_uses_typed_gnome_notification(store,monkeypatch):
    store.configure(notify=True,sound=True)
    j,_=store.create('t','external');j=store.finish(j['id'],'succeeded',code=0)
    calls=[]
    monkeypatch.setattr(notify.shutil,'which',lambda k:'/bin/'+k)
    def tool(args,**kwargs):
        calls.append(args)
        if any('GetCapabilities' in arg for arg in args): return 0,"(['sound'],)",''
        return 0,'',''
    monkeypatch.setattr(notify,'tool',tool)
    notify.completed(store,j)
    sent=[c for c in calls if 'org.freedesktop.Notifications.Notify' in c]
    assert len(sent)==1 and any("'sound-name': <'complete'>" in arg for arg in sent[0])
    assert not any(c[0]=='notify-send' for c in calls)


def test_dbus_arguments_preserve_quoted_titles_and_option_like_text(store, monkeypatch):
    store.configure(notify=True, sound=False)
    job, _ = store.create("'quoted'", 'external')
    job = store.finish(job['id'], 'succeeded', code=0)
    calls = []
    monkeypatch.setattr(notify.shutil, 'which', lambda name: '/bin/'+name)
    monkeypatch.setattr(notify, 'summary', lambda job: '--help')
    monkeypatch.setattr(notify, 'tool', lambda args, **kwargs: (calls.append(args) or (0, '', '')))
    notify.completed(store, job)
    call = next(c for c in calls if 'org.freedesktop.Notifications.Notify' in c)
    args = call[call.index('org.freedesktop.Notifications.Notify')+1:]
    assert args[0] == '--'
    assert json.loads(args[4]) == "'quoted'"
    assert json.loads(args[5]) == '--help'
