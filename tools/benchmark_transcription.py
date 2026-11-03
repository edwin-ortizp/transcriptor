"""Offline comparison. Run --help; the manifest names every input explicitly."""
import argparse
import contextlib
import csv
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import unicodedata
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def normalize(text):
    text = unicodedata.normalize("NFC", text).casefold()
    return " ".join("".join(" " if unicodedata.category(c)[0] in "PS" else c
                            for c in text).split())


def distance(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for i, expected in enumerate(reference, 1):
        current = [i]
        for j, actual in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (expected != actual)))
        previous = current
    return previous[-1]


def scores(reference, hypothesis):
    expected, actual = normalize(reference), normalize(hypothesis)
    words = expected.split()
    chars = expected.replace(" ", "")
    word_errors = distance(words, actual.split())
    char_errors = distance(chars, actual.replace(" ", ""))
    return {"wer": word_errors / len(words) if words else (0 if not actual else None),
            "cer": char_errors / len(chars) if chars else (0 if not actual else None),
            "word_errors": word_errors, "reference_words": len(words),
            "char_errors": char_errors, "reference_chars": len(chars)}


def read_wav(path):
    import numpy as np
    import audio
    try:
        with wave.open(str(path), "rb") as wav:
            rate, channels = wav.getframerate(), wav.getnchannels()
            if (wav.getsampwidth() != 2 or channels not in (1, 2)
                    or rate not in (16000, 48000) or wav.getcomptype() != "NONE"):
                raise ValueError("Se requiere WAV PCM de 16 bits, mono/estéreo, a 16 o 48 kHz")
            raw = wav.readframes(wav.getnframes())
            if not raw or len(raw) != wav.getnframes() * channels * 2:
                raise ValueError("WAV vacío o incompleto")
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, channels) / 32768
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"WAV no compatible: {exc}") from exc
    prepared = audio._to_16k_mono(samples) if rate == 48000 else samples.mean(axis=1)
    if not len(prepared):
        raise ValueError("WAV demasiado corto")
    return prepared


def peak_ram_bytes():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def run_worker(config):
    # Set before importing model libraries. No settings, app, capture or mappings.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import transcriber
    from speech_detection import SpeechDetector
    device = config["device"]
    if device == "auto":
        device = "cuda" if transcriber.has_gpu() else "cpu"
    started = time.perf_counter()
    engine = transcriber.load_model(config["model"], device == "cpu", config["threads"], strict=True)
    load_seconds = time.perf_counter() - started
    detector = SpeechDetector() if config["vad"] else None
    engine.vad_filter = not config["vad"]
    rows = []
    for sample in config["samples"]:
        samples = read_wav(sample["audio"])
        def infer():
            texts = []
            size = 12 * 16000
            for offset in range(0, len(samples), size):
                chunk = samples[offset:offset + size]
                if detector:
                    detected = detector.contains_speech(chunk)
                    if detected is None:
                        raise RuntimeError("Silero no disponible: " + detector.error)
                    if not detected:
                        continue
                texts.append(engine.transcribe(chunk, config["language"] or None))
            return " ".join(text for text in texts if text)
        infer()  # one warm-up per sample; never included in the measured runs
        times, texts = [], []
        for _ in range(3):
            started = time.perf_counter()
            texts.append(infer())
            times.append(time.perf_counter() - started)
        median_index = sorted(range(3), key=times.__getitem__)[1]
        text, seconds = texts[median_index], statistics.median(times)
        rows.append({"sample": sample["audio"], "reference": sample["reference"],
                     "transcription": text, "transcriptions": texts,
                     "audio_seconds": len(samples) / 16000, "inference_seconds": seconds,
                     "repetition_seconds": times, "rtf": seconds / (len(samples) / 16000),
                     **scores(sample["reference"], text)})
    versions = {}
    for package in ("faster-whisper", "ctranslate2", "onnx-asr", "onnxruntime", "onnxruntime-gpu", "numpy"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {"model": config["model"], "device": engine.device, "requested_device": config["device"],
            "vad": config["vad"], "whisper_internal_vad": not config["vad"] if engine.name == "whisper" else False,
            "language": config["language"], "threads": config["threads"],
            "block_seconds": 12, "warmups": 1, "repetitions": 3,
            "load_seconds": load_seconds, "process_peak_ram_bytes": peak_ram_bytes(),
            "pid": os.getpid(), "python": platform.python_version(), "platform": platform.platform(),
            "versions": versions, "samples": rows}


def prepare_manifest(path):
    import models
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data.get("samples"), list) or not data["samples"]:
        raise ValueError("El manifiesto requiere samples con audio y reference (o reference_file)")
    samples = []
    for item in data["samples"]:
        audio_path = (path.parent / item["audio"]).resolve()
        # Validate every file before loading any model.
        read_wav(audio_path)
        reference = item.get("reference")
        if reference is None:
            reference = (path.parent / item["reference_file"]).read_text(encoding="utf-8-sig")
        if not isinstance(reference, str):
            raise ValueError("reference debe ser texto")
        samples.append({"audio": str(audio_path), "reference": reference})
    chosen = []
    if not isinstance(data.get("models"), list) or not data["models"]:
        raise ValueError("El manifiesto requiere models con IDs de modelos instalados")
    for model_id in data["models"]:
        info = models.resolve(model_id)
        if info is None or info.get("download"):
            raise ValueError(f"Modelo no instalado: {model_id}")
        chosen.append(info)
    return data, samples, chosen


def execute(config):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker"],
                            input=json.dumps(config), text=True, encoding="utf-8", capture_output=True, timeout=600,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "El proceso del modelo terminó sin resultado")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("--output", type=Path, default=Path("benchmark-results"), help="Prefijo de JSON y CSV (no sobrescribe)")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"))
    parser.add_argument("--vad", choices=("on", "off", "both"), default="both")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        with contextlib.redirect_stdout(sys.stderr):
            result = run_worker(json.load(sys.stdin))
        print(json.dumps(result, ensure_ascii=True))
        return
    if args.manifest is None or args.threads < 1:
        parser.error("Indica un manifiesto y --threads mayor que cero")
    try:
        data, samples, chosen = prepare_manifest(args.manifest.resolve())
        device = args.device or data.get("device", "auto")
        if device not in ("auto", "cpu", "cuda"):
            raise ValueError("device debe ser auto, cpu o cuda")
        json_path, csv_path = args.output.with_suffix(".json"), args.output.with_suffix(".csv")
        if json_path.exists() or csv_path.exists():
            raise ValueError("La salida ya existe; elige otro --output")
        results = []
        for model in chosen:
            for vad in ([True, False] if args.vad == "both" else [args.vad == "on"]):
                config = {"model": model, "device": device, "vad": vad, "samples": samples,
                          "language": data.get("language", "es"), "threads": args.threads}
                print(f"Comparando {model['id']} · {device} · VAD {'on' if vad else 'off'}", file=sys.stderr)
                try:
                    results.append(execute(config))
                except Exception as exc:
                    results.append({"model": model, "requested_device": device, "vad": vad, "error": str(exc)})
        payload = {"version": 1, "normalization": "NFC, casefold, puntuación/símbolos como separadores, acentos conservados; CER sin espacios",
                   "memory": "Pico RAM del proceso completo, incluye calentamiento y todas sus muestras; no VRAM",
                   "results": results}
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with json_path.open("x", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
        fields = ("model", "device", "vad", "sample", "reference", "transcription", "wer", "cer",
                  "load_seconds", "inference_seconds", "audio_seconds", "rtf", "process_peak_ram_bytes", "error")
        with csv_path.open("x", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for result in results:
                common = {k: result.get(k, "") for k in fields}
                common["model"] = result["model"]["id"]
                for row in result.get("samples", [{}]):
                    writer.writerow({**common, **{k: v for k, v in row.items() if k in fields}})
        print(f"Resultados: {json_path} y {csv_path}")
        if any("error" in result for result in results):
            return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
