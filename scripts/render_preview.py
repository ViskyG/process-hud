#!/usr/bin/env python3
"""Render the actual Qt HUD with labelled demo data; no desktop or session capture."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = '2'

import argparse
from pathlib import Path
import tempfile
import time
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication
from process_hud.gui import Hud
from process_hud.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path, nargs='?', default=Path('docs/images/process-hud.png'))
    args = parser.parse_args()
    app = QApplication([])
    with tempfile.TemporaryDirectory(prefix='process-hud-preview-') as folder:
        store = Store(folder)
        store.configure(notify=False, sound=False, position=[100, 100])
        for title, current, total, eta in [('Сборка интерфейса', 68, 100, 24),
                                           ('Тесты API', 42, 60, 12),
                                           ('Импорт отчётов', 840, 1200, 36)]:
            job, _ = store.create(title, 'external')
            store.update(job['id'], state='running', started_at=time.time())
            store.set_progress(job['id'], {'current': current, 'total': total, 'eta_seconds': eta})
        job, _ = store.create('Архив проекта', 'external')
        store.finish(job['id'], 'succeeded', code=0)
        hud = Hud(store); hud.timer.stop(); hud.show(); app.processEvents()
        shot = hud.grab().toImage(); shot.setDevicePixelRatio(1)
        canvas = QImage(shot.width()+96, shot.height()+80, QImage.Format.Format_ARGB32)
        canvas.fill(QColor('#202735'))
        painter = QPainter(canvas)
        painter.drawImage(48, 40, shot); painter.end()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        assert canvas.save(str(args.output))
        hud.quitting = True; hud.details.hide(); hud.close()
    print(args.output)


if __name__ == '__main__':
    main()
