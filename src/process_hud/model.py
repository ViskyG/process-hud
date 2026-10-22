from __future__ import annotations
import math
import time
from .util import clean

TERMINAL = {'succeeded', 'failed', 'cancelled'}
STATES = {'starting', 'running', 'paused', 'unknown'} | TERMINAL


def number(value, name: str, *, minimum: float = 0, maximum: float = float('inf')) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a number')
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f'{name} out of range') from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{name} out of range')
    return value


def progress(previous: dict | None, event: dict, now: float | None = None) -> dict:
    """An estimate is an estimate, never terminal evidence. Invalid input is rejected."""
    now = time.time() if now is None else now
    previous = previous or {}
    if not isinstance(event, dict):
        raise ValueError('Progress event must be an object')
    phase = clean(event.get('phase', previous.get('phase', '')), 160)
    unit = clean(event.get('unit', previous.get('unit', '')), 40)
    same_phase = phase == previous.get('phase', '') and unit == previous.get('unit', '')
    old = previous if same_phase else {}
    p = {'phase': phase, 'unit': unit, 'message': clean(event.get('message', old.get('message', '')), 500),
         'source': clean(event.get('source', old.get('source', 'explicit')), 100),
         'updated_at': now, 'percent': None, 'eta_seconds': None, 'rate': None}
    if event.get('indeterminate') is True:
        return p
    current = event.get('current') if 'percent' in event else event.get('current', old.get('current'))
    total = event.get('total') if 'percent' in event else event.get('total', old.get('total'))
    if current is not None:
        current = number(current, 'current')
        p['current'] = current
    if total is not None:
        total = number(total, 'total', minimum=1e-12)
        p['total'] = total
    reported_percent = event.get('percent')
    if reported_percent is not None:
        reported_percent = number(reported_percent, 'percent', maximum=100)
    if current is not None and total is not None:
        if current > total:
            raise ValueError('current exceeds total')
        p['percent'] = current / total * 100
        if reported_percent is not None and not math.isclose(p['percent'], reported_percent, abs_tol=1e-7):
            raise ValueError('percent conflicts with current/total')
    elif reported_percent is not None:
        p['percent'] = reported_percent
    elif not any(k in event for k in ('current', 'total', 'percent')):
        p['percent'] = old.get('percent')
    # Measurements, not message-only heartbeats, drive a rate sample.
    measured = ('current' in event and current is not None) or 'percent' in event
    p['measurement_at'] = now if measured else old.get('measurement_at', old.get('updated_at', now))
    value = current if current is not None and total is not None else p['percent']
    extent = total if current is not None and total is not None else 100.0
    signature = ('units', total, unit) if current is not None and total is not None else ('percent', 100, '')
    sample = old.get('_sample')
    # A changed total or measurement basis invalidates the previous estimate,
    # including updates which only supply a new total and no current counter.
    if sample and (tuple(sample['signature']) != signature or value is None):
        sample = None
    if measured and value is not None:
        base = sample if sample and tuple(sample['signature']) == signature and value >= sample['value'] else None
        if base and now > base['time'] and value > base['value']:
            delta = now-base['time']
            if delta >= 1:
                rate = (value-base['value'])/delta
                previous_rate = old.get('rate')
                if previous_rate:
                    rate = .35*rate + .65*previous_rate
                age = now-base['origin']
                # At least 3 seconds and 2 increasing observations; no early precision theatre.
                count = base.get('count', 1)+1
                p['_sample'] = {'value': value, 'time': now, 'origin': base['origin'],
                                'count': count, 'signature': signature}
                p['rate'] = rate
                if count >= 3 and age >= 3 and value < extent:
                    p['eta_seconds'] = (extent-value)/rate
            else:
                p['_sample'] = base
                p['rate'] = old.get('rate')
                p['eta_seconds'] = old.get('eta_seconds')
        elif base:
            p['_sample'] = base
            # No increasing work: keep no ETA; absence of progress is NOT a hang warning.
        else:
            p['_sample'] = {'value': value, 'time': now, 'origin': now,
                            'count': 1, 'signature': signature}
    elif sample:
        p['_sample'] = sample
        p['rate'] = old.get('rate')
        p['eta_seconds'] = old.get('eta_seconds')
    if event.get('eta_seconds') is not None:
        p['eta_seconds'] = number(event['eta_seconds'], 'eta_seconds', maximum=365*86400)
        p['eta_source'] = 'reported'
        p['measurement_at'] = now
    elif p['eta_seconds'] is not None:
        p['eta_source'] = old.get('eta_source', 'rate_estimate') if not measured else 'rate_estimate'
    return p


def public_progress(p: dict | None, now: float | None = None) -> dict:
    p = {k: v for k, v in (p or {}).items() if not k.startswith('_')}
    now = time.time() if now is None else now
    # Never keep a stale countdown looking authoritative. Preserve percent as last observed.
    if now-p.get('measurement_at', p.get('updated_at', now)) > 30:
        p['eta_seconds'] = None
    return p


def duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f'{seconds} с'
    if seconds < 3600:
        return f'{round(seconds/60)} мин'
    return f'{seconds//3600} ч {(seconds%3600)//60} мин'


def summary(job: dict, now: float | None = None) -> str:
    state = job['state']
    if state in TERMINAL:
        return {'succeeded': 'готово', 'failed': 'ошибка', 'cancelled': 'прервано'}[state]
    if job.get('observation') == 'disconnected':
        return 'нет связи'
    if state == 'unknown':
        return 'неизвестно'
    if state == 'paused':
        return 'приостановлено'
    p = public_progress(job.get('progress'), now)
    values = []
    if p.get('percent') is not None:
        values.append(f"{p['percent']:.0f}%")
    if p.get('eta_seconds') is not None:
        values.append('~'+duration(p['eta_seconds']))
    return ' · '.join(values) or ('запуск' if state == 'starting' else 'выполняется')


def place_details(hud: tuple[int, int, int, int], size: tuple[int, int],
                  screen: tuple[int, int, int, int], gap: int = 9) -> tuple[int, int, int, int]:
    """Pure geometry, including monitors with negative coordinates."""
    x, y, width, height = hud
    sx, sy, sw, sh = screen
    dw, dh = min(size[0], sw-16), min(size[1], sh-16)
    if x-dw-gap >= sx+8:
        dx, dy = x-dw-gap, y
    elif x+width+gap+dw <= sx+sw-8:
        dx, dy = x+width+gap, y
    else:
        dx, dy = x, y+height+gap
    return max(sx+8, min(dx, sx+sw-dw-8)), max(sy+8, min(dy, sy+sh-dh-8)), dw, dh
