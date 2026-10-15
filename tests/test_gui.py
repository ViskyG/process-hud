"""Optional native Qt checks. Offscreen geometry is NOT GNOME compositor evidence."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
pytest.importorskip('PySide6', reason='PySide6 not installed; native UI NOT tested in this environment')
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from process_hud.gui import Hud, TaskDelegate

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_native_growth_and_independent_flyout(store,app):
    hud=Hud(store);hud.show();app.processEvents()
    h0=hud.height()
    for i in range(3):
        store.create('row '+str(i),'external')
    hud.refresh();app.processEvents();hud.fit_screen();app.processEvents()
    assert len(hud.model.jobs)==3
    assert hud.height()>h0
    h3=hud.height()
    hud.choose(hud.model.index(0,0));app.processEvents()
    assert hud.details.isWindow() and hud.details.parentWidget() is None
    assert hud.height()==h3
    assert hud.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert hud.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    hud.hide_all();app.processEvents()
    store.create('new hidden row','external');hud.refresh();app.processEvents()
    assert not hud.isVisible()
    store.configure(visible=True);hud.refresh();app.processEvents()
    assert hud.isVisible()
    hud.timer.stop();hud.details.hide();hud.hide();hud.deleteLater();app.processEvents()


def test_row_cross_removes_record_without_changing_task_state(store,app):
    first,_=store.create('keep running','external')
    store.update(first['id'],state='running')
    second,_=store.create('remove with cross','external')
    store.update(second['id'],state='running')
    hud=Hud(store);hud.show();app.processEvents()
    index=next(hud.model.index(i,0) for i,j in enumerate(hud.model.jobs) if j['id']==second['id'])
    QTest.mouseClick(hud.list_view.viewport(),Qt.MouseButton.LeftButton,
                     pos=hud.list_view.visualRect(index).topLeft()+QPoint(35,15))
    app.processEvents()
    assert hud.details.isVisible()
    point=TaskDelegate.remove_rect(hud.list_view.visualRect(index)).center()
    QTest.mouseClick(hud.list_view.viewport(),Qt.MouseButton.LeftButton,pos=point)
    app.processEvents()
    removed=store.get(second['id'])
    assert removed['removed_at'] is not None and removed['state']=='running'
    assert not hud.details.isVisible()
    assert [j['id'] for j in store.list()]==[first['id']]
    hud.timer.stop();hud.hide();hud.deleteLater();app.processEvents()


def test_cross_cursor_and_undo_restore_a_running_row(store, app, monkeypatch):
    import process_hud.runtime as runtime
    monkeypatch.setattr(runtime, 'recover_job', lambda *args, **kwargs: None)
    job, _ = store.create('running undo', 'external')
    store.update(job['id'], state='running')
    hud = Hud(store); hud.show(); hud.activateWindow(); app.processEvents()
    index = hud.model.index(0, 0)
    viewport = hud.list_view.viewport()
    cross = TaskDelegate.remove_rect(hud.list_view.visualRect(index)).center()
    QTest.mouseMove(hud, QPoint(30, 4)); app.processEvents()
    assert hud.cursor().shape() == Qt.CursorShape.SizeAllCursor
    QTest.mouseMove(viewport, QPoint(35, 15)); app.processEvents()
    assert viewport.cursor().shape() == Qt.CursorShape.ArrowCursor
    QTest.mouseMove(viewport, cross); app.processEvents()
    assert viewport.cursor().shape() == Qt.CursorShape.PointingHandCursor
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=cross); app.processEvents()
    assert store.get(job['id'])['removed_at'] is not None
    assert viewport.cursor().shape() == Qt.CursorShape.ArrowCursor
    QTest.keyClick(viewport, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier); app.processEvents()
    restored = store.get(job['id'])
    assert restored['removed_at'] is None and restored['state'] == 'running'
    assert [j['id'] for j in hud.model.jobs] == [job['id']]
    assert not hud.undo_shortcut.isEnabled()
    hud.timer.stop(); hud.details.hide(); hud.hide(); hud.deleteLater(); app.processEvents()


def test_undo_restores_in_reverse_order_and_skips_already_restored_rows(store, app, monkeypatch):
    import process_hud.runtime as runtime
    monkeypatch.setattr(runtime, 'recover_job', lambda *args, **kwargs: None)
    first, _ = store.create('first', 'external')
    second, _ = store.create('second', 'external')
    hud = Hud(store); app.processEvents()
    hud.remove_record(first['id']); hud.remove_record(second['id'])
    store.restore(second['id'])
    hud.undo_remove()
    assert {j['id'] for j in store.list()} == {first['id'], second['id']}
    assert not hud.undo_shortcut.isEnabled()
    hud.timer.stop(); hud.details.hide(); hud.hide(); hud.deleteLater(); app.processEvents()


def test_technical_gui_exit_preserves_visibility(store, app):
    store.configure(visible=True)
    hud = Hud(store); hud.show(); app.processEvents()
    hud.timer.stop(); hud.quitting = True; hud.close(); app.processEvents()
    assert store.setting('visible') is True
    hud.details.hide(); hud.deleteLater(); app.processEvents()
