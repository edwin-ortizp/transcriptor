"""Failure and resource tests without devices, downloads or personal settings."""
import queue
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import app
import audio
from speech_detection import SpeechDetector
from tools import benchmark_transcription as benchmark


class Capture:
    def __init__(self, label, device, output, t0, level, mn, mx, partial, raw):
        self.label, self.device, self.out = label, device, output
        self.level, self.partial, self.raw = level, partial, raw
        self.min_chunk, self.max_chunk = mn, mx
        self.ready = threading.Event()
        self.error = None
        self.alive = False

    def start(self):
        self.alive = True
        self.ready.set()

    def stop(self):
        self.alive = False

    def is_alive(self):
        return self.alive

    def join(self, *args):
        pass


class ReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, value in (
            ('settings.PATH', Path(self.tmp.name) / 'settings.json'),
            ('app.force_cpu_after_failure', False),
            ('app.models.resolve', lambda identity: {'id':identity, 'engine':'whisper', 'path':'test-model', 'label':'Test model'}),
            ('app.audio.SourceCapture', Capture),
            ('app.audio.mic', lambda identity: (SimpleNamespace(name='Mic', id=identity), False)),
            ('app.audio.system_loopback', lambda identity: (SimpleNamespace(name='Speaker', id=identity), False)),
        ):
            mock = patch(target, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.session = app.Session()
        self.session.speech_detection = False
        self.session.state, self.session.active, self.session.running = 'recording', True, True
        self.session.queue = queue.Queue()
        self.session.t0 = app.time.monotonic()
        self.notes = []
        self.session._note = lambda text, *args: self.notes.append(text)
        self.session.persist = Mock()
        self.session.corrections = SimpleNamespace(hotwords=lambda: 'Siigo')

    def engine(self, device='cuda', result='Texto'):
        return SimpleNamespace(model=object(), device=device, name='whisper', chunk=(15, 28),
                               live_ok=device == 'cuda', transcribe=Mock(return_value=result))

    def test_reconnect_same_device_backoff_and_other_source_survives(self):
        session = self.session
        session.sources['Yo'] = {'enabled': True, 'id': 'original'}
        session._open_capture('Yo')
        session._open_capture('Otros')
        other = session.captures['Otros']
        old = session.captures['Yo']
        old.error, old.alive = RuntimeError('USB perdido'), False
        session._monitor_sources_once()
        health = session.source_health['Yo']
        first = health['next']
        with patch('app.audio.mic', side_effect=RuntimeError('ausente')) as lookup:
            session._monitor_sources_once(first - .1)
            lookup.assert_not_called()
            for expected in (2, 5, 10, 10):
                now = health['next']
                session._monitor_sources_once(now)
                self.assertEqual(health['next'], now + expected)
            self.assertEqual(lookup.call_args.args, ('original',))
        session._monitor_sources_once(health['next'])
        self.assertIs(session.captures['Otros'], other)
        self.assertEqual(session.sources_state()['Yo']['effective_id'], 'original')
        self.assertEqual(session.sources_state()['Yo']['health'], 'healthy')
        self.assertEqual(len(session.source_gaps), 1)
        self.assertIsNotNone(session.source_gaps[0]['duration'])
        self.assertEqual(session.source_gaps[0]['outcome'], 'recuperada')

    def test_failed_open_keeps_one_gap_and_retries_without_duplicate_capture(self):
        session = self.session
        session.sources['Yo']['enabled'] = True
        session._source_failed('Yo', 'missing')
        health = session.source_health['Yo']
        class Broken(Capture):
            def start(self):
                self.error = RuntimeError('open failed')
                self.ready.set()
        with patch('app.audio.SourceCapture', Broken):
            session._monitor_sources_once(health['next'])
        self.assertIs(session.source_health['Yo'], health)
        self.assertEqual(len(session.source_gaps), 1)
        self.assertNotIn('Yo', session.captures)
        self.assertIsNone(session.source_gaps[0]['end'])

    def test_manual_change_cancels_old_retry_and_old_callbacks(self):
        session = self.session
        session.sources['Yo'] = {'enabled': True, 'id': 'old'}
        session.on_partial, session.level, session._backup_submit = Mock(), Mock(), Mock()
        session._open_capture('Yo')
        old = session.captures['Yo']
        session.set_source('Yo', device_id='new')
        old.partial('Yo', 0, np.ones(10))
        old.level('Yo', 1)
        old.raw('Yo', 0, np.ones(10))
        session._backup_submit.assert_not_called()
        session.on_partial.assert_not_called()
        session.level.assert_not_called()
        self.assertEqual(session.captures['Yo'].device.id, 'new')
        session.set_source('Yo', enabled=False)
        with patch.object(session, '_open_capture') as reopen:
            session._monitor_sources_once(1e20)
            reopen.assert_not_called()

    def test_pause_and_stop_cancel_reconnection(self):
        for action in ('pause', 'stop'):
            session = self.session
            session.state, session.active, session.running = 'recording', True, True
            session.sources['Yo']['enabled'] = True
            session._source_failed('Yo', 'missing')
            getattr(session, action)()
            with patch.object(session, '_open_capture') as reopen:
                session._monitor_sources_once(1e20)
                reopen.assert_not_called()
            self.assertIsNotNone(session.source_gaps[-1]['end'])

    def test_gpu_failure_retries_final_once_with_same_context_and_hotwords(self):
        session = self.session
        gpu, cpu = self.engine(), self.engine('cpu')
        gpu.transcribe.side_effect = RuntimeError('CUDA out of memory')
        session.engine, session.models, session.model_name = gpu, {'gpu': gpu}, 'multi_turbo'
        chunk = np.ones(16000, dtype=np.float32)
        session._open_capture('Otros')
        with patch('app.transcriber.load_model', return_value=cpu) as load:
            self.assertEqual(session._infer(chunk, 'es', 'Contexto'), 'Texto')
            self.assertTrue(load.call_args.args[1])
        self.assertTrue(app.force_cpu_after_failure)
        self.assertIsNone(gpu.model)
        self.assertIs(session.engine, cpu)
        self.assertEqual(cpu.hotwords, 'Siigo')
        cpu.transcribe.assert_called_once_with(chunk, 'es', 'Contexto', fast=False)
        self.assertEqual(session.captures['Otros'].min_chunk, 15)
        session._infer(chunk, 'es', 'Siguiente')
        self.assertEqual(gpu.transcribe.call_count, 1)
        self.assertEqual(cpu.transcribe.call_count, 2)

    def test_non_cuda_errors_and_failed_cpu_are_not_retried_indefinitely(self):
        session = self.session
        session.engine = self.engine()
        session.engine.transcribe.side_effect = ValueError('invalid audio')
        with patch('app.transcriber.load_model') as load:
            with self.assertRaisesRegex(ValueError, 'invalid'):
                session._infer(np.ones(10), 'es')
            load.assert_not_called()
        gpu, cpu = self.engine(), self.engine('cpu')
        gpu.transcribe.side_effect = RuntimeError('cuDNN failure')
        cpu.transcribe.side_effect = RuntimeError('CPU failed')
        session.engine, session.model_name = gpu, 'multi_turbo'
        with patch('app.transcriber.load_model', return_value=cpu) as load:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'CPU failed'):
                    session._infer(np.ones(10), 'es')
            load.assert_called_once()

    def test_cpu_load_failure_does_not_prevent_next_final_trying_again(self):
        session = self.session
        gpu, cpu = self.engine(), self.engine('cpu')
        gpu.transcribe.side_effect = RuntimeError('CUDA failure')
        session.engine, session.model_name = gpu, 'test-model'
        with patch('app.transcriber.load_model', side_effect=[OSError('CPU load failed'), cpu]) as load:
            with self.assertRaisesRegex(OSError, 'CPU load failed'):
                session._infer(np.ones(10), 'es')
            self.assertEqual(session._infer(np.ones(10), 'es'), 'Texto')
            self.assertEqual(load.call_count, 2)

    def test_failed_partial_changes_engine_but_only_final_is_retried(self):
        session = self.session
        gpu, cpu = self.engine(), self.engine('cpu')
        gpu.transcribe.side_effect = RuntimeError('cuBLAS failure')
        session.engine, session.model_name = gpu, 'test-model'
        with patch('app.transcriber.load_model', return_value=cpu):
            self.assertEqual(session._infer(np.ones(10), 'es', fast=True), '')
        cpu.transcribe.assert_not_called()
        session._infer(np.ones(10), 'es', 'Contexto final')
        self.assertEqual(cpu.transcribe.call_count, 1)

    def test_vad_only_skips_speechless_audio_and_failure_uses_whisper_filter(self):
        session = self.session
        session.speech_detection = True
        session.engine = self.engine('cpu')
        chunk = np.ones(16000, dtype=np.float32)
        with patch.object(session.detector, 'contains_speech', side_effect=[False, True, None, None]):
            self.assertEqual(session._infer(chunk, 'es'), '')
            session.engine.transcribe.assert_not_called()
            session._infer(chunk, 'es')
            self.assertFalse(session.engine.vad_filter)
            self.assertIs(session.engine.transcribe.call_args.args[0], chunk)
            session._infer(chunk, 'es')
            session._infer(chunk, 'es')
            self.assertTrue(session.engine.vad_filter)
        self.assertEqual(sum('Detector de voz' in note for note in self.notes), 1)

    def test_idle_release_checks_activity_pending_work_and_reload(self):
        session = self.session
        engine = self.engine('cpu')
        session.models = {'cached': engine}
        session.engine = engine
        session.last_model_use = 100
        self.assertFalse(session.unload_if_idle(1000))
        session.active = session.running = False
        session.state = 'paused'
        self.assertFalse(session.unload_if_idle(1000))
        session.state = 'finished'
        session.queue.put(('Otros', 0, np.ones(10)))
        self.assertFalse(session.unload_if_idle(1000))
        session.queue.get()
        self.assertFalse(session.unload_if_idle(699))
        self.assertTrue(session.unload_if_idle(700))
        self.assertFalse(session.models)
        self.assertIsNone(engine.model)
        with patch('app.transcriber.load_model', return_value=self.engine('cpu')) as load:
            session.get_model('multi_turbo')
            load.assert_called_once()

    def test_idle_option_and_inflight_worker_prevent_unload(self):
        session = self.session
        session.state, session.running, session.active = 'finished', False, False
        session.last_model_use = 0
        app.settings.save({'unload_models_when_idle': False})
        self.assertFalse(session.unload_if_idle(1000))
        app.settings.save({'unload_models_when_idle': True})
        session.worker = SimpleNamespace(is_alive=lambda: True)
        self.assertFalse(session.unload_if_idle(1000))

    def test_concurrent_reload_wins_over_idle_release(self):
        session = self.session
        session.state, session.running, session.active = 'ready', False, False
        session.last_model_use = 0
        loading, proceed = threading.Event(), threading.Event()
        engine = self.engine('cpu')
        def load(*args):
            loading.set()
            self.assertTrue(proceed.wait(2))
            return engine
        unloaded = []
        with patch('app.transcriber.load_model', side_effect=load):
            loader = threading.Thread(target=session.get_model, args=('multi_turbo',))
            loader.start()
            self.assertTrue(loading.wait(2))
            maintenance = threading.Thread(target=lambda: unloaded.append(session.unload_if_idle()))
            maintenance.start()
            proceed.set()
            loader.join(2)
            maintenance.join(2)
        self.assertEqual(unloaded, [False])
        self.assertIsNotNone(engine.model)

    def test_failed_final_is_reported_and_next_block_is_saved(self):
        import io
        session = self.session
        session.state, session.running, session.active = 'finishing', False, False
        session.file = io.StringIO()
        session.corrections.apply = lambda text: 'Corregido: ' + text
        engine = self.engine('cpu')
        engine.transcribe.side_effect = [RuntimeError('CPU failed'), 'Segundo bloque']
        session.engine = engine
        for start in (1, 2):
            session.queue.put(('Otros', start, np.ones(1600, dtype=np.float32)))
        with patch('app.session_finished'):
            session._work(session.queue, engine, 'es')
        self.assertIn('bloques sin transcribir', session.failure_text)
        self.assertTrue(any('bloque no transcrito de 1.00 a 1.10' in note for note in self.notes))
        self.assertEqual([event['text'] for event in session.history if event['type'] == 'text'],
                         ['Corregido: Segundo bloque'])
        self.assertEqual(session.state, 'finished')


class AudioSafetyTest(unittest.TestCase):
    def test_selected_missing_device_never_uses_default(self):
        with patch('audio.sc.all_microphones', return_value=[]), patch('audio.sc.default_microphone') as default:
            with self.assertRaisesRegex(RuntimeError, 'seleccionado'):
                audio.mic('missing')
            default.assert_not_called()
        with patch('audio.sc.all_speakers', return_value=[]), patch('audio.sc.default_speaker') as default:
            with self.assertRaisesRegex(RuntimeError, 'seleccionada'):
                audio.system_loopback('missing')
            default.assert_not_called()

    def test_capture_failure_flushes_audio_and_preserves_raw(self):
        class Recorder:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def record(self, **kwargs):
                if getattr(self, 'read', False):
                    raise RuntimeError('USB disconnected')
                self.read = True
                return np.full((12000, 1), .1, dtype=np.float32)
        device = SimpleNamespace(recorder=lambda **kwargs: Recorder())
        output, raw = queue.Queue(), []
        capture = audio.SourceCapture('Yo', device, output, 0, raw_cb=lambda *args: raw.append(args))
        capture.run()
        self.assertIn('USB disconnected', str(capture.error))
        label, start, chunk = output.get_nowait()
        np.testing.assert_array_equal(chunk, raw[0][2])
        self.assertEqual((label, start), raw[0][:2])

    def test_real_bundled_silero_silence_noise_and_failure(self):
        detector = SpeechDetector()
        self.assertFalse(detector.contains_speech(np.zeros(16000, dtype=np.float32)))
        self.assertEqual(detector.error, '')
        noise = np.random.default_rng(5).normal(0, .002, 16000).astype(np.float32)
        self.assertFalse(detector.contains_speech(noise))
        with patch('faster_whisper.vad.get_speech_timestamps', side_effect=OSError('missing asset')):
            self.assertIsNone(detector.contains_speech(noise))
        self.assertIsNone(detector.contains_speech(noise))


class BenchmarkTest(unittest.TestCase):
    def test_metrics_keep_accents_and_handle_insertions_and_empty_references(self):
        self.assertEqual(benchmark.scores('¡José habló!', 'JOSE\u0301 habló.')['wer'], 0)
        self.assertEqual(benchmark.scores('uno dos', 'uno tres cuatro')['wer'], 1)
        self.assertGreater(benchmark.scores('sí', 'si')['cer'], 0)
        self.assertEqual(benchmark.scores('', '')['wer'], 0)
        self.assertIsNone(benchmark.scores('', 'inventado')['wer'])

    def test_wav_conversion_and_rejected_formats(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'sample.wav'
            for rate, channels, width, valid in ((48000, 2, 2, True), (16000, 1, 2, True),
                                                 (44100, 1, 2, False), (16000, 1, 1, False)):
                with wave.open(str(path), 'wb') as wav:
                    wav.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
                    wav.writeframes(bytes(rate * channels * width))
                if valid:
                    self.assertEqual(len(benchmark.read_wav(path)), 16000)
                else:
                    with self.assertRaisesRegex(ValueError, 'PCM'):
                        benchmark.read_wav(path)

    def test_missing_model_fails_before_worker_or_download(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            manifest = root / 'manifest.json'
            manifest.write_text('{"samples":[{"audio":"a.wav","reference":"hola"}],"models":["dl:turbo"]}')
            with patch.object(benchmark, 'read_wav', return_value=np.ones(10)), patch('models.resolve', return_value={'download':'turbo'}):
                with self.assertRaisesRegex(ValueError, 'no instalado'):
                    benchmark.prepare_manifest(manifest)

    def test_worker_protocol_uses_separate_process_and_captures_failure(self):
        import subprocess
        with patch('tools.benchmark_transcription.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{"pid":42}')) as run:
            self.assertEqual(benchmark.execute({'vad': True})['pid'], 42)
            self.assertIn('--worker', run.call_args.args[0])
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='CUDA missing')):
            with self.assertRaisesRegex(RuntimeError, 'CUDA missing'):
                benchmark.execute({})


if __name__ == '__main__':
    unittest.main()
