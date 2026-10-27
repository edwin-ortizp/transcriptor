"""Preferencias persistentes en settings.json (junto a la app)."""
import json
from pathlib import Path

PATH = Path(__file__).parent / "settings.json"
DEFAULTS = {
    "output_dir": "",
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
    data = load()
    data.update({k: v for k, v in partial.items() if k in DEFAULTS})
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
