"""Descubre modelos instalados: Whisper (CTranslate2) y Parakeet (ONNX).

Busca en transcriptor/modelos/ (descargados o copiados a mano) y en las carpetas de SpeechPulse.
"""
import os
from pathlib import Path

import download

KNOWN = {
    "whisper-large-v3-turbo": "Whisper large-v3-turbo",
    "parakeet-tdt-v3": "Parakeet TDT v3 (rápido)",
    "multi_turbo": "SpeechPulse · large-v3-turbo",
    "multi_large": "SpeechPulse · large-v2",
    "english_base": "SpeechPulse · base.en (solo inglés)",
    "parakeet_tdt": "SpeechPulse · Parakeet TDT v3 (rápido)",
}
TURBO_IDS = ("whisper-large-v3-turbo", "multi_turbo")


def _search_dirs():
    dirs = [download.MODELS_DIR]
    if extra := os.environ.get("TRANSCRIPTOR_MODELS_DIR"):
        dirs += [Path(p) for p in extra.split(os.pathsep)]
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
        if base:
            dirs.append(Path(base) / "SpeechPulse" / "models")
    return dirs


def _engine_of(d: Path):
    if d.name.endswith(".descargando"):
        return None
    if (d / "model.bin").is_file() and (d / "config.json").is_file():
        return "whisper"
    if any(d.glob("encoder-model*.onnx")) and (d / "vocab.txt").is_file():
        return "parakeet"
    return None


def _source_of(d: Path) -> str:
    if d.parent == download.MODELS_DIR:
        return f"Local: modelos/{d.name}"
    if "SpeechPulse" in d.parts:
        return "SpeechPulse (instalado en este equipo)"
    return f"Local: {d}"


def is_turbo(model_id):
    return model_id in TURBO_IDS or "turbo" in model_id.lower()


def discover():
    found, seen = [], set()
    for root in _search_dirs():
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            engine = _engine_of(d) if d.is_dir() else None
            if engine and d.name not in seen:
                seen.add(d.name)
                found.append({"id": d.name, "label": KNOWN.get(d.name, d.name),
                              "path": str(d), "engine": engine, "source": _source_of(d)})
    # Turbo primero, luego Parakeet, luego el resto
    found.sort(key=lambda m: (not is_turbo(m["id"]), m["engine"] != "parakeet"))
    # Ofrecer la descarga solo de lo que no esté ya disponible
    has_turbo = any(is_turbo(m["id"]) for m in found)
    has_parakeet = any(m["engine"] == "parakeet" for m in found)
    for key, have, engine in (("turbo", has_turbo, "whisper"), ("parakeet", has_parakeet, "parakeet")):
        if not have:
            spec = download.SPECS[key]
            found.append({"id": "dl:" + key, "label": f"{spec['label']} (descargar ~{spec['mb']:,} MB)".replace(",", "."),
                          "path": "", "engine": engine, "download": key,
                          "source": f"huggingface.co/{spec['repo']} · {spec['origin']}"})
    return found


def resolve(model_id):
    for m in discover():
        if m["id"] == model_id:
            return m
    return None


def is_english_only(model_id):
    return model_id.endswith(".en") or model_id.startswith("english")


def cuda_dll_dirs():
    base = os.environ.get("LOCALAPPDATA")
    candidates = [Path(base) / "SpeechPulse" / "lib" / "whisper_infer"] if base else []
    return [d for d in candidates if (d / "cublas64_12.dll").is_file()]
