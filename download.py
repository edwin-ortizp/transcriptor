"""Descarga de modelos a transcriptor/modelos/ la primera vez (equipos sin SpeechPulse)."""
import shutil
import threading
import time
from pathlib import Path

MODELS_DIR = Path(__file__).parent / "modelos"

SPECS = {
    "turbo": {
        "repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "dir": "whisper-large-v3-turbo",
        "patterns": ["model.bin", "config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json"],
        "mb": 1550,
        "label": "Whisper large-v3-turbo",
        "origin": "OpenAI Whisper large-v3-turbo (MIT), convertido a CTranslate2 por Mobius Labs",
    },
    "parakeet": {
        "repo": "istupakov/parakeet-tdt-0.6b-v3-onnx",
        "dir": "parakeet-tdt-v3",
        "patterns": ["*.int8.onnx", "vocab.txt", "config.json", "nemo128.onnx"],
        "mb": 640,
        "label": "Parakeet TDT v3",
        "origin": "NVIDIA parakeet-tdt-0.6b-v3 (CC-BY-4.0), convertido a ONNX por istupakov",
    },
}


def _size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 2**20


def download(key: str, progress=None) -> Path:
    """Descarga el modelo `key` y devuelve su carpeta. `progress(mb, total_mb)` se llama ~2 veces por segundo."""
    from huggingface_hub import snapshot_download

    spec = SPECS[key]
    final = MODELS_DIR / spec["dir"]
    if final.exists():
        return final
    MODELS_DIR.mkdir(exist_ok=True)
    tmp = MODELS_DIR / (spec["dir"] + ".descargando")
    done = threading.Event()

    def watch():
        while not done.wait(0.5):
            if progress:
                progress(_size_mb(tmp), spec["mb"])

    threading.Thread(target=watch, daemon=True).start()
    try:
        snapshot_download(spec["repo"], local_dir=tmp, allow_patterns=spec["patterns"])
    finally:
        done.set()
    shutil.rmtree(tmp / ".cache", ignore_errors=True)
    tmp.rename(final)  # solo aparece como modelo cuando está completo
    if progress:
        progress(spec["mb"], spec["mb"])
    return final


def cli(key: str):
    if key not in SPECS:
        raise SystemExit(f"Modelo desconocido: {key}. Opciones: {', '.join(SPECS)}")
    spec = SPECS[key]
    print(f"Descargando {spec['label']} (~{spec['mb']} MB) a {MODELS_DIR / spec['dir']} …")
    t = time.time()

    def show(mb, total):
        print(f"\r  {mb:7.0f} / {total} MB", end="", flush=True)

    path = download(key, show)
    print(f"\nListo en {time.time() - t:.0f} s: {path}")
