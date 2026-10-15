import math
import pytest
from process_hud.model import progress, public_progress, place_details, summary

@pytest.mark.parametrize('event', [
    {'percent': -1}, {'percent': 101}, {'percent': float('nan')}, {'current': True},
    {'current': 11, 'total': 10}, {'total': 0}, {'eta_seconds': -1}, {'current':'20'},
    {'total': float('inf')}
])
def test_invalid_progress(event):
    with pytest.raises(ValueError):
        progress({}, event, 100)


def test_eta_has_evidence_and_resets():
    p = progress({}, {'current':0,'total':100,'phase':'A'}, 100)
    assert p['eta_seconds'] is None
    p = progress(p, {'current':10}, 102)
    assert p['eta_seconds'] is None
    p = progress(p, {'current':20}, 104)
    assert p['eta_seconds'] == pytest.approx(16)
    assert public_progress(p, 140)['eta_seconds'] is None
    p = progress(p, {'current':0,'total':50,'phase':'B'}, 106)
    assert p['eta_seconds'] is None
    assert p['percent'] == 0


def test_no_work_no_estimate():
    p = progress({}, {'current':30}, 100)
    assert p['percent'] is None and p['eta_seconds'] is None
    p = progress(p, {'current':30}, 150)
    assert p['eta_seconds'] is None


def test_unknown_without_invented_progress():
    j = {'state':'running','progress':{}}
    assert summary(j) == 'выполняется'
    j['observation'] = 'disconnected'
    assert summary(j) == 'нет связи'


def test_hundred_percent_is_not_done():
    j = {'state':'running','progress':progress({}, {'percent':100})}
    assert summary(j) == '100%'


@pytest.mark.parametrize('screen,hud', [
    ((0,0,1440,900),(1100,60,294,100)),
    ((-2560,0,2560,1440),(-2300,20,294,100)),
    ((0,0,375,812),(8,18,294,100)),
    ((2560,0,2560,1440),(4900,1300,215,130)),
])
def test_popup_screen_bounds(screen,hud):
    x,y,w,h = place_details(hud,(405,370),screen)
    sx,sy,sw,sh = screen
    assert sx <= x and sy <= y and x+w <= sx+sw and y+h <= sy+sh
    if sw >= 1000:
        assert x+w < hud[0] or x > hud[0]+hud[2]


def test_messages_do_not_refresh_stale_eta():
    p=progress({}, {'current':0,'total':100},100)
    p=progress(p,{'current':10},102)
    p=progress(p,{'current':20},104)
    assert p['eta_seconds'] is not None
    p=progress(p,{'message':'Still working'},150)
    assert public_progress(p,150)['eta_seconds'] is None


def test_explicit_percentage_replaces_old_unit_counter():
    p=progress({}, {'current':20,'total':100},100)
    p=progress(p,{'percent':35},102)
    assert p['percent']==35 and 'current' not in p
