"""Exercise pywebview event dispatch, without opening windows or real sessions."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from webview.event import Event

import app
from desktop import Desktop


def fake_window(*args, **kwargs):
    window = Mock()
    window.events = SimpleNamespace(closing=Event(window, should_lock=True))
    return window


class DesktopEventsTest(unittest.TestCase):
    def setUp(self):
        self.stop = Mock(return_value={'state':'finishing'})
        self.probe = Mock(return_value={'active':False})
        with patch('webview.create_window', side_effect=fake_window):
            self.desktop = Desktop('http://127.0.0.1:8791', self.stop, Mock(), Mock(), self.probe)

    def test_widget_close_discards_dict_and_cancels_destruction(self):
        with patch('webview.event.logger.exception') as errors:
            # True from Event.set means a callback returned False: cancel destruction.
            self.assertTrue(self.desktop.widget.events.closing.set())
        errors.assert_not_called()
        self.stop.assert_called_once()
        self.desktop.widget.hide.assert_called_once()

    def test_manager_close_does_not_stop_capture(self):
        with patch('webview.event.logger.exception') as errors:
            self.assertTrue(self.desktop.manager.events.closing.set())
        errors.assert_not_called()
        self.stop.assert_not_called()
        self.desktop.manager.hide.assert_called_once()

    def test_exit_allows_both_windows_to_be_destroyed(self):
        self.desktop.exiting = True
        with patch('webview.event.logger.exception') as errors:
            self.assertFalse(self.desktop.widget.events.closing.set())
            self.assertFalse(self.desktop.manager.events.closing.set())
        errors.assert_not_called()
        self.stop.assert_not_called()


class LifespanTest(unittest.TestCase):
    def test_startup_and_shutdown_are_managed_by_lifespan(self):
        queue, probe, session, repository = Mock(), Mock(), Mock(), Mock()
        session.worker = None
        with patch.object(app,'repository',repository), patch.object(app,'probe',probe), \
             patch.object(app,'session',session), patch.object(app,'analysis_queue',queue), \
             patch.object(app,'get_analysis_queue',return_value=queue):
            with TestClient(app.app):
                repository.list.assert_called_once()
                queue.start.assert_called_once()
                self.assertIsNotNone(app.loop)
                self.assertFalse(app.shutting_down)
            queue.stop.assert_called_once()
            probe.stop.assert_called_once()
            session.stop.assert_called_once()
            self.assertIsNone(app.loop)
        app.shutting_down = False


class DialogTest(unittest.TestCase):
    def test_native_dialogs_use_file_dialog_enum(self):
        import webview
        window = Mock()
        window.create_file_dialog.return_value = None
        with patch.object(app,'window',window), patch.object(app,'desktop',None):
            app.browse_output()
            self.assertEqual(window.create_file_dialog.call_args.args[0],webview.FileDialog.FOLDER)
            app.browse_dict()
            self.assertEqual(window.create_file_dialog.call_args.args[0],webview.FileDialog.OPEN)
            app.import_model()
            self.assertEqual(window.create_file_dialog.call_args.args[0],webview.FileDialog.OPEN)
            with self.assertRaises(app.HTTPException):
                app.open_session(app.OpenSessionReq())
            self.assertEqual(window.create_file_dialog.call_args.args[0],webview.FileDialog.OPEN)


class WidgetGeometryTest(unittest.TestCase):
    def test_compact_restore_dpi_drag_and_idempotence(self):
        class Size:
            def __init__(self,width,height):self.Width,self.Height=width,height
        class Point:
            def __init__(self,x,y):self.X,self.Y=x,y
        class Form:
            def __init__(self,scale):
                self.DeviceDpi=round(96*scale)
                self.MinimumSize=Size(round(300*scale),round(360*scale))
                self.MaximumSize=Size(0,0)
                self.Size=Size(round(440*scale),round(620*scale))
                self.Location=Point(100,100)
            @property
            def Width(self):return self.Size.Width
            @property
            def Height(self):return self.Size.Height
            @property
            def Left(self):return self.Location.X
            @property
            def Top(self):return self.Location.Y
            def Invoke(self,callback):callback()
        area=SimpleNamespace(Left=0,Top=0,Right=2000,Bottom=1600,Width=2000,Height=1600)
        modules={'System':SimpleNamespace(Action=lambda fn:fn),
                 'System.Drawing':SimpleNamespace(Size=Size,Point=Point),
                 'System.Windows.Forms':SimpleNamespace(Screen=SimpleNamespace(FromControl=lambda form:SimpleNamespace(WorkingArea=area)))}
        for scale in (1,1.5,2):
            with self.subTest(scale=scale),patch('webview.create_window',side_effect=fake_window),patch.dict('sys.modules',modules):
                shell=Desktop('http://localhost',Mock(),Mock(),Mock(),Mock())
                form=Form(scale);shell.widget.native=form
                self.assertTrue(shell.set_compact(True))
                self.assertEqual((form.Width,form.Height),(round(320*scale),round(64*scale)))
                shell.set_compact(True)
                form.Location=Point(500,400)
                shell.set_compact(False)
                self.assertEqual((form.Width,form.Height),(round(440*scale),round(620*scale)))
                self.assertEqual((form.Left,form.Top),(500,min(400,1600-round(620*scale))))
                shell.set_compact(False)
                self.assertEqual((form.Width,form.Height),(round(440*scale),round(620*scale)))
                shell.set_compact(True)
                form.Location=Point(9000,9000)
                shell.set_compact(False)
                self.assertLessEqual(form.Left+form.Width,area.Right)
                self.assertLessEqual(form.Top+form.Height,area.Bottom)


if __name__ == '__main__':
    unittest.main()
