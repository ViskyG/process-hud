"""A real small workload with explicit progress. Run through process-hud run."""
import argparse
import time
from process_hud.api import Reporter

p = argparse.ArgumentParser()
p.add_argument('--seconds', type=float, default=12)
p.add_argument('--fail', action='store_true')
a = p.parse_args()
r = Reporter()
for n in range(41):
    r.update(current=n, total=40, phase='Демонстрация', unit='steps', force=True)
    if n % 10 == 0:
        print(f'Processed {n} / 40 steps', flush=True)
    time.sleep(max(0,a.seconds)/41)
if a.fail:
    raise SystemExit(3)
print('Demo completed', flush=True)
