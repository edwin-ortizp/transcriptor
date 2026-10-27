"""Descarga de modelos a transcriptor/modelos/ la primera vez (equipos sin SpeechPulse).

Fuentes, en orden: Hugging Face y, si falla, la release de GitHub del proyecto (copia .zip del mismo modelo).
También se puede importar un .zip descargado a mano.
"""
import shutil
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

MODELS_DIR = Path(__file__).parent / "modelos"
GITHUB_RELEASE = "https://github.com/edwin-ortizp/transcriptor/releases/download/modelos-v1/"
SOURCES = {"auto": "Hugging Face y, si falla, GitHub", "github": "Solo GitHub", "huggingface": "Solo Hugging Face"}


def github_only():
    """No contactar Hugging Face en absoluto (ni las librerías por su cuenta)."""
    import os
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

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


def _from_huggingface(spec, tmp: Path, progress):
    from huggingface_hub import snapshot_download

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


def _from_github(spec, tmp: Path, progress):
    url = GITHUB_RELEASE + spec["dir"] + ".zip"
    zpath = tmp.with_suffix(".zip")
    with urllib.request.urlopen(url, timeout=30) as r, open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0) / 2**20 or spec["mb"]
        got, last = 0, 0.0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            got += len(chunk)
            if progress and time.time() - last > 0.5:
                last = time.time()
                progress(got / 2**20, int(total))
    try:
        _extract(zpath, tmp)
    finally:
        zpath.unlink(missing_ok=True)


def _extract(zpath: Path, dest: Path):
    """Extrae el .zip en `dest`, aplanando la carpeta raíz si el zip trae una."""
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        roots = {n.split("/", 1)[0] for n in names}
        strip = len(roots) == 1 and all("/" in n for n in names)
        dest.mkdir(parents=True, exist_ok=True)
        for n in names:
            rel = n.split("/", 1)[1] if strip else n
            target = (dest / rel).resolve()
            if dest.resolve() not in target.parents:
                raise ValueError(f"ruta no permitida en el zip: {n}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def download(key: str, progress=None, log=print, source: str = "auto") -> Path:
    """Descarga el modelo `key` y devuelve su carpeta. `progress(mb, total_mb)` se llama ~2 veces por segundo."""
    spec = SPECS[key]
    final = MODELS_DIR / spec["dir"]
    if final.exists():
        return final
    MODELS_DIR.mkdir(exist_ok=True)
    tmp = MODELS_DIR / (spec["dir"] + ".descargando")
    errors = []
    order = {"auto": [("Hugging Face", _from_huggingface), ("GitHub", _from_github)],
             "github": [("GitHub", _from_github)],
             "huggingface": [("Hugging Face", _from_huggingface)]}[source]
    for name, fetch in order:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            fetch(spec, tmp, progress)
            break
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            log(f"[descarga] {name} no disponible ({exc}); probando la siguiente fuente…")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(" | ".join(errors))
    tmp.rename(final)  # solo aparece como modelo cuando está completo
    if progress:
        progress(spec["mb"], spec["mb"])
    return final


def import_zip(zpath) -> Path:
    """Importa un modelo desde un .zip (p. ej. bajado a mano de la release de GitHub) a modelos/."""
    zpath = Path(zpath)
    MODELS_DIR.mkdir(exist_ok=True)
    name = zpath.stem
    final = MODELS_DIR / name
    if final.exists():
        raise FileExistsError(f"ya existe modelos/{name}")
    tmp = MODELS_DIR / (name + ".descargando")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        _extract(zpath, tmp)
        if not _looks_like_model(tmp):
            raise ValueError("el .zip no contiene un modelo Whisper (model.bin) ni Parakeet (encoder-model*.onnx)")
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    tmp.rename(final)
    return final


def _looks_like_model(d: Path) -> bool:
    return (d / "model.bin").is_file() or any(d.glob("encoder-model*.onnx"))


def cli(key: str, source: str = "auto"):
    if key not in SPECS:
        raise SystemExit(f"Modelo desconocido: {key}. Opciones: {', '.join(SPECS)}")
    spec = SPECS[key]
    print(f"Descargando {spec['label']} (~{spec['mb']} MB) a {MODELS_DIR / spec['dir']} · fuente: {SOURCES[source]} …")
    t = time.time()

    def show(mb, total):
        print(f"\r  {mb:7.0f} / {total} MB", end="", flush=True)

    path = download(key, show, log=lambda m: print("\n" + m), source=source)
    print(f"\nListo en {time.time() - t:.0f} s: {path}")


def cli_import(zpath: str):
    print(f"Importando {zpath} …")
    path = import_zip(zpath)
    print(f"Listo: {path}")
