"""Session regression tests; no microphone, model download or personal settings."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import app


class Capture(threading.Thread):
    def __init__(self, label, device, queue, t0, *args):
        super().__init__(daemon=True)
        self.label, self.device, self.queue, self.t0 = label, device, queue, t0
        self.done = threading.Event()
        self.ready = threading.Event()
        self.error = None

    def run(self):
        self.ready.set()
        self.done.wait()
        self.queue.put((self.label, time.time() - self.t0, [0] * 1600))

    def stop(self):
        self.done.set()


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for target, value in [
            ('settings.PATH', self.root / 'settings.json'),
            ('app.OUT_DIR', self.root / 'default'),
            ('app.audio.SourceCapture', Capture),
            ('app.audio.system_loopback', lambda *_: (SimpleNamespace(name='Fake'), False)),
            ('app.audio.mic', lambda *_: (SimpleNamespace(name='Fake mic'), False)),
        ]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        self.engine = SimpleNamespace(chunk=(1, 2), name='Fake', device='cpu', live_ok=False,
            transcribe=lambda *a, **k: 'Texto simulado.')
        self.session = app.Session()
        self.session.get_model = lambda _: self.engine
        p = patch.object(app, 'session', self.session); p.start(); self.addCleanup(p.stop)
        self.client = TestClient(app.app)
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.session.stop()
        if self.session.worker:
            self.session.worker.join(3)

    def wait(self, state):
        deadline = time.monotonic() + 4
        while self.session.state != state and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.session.state, state)

    def start(self):
        self.assertEqual(self.client.post('/start', json={'title':'Prueba'}).status_code, 200)
        self.wait('recording')

    def test_pause_resume_finish_new_session(self):
        self.start()
        path = self.session.path
        self.assertEqual(self.client.post('/start', json={}).status_code, 409)
        self.assertEqual(self.client.post('/new-session').status_code, 409)
        self.client.post('/pause'); self.wait('paused')
        self.assertIsNotNone(self.session.file)
        frozen = self.session.paused_at - self.session.t0
        time.sleep(.15)
        self.client.post('/resume')
        self.assertLess(abs((time.time() - self.session.t0) - frozen), .06)
        self.assertEqual(self.session.path, path)
        self.client.post('/stop'); self.wait('finished')
        self.assertIsNone(self.session.file)
        text = path.read_text(encoding='utf-8')
        self.assertIn('Pausa', text); self.assertIn('Reanudación', text)
        self.assertEqual(text.count('Texto simulado.'), 2)
        self.client.post('/new-session')
        self.assertEqual(self.session.history, [])
        self.start()
        self.assertNotEqual(self.session.path, path)
        self.client.post('/stop'); self.wait('finished')
        self.assertEqual(path.read_text(encoding='utf-8'), text)

    def test_folder_validation_and_next_session(self):
        folder = self.root / 'Carpeta con espacios y ñ'
        response = self.client.post('/settings', json={'output_dir':str(folder)})
        self.assertEqual(response.status_code, 200)
        self.start()
        path = self.session.path
        self.assertEqual(path.parent, folder)
        self.client.post('/settings', json={'output_dir':str(self.root / 'other')})
        self.assertEqual(self.session.path, path)
        blocked = self.root / 'file'; blocked.write_text('x')
        previous = app.settings.load()['output_dir']
        self.assertEqual(self.client.post('/settings', json={'output_dir':str(blocked)}).status_code, 400)
        self.assertEqual(app.settings.load()['output_dir'], previous)
        with patch('app.tempfile.TemporaryFile', side_effect=PermissionError('denied')):
            self.assertEqual(self.client.post('/settings', json={'output_dir':str(folder)}).status_code, 400)
        self.client.post('/pause'); self.wait('paused')
        self.client.post('/stop'); self.wait('finished')
        self.client.post('/settings', json={'output_dir':''})
        self.assertEqual(app.output_dir(), app.OUT_DIR)

    def test_failed_start_and_cancel_loading(self):
        self.session.get_model = lambda _: (_ for _ in ()).throw(RuntimeError('No model'))
        self.client.post('/start', json={}); self.wait('finished')
        self.assertIn('No model', self.session.status_text)
        self.client.post('/new-session')
        gate = threading.Event()
        self.session.get_model = lambda _: (gate.wait(2), self.engine)[1]
        self.client.post('/start', json={})
        self.client.post('/stop'); gate.set(); self.wait('finished')
        self.assertIsNone(self.session.file)

    def test_capture_failure_drains_started_sources(self):
        self.session.sources['Yo']['enabled'] = True
        with patch('app.audio.mic', side_effect=RuntimeError('Mic unavailable')):
            self.client.post('/start', json={}); self.wait('finished')
        self.assertIsNone(self.session.file)
        self.assertIn('Texto simulado.', self.session.path.read_text(encoding='utf-8'))

    def test_existing_name_never_appended(self):
        from datetime import datetime
        fixed = datetime(2026, 1, 1, 12, 0, 0)
        folder = app.OUT_DIR
        folder.mkdir()
        old = folder / '2026-01-01_120000 Prueba.txt'
        old.write_text('Original', encoding='utf-8')
        with patch('app.datetime') as clock:
            clock.now.return_value = fixed
            self.start()
            self.assertEqual(self.session.path.name, '2026-01-01_120000 Prueba (1).txt')
            self.client.post('/stop'); self.wait('finished')
        self.assertEqual(old.read_text(encoding='utf-8'), 'Original')

    def test_reconnect_snapshot(self):
        self.start(); self.client.post('/pause'); self.wait('paused')
        with self.client.websocket_connect('/ws') as ws:
            snapshot = ws.receive_json()
        self.assertEqual(snapshot['state'], 'paused')
        self.assertEqual(snapshot['session_id'], self.session.session_id)
        self.assertEqual(snapshot['path'], str(self.session.path))
        self.assertTrue(snapshot['history'])


if __name__ == '__main__':
    unittest.main()
