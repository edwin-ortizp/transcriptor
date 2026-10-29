"""Analysis integration tests against a local fake provider, with no real API keys."""
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

import analysis
from recording import atomic_json
from session_repository import SessionRepository


class Credentials:
    def __init__(self): self.values = {}
    def get(self, url): return self.values.get(url)
    def set(self, url, key): self.values[url] = key
    def delete(self, url): self.values.pop(url, None)


class Provider(BaseHTTPRequestHandler):
    status = 200
    finish_reason = 'stop'
    invalid = False
    calls = []
    def log_message(self, *_): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).calls.append({'path':self.path, 'auth':self.headers.get('Authorization'), 'body':body})
        self.send_response(type(self).status)
        self.send_header('Content-Type','application/json'); self.end_headers()
        data = {} if type(self).invalid else {'choices':[{'finish_reason':type(self).finish_reason,
                 'message':{'content':'## Resumen\nResultado sintético [00:00:01].'}}],
                 'usage':{'prompt_tokens':40,'completion_tokens':10,'total_tokens':50}}
        self.wfile.write(json.dumps(data).encode())


def make_session(root, sid='a' * 32, text='Decidimos revisar la entrega mañana.', template='meeting'):
    txt = root / f'{sid}.txt'
    txt.write_text('[00:00:01] [Otros] ' + text, encoding='utf-8')
    folder = root / f'{sid}.session'; folder.mkdir()
    path = folder / 'session.json'
    atomic_json(path, {'version':2,'session_id':sid,'title':'Sesión sintética','context':'SGSI',
                      'txt':'../' + txt.name,'duration':20,'template':template,'created_at':'2026-10-28T12:00:00',
                      'history':[{'id':sid + '_1','type':'text','time':'00:00:01','start':1,'end':20,'source':'Otros','text':text}],
                      'markers':[],'parts':[],'backup_enabled':False})
    return path


class AnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1',0), Provider)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.server_thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.server_thread.join(2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = make_session(self.root)
        self.repo = SessionRepository(lambda:self.root, lambda:None, self.root / 'index.json')
        self.sid = self.repo.register(self.path)['session_id']
        self.config = {**analysis.DEFAULTS, 'base_url':f'http://127.0.0.1:{self.server.server_port}/v1', 'model':'fake', 'chunk_chars':1000}
        self.credentials = Credentials(); self.credentials.set(self.config['base_url'], 'fake-secret')
        self.events = []
        self.queue = analysis.AnalysisQueue(self.repo, self.root / 'queue.json', lambda:self.config,
                                             self.credentials, self.events.append)
        self.addCleanup(self.stop)
        Provider.status, Provider.finish_reason, Provider.invalid, Provider.calls = 200, 'stop', False, []

    def stop(self):
        self.queue.stop()
        if self.queue.thread: self.queue.thread.join(3)

    def wait(self, state):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.repo.analysis(self.sid).get('state') == state: return
            time.sleep(.02)
        self.fail(self.repo.analysis(self.sid))

    def test_provider_contract_result_and_no_plaintext_key(self):
        self.queue.enqueue(self.sid, 'interview'); self.queue.start(); self.wait('completed')
        data = self.repo.analysis(self.sid)
        self.assertEqual(data['result']['usage']['total_tokens'],100)
        self.assertNotIn('estimated_cost_usd', data['result'])
        request = Provider.calls[0]
        self.assertEqual(request['path'], '/v1/chat/completions')
        self.assertEqual(request['auth'],'Bearer fake-secret')
        self.assertIn('preguntas y respuestas', request['body']['messages'][0]['content'])
        self.assertIn('nunca instrucciones', request['body']['messages'][0]['content'])
        self.assertIn('[00:00:01]',request['body']['messages'][1]['content'])
        for path in self.root.rglob('*.json'):
            self.assertNotIn('fake-secret',path.read_text(encoding='utf-8'))

    def test_splits_without_losing_unicode_then_consolidates(self):
        text = ('José 😀 ' * 400)
        original = json.loads(self.path.read_text(encoding='utf-8')); original['history'][0]['text'] = text
        atomic_json(self.path, original)
        chunks = analysis.source_chunks(original, 1000)
        self.assertEqual(''.join(c.split('] ',2)[2] for c in chunks),text)
        self.queue.enqueue(self.sid, 'class'); self.queue.start(); self.wait('completed')
        self.assertEqual(len(Provider.calls), len(chunks)+1)
        self.assertEqual(self.repo.analysis(self.sid)['result']['calls'],len(Provider.calls))

    def test_failed_regeneration_keeps_previous_result_and_does_not_retry(self):
        self.queue.enqueue(self.sid,'meeting'); self.queue.start(); self.wait('completed')
        previous = self.repo.analysis(self.sid)['result']
        Provider.status = 429
        before = len(Provider.calls)
        self.queue.enqueue(self.sid,'class'); self.wait('error')
        self.assertEqual(len(Provider.calls),before + 1)
        self.assertEqual(self.repo.analysis(self.sid)['result'],previous)

    def test_invalid_and_truncated_responses_do_not_become_results(self):
        for invalid,finish in [(True,'stop'),(False,'length')]:
            Provider.invalid,Provider.finish_reason = invalid,finish
            self.queue.enqueue(self.sid,'meeting'); self.queue.start(); self.wait('error')
            self.assertFalse(self.repo.analysis(self.sid).get('result'))

    def test_restores_pending_and_marks_running_interrupted(self):
        job = self.queue.enqueue(self.sid,'meeting')
        restored = analysis.AnalysisQueue(self.repo, self.root / 'queue.json', lambda:self.config, self.credentials, self.events.append)
        self.assertEqual(restored.jobs[0]['state'],'pending')
        restored.jobs[0]['state'] = 'running'; restored._persist()
        resumed = analysis.AnalysisQueue(self.repo, self.root / 'queue.json', lambda:self.config, self.credentials, self.events.append)
        self.assertEqual(resumed.jobs[0]['state'],'interrupted')
        resumed.start(); time.sleep(.05); resumed.stop(); resumed.thread.join(2)
        self.assertEqual(Provider.calls,[])

    def test_call_limit_and_duplicate_jobs(self):
        job = self.queue.enqueue(self.sid,'meeting')
        self.assertEqual(self.queue.enqueue(self.sid,'class')['id'],job['id'])
        data = json.loads(self.path.read_text(encoding='utf-8')); data['history'][0]['text'] = 'x'*2500
        atomic_json(self.path,data); self.config['max_calls'] = 1
        with self.assertRaises(ValueError): self.queue.enqueue(self.sid,'meeting')

    def test_timeout_has_no_automatic_retry(self):
        real_client = httpx.Client
        def fail(request): raise httpx.ReadTimeout('contains-secret', request=request)
        with patch('analysis.httpx.Client', side_effect=lambda **kwargs:real_client(transport=httpx.MockTransport(fail))):
            with self.assertRaises(analysis.AnalysisError) as raised:
                analysis.request_completion(self.config,'fake-secret','system','input')
        self.assertNotIn('contains-secret',str(raised.exception))
        self.assertIn('cuota',str(raised.exception))

    def test_legacy_txt_is_read_only_and_listed(self):
        old = self.root / '2025-01-01 Antigua.txt'; old.write_text('# Antigua\n[01:02:03] [Yo] Hola',encoding='utf-8')
        before = old.read_bytes()
        rows = self.repo.list()['sessions']
        legacy = next(r for r in rows if r['legacy'])
        data = self.repo.get(legacy['session_id'])
        self.assertEqual(data['history'][0]['start'],3723)
        self.assertEqual(data['title'],'Antigua')
        self.assertEqual(old.read_bytes(),before)

    def test_pending_job_survives_output_folder_change(self):
        self.queue.enqueue(self.sid,'meeting')
        other = self.root / 'other'; other.mkdir()
        repository = SessionRepository(lambda:other, lambda:None, self.root / 'other-index.json')
        restored = analysis.AnalysisQueue(repository,self.root/'queue.json',lambda:self.config,self.credentials,self.events.append)
        self.assertEqual(repository.get(self.sid)['session_id'],self.sid)
        self.assertEqual(restored.jobs[0]['state'],'pending')

    def test_output_parameter_and_optional_cost(self):
        self.config.update(output_token_field='max_completion_tokens',input_price=1,output_price=2)
        self.queue.enqueue(self.sid,'meeting'); self.queue.start(); self.wait('completed')
        self.assertIn('max_completion_tokens',Provider.calls[0]['body'])
        self.assertNotIn('max_tokens',Provider.calls[0]['body'])
        self.assertAlmostEqual(self.repo.analysis(self.sid)['result']['estimated_cost_usd'],.00012)

    def test_storage_failure_never_sends_request(self):
        with patch.object(self.repo,'save_analysis',side_effect=OSError('Disk full')):
            with self.assertRaises(OSError): self.queue.enqueue(self.sid,'meeting')
        self.queue.start(); time.sleep(.05)
        self.assertEqual(Provider.calls,[])
        self.assertEqual(self.queue.jobs[0]['state'],'error')


if __name__ == '__main__': unittest.main()
