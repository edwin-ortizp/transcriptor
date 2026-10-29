"""Captura de audio del sistema (loopback) y micrófono, segmentada en bloques."""
import queue
import threading
import time
import warnings

import numpy as np
import soundcard as sc

warnings.filterwarnings("ignore", message=".*discontinuity.*")

CAPTURE_RATE = 48000
TARGET_RATE = 16000
FACTOR = CAPTURE_RATE // TARGET_RATE
READ_SECONDS = 0.25
PARTIAL_EVERY = 0.6  # cada cuánto se entrega el audio acumulado para el texto provisional

MIN_CHUNK = 6.0       # segundos mínimos antes de buscar una pausa para cortar
MAX_CHUNK = 20.0      # al llegar aquí se corta en el punto más silencioso reciente
SILENCE_RMS = 0.006   # silencio absoluto (sala callada)
PAUSE_TAIL = 0.4      # segundos de pausa necesarios para cortar
PAUSE_RATIO = 0.35    # con ruido de fondo, "pausa" = volumen < 35 % del habla típica del bloque
SEARCH_BACK = 4.0     # al forzar el corte, se busca la pausa en los últimos N segundos
FRAME = 320           # 20 ms a 16 kHz


def _frame_rms(audio: np.ndarray) -> np.ndarray:
    n = len(audio) // FRAME * FRAME
    return np.sqrt((audio[:n].reshape(-1, FRAME) ** 2).mean(axis=1))


def _quietest_cut(audio: np.ndarray) -> int:
    """Índice de muestra del punto más silencioso en los últimos SEARCH_BACK s (ventana de 100 ms)."""
    rms = _frame_rms(audio)
    win = 5  # 5 x 20 ms
    if len(rms) <= win:
        return len(audio)
    smooth = np.convolve(rms, np.ones(win) / win, mode="valid")
    first = max(0, len(smooth) - int(SEARCH_BACK * TARGET_RATE / FRAME))
    i = first + int(np.argmin(smooth[first:]))
    return (i + win // 2) * FRAME


def _to_16k_mono(data: np.ndarray) -> np.ndarray:
    mono = data.mean(axis=1) if data.ndim == 2 else data
    n = len(mono) // FACTOR * FACTOR
    return mono[:n].reshape(-1, FACTOR).mean(axis=1).astype(np.float32)


class SourceCapture(threading.Thread):
    """Lee una fuente y entrega bloques (label, t_inicio, audio) a una cola."""

    def __init__(self, label, device, out_queue, t0, level_cb=None,
                 min_chunk=MIN_CHUNK, max_chunk=MAX_CHUNK, partial_cb=None, raw_cb=None):
        super().__init__(daemon=True)
        self.raw_cb = raw_cb
        self.label = label
        self.device = device
        self.out = out_queue
        self.t0 = t0
        self.level_cb = level_cb
        self.min_chunk = min_chunk
        self.max_chunk = max_chunk
        self.partial_cb = partial_cb
        self.stop_event = threading.Event()
        self.stop_at = None
        self.ready = threading.Event()
        self.error = None

    def run(self):
        try:
            self._record()
        except Exception as exc:
            self.error = exc
        finally:
            self.ready.set()

    def _record(self):
        buf, buf_start = [], None
        last_partial = 0.0
        frames = int(CAPTURE_RATE * READ_SECONDS)
        with self.device.recorder(samplerate=CAPTURE_RATE) as rec:
            next_sample = time.monotonic() - self.t0
            self.ready.set()
            while not self.stop_event.is_set():
                block = _to_16k_mono(rec.record(numframes=frames))
                block_start = next_sample
                if self.stop_at is not None:
                    block = block[:max(0, round((self.stop_at - block_start) * TARGET_RATE))]
                if not len(block):
                    break
                next_sample += len(block) / TARGET_RATE
                if self.raw_cb:
                    self.raw_cb(self.label, block_start, block)
                rms = float(np.sqrt(np.mean(block ** 2))) if len(block) else 0.0
                if self.level_cb:
                    self.level_cb(self.label, rms)
                if buf_start is None:
                    if rms < SILENCE_RMS:
                        continue  # no empezar bloques con silencio puro
                    buf_start = block_start
                buf.append(block)
                dur = sum(len(b) for b in buf) / TARGET_RATE
                if dur >= self.min_chunk and self._ends_in_pause(buf):
                    self.out.put((self.label, buf_start, np.concatenate(buf)))
                    buf, buf_start = [], None
                elif dur >= self.max_chunk:
                    # sin pausa clara: cortar en el punto más silencioso reciente, nunca a ciegas
                    data = np.concatenate(buf)
                    cut = _quietest_cut(data)
                    self.out.put((self.label, buf_start, data[:cut]))
                    rest = data[cut:]
                    buf = [rest] if len(rest) else []
                    buf_start = buf_start + cut / TARGET_RATE if len(rest) else None
                elif self.partial_cb and time.monotonic() - last_partial >= PARTIAL_EVERY:
                    last_partial = time.monotonic()
                    self.partial_cb(self.label, buf_start, np.concatenate(buf))
        if buf:
            self.out.put((self.label, buf_start, np.concatenate(buf)))

    @staticmethod
    def _ends_in_pause(buf) -> bool:
        rms = _frame_rms(np.concatenate(buf))
        tail = int(PAUSE_TAIL * TARGET_RATE / FRAME)
        if len(rms) <= tail * 2:
            return False
        speech = np.percentile(rms[:-tail], 70)  # nivel típico de habla en el bloque
        threshold = max(SILENCE_RMS, PAUSE_RATIO * speech)
        return float(rms[-tail:].mean()) < threshold

    def stop(self):
        self.stop_at = time.monotonic() - self.t0
        self.stop_event.set()


def list_devices():
    return {
        "mics": [{"id": m.id, "name": m.name} for m in sc.all_microphones(include_loopback=False)],
        "speakers": [{"id": s.id, "name": s.name} for s in sc.all_speakers()],
        "default_mic": sc.default_microphone().id,
        "default_speaker": sc.default_speaker().id,
    }


def system_loopback(speaker_id=None):
    """Devuelve (dispositivo, usó_predeterminado). Si el id ya no existe, usa el predeterminado."""
    ids = {s.id for s in sc.all_speakers()}
    if speaker_id and speaker_id in ids:
        return sc.get_microphone(speaker_id, include_loopback=True), False
    return sc.get_microphone(sc.default_speaker().id, include_loopback=True), bool(speaker_id)


def mic(mic_id=None):
    ids = {m.id for m in sc.all_microphones(include_loopback=False)}
    if mic_id and mic_id in ids:
        return sc.get_microphone(mic_id), False
    return sc.default_microphone(), bool(mic_id)


def new_queue():
    return queue.Queue()
