"""Local session metadata and bounded, asynchronous stereo WAV backup."""
import json
import math
import os
import queue
import struct
import threading
import tempfile
import time
from pathlib import Path

import numpy as np

RATE = 16000


def atomic_json(path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def atomic_bytes(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp',
                                         mode='wb', delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        # Briefly open readers/antivirus can deny replacement on Windows.
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 4:
                    raise
                time.sleep(.02 * (attempt + 1))
    finally:
        if temporary and temporary.exists(): temporary.unlink()


def load_manifest(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') not in (1, 2):
        raise ValueError('Formato de sesión no compatible')
    def moment(value):
        return isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    for key in ('session_id', 'title', 'context', 'txt'):
        if not isinstance(data.get(key), str):
            raise ValueError('Metadatos de sesión inválidos')
    if not moment(data.get('duration')):
        raise ValueError('Duración inválida')
    for key in ('history', 'markers', 'parts'):
        if not isinstance(data.get(key), list):
            raise ValueError('Lista de sesión inválida')
    ids = set()
    for item in data['history'] + data['markers']:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('text'), str):
            raise ValueError('Fragmento o marcador inválido')
        if not moment(item.get('start')) or item['id'] in ids:
            raise ValueError('Tiempo o identificador inválido')
        ids.add(item['id'])
        if item.get('type') == 'text' and (not moment(item.get('end')) or item['end'] < item['start']
                                         or item.get('source') not in ('Yo', 'Otros')):
            raise ValueError('Bloque de transcripción inválido')
    for part in data['parts']:
        if not isinstance(part, dict) or not isinstance(part.get('file'), str):
            raise ValueError('Referencia de audio inválida')
        if not moment(part.get('start')) or not moment(part.get('duration')):
            raise ValueError('Tiempo de audio inválido')
    return data


class AudioBackup:
    def __init__(self, folder, on_error, part_seconds=1800):
        self.folder = Path(folder)
        self.on_error = on_error
        self.part_frames = int(part_seconds * RATE)
        self.queue = queue.Queue(maxsize=128)
        self.error = ''
        self.parts = {}
        self.duration = 0
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def fail(self, exc):
        if not self.error:
            self.error = str(exc)
            self.on_error(self.error)

    def submit(self, source, start, samples):
        if self.error:
            return
        try:
            self.queue.put_nowait((source, max(0, round(start * RATE)), samples.copy()))
        except queue.Full:
            self.fail('El disco no alcanza a guardar el audio; respaldo incompleto')

    @staticmethod
    def _header(file, frames):
        size = frames * 4
        file.seek(0)
        file.write(struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + size, b'WAVE',
                               b'fmt ', 16, 1, 2, RATE, RATE * 4, 4, 16, b'data', size))

    def _part(self, number):
        if number not in self.parts:
            path = self.folder / f'audio-{number + 1:04d}.wav'
            if path.is_file() and path.stat().st_size >= 44:
                # Backup switched back on in the same session: keep what was already saved.
                file = path.open('r+b')
                self.parts[number] = [file, (path.stat().st_size - 44) // 4, path.name]
                return self.parts[number]
            file = path.open('w+b')
            try:
                self._header(file, 0)
            except Exception:
                file.close()
                raise
            self.parts[number] = [file, 0, path.name]
        return self.parts[number]

    def _write(self, source, start, samples):
        pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2')
        while len(pcm):
            number, offset = divmod(start, self.part_frames)
            count = min(len(pcm), self.part_frames - offset)
            part = self._part(number)
            file = part[0]
            file.seek(44 + offset * 4)
            old = file.read(count * 4)
            stereo = np.zeros((count, 2), dtype='<i2')
            if old:
                stereo[:len(old) // 4] = np.frombuffer(old, dtype='<i2').reshape(-1, 2)
            stereo[:, 0 if source == 'Yo' else 1] = pcm[:count]
            file.seek(44 + offset * 4)
            file.write(stereo.tobytes())
            part[1] = max(part[1], offset + count)
            self._header(file, part[1])
            file.flush()
            start += count
            pcm = pcm[count:]

    def _work(self):
        while True:
            item = self.queue.get()
            if item is None:
                break
            if not self.error:
                try:
                    self._write(*item)
                except Exception as exc:
                    self.fail(exc)

    def close(self, duration):
        self.queue.put(None)
        self.thread.join()
        self.duration = duration
        try:
            if not self.error:
                total = max(0, round(duration * RATE))
                for number in range((total + self.part_frames - 1) // self.part_frames):
                    part = self._part(number)
                    frames = min(self.part_frames, total - number * self.part_frames)
                    frames = max(frames, part[1])
                    part[0].truncate(44 + frames * 4)
                    self._header(part[0], frames)
                    part[1] = frames
        except Exception as exc:
            self.fail(exc)
        finally:
            for file, _, _ in self.parts.values():
                try:
                    file.close()
                except OSError as exc:
                    self.fail(exc)

    def manifest(self):
        return [{'file': p[2], 'start': n * self.part_frames / RATE,
                 'duration': p[1] / RATE} for n, p in sorted(self.parts.items())]
