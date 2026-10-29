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
        self.queue.put((self.label, time.monotonic() - self.t0, [0] * 1600))

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
        app.settings.save({'dict_paths':str(self.root / 'empty.tsv')})
        p = patch.object(app, 'fixes', app.corrections.Corrections(str(self.root / 'empty.tsv'))); p.start(); self.addCleanup(p.stop)
        self.engine = SimpleNamespace(chunk=(1, 2), name='Fake', device='cpu', live_ok=False,
            transcribe=lambda *a, **k: 'Texto simulado.')
        self.session = app.Session()
        self.session.get_model = lambda _: self.engine
        p = patch.object(app, 'session', self.session); p.start(); self.addCleanup(p.stop)
        repo = app.SessionRepository(app.output_dir, app.live_session, self.root / 'session-index.json')
        p = patch.object(app, 'repository', repo); p.start(); self.addCleanup(p.stop)
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
        self.assertLess(abs((time.monotonic() - self.session.t0) - frozen), .06)
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
        old = folder / '2026-01-01_120000-Prueba.txt'
        old.write_text('Original', encoding='utf-8')
        with patch('app.datetime') as clock:
            clock.now.return_value = fixed
            self.start()
            self.assertEqual(self.session.path.name, '2026-01-01_120000-Prueba (1).txt')
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

    def test_context_validation_and_prompt(self):
        self.assertEqual(self.client.post('/start', json={'context': 'á' * 2001}).status_code, 422)
        self.assertEqual(self.session.state, 'ready')
        seen = []
        self.engine.transcribe = lambda chunk, language, prompt: (seen.append(prompt), 'Texto.')[1]
        self.client.post('/start', json={'context': 'SGSI · José 😀'})
        self.wait('recording')
        self.client.post('/pause'); self.wait('paused')
        self.assertIn('SGSI · José 😀', seen[0])
        self.client.post('/stop'); self.wait('finished')
        self.client.post('/new-session')
        self.assertEqual(self.session.context, '')

    def test_markers_use_capture_clock_and_reopen(self):
        import json
        self.start()
        self.session.t0 -= 12
        response = self.client.post('/markers', json={'text': 'Acuerdo'}).json()
        self.assertGreaterEqual(response['start'], 12)
        marker_id = response['id']
        self.assertEqual(self.client.patch('/markers/' + marker_id, json={'text':'Decisión'}).status_code, 200)
        self.assertNotIn('Marcador: Acuerdo', self.session.path.read_text(encoding='utf-8'))
        self.client.post('/pause'); self.wait('paused')
        self.assertEqual(self.client.post('/markers', json={}).status_code, 409)
        self.client.post('/stop'); self.wait('finished')
        manifest = self.session.archive_path
        original = json.loads(manifest.read_text(encoding='utf-8'))
        self.assertEqual(original['markers'][0]['text'], 'Decisión')
        self.assertTrue(all('id' in item for item in original['history']))
        self.client.post('/new-session')
        self.assertEqual(self.client.post('/open-session', json={'path':str(manifest)}).status_code, 200)
        self.assertEqual(self.session.state, 'ready')
        reopened = self.client.post('/open-session', json={'path':str(manifest)}).json()
        self.assertEqual(reopened['markers'][0]['id'], marker_id)
        self.assertEqual(self.client.delete('/sessions/' + reopened['session_id'] + '/markers/' + marker_id).status_code, 200)
        self.assertFalse(json.loads(manifest.read_text(encoding='utf-8'))['markers'])

    def test_backup_reopen_missing_and_scoped_audio(self):
        import json
        import numpy as np
        self.client.post('/start', json={'backup_audio':True})
        self.wait('recording')
        self.assertEqual(self.client.post('/settings', json={'backup_audio':False}).status_code, 409)
        self.session.backup.submit('Yo', 0, np.ones(1600, dtype=np.float32) * .25)
        self.assertEqual(self.client.get(f'/session-audio/{self.session.session_id}/0').status_code, 409)
        self.client.post('/stop'); self.wait('finished')
        manifest = self.session.archive_path
        data = self.client.get('/session-data').json()
        self.assertTrue(data['parts'][0]['available'])
        url = data['parts'][0]['url']
        self.assertEqual(self.client.get(url).content[:4], b'RIFF')
        self.assertEqual(self.client.get('/session-audio/old/0').status_code, 409)
        self.assertEqual(self.client.get(f'/session-audio/{self.session.session_id}/99').status_code, 404)
        self.client.post('/new-session')
        self.client.post('/open-session', json={'path':str(manifest)})
        marker_data = json.loads(manifest.read_text(encoding='utf-8'))
        self.assertEqual(json.loads(manifest.read_text(encoding='utf-8'))['parts'], marker_data['parts'])
        (manifest.parent / data['parts'][0]['file']).unlink()
        sid = marker_data['session_id']
        self.assertFalse(self.client.get('/sessions/' + sid).json()['parts'][0]['available'])
        self.assertEqual(self.client.get('/sessions/' + sid + '/audio/0').status_code, 404)
        marker_data['parts'][0]['file'] = '../escape.wav'
        manifest.write_text(json.dumps(marker_data), encoding='utf-8')
        self.assertEqual(self.client.post('/open-session', json={'path':str(manifest)}).status_code, 400)

    def test_delete_session_sends_every_file_to_trash(self):
        import shutil
        from unittest.mock import patch
        removed = []
        def fake_trash(paths):
            removed.extend(paths)
            for path in paths:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        self.client.post('/start', json={'backup_audio':True})
        self.wait('recording')
        sid = self.session.session_id
        self.assertEqual(self.client.delete('/sessions/' + sid).status_code, 409)  # still recording
        self.client.post('/stop'); self.wait('finished')
        folder, txt = self.session.archive_path.parent, self.session.path
        (folder / 'analysis.json').write_text('{}', encoding='utf-8')
        with patch('app.trash.send_to_trash', side_effect=fake_trash):
            self.assertEqual(self.client.delete('/sessions/' + sid).status_code, 200)
            self.assertEqual(self.client.delete('/sessions/' + sid).status_code, 404)
        self.assertEqual(set(removed), {folder, txt})
        self.assertFalse(folder.exists() or txt.exists())
        self.assertEqual(self.session.state, 'ready')  # the widget no longer shows it
        self.assertNotIn(sid, [r['session_id'] for r in self.client.get('/sessions').json()['sessions']])

    def test_delete_legacy_txt_session_with_its_analysis_and_edits(self):
        import shutil
        from unittest.mock import patch
        Path(app.output_dir()).mkdir(parents=True, exist_ok=True)
        txt = Path(app.output_dir()) / 'vieja.txt'
        txt.write_text('# Vieja\n\n[00:00:01] [Otros] Hola\n', encoding='utf-8')
        sid = next(r['session_id'] for r in self.client.get('/sessions').json()['sessions'] if r['title'] == 'Vieja')
        txt.with_suffix('.analysis.json').write_text('{}', encoding='utf-8')
        txt.with_suffix('.txt.edits').mkdir()
        removed = []
        def fake_trash(paths):
            removed.extend(paths)
            for path in paths: shutil.rmtree(path) if path.is_dir() else path.unlink()
        with patch('app.trash.send_to_trash', side_effect=fake_trash):
            self.assertEqual(self.client.delete('/sessions/' + sid).status_code, 200)
        self.assertEqual(len(removed), 3)
        self.assertFalse(txt.exists())

    def test_failed_trash_keeps_the_session(self):
        from unittest.mock import patch
        self.client.post('/start', json={}); self.wait('recording')
        self.client.post('/stop'); self.wait('finished')
        sid = self.session.session_id
        with patch('app.trash.send_to_trash', side_effect=OSError('en uso')):
            response = self.client.delete('/sessions/' + sid)
        self.assertEqual(response.status_code, 500)
        self.assertIn('en uso', response.json()['detail'])
        self.assertIn(sid, [r['session_id'] for r in self.client.get('/sessions').json()['sessions']])

    def test_backup_can_be_switched_during_the_session_without_losing_audio(self):
        import json
        import wave
        import numpy as np
        import settings as prefs
        self.assertTrue(self.client.post('/backup', json={'enabled':False}).json()['backup_next'] is False)
        self.client.post('/start', json={'backup_audio':False})
        self.wait('recording')
        self.assertFalse(self.client.post('/backup', json={'enabled':False}).json()['backup_active'])
        self.assertTrue(self.client.post('/backup', json={'enabled':True}).json()['backup_active'])
        self.session._backup_submit('Yo', 0, np.ones(1600, dtype=np.float32) * .25)
        self.assertFalse(self.client.post('/backup', json={'enabled':False}).json()['backup_active'])
        self.session._backup_submit('Yo', 0.5, np.ones(1600, dtype=np.float32) * .5)  # ignored while off
        self.client.post('/backup', json={'enabled':True})
        self.session._backup_submit('Otros', 1.0, np.ones(1600, dtype=np.float32) * .25)
        self.client.post('/stop'); self.wait('finished')
        data = json.loads(self.session.archive_path.read_text(encoding='utf-8'))
        self.assertTrue(data['backup_enabled'])
        with wave.open(str(self.session.archive_path.parent / data['parts'][0]['file'])) as audio:
            frames = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').reshape(-1, 2)
        self.assertGreater(frames[100, 0], 0)          # first stretch kept after re-enabling
        self.assertEqual(frames[8000 + 100, 0], 0)     # nothing recorded while off
        self.assertGreater(frames[16000 + 100, 1], 0)  # second stretch
        notes = [h['text'] for h in data['history'] if h['type'] == 'note']
        self.assertIn('Respaldo de audio desactivado', notes)
        self.assertIn('Respaldo de audio activado', notes)
        self.assertTrue(self.client.post('/backup', json={'enabled':True}).json()['backup_next'])
        self.assertTrue(prefs.load()['backup_audio'])

    def test_failed_backup_keeps_transcribing_and_records_warning(self):
        import json
        import numpy as np
        self.client.post('/start', json={'backup_audio':True})
        self.wait('recording')
        with patch.object(self.session.backup, '_write', side_effect=OSError('Disco lleno')):
            self.session.backup.submit('Yo', 0, np.zeros(1600, dtype=np.float32))
            deadline = time.monotonic() + 1
            while not self.session.backup.error and time.monotonic() < deadline:
                time.sleep(.01)
            self.client.post('/stop'); self.wait('finished')
        self.assertIn('Texto simulado.', self.session.path.read_text(encoding='utf-8'))
        self.assertIn('Disco lleno', json.loads(self.session.archive_path.read_text(encoding='utf-8'))['backup_error'])

    def test_opening_another_session_does_not_change_capture(self):
        from test_analysis import make_session
        old = make_session(self.root, 'b'*32)
        self.start()
        active_id, active_path = self.session.session_id, self.session.path
        response = self.client.post('/open-session', json={'path':str(old)})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['session_id'],'b'*32)
        self.assertEqual((self.session.session_id,self.session.path),(active_id,active_path))
        self.assertEqual(self.session.state,'recording')
        self.assertEqual(self.client.get('/sessions/'+'b'*32).status_code,200)
        self.assertEqual(self.client.get('/sessions/'+'b'*32+'/audio/0').status_code,409)

    def test_preparation_remembers_template_and_clears_context_after_start(self):
        self.assertEqual(self.client.post('/preparation',json={'title':'Entrevista','context':'José',
                         'template':'interview','backup_audio':False}).status_code,200)
        self.client.post('/start',json={}); self.wait('recording')
        self.assertEqual((self.session.title,self.session.context,self.session.template),('Entrevista','José','interview'))
        self.assertEqual(self.client.post('/preparation',json={}).status_code,409)
        self.assertEqual(self.client.get('/preparation').json()['context'],'')
        self.assertEqual(self.client.get('/preparation').json()['template'],'interview')

    def test_automatic_analysis_only_after_finalization(self):
        calls = []
        queue = SimpleNamespace(enqueue=lambda *args,**kw:calls.append((args,kw)),start=lambda:None)
        with patch('app.analysis_config',return_value={'automatic':True}), patch('app.get_analysis_queue',return_value=queue):
            self.start(); self.client.post('/pause'); self.wait('paused')
            self.assertFalse(calls)
            self.client.post('/resume'); self.wait('recording')
            self.client.post('/stop'); self.wait('finished')
            self.assertEqual(len(calls),1)
            self.assertTrue(calls[0][1]['automatic'])
            path = self.session.archive_path
            self.client.post('/open-session',json={'path':str(path)})
            self.assertEqual(len(calls),1)

    def test_analysis_settings_never_return_or_persist_the_key(self):
        from test_analysis import Credentials
        credentials = Credentials()
        with patch('app.credentials',credentials):
            response = self.client.post('/analysis-settings',json={'base_url':'http://127.0.0.1:9999/v1',
                                        'model':'fake','api_key':'synthetic-secret','automatic':True})
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.json()['has_key'])
            self.assertNotIn('synthetic-secret',response.text)
            self.assertNotIn('synthetic-secret',app.settings.PATH.read_text(encoding='utf-8'))
            self.assertNotIn('synthetic-secret',self.client.get('/settings').text)
            self.assertNotIn('synthetic-secret',self.client.get('/analysis-settings').text)

    def test_saved_mappings_only_affect_the_next_session(self):
        mapping=self.root/'rules.tsv'
        mapping.write_text('si go\tPrimera\tword\tnocase\n',encoding='utf-8')
        app.settings.save({'dict_paths':str(mapping)})
        self.engine.transcribe=lambda *a,**kw:'si go'
        self.start()
        self.assertIn('Primera',self.engine.hotwords)
        info=self.client.get('/dictionary/mappings').json()['files'][0]
        result=self.client.patch('/dictionary/mappings/'+info['id']+'/rules/1',json={
            'revision':info['revision'],'source':'si go','target':'Segunda','mode':'word','case':'nocase'})
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.client.post('/dictionary/mappings/preview',json={'text':'si go'}).json()['text'],'Segunda')
        self.assertIn('Primera',self.engine.hotwords)
        self.client.post('/stop');self.wait('finished')
        first=self.session.path.read_text(encoding='utf-8')
        self.assertIn('Primera',first);self.assertNotIn('Segunda',first)
        self.client.post('/new-session');self.start()
        self.assertIn('Segunda',self.engine.hotwords)
        self.client.post('/stop');self.wait('finished')
        self.assertIn('Segunda',self.session.path.read_text(encoding='utf-8'))

    def test_discretion_survives_new_session_and_reconnect(self):
        self.client.post('/discretion', json={'enabled':True})
        self.addCleanup(lambda: self.client.post('/discretion', json={'enabled':False}))
        self.start(); self.client.post('/stop'); self.wait('finished')
        self.client.post('/new-session')
        with self.client.websocket_connect('/ws') as ws:
            self.assertTrue(ws.receive_json()['discreet'])

    def test_probe_released_before_start_and_source_changes(self):
        events = []
        with patch.object(app.probe, 'stop', side_effect=lambda: events.append('stop')):
            self.client.post('/source', json={'source':'Yo', 'enabled':True})
            self.client.post('/start', json={})
        self.wait('recording')
        self.assertEqual(events, ['stop','stop'])
        self.assertEqual(self.client.post('/probe/start').status_code, 409)


if __name__ == '__main__':
    unittest.main()
