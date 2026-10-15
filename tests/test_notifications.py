from process_hud import notify


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
