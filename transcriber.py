"""Motores de transcripción: faster-whisper y Parakeet (onnx-asr), con fallback a CPU."""
import os
import gc

import models

for _d in models.cuda_dll_dirs():  # reutiliza cuBLAS/cuDNN de SpeechPulse
    os.add_dll_directory(str(_d))
    os.environ["PATH"] = str(_d) + os.pathsep + os.environ.get("PATH", "")


CPU_MODES = {"ligero": "mitad de los núcleos", "equilibrado": "todos menos 2", "maximo": "todos menos 1"}


def cpu_threads(mode="ligero"):
    """Núcleos a usar en CPU. 'ligero' (por defecto) deja la mitad libres para no trabar el equipo."""
    n = os.cpu_count() or 2
    if mode == "maximo":
        return max(1, n - 1)
    if mode == "equilibrado":
        return max(1, n - 2, n // 2)
    return max(1, n // 2)


def has_gpu():
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return True
    except Exception:
        pass
    try:
        import onnxruntime as ort
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


class WhisperEngine:
    name = "whisper"
    chunk = (6.0, 20.0)  # segundos mín./máx. por bloque (Whisper admite hasta 30 s)
    live_ok = True  # admite texto provisional
    vad_filter = True
    hotwords = ""  # términos del diccionario para orientar a Whisper

    def __init__(self, path, force_cpu=False, threads=None, strict=False):
        from faster_whisper import WhisperModel
        self.device = "cpu"
        if not force_cpu:
            try:
                self.model = WhisperModel(path, device="cuda", compute_type="float16")
                self.device = "cuda"
                return
            except Exception as exc:  # sin CUDA / cuDNN
                if strict:
                    raise
                print(f"[transcriptor] CUDA no disponible ({exc}); usando CPU.")
        self.model = WhisperModel(path, device="cpu", compute_type="int8",
                                  cpu_threads=threads or cpu_threads())
        # En CPU Whisper tarda lo mismo con 3 s que con 28 s (siempre procesa 30 s de ventana):
        # bloques largos y sin texto provisional para que el procesador alcance.
        self.chunk = (15.0, 28.0)
        self.live_ok = False

    def transcribe(self, audio, language=None, prompt=None, fast=False) -> str:
        segments, _ = self.model.transcribe(
            audio,
            language=language,
            vad_filter=self.vad_filter,
            beam_size=1 if fast or self.device == "cpu" else 5,
            initial_prompt=prompt,
            hotwords=self.hotwords or None,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()


class ParakeetEngine:
    name = "parakeet"
    chunk = (3.0, 12.0)  # con texto en vivo se pueden cortar frases más largas
    live_ok = True
    MODEL = "nemo-parakeet-tdt-0.6b-v3"

    def __init__(self, path, force_cpu=False, threads=None, strict=False):
        import onnx_asr
        import onnxruntime as ort
        ort.set_default_logger_severity(3)  # oculta avisos de rendimiento
        local = path if path != self.MODEL else None
        providers = ["CPUExecutionProvider"]
        if not force_cpu and "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")
        if strict and not force_cpu and len(providers) == 1:
            raise RuntimeError("CUDAExecutionProvider no está disponible")
        cpu_opts = ort.SessionOptions()
        cpu_opts.intra_op_num_threads = threads or cpu_threads()
        cpu_opts.inter_op_num_threads = 1
        try:
            opts = None if len(providers) > 1 else cpu_opts
            self.model = onnx_asr.load_model(self.MODEL, local, quantization="int8",
                                             providers=providers, sess_options=opts)
        except Exception as exc:
            if strict or len(providers) == 1:
                raise
            print(f"[transcriptor] CUDA no disponible para Parakeet ({exc}); usando CPU.")
            providers = ["CPUExecutionProvider"]
            self.model = onnx_asr.load_model(self.MODEL, local, quantization="int8",
                                             providers=providers, sess_options=cpu_opts)
        if not force_cpu or strict:
            sessions = [getattr(getattr(self.model, "asr", None), name, None) for name in ("_encoder", "_decoder_joint")]
            if strict and not all(session is not None for session in sessions):
                raise RuntimeError("No se pueden verificar los proveedores ONNX de este modelo")
            for session in sessions:
                if session is None:
                    continue
                session.disable_fallback()
                if strict and not force_cpu and "CUDAExecutionProvider" not in session.get_providers():
                    raise RuntimeError("ONNX cambió a CPU al cargar; comparación CUDA cancelada")
        self.device = "cuda" if providers[0].startswith("CUDA") else "cpu"

    def transcribe(self, audio, language=None, prompt=None, fast=False) -> str:
        return str(self.model.recognize(audio, sample_rate=16000)).strip()


def load_model(info: dict, force_cpu: bool = False, threads: int | None = None, strict: bool = False):
    engine = ParakeetEngine if info.get("engine") == "parakeet" else WhisperEngine
    return engine(info["path"], force_cpu, threads, strict=strict)


def is_cuda_error(exc):
    message = str(exc).lower()
    return any(word in message for word in (
        "cuda", "cudnn", "cublas", "cufft", "cudaexecutionprovider"))


def release_engine(engine):
    # Drop native weights even when a worker still holds the lightweight adapter.
    if hasattr(engine, "model"):
        engine.model = None
    gc.collect()
