"""Preferencias persistentes en settings.json (junto a la app)."""
import json
import threading
from pathlib import Path

LOCK = threading.RLock()
PATH = Path(__file__).parent / "settings.json"
DEFAULTS = {
    "output_dir": "",
    "backup_audio": False,
    "draft_title": "Reunión",
    "draft_context": "",
    "template": "meeting",
    "analysis": {},
    "insights": {},
    "use_mic": False,
    "mic_id": "",
    "speaker_id": "",
    "language": "es",
    "model": "multi_turbo",
    "dict_paths": "",
    "opacity": 1.0,
    "opacity_hover": True,
    "live_partials": True,
    "cpu_mode": "ligero",
    "download_source": "auto",
}


def load() -> dict:
    data = dict(DEFAULTS)
    if PATH.is_file():
        try:
            data.update(json.loads(PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return data


def save(partial: dict) -> dict:
    with LOCK:
        data = load()
        data.update({k: v for k, v in partial.items() if k in DEFAULTS})
        temporary = PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(PATH)
        return data
