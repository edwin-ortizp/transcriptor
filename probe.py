"""Audio-only device check: no model, transcription or files."""
import threading
import time

import numpy as np

import audio


class AudioProbe:
    def __init__(self, notify):
        self.notify = notify
        self.lock = threading.RLock()
        self.event = threading.Event()
        self.threads = []
        self.results = {}

    def stop(self):
        with self.lock:
            self.event.set()
            for thread in self.threads:
                thread.join(timeout=5)
            if any(t.is_alive() for t in self.threads):
                raise RuntimeError('La prueba todavía está liberando el dispositivo')
            self.threads = []
        return {'active': False}

    def start(self, sources):
        with self.lock:
            self.stop()
            self.event = threading.Event()
            self.results = {}
            for label, source in sources.items():
                if not source['enabled']:
                    self.results[label] = {'state': 'disabled', 'name': '', 'level': 0}
                    continue
                self.results[label] = {'state': 'opening', 'name': '', 'level': 0}
                thread = threading.Thread(target=self._read, args=(label, source.copy(), self.event), daemon=True)
                self.threads.append(thread)
                thread.start()
        return self.snapshot()

    def snapshot(self):
        return {'type': 'probe', 'active': any(t.is_alive() for t in self.threads),
                'results': dict(self.results)}

    def _read(self, label, source, stop):
        try:
            device, fallback = (audio.mic if label == 'Yo' else audio.system_loopback)(source['id'])
            with device.recorder(samplerate=audio.CAPTURE_RATE) as recorder:
                started = time.monotonic()
                while not stop.is_set() and time.monotonic() - started < 60:
                    block = recorder.record(numframes=int(audio.CAPTURE_RATE * audio.READ_SECONDS))
                    level = float(np.sqrt(np.mean(block ** 2))) if len(block) else 0
                    self.results[label] = {'state': 'signal' if level >= audio.SILENCE_RMS else 'silence',
                                           'name': device.name, 'level': min(1, level * 20),
                                           'fallback': fallback}
                    self.notify(self.snapshot())
        except Exception as exc:
            self.results[label] = {'state': 'error', 'name': '', 'level': 0, 'error': str(exc)}
            self.notify(self.snapshot())
