"""Synthetic audio tests; no real devices, models or conversations."""
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

import audio
from probe import AudioProbe
from recording import AudioBackup, RATE


class BackupTest(unittest.TestCase):
    def test_channels_silence_boundaries_and_padding(self):
        with tempfile.TemporaryDirectory() as root:
            errors = []
            writer = AudioBackup(root, errors.append, part_seconds=1)
            writer.submit('Yo', .5, np.full(RATE, .5, dtype=np.float32))
            writer.submit('Otros', .75, np.full(RATE // 2, -.25, dtype=np.float32))
            writer.close(2.25)
            self.assertFalse(errors)
            parts = writer.manifest()
            self.assertEqual([p['duration'] for p in parts], [1, 1, .25])
            samples = []
            for part in parts:
                with wave.open(str(Path(root) / part['file']), 'rb') as wav:
                    self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (2, 2, RATE))
                    samples.append(np.frombuffer(wav.readframes(wav.getnframes()), '<i2').reshape(-1, 2))
            pcm = np.concatenate(samples)
            self.assertTrue(np.all(pcm[:RATE // 2] == 0))
            self.assertTrue(np.all(pcm[RATE:RATE + 4000, 0] == 16383))
            self.assertTrue(np.all(pcm[RATE:RATE + 4000, 1] == -8191))
            self.assertTrue(np.all(pcm[int(1.5 * RATE):] == 0))

    def test_write_failure_is_reported_and_worker_closes(self):
        with tempfile.TemporaryDirectory() as root:
            errors = []
            writer = AudioBackup(root, errors.append)
            with patch.object(writer, '_write', side_effect=OSError('Disco lleno')):
                writer.submit('Yo', 0, np.ones(100, dtype=np.float32))
                writer.close(1)
            self.assertIn('Disco lleno', errors[0])
            self.assertFalse(writer.thread.is_alive())

    def test_full_queue_does_not_block_capture(self):
        with tempfile.TemporaryDirectory() as root:
            errors, gate = [], threading.Event()
            writer = AudioBackup(root, errors.append)
            real_write = writer._write
            def blocked(*args):
                gate.wait(2)
                real_write(*args)
            with patch.object(writer, '_write', side_effect=blocked):
                for _ in range(140):
                    writer.submit('Yo', 0, np.ones(16, dtype=np.float32))
                self.assertTrue(errors)
                gate.set()
                writer.close(1)


class FakeRecorder:
    def __init__(self, values):
        self.values = iter(values)
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def record(self, numframes):
        return next(self.values)


class CaptureTest(unittest.TestCase):
    def test_raw_callback_keeps_silence_and_uses_sample_times(self):
        import queue
        silence = np.zeros((12000, 1), dtype=np.float32)
        voice = np.full((12000, 1), .1, dtype=np.float32)
        recorder = FakeRecorder([silence, voice])
        device = type('Device', (), {'recorder':lambda *_args, **_kw: recorder})()
        chunks, raw = queue.Queue(), []
        cap = audio.SourceCapture('Yo', device, chunks, 100, raw_cb=lambda *x: raw.append(x))
        def capture(*args):
            raw.append(args)
            if len(raw) == 2:
                cap.stop_event.set()
        cap.raw_cb = capture
        with patch('audio.time.monotonic', return_value=102):
            cap._record()
        self.assertEqual([r[1] for r in raw], [2, 2.25])
        self.assertTrue(np.all(raw[0][2] == 0))
        self.assertEqual(chunks.get()[1], 2.25)


class ProbeTest(unittest.TestCase):
    def test_silence_is_not_error_and_stop_closes_device(self):
        closed = []
        class Recorder:
            def __enter__(self): return self
            def __exit__(self, *_): closed.append(True)
            def record(self, numframes):
                time.sleep(.01)
                return np.zeros((numframes, 1), dtype=np.float32)
        device = type('Device', (), {'name':'Sintético', 'recorder':lambda *_args, **_kw: Recorder()})()
        messages = []
        probe = AudioProbe(messages.append)
        with patch('probe.audio.mic', return_value=(device, False)):
            probe.start({'Yo':{'enabled':True, 'id':''}, 'Otros':{'enabled':False, 'id':''}})
            deadline = time.monotonic() + 1
            while not messages and time.monotonic() < deadline:
                time.sleep(.01)
            probe.stop()
        self.assertTrue(closed)
        self.assertEqual(probe.results['Yo']['state'], 'silence')
        self.assertEqual(probe.results['Otros']['state'], 'disabled')
        self.assertFalse(probe.threads)


if __name__ == '__main__':
    unittest.main()
