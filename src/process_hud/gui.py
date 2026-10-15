"""Native Qt desktop view. Import only when GUI is actually requested."""
from __future__ import annotations
import json
import os
import sys
import threading
import time
from PySide6.QtCore import QAbstractListModel, QModelIndex, QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPen, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QListView,
    QMenu, QPlainTextEdit, QPushButton, QStyledItemDelegate, QStyle, QToolTip, QVBoxLayout, QWidget)
from .model import duration, place_details, public_progress, summary
from .store import Store
from .util import identity, lock_file

ROLE = Qt.ItemDataRole.UserRole + 1
ROW = 30
COLORS = {'running': '#a9caff', 'starting': '#a9caff', 'succeeded': '#99d8ba',
          'failed': '#ffb1b1', 'cancelled': '#ffdc98', 'unknown': '#ffdc98', 'paused': '#ffdc98'}


class TaskModel(QAbstractListModel):
    def __init__(self):
        super().__init__(); self.jobs = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.jobs)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.jobs):
            return None
        j = self.jobs[index.row()]
        if role == ROLE:
            return j
        if role == Qt.ItemDataRole.DisplayRole:
            return j['title']
        if role == Qt.ItemDataRole.ToolTipRole:
            return j['title']+' — '+summary(j)
        return None

    def refresh(self, jobs):
        if [j['id'] for j in jobs] != [j['id'] for j in self.jobs]:
            self.beginResetModel(); self.jobs = jobs; self.endResetModel()
        else:
            self.jobs = jobs
            if jobs:
                self.dataChanged.emit(self.index(0, 0), self.index(len(jobs)-1, 0), [ROLE, Qt.ItemDataRole.DisplayRole])


class TaskDelegate(QStyledItemDelegate):
    @staticmethod
    def remove_rect(row_rect):
        return QRect(row_rect.right()-26, row_rect.center().y()-10, 20, 20)

    def sizeHint(self, option, index):
        return QSize(240, ROW)

    def paint(self, p, option, index):
        j = index.data(ROLE)
        if not j:
            return
        p.save(); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(1, 1, -1, -1)
        if option.state & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Selected):
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(242, 248, 255, 30)); p.drawRoundedRect(r, 7, 7)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(COLORS.get(j['state'], '#ffdc98')))
        p.drawEllipse(QPoint(option.rect.left()+11, option.rect.center().y()), 3, 3)
        remove = self.remove_rect(option.rect)
        if getattr(self.parent(), 'hover_remove', None) == j['id']:
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(242, 248, 255, 45))
            p.drawRoundedRect(QRectF(remove), 5, 5)
        p.setPen(QColor(237, 243, 251, 210 if option.state & QStyle.StateFlag.State_MouseOver else 135))
        font = QFont('Sans Serif'); font.setPixelSize(15); p.setFont(font)
        p.drawText(remove, Qt.AlignmentFlag.AlignCenter, '×')
        font = QFont('Sans Serif'); font.setPixelSize(11); p.setFont(font)
        content_right = remove.left()-5
        text = summary(j); sw = min(p.fontMetrics().horizontalAdvance(text)+8,
                                  int((content_right-option.rect.left())*.56))
        sr = QRect(content_right-sw, option.rect.top(), sw, option.rect.height())
        p.setPen(QColor('#eff4fc')); p.drawText(sr, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        font.setPixelSize(12); font.setWeight(QFont.Weight.Medium); p.setFont(font)
        nr = QRect(option.rect.left()+25, option.rect.top(),
                   max(10, sr.left()-option.rect.left()-33), option.rect.height())
        label = p.fontMetrics().elidedText(j['title'], Qt.TextElideMode.ElideRight, max(10, nr.width()))
        p.setPen(QColor('#f4f6f9')); p.drawText(nr, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        p.restore()

    def helpEvent(self, event, view, option, index):
        if self.remove_rect(option.rect).contains(event.pos()):
            QToolTip.showText(event.globalPos(), 'Убрать запись; процесс продолжит работу', view)
            return True
        return super().helpEvent(event, view, option, index)


class TaskListView(QListView):
    def __init__(self, hud):
        super().__init__(hud)
        self.hud = hud
        self.remove_press = None
        self.hover_remove = None
        # Child widgets must not inherit the HUD header's move/resize cursor.
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def mouseMoveEvent(self, event):
        self.update_hover(event.position().toPoint())
        super().mouseMoveEvent(event)

    def update_hover(self, pos=None):
        pos = self.viewport().mapFromGlobal(QCursor.pos()) if pos is None else pos
        self.hover_remove = self.remove_id_at(pos) if self.viewport().rect().contains(pos) else None
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if self.hover_remove
                                  else Qt.CursorShape.ArrowCursor)
        self.viewport().update()

    def leaveEvent(self, event):
        self.hover_remove = None
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self.viewport().update()
        super().leaveEvent(event)

    def remove_id_at(self, pos):
        index = self.indexAt(pos)
        if index.isValid() and TaskDelegate.remove_rect(self.visualRect(index)).contains(pos):
            job = index.data(ROLE)
            return job['id'] if job else None
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.remove_press = self.remove_id_at(event.position().toPoint())
            if self.remove_press:
                self.hud.activateWindow()
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                event.accept(); return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.remove_press:
            job_id = self.remove_press
            self.remove_press = None
            if event.button() == Qt.MouseButton.LeftButton and self.remove_id_at(event.position().toPoint()) == job_id:
                self.hud.remove_record(job_id)
            event.accept(); return
        super().mouseReleaseEvent(event)


class GlassWindow(QWidget):
    def __init__(self, alpha):
        # Independent top-level window, never parented into the compact HUD's geometry.
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.alpha = alpha
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = min(.95, self.alpha + (.07 if self.underMouse() and self.alpha < .5 else 0))
        p.setBrush(QColor(15, 20, 31, round(alpha*255)))
        p.setPen(QPen(QColor(237, 245, 255, 40), 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 12, 12)


class Details(GlassWindow):
    def __init__(self, hud):
        super().__init__(.86)
        self.hud = hud
        self.task_id = None
        self.setWindowTitle('Подробности задачи')
        self.setStyleSheet('''QLabel { color: #dae4f0; background: transparent; }
            QPushButton { color: #edf3f9; background: rgba(255,255,255,22); border: 0; border-radius: 6px; padding: 7px; }
            QPushButton:hover { background: rgba(255,255,255,40); }
            QPlainTextEdit { color: #cbd9eb; background: rgba(0,0,0,50); border: 0; border-radius: 6px; padding: 7px; }
            QMenu { background: #202735; color: white; }''')
        lay = QVBoxLayout(self); lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(9)
        head = QHBoxLayout()
        self.title = QLabel(); self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True); self.title.setStyleSheet('font-size: 14px; font-weight: 600;')
        self.title.setMinimumWidth(0)
        self.dismiss = QPushButton('×'); self.dismiss.setFixedSize(27, 27); self.dismiss.clicked.connect(self.hide)
        self.dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        head.addWidget(self.title, 1); head.addWidget(self.dismiss); lay.addLayout(head)
        self.meta = QLabel(); self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setWordWrap(True); self.meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.meta.setStyleSheet('font-size: 11px;')
        lay.addWidget(self.meta)
        self.logs = QPlainTextEdit(); self.logs.setReadOnly(True); self.logs.setMaximumBlockCount(1000)
        self.logs.setFont(QFont('Monospace', 9)); self.logs.setMinimumHeight(60)
        lay.addWidget(self.logs, 1)
        actions = QHBoxLayout()
        copy = QPushButton('Копировать отчёт'); copy.clicked.connect(self.copy_report)
        remove = QPushButton('Убрать запись'); remove.clicked.connect(self.remove_record)
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        actions.addWidget(copy); actions.addWidget(remove); lay.addLayout(actions)
        self.message = QLabel('Удаление записи не останавливает процесс.'); self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText); self.message.setStyleSheet('font-size: 10px; color: #9cabbe;')
        lay.addWidget(self.message)
        self.resize(405, 370)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.hide)

    def open_task(self, task_id):
        self.task_id = task_id
        self.refresh()
        self.reposition()
        self.show(); self.raise_()

    def reposition(self):
        screen = self.hud.screen() or QApplication.primaryScreen()
        if not screen:
            return
        b, a = self.hud.geometry(), screen.availableGeometry()
        x, y, w, h = place_details((b.x(),b.y(),b.width(),b.height()), (405,370),
                                   (a.x(),a.y(),a.width(),a.height()))
        self.setGeometry(x,y,w,h)

    def refresh(self):
        if not self.task_id:
            return
        try:
            j = self.hud.store.report(self.task_id, 100)
        except KeyError:
            self.hide(); return
        if j['removed_at']:
            self.hide(); return
        self.title.setText(j['title'])
        elapsed = (j['ended_at'] or time.time())-(j['started_at'] or j['created_at'])
        p = public_progress(j['progress'])
        parts = [summary(j)+'  ·  '+duration(elapsed), j['backend']+'  ·  '+j['id'],
                 'Наблюдение: '+j['observation']]
        if p.get('current') is not None and p.get('total') is not None:
            parts.append(f"{p['current']:g} / {p['total']:g} {p.get('unit','')}")
        if p.get('phase'):
            parts.append(p['phase'])
        if p.get('message'):
            parts.append(p['message'])
        if j.get('exit_code') is not None:
            parts.append('Код завершения: '+str(j['exit_code']))
        if j.get('detail'):
            parts.append(j['detail'])
        deliveries = [e['state'] for e in j.get('codex_events', {}).values()]
        if 'uncertain' in deliveries:
            parts.append('Передача события Codex не подтверждена; проверьте исходный диалог.')
        elif 'expired' in deliveries:
            parts.append('Срок доставки события Codex истёк; требуется ручная проверка.')
        self.meta.setText('\n'.join(parts))
        log = j['log_tail'] or 'Доступного вывода пока нет.'
        if log != self.logs.toPlainText():
            bar = self.logs.verticalScrollBar(); follow = bar.value() >= bar.maximum()-3
            old = bar.value(); self.logs.setPlainText(log)
            bar.setValue(bar.maximum() if follow else old)

    def copy_report(self):
        if self.task_id:
            QApplication.clipboard().setText(json.dumps(self.hud.store.report(self.task_id, 60), ensure_ascii=False, indent=2))

    def remove_record(self):
        if self.task_id:
            self.hud.remove_record(self.task_id)


class Hud(GlassWindow):
    def __init__(self, store):
        super().__init__(float(store.setting('opacity')))
        self.store = store; self.drag_offset = None; self.resizing = False
        self.undo_removals = []
        self.undo_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Undo), self,
                                      activated=self.undo_remove)
        self.undo_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.undo_shortcut.setEnabled(False)
        self.quit_epoch = store.setting('ui_quit', 0)
        self.quitting = False
        self.last_reconcile = 0
        self.setWindowTitle('Process HUD')  # OS identity only; no painted product title.
        self.setMinimumWidth(215)
        self.model = TaskModel()
        self.list_view = TaskListView(self)
        self.list_view.setModel(self.model); self.list_view.setItemDelegate(TaskDelegate(self.list_view))
        self.list_view.setUniformItemSizes(True); self.list_view.setMouseTracking(True)
        self.list_view.setFrameShape(QFrame.Shape.NoFrame)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list_view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.list_view.setStyleSheet('''QListView { background: transparent; border: 0; outline: 0; }
            QScrollBar:vertical { background: transparent; width: 4px; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,55); border-radius: 2px; min-height: 20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }''')
        self.list_view.viewport().setAutoFillBackground(False)
        self.list_view.clicked.connect(self.choose)
        self.list_view.activated.connect(self.choose)
        self.close_btn = QPushButton('×', self); self.close_btn.setFixedSize(18,18)
        self.close_btn.setStyleSheet('QPushButton { color: #edf3f9; background: rgba(20,25,36,200); border: 0; border-radius: 8px; }')
        self.close_btn.setToolTip('Скрыть; процессы продолжат работать')
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.hide_all); self.close_btn.hide()
        self.empty = QLabel('Нет задач', self); self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty.setStyleSheet('color: rgba(238,244,255,160); font-size: 11px; background: transparent;')
        self.details = Details(self)
        screen = QApplication.primaryScreen()
        a = screen.availableGeometry() if screen else QRect(0,0,1280,720)
        width = int(store.setting('width'))
        self.resize(width, 44)
        pos = store.setting('position')
        self.move(*(pos if pos else [a.right()-width-28, a.top()+58]))
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(750)
        self.refresh()
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.hide_all)
        for screen in QApplication.screens():
            screen.availableGeometryChanged.connect(self.fit_screen)
        QApplication.instance().screenRemoved.connect(self.fit_screen)
        QApplication.instance().screenAdded.connect(self.fit_screen)

    def choose(self, index):
        j = index.data(ROLE)
        if not j:
            return
        if self.details.isVisible() and self.details.task_id == j['id']:
            self.details.hide()
        else:
            self.details.open_task(j['id'])

    def remove_record(self, job_id):
        if self.store.get(job_id)['removed_at'] is not None:
            return
        removed = self.store.remove(job_id)
        self.undo_removals.append((job_id, removed['removed_at']))
        self.undo_removals = self.undo_removals[-100:]
        self.undo_shortcut.setEnabled(True)
        if self.details.task_id == job_id:
            self.details.hide()
        self.refresh()

    def undo_remove(self):
        while self.undo_removals:
            job_id, removed_at = self.undo_removals.pop()
            job = self.store.get(job_id)
            if job['removed_at'] != removed_at:
                continue
            self.store.restore(job_id)
            self.refresh()
            # Observer/bridge recovery can include bounded subprocess startup.
            # Keep it outside Qt's event loop; it never re-executes the workload.
            from .runtime import recover_job
            threading.Thread(target=recover_job, args=(self.store, job_id),
                             kwargs={'restored': True}, daemon=True).start()
            break
        self.undo_shortcut.setEnabled(bool(self.undo_removals))

    def refresh(self):
        if self.store.setting('ui_quit', 0) != self.quit_epoch:
            self.quitting = True
            QApplication.quit(); return
        jobs = self.store.list()
        self.model.refresh(jobs)
        self.empty.setVisible(not jobs)
        self.alpha = float(self.store.setting('opacity'))
        self.fit_screen()
        self.list_view.update_hover()
        visible = self.store.setting('visible')
        if visible and not self.isVisible():
            self.show()
        elif not visible and self.isVisible():
            self.hide(); self.details.hide()
        if self.details.isVisible():
            self.details.refresh()
        self.update()
        # No external calls or process spawning in Qt event loop. Recovery is explicit
        # via CLI ensure/recover so that network trouble can never freeze the HUD.

    def fit_screen(self, *args):
        screen = QApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        if not screen:
            return
        a = screen.availableGeometry()
        width = min(int(self.store.setting('width')), max(215,a.width()-16))
        wanted = 14 + (len(self.model.jobs)*ROW if self.model.jobs else ROW)
        height = min(wanted, a.height()-16)
        self.resize(width, height)
        x = max(a.left()+8, min(self.x(), a.right()-width-7))
        y = max(a.top()+8, min(self.y(), a.bottom()-height-7))
        self.move(x,y)
        if self.details.isVisible():
            self.details.reposition()

    def resizeEvent(self, event):
        self.list_view.setGeometry(4, 9, self.width()-8, max(0,self.height()-14))
        self.empty.setGeometry(4, 9, self.width()-8, ROW)
        self.close_btn.move(self.width()-19, 0)
        super().resizeEvent(event)

    def enterEvent(self, event):
        self.close_btn.show(); self.update(); super().enterEvent(event)

    def leaveEvent(self, event):
        self.close_btn.hide(); self.update()
        if not self.resizing and self.drag_offset is None:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if event.position().x() >= self.width()-7:
                self.resizing = True
            elif event.position().y() < 9:
                self.drag_offset = event.globalPosition().toPoint()-self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.globalPosition().toPoint()
        if self.resizing:
            w = max(215, min(800, pos.x()-self.x()))
            self.store.configure(width=w); self.fit_screen()
        elif self.drag_offset is not None:
            self.move(pos-self.drag_offset)
            if self.details.isVisible():
                self.details.reposition()
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor if event.position().x() >= self.width()-7 else
                           (Qt.CursorShape.SizeAllCursor if event.position().y()<9 else Qt.CursorShape.ArrowCursor))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.resizing = False; self.drag_offset = None
        self.fit_screen(); self.store.configure(position=[self.x(),self.y()])
        super().mouseReleaseEvent(event)

    def hide_all(self):
        self.store.configure(visible=False); self.details.hide(); self.hide()

    def closeEvent(self, event):
        if self.quitting:
            event.accept(); return
        event.ignore(); self.hide_all()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet('QMenu { background: #202735; color: white; padding: 5px; }')
        hide = menu.addAction('Скрыть плашку'); hide.triggered.connect(self.hide_all)
        undo = menu.addAction('Вернуть убранную запись\tCtrl+Z')
        undo.setEnabled(bool(self.undo_removals)); undo.triggered.connect(self.undo_remove)
        for key, label in [('notify', 'Уведомления'), ('sound', 'Звук')]:
            act = menu.addAction(label); act.setCheckable(True); act.setChecked(self.store.setting(key))
            act.triggered.connect(lambda enabled, k=key: self.store.configure(**{k: enabled}))
        menu.exec(event.globalPos())


def run(store: Store):
    try:
        with lock_file(store.home/'ui.lock', blocking=False):
            app = QApplication.instance() or QApplication(sys.argv[:1])
            app.setApplicationName('Process HUD'); app.setQuitOnLastWindowClosed(False)
            ref = identity(os.getpid())
            hud = Hud(store)
            store.configure(ui_identity=ref)
            try:
                return app.exec()
            finally:
                # Do not overwrite a newer owner's identity after an unusual recovery.
                if store.setting('ui_identity') == ref:
                    store.configure(ui_identity=None)
    except BlockingIOError:
        return 0
