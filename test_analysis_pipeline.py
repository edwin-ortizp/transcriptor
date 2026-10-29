"""Restricted-model tests: synthetic transcripts, no external endpoints or credentials."""
import copy
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import analysis
from analysis_pipeline import Budget, BudgetError, Pipeline, instructions, source_sections
from recording import atomic_json
from session_repository import SessionRepository
from test_analysis import Credentials, make_session


class RestrictedAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = make_session(self.root)
        self.repo = SessionRepository(lambda:self.root, lambda:None, self.root/'index.json')
        self.sid = self.repo.register(self.path)['session_id']
        self.config = {**analysis.DEFAULTS, 'base_url':'http://127.0.0.1:1/v1', 'model':'restricted-fake',
                       'context_window_tokens':4096, 'partial_output_tokens':128,
                       'consolidation_output_tokens':128, 'final_output_tokens':192, 'max_calls':500}
        self.credentials = Credentials(); self.credentials.set(self.config['base_url'], 'synthetic-key')
        self.events, self.calls = [], []
        self.queue = self.new_queue()
        self.addCleanup(self.cleanup)

    def new_queue(self):
        return analysis.AnalysisQueue(self.repo, self.root/'queue.json', lambda:self.config,
                                      self.credentials, self.events.append)

    def cleanup(self):
        self.queue.stop()
        if self.queue.thread: self.queue.thread.join(3)
        self.tmp.cleanup()

    def source(self, text, markers=None):
        data = json.loads(self.path.read_text(encoding='utf-8'))
        data['history'][0]['text'] = text
        if markers is not None: data['markers'] = markers
        atomic_json(self.path, data)
        return data

    def provider(self, config, key, system, text):
        self.assertEqual(key, 'synthetic-key')
        self.assertTrue(Budget(self.config).fits(system, text, config['max_output_tokens']))
        self.calls.append((config.copy(), system, text))
        return 'Notas sintéticas [00:00:01].', {'prompt_tokens':10, 'completion_tokens':5, 'total_tokens':15}

    def wait(self, state):
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            if self.repo.analysis(self.sid).get('state') == state: return self.repo.analysis(self.sid)
            time.sleep(.01)
        saved = self.repo.analysis(self.sid)
        self.fail({k:v for k,v in saved.items() if k not in ('stages','result')})

    def run_job(self):
        self.queue.enqueue(self.sid, 'meeting')
        self.queue.start()

    def test_unicode_source_and_markers_are_not_lost_under_small_context(self):
        text = 'José habló 😀 sobre seguridad. ' * 350
        data = self.source(text, [{'id':'m1','start':3,'label':'Decisión única'}])
        budget = Budget(self.config)
        system = instructions(analysis.SYSTEM, analysis.TEMPLATES['meeting']['sections'], 'extract')
        parts = source_sections(data, budget, system)
        self.assertGreater(len(parts), 3)
        texts = []
        for part in parts:
            self.assertTrue(budget.fits(system, part['text'], budget.output('extract')))
            for line in part['text'].split('Material:\n',1)[1].split('\n'):
                if '[Marcador;' not in line:
                    texts.append(re.sub(r'^\[[^]]+\] \[[^]]+\] ', '', line))
        self.assertEqual(''.join(texts), text)
        self.assertEqual(sum(p['text'].count('Decisión única') for p in parts), 1)

    def test_long_meeting_consolidates_multiple_levels_with_independent_budgets(self):
        self.config.update(chunk_chars=700, max_calls=500)
        self.source('Texto sintético ' * 1600)
        def verbose(config, key, system, text):
            self.provider(config,key,system,text)
            return '[00:00:01] ' + ('nota ' * 100), {'total_tokens':120}
        with patch('analysis.request_completion', side_effect=verbose):
            self.run_job(); saved = self.wait('completed')
        nodes = saved['stages']
        levels = {n['id'].split(':')[1] for n in nodes if n['stage'] == 'consolidate'}
        self.assertGreaterEqual(len(levels), 2)
        self.assertEqual(len(saved['result']['details']), saved['progress']['total_sections'])
        self.assertTrue(all(n['state'] == 'completed' for n in saved['result']['details']))
        self.assertEqual(self.calls[-1][0]['max_output_tokens'],192)
        self.assertEqual(self.calls[0][0]['max_output_tokens'],128)
        self.assertLessEqual(len(self.calls),500)
        self.assertNotIn('synthetic-key',(self.root/'queue.json').read_text(encoding='utf-8'))

    def test_timeout_resume_requires_acknowledgment_and_reuses_saved_extraction(self):
        def fail_final(config,key,system,text):
            result = self.provider(config,key,system,text)
            if 'síntesis global compacta' in system:
                raise analysis.AnalysisError('Timeout incierto', uncertain=True)
            return result
        with patch('analysis.request_completion', side_effect=fail_final):
            self.run_job(); failed = self.wait('error')
        self.assertTrue(failed['uncertain'])
        self.assertEqual(len(self.calls),2)
        self.assertEqual(len([s for s in failed['stages'] if s['state']=='completed']),1)
        with self.assertRaisesRegex(ValueError,'Confirma'): self.queue.resume(failed['id'])
        self.queue.stop(); self.queue.thread.join(2)
        self.queue = self.new_queue()
        self.assertEqual(self.queue.stages(failed['id'])['state'],'error')
        with patch('analysis.request_completion',side_effect=self.provider):
            self.queue.resume(failed['id'], True); self.queue.start(); saved = self.wait('completed')
        self.assertEqual(len(self.calls),3)
        self.assertIn('síntesis global compacta',self.calls[-1][1])
        self.assertEqual(saved['result']['calls'],3)
        self.assertFalse(saved['uncertain'])

    def test_restart_marks_inflight_uncertain_and_never_sends_by_itself(self):
        job = self.queue.enqueue(self.sid,'meeting')
        live = self.queue.jobs[0]
        live['state'] = 'running'
        live['checkpoint']['requests'].append({'stage_id':'extract:0','state':'in_flight','started_at':time.time(),'reserved_tokens':500})
        self.queue._persist()
        with patch('analysis.request_completion',side_effect=self.provider):
            restored = self.new_queue()
            self.assertEqual(restored.stages(job['id'])['state'],'interrupted')
            self.assertTrue(restored.stages(job['id'])['uncertain'])
            restored.start(); time.sleep(.03); restored.stop(); restored.thread.join(2)
        self.assertEqual(self.calls,[])

    def test_call_ceiling_can_be_raised_without_repeating_successful_nodes(self):
        self.config.update(max_calls=3, chunk_chars=500, final_output_tokens=2000)
        self.source('texto ' * 110)
        # Two extraction calls fit preflight, but their large notes need another level.
        def large(config,key,system,text):
            self.provider(config,key,system,text)
            return '[00:00:01] ' + 'nota '*220, {'total_tokens':280}
        with patch('analysis.request_completion',side_effect=large):
            self.run_job(); failed = self.wait('error')
        self.assertIn('máximo de llamadas',failed['error'])
        self.assertEqual(len(self.calls),2)  # stops before a call that could not finish the analysis
        self.config['max_calls'] = 10
        with patch('analysis.request_completion',side_effect=self.provider):
            self.queue.resume(failed['id']); saved = self.wait('completed')
        self.assertEqual(saved['result']['calls'],5)
        self.assertEqual(len(self.calls),5)

    def test_changed_source_or_provider_or_prompt_budget_requires_regeneration(self):
        with patch('analysis.request_completion',side_effect=analysis.AnalysisError('Proveedor caído')):
            self.run_job(); failed = self.wait('error')
        for field,value in [('model','other'),('context_window_tokens',8192),('base_url','http://127.0.0.1:2')]:
            old = self.config[field]; self.config[field] = value
            with self.assertRaisesRegex(ValueError,'cambiaron'): self.queue.resume(failed['id'])
            self.config[field] = old
        self.source('Texto modificado')
        with self.assertRaisesRegex(ValueError,'cambiaron'): self.queue.resume(failed['id'])

    def test_changing_final_output_budget_reuses_successful_source_sections(self):
        def fail(config,key,system,text):
            result = self.provider(config,key,system,text)
            if 'síntesis global compacta' in system: raise analysis.AnalysisError('Salida insuficiente')
            return result
        with patch('analysis.request_completion',side_effect=fail):
            self.run_job(); failed = self.wait('error')
        original = copy.deepcopy(failed['stages'][0])
        self.config['final_output_tokens'] = 256
        with patch('analysis.request_completion',side_effect=self.provider):
            self.queue.resume(failed['id']); saved = self.wait('completed')
        self.assertEqual(len(self.calls),3)
        self.assertEqual(self.calls[-1][0]['max_output_tokens'],256)
        self.assertEqual(saved['stages'][0], original)

    def test_crash_after_final_checkpoint_does_not_repeat_any_request(self):
        self.queue.enqueue(self.sid,'meeting')
        job = self.queue.jobs[0]
        job['state'] = 'running';self.queue._persist()
        with patch('analysis.request_completion',side_effect=self.provider):
            result = self.queue._run(job)
        self.assertEqual(len(self.calls),2)
        self.queue = self.new_queue()
        self.assertFalse(self.queue.stages(job['id'])['uncertain'])
        with patch('analysis.request_completion',side_effect=self.provider):
            self.queue.resume(job['id']);self.queue.start();saved = self.wait('completed')
        self.assertEqual(len(self.calls),2)
        self.assertEqual(saved['result']['text'],result['text'])

    def test_no_reduction_stops_after_one_compaction_and_keeps_details(self):
        self.config.update(chunk_chars=500, final_output_tokens=2000)
        self.source('texto '*110)
        def never_compacts(config,key,system,text):
            self.provider(config,key,system,text)
            if 'Consolida estas notas' in system:
                return 'nota '*450, {}
            return '[00:00:01] ' + 'nota '*220, {}
        with patch('analysis.request_completion',side_effect=never_compacts):
            self.run_job(); failed = self.wait('error')
        self.assertIn('no redujo',failed['error'])
        self.assertEqual(sum(n['stage']=='extract' and n['state']=='completed' for n in failed['stages']),2)
        self.assertLess(len(self.calls),10)

    def test_compaction_must_reduce_the_original_level_not_just_an_expanded_answer(self):
        config = {**self.config,'chunk_chars':500}
        data = self.source('x'*600)
        stages = []
        def execute(spec,system,text):
            stages.append(spec['stage'])
            length = {'extract':300,'consolidate':1000,'compact':800}[spec['stage']]
            return {**spec,'text':'n'*length,'state':'completed'}
        pipeline = Pipeline(data,config,{},analysis.SYSTEM,analysis.TEMPLATES['meeting']['sections'],execute)
        with patch.object(pipeline.budget,'fits',side_effect=lambda system,*args:'síntesis global' not in system):
            with self.assertRaisesRegex(BudgetError,'volumen original'): pipeline.run()
        self.assertEqual(stages,['extract','extract','consolidate','compact'])

    def test_previous_result_survives_failed_regeneration(self):
        with patch('analysis.request_completion',side_effect=self.provider):
            self.run_job(); original = self.wait('completed')['result']
        with patch('analysis.request_completion',side_effect=analysis.AnalysisError('Cuota')):
            self.run_job(); failed = self.wait('error')
        self.assertEqual(failed['result'],original)
        self.assertTrue(failed['can_continue'])

    def test_rate_limits_reserve_before_transmission_and_persist_wait(self):
        job_id = self.queue.enqueue(self.sid,'meeting')['id']
        job = self.queue.jobs[0]
        job['config'].update(requests_per_minute=1, tokens_per_minute=300)
        job['checkpoint']['requests'] = [
            {'stage_id':'prior-a','state':'completed','started_at':100.,'reserved_tokens':100},
            {'stage_id':'prior-b','state':'completed','started_at':100.1,'reserved_tokens':100}]
        clock = [110.]
        waits = []
        def advance(timeout):
            waits.append(self.queue.stages(job_id)['wait'])
            clock[0] += timeout + .001
        spec = {'id':'final','stage':'final','estimated_input_tokens':200,'output_budget':50}
        with patch('analysis.time.time',side_effect=lambda:clock[0]), patch.object(self.queue.condition,'wait',side_effect=advance):
            attempt = self.queue._reserve(job,spec)
        self.assertGreaterEqual(attempt['started_at'],160.1)
        self.assertTrue(waits)
        self.assertTrue(any(e.get('phase')=='waiting_quota' for e in self.events))
        stored = json.loads((self.root/'queue.json').read_text(encoding='utf-8'))[0]
        self.assertEqual(stored['checkpoint']['requests'][-1]['state'],'in_flight')
        self.assertNotIn('wait',stored)

    def test_real_usage_releases_unused_reservation_and_quota_changes_do_not_invalidate(self):
        self.config.update(tokens_per_minute=6000)
        def fail(config,key,system,text):
            result = self.provider(config,key,system,text)
            if 'síntesis global compacta' in system: raise analysis.AnalysisError('Error confirmado')
            return result
        with patch('analysis.request_completion',side_effect=fail):
            self.run_job(); failed = self.wait('error')
        request = self.queue.jobs[0]['checkpoint']['requests'][0]
        self.assertEqual(request['reserved_tokens'],15)
        self.config['tokens_per_minute'] = 7000
        with patch('analysis.request_completion',side_effect=self.provider):
            self.queue.resume(failed['id']); saved = self.wait('completed')
        self.assertEqual(len(self.calls),3)
        self.assertEqual(saved['result']['usage']['total_tokens'],30)

    def test_tokens_per_minute_wait_without_a_request_count_limit(self):
        self.queue.enqueue(self.sid,'meeting')
        job = self.queue.jobs[0]
        job['config'].update(requests_per_minute=None, tokens_per_minute=300)
        job['checkpoint']['requests'] = [{'stage_id':'prior','state':'completed','started_at':100.,'reserved_tokens':100}]
        clock = [110.]
        def advance(timeout): clock[0] += timeout + .01
        with patch('analysis.time.time',side_effect=lambda:clock[0]), patch.object(self.queue.condition,'wait',side_effect=advance):
            attempt = self.queue._reserve(job,{'id':'final','stage':'final','estimated_input_tokens':200,'output_budget':50})
        self.assertGreaterEqual(attempt['started_at'],160.)

    def test_exit_while_waiting_does_not_transmit_or_add_a_reservation(self):
        self.queue.enqueue(self.sid,'meeting')
        job = self.queue.jobs[0]
        job['state']='running';job['config']['requests_per_minute']=1
        job['checkpoint']['requests']=[{'stage_id':'prior','state':'completed','started_at':time.time(),'reserved_tokens':100}]
        with patch.object(self.queue.condition,'wait',side_effect=lambda **kw:self.queue.stop()):
            with self.assertRaisesRegex(analysis.AnalysisError,'interrumpido'):
                self.queue._reserve(job,{'id':'final','stage':'final','estimated_input_tokens':200,'output_budget':50})
        self.assertEqual(len(job['checkpoint']['requests']),1)
        self.assertEqual(job['state'],'interrupted')

    def test_atomic_checkpoint_retries_only_transient_windows_sharing_conflicts(self):
        import recording
        path = self.root/'atomic.json'
        path.write_text('{"previous":true}',encoding='utf-8')
        real_replace = Path.replace
        attempts = []
        def temporary_conflict(source,target):
            attempts.append(source)
            if len(attempts)==1:
                error=PermissionError('synthetic sharing conflict');error.winerror=32;raise error
            return real_replace(source,target)
        with patch.object(Path,'replace',temporary_conflict),patch('recording.time.sleep'):
            recording.atomic_json(path,{'new':True})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'new':True})
        self.assertEqual(len(attempts),2)
        with patch.object(Path,'replace',side_effect=PermissionError('synthetic permission failure')):
            with self.assertRaises(PermissionError): recording.atomic_json(path,{'failed':True})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'new':True})
        self.assertEqual(list(self.root.glob('atomic.json.*.tmp')),[])

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing behavior')
    def test_atomic_manifest_survives_a_real_open_reader_on_windows(self):
        import recording
        path = self.root/'reader-conflict.json'
        path.write_text('{"previous":true}',encoding='utf-8')
        reader = path.open(encoding='utf-8')
        started, errors, attempts = threading.Event(), [], []
        real_replace = Path.replace
        def replace(source,target):
            attempts.append(source)
            started.set()
            return real_replace(source,target)
        def write():
            try: recording.atomic_json(path,{'new':True})
            except Exception as exc: errors.append(exc)
        with patch.object(Path,'replace',replace):
            worker = threading.Thread(target=write)
            try:
                worker.start()
                self.assertTrue(started.wait(2))
                time.sleep(.04)
                self.assertEqual(json.load(reader),{'previous':True})
            finally:
                reader.close();worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.assertGreater(len(attempts),1)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'new':True})

    def test_429_retry_after_is_safe_and_never_retried_automatically(self):
        real_client = httpx.Client
        transport = httpx.MockTransport(lambda req:httpx.Response(429,headers={'Retry-After':'30'},json={'error':'private provider payload'}))
        with patch('analysis.httpx.Client',side_effect=lambda **kw:real_client(transport=transport)):
            with self.assertRaises(analysis.AnalysisError) as raised:
                analysis.request_completion(self.config,'synthetic-key','system','input')
        self.assertEqual(raised.exception.retry_after,30)
        self.assertNotIn('private',str(raised.exception))
        self.assertFalse(raised.exception.uncertain)
        with patch('analysis.request_completion',side_effect=raised.exception) as request:
            self.run_job(); failed = self.wait('error'); time.sleep(.03)
        self.assertEqual(request.call_count,1)
        self.assertGreater(self.queue.jobs[0]['cooldown_until'],time.time()+25)
        self.assertTrue(failed['can_continue'])

    def test_truncated_response_keeps_usage_and_never_becomes_a_completed_node(self):
        real_client = httpx.Client
        transport = httpx.MockTransport(lambda req:httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'unfinished'}}], 'usage':{'total_tokens':99}}))
        with patch('analysis.httpx.Client',side_effect=lambda **kw:real_client(transport=transport)):
            self.run_job(); failed = self.wait('error')
        self.assertEqual(failed['usage']['total_tokens'],99)
        self.assertFalse(failed.get('result'))
        self.assertEqual(failed['stages'][0]['state'],'error')

    def test_storage_failure_after_response_is_recoverable_without_repeating_it(self):
        real_persist = self.queue._persist
        failed_once = [False]
        def fail_once():
            nodes = self.queue.jobs[0].get('checkpoint',{}).get('nodes',{})
            if not failed_once[0] and any(n.get('state')=='completed' for n in nodes.values()):
                failed_once[0] = True
                raise OSError('Synthetic disk failure')
            return real_persist()
        with patch.object(self.queue,'_persist',side_effect=fail_once), patch('analysis.request_completion',side_effect=self.provider):
            self.run_job(); failed = self.wait('error')
        self.assertEqual(len(self.calls),1)
        with patch('analysis.request_completion',side_effect=self.provider):
            self.queue.resume(failed['id']); self.wait('completed')
        self.assertEqual(len(self.calls),2)

    def test_invalid_budgets_fail_before_any_provider_request(self):
        for changes in ({'context_window_tokens':2048}, {'tokens_per_minute':100}, {'requests_per_minute':0}, {'partial_output_tokens':9000}):
            config = {**self.config, **changes}
            # At 2048 use an explicitly impossible final reserve.
            if 'context_window_tokens' in changes: config['final_output_tokens'] = 1800
            with self.assertRaises(ValueError): analysis.validate_config(config)
        self.assertEqual(self.calls,[])

    def test_optional_explicit_tokenizer_and_offline_conservative_fallback(self):
        fake = SimpleNamespace(get_encoding=lambda _:SimpleNamespace(encode=lambda text, **kw:list(text)))
        with patch.dict('sys.modules',{'tiktoken':fake}):
            budget = Budget({**self.config,'tokenizer_encoding':'cl100k_base'})
            self.assertEqual(budget.tokens('😀'),1)
        self.assertEqual(Budget(self.config).tokens('😀'),4)
        with patch.dict('sys.modules',{'tiktoken':None}):
            with self.assertRaises(BudgetError): Budget({**self.config,'tokenizer_encoding':'cl100k_base'})

    def test_job_api_exposes_stages_without_credentials_and_continues_explicitly(self):
        import app
        from fastapi.testclient import TestClient
        with patch('analysis.request_completion',side_effect=analysis.AnalysisError('Error')):
            self.run_job(); failed = self.wait('error')
        with patch('app.analysis_queue',self.queue), patch('analysis.request_completion',side_effect=self.provider):
            client = TestClient(app.app)
            response = client.get(f"/analysis-jobs/{failed['id']}/stages")
            self.assertEqual(response.status_code,200)
            self.assertNotIn('config',response.json())
            self.assertNotIn('synthetic-key',response.text)
            response = client.post(f"/analysis-jobs/{failed['id']}/continue",json={})
            self.assertEqual(response.status_code,200)
            self.wait('completed')
        self.assertEqual(len(self.calls),2)



class OverlongModelTest(unittest.TestCase):
    """Providers that ignore max_tokens (seen with a local proxy): about 8 bytes per requested token."""

    def long_meeting(self, minutes=50):
        history = [{'type':'text', 'time':f'{i*10//3600:02d}:{i*10//60%60:02d}:{i*10%60:02d}', 'start':i*10, 'end':i*10+9,
                    'source':'Otros', 'id':f'b{i}', 'text':'Hablamos del cliente genérico y del RFC, quedamos en revisar. ' * 3}
                   for i in range(minutes * 6)]
        return {'history':history, 'context':'', 'markers':[]}

    def pipeline(self, execute, **config):
        config = analysis.validate_config({'model':'m', **config})
        return Pipeline(self.long_meeting(), config, {'nodes':{}, 'requests':[]}, analysis.SYSTEM,
                        analysis.TEMPLATES['meeting']['sections'], execute)

    def test_follows_word_limit_when_max_tokens_is_ignored(self):
        calls = []
        def execute(spec, system, text):
            calls.append(spec['stage'])
            words = re.search(r'unas (\d+) palabras', system)
            size = spec['output_budget'] * 8 if not words else min(spec['output_budget'] * 8, int(words.group(1)) * 6)
            node = {**spec, 'text':'n' * size, 'state':'completed'}
            pipeline.checkpoint['nodes'][spec['id']] = node
            return node
        pipeline = self.pipeline(execute)
        final, details = pipeline.run()
        self.assertEqual(calls[-1], 'final')
        self.assertGreater(len(details), 8)
        self.assertGreater(pipeline.checkpoint['overshoot'], 1.5)

    def test_model_that_never_shortens_gets_an_explicit_message(self):
        def execute(spec, system, text):
            node = {**spec, 'text':'n' * 3800, 'state':'completed'}
            pipeline.checkpoint['nodes'][spec['id']] = node
            return node
        pipeline = self.pipeline(execute)
        with self.assertRaisesRegex(BudgetError, 'ignora el límite de salida'):
            pipeline.run()

    def test_stops_before_compaction_that_would_exceed_the_call_limit(self):
        requests = []
        def execute(spec, system, text):
            requests.append(spec['id'])
            node = {**spec, 'text':'n' * 3800 if spec['stage'] != 'compact' else 'n' * 300, 'state':'completed'}
            pipeline.checkpoint['nodes'][spec['id']] = node
            pipeline.checkpoint['requests'] = requests
            return node
        pipeline = self.pipeline(execute, max_calls=12)
        with self.assertRaisesRegex(BudgetError, 'máximo de llamadas'):
            pipeline.run()
        self.assertLessEqual(len(requests), 12)


if __name__ == '__main__': unittest.main()
