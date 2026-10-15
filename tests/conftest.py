import os
import sys
import time
from pathlib import Path
import pytest
from process_hud.store import Store
from process_hud.util import alive

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path/'state')
    s.configure(notify=False, sound=False, poll_seconds=.2)
    yield s
    for job in s.list():
        s.remove(job['id'])
    # Tests create only bounded tiny workloads. A test must not silently leak one.
    deadline = time.monotonic()+8
    while time.monotonic()<deadline:
        if all(alive(j.get('worker')) is not True and alive(j.get('codex_guard')) is not True
               for j in s.list(True)):
            break
        time.sleep(.05)


def await_finished(store, jid, timeout=10):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        job = store.get(jid)
        if job['finished']:
            return job
        time.sleep(.05)
    raise AssertionError(store.report(jid))
