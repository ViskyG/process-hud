"""Producer instrumentation. No background thread and no model dependency."""
from __future__ import annotations
import json
import os
import time


class Reporter:
    """Print structured progress at most a few times per second, even outside a HUD.

    Use Reporter().update(current=n, total=total, phase='Import', unit='rows').
    The managed supervisor extracts only progress; exit status remains OS-owned.
    """
    def __init__(self, interval: float = .25):
        self.interval = max(.1, interval)
        self.last = float('-inf')

    def update(self, *, current=None, total=None, percent=None, phase=None,
               unit=None, message=None, eta_seconds=None, force=False):
        now = time.monotonic()
        if not force and now-self.last < self.interval:
            return
        event = {k: v for k, v in locals().items() if k in
                 ('current','total','percent','phase','unit','message','eta_seconds') and v is not None}
        # Fail on non-serializable/nonfinite inputs before writing partial JSON.
        text = '@@HUD '+json.dumps(event, ensure_ascii=False, allow_nan=False)
        print(text, flush=True)
        self.last = now

    def attention(self, reason: str):
        """Explicitly request a decision; does not imply failure or stop work."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Provide a nonempty attention reason')
        print('@@HUD '+json.dumps({'attention': reason}, ensure_ascii=False), flush=True)


def report_to_task(task_id: str, *, home=None, **event):
    """Use from an explicitly registered external producer on this machine."""
    from .store import Store
    return Store(home).set_progress(task_id, event)
