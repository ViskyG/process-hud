"""Durable, deterministic Codex attention events. No model or workload control."""
from __future__ import annotations
import hashlib
import time
from .model import TERMINAL
from .store import Store

SETTLED = {'accepted', 'consumed', 'uncertain', 'dismissed', 'superseded', 'expired'}


def event_id(home, job_id, key):
    # Preserve completion identities accepted by earlier bridge versions.
    suffix = '' if key == 'completion' else '/'+key
    return hashlib.sha256((str(home)+'/'+job_id+suffix).encode()).hexdigest()


def plan(job, now, grace):
    """Return candidate (key, kind) pairs and connection bookkeeping."""
    health = dict(job.get('codex_health') or {})
    if job['removed_at']:
        return [], health
    view = Store.view(job)
    disconnected = not job['finished'] and view.get('observation') == 'disconnected'
    if disconnected:
        health.setdefault('disconnected_since', now)
    else:
        health.pop('disconnected_since', None)
    if job['finished']:
        return ([('completion', 'completion')] if job['state'] in TERMINAL else
                [('outcome_unknown', 'outcome_unknown')]), health
    candidates = []
    if view.get('observation') == 'lost':
        return [('observer_lost', 'observer_lost')], health
    elif disconnected and now-health['disconnected_since'] >= grace:
        return [('connection_lost', 'connection_lost')], health
    request = job.get('attention_request')
    if request:
        return [('attention:'+request['id'], 'needs_attention')], health
    after = (job.get('codex_origin') or {}).get('attention_after')
    if after and now-(job['started_at'] or job['created_at']) >= after:
        candidates.append(('deadline', 'deadline'))
    return candidates, health


def sync(store, job_id, now=None):
    now = time.time() if now is None else now
    grace = float(store.setting('connection_grace_seconds', 30))

    def changes(job):
        candidates, health = plan(job, now, grace)
        valid = dict(candidates)
        events = {k: dict(v) for k, v in (job.get('codex_events') or {}).items()}
        for key, event in events.items():
            if event['state'] == 'pending' and key not in valid:
                event.update(state='dismissed' if job['removed_at'] else 'superseded', updated_at=now)
        for key, kind in candidates:
            if key in events and events[key]['state'] == 'superseded':
                # An undelivered warning can become relevant again (e.g. a later
                # connection loss). Accepted/uncertain/consumed events never rearm.
                events[key].update(state='pending', created_at=now, updated_at=now)
            elif key not in events:
                legacy = job.get('codex_delivery', {}) if key == 'completion' else {}
                state = legacy.get('state', 'pending')
                events[key] = dict(legacy, event=event_id(store.home, job_id, key), key=key,
                                   kind=kind, state=state, created_at=now, updated_at=now)
        return events, health

    job = store.get(job_id)
    if not job.get('codex_origin'):
        return job
    events, health = changes(job)
    if events == job.get('codex_events', {}) and health == job.get('codex_health', {}):
        return job

    def apply(j):
        events, health = changes(j)
        j.update(codex_events=events, codex_health=health)
    return store.mutate(job_id, apply)


def pending(job):
    return [e for e in job.get('codex_events', {}).values() if e['state'] == 'pending']


def delivered(store, job_id, key, result):
    def apply(job):
        event = job.get('codex_events', {}).get(key)
        if event is None or event['state'] in {'consumed', 'dismissed'}:
            return
        for field in ('error', 'error_code', 'reason'):
            event.pop(field, None)
        event.update(result, updated_at=time.time())
        if key == 'completion':
            job['codex_delivery'] = dict(result, updated_at=time.time())
    return store.mutate(job_id, apply)
