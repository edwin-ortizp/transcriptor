"""Conservative, offline speech gate using faster-whisper's bundled Silero ONNX."""
import threading


class SpeechDetector:
    def __init__(self):
        self.lock = threading.Lock()
        self.error = ""

    def contains_speech(self, samples):
        """None means unavailable: callers must transcribe rather than discard audio."""
        with self.lock:
            if self.error:
                return None
            try:
                from faster_whisper.vad import VadOptions, get_speech_timestamps
                return bool(get_speech_timestamps(samples, VadOptions(
                    threshold=0.35, min_speech_duration_ms=0, speech_pad_ms=400)))
            except Exception as exc:
                self.error = str(exc)
                return None
