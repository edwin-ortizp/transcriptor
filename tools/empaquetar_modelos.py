"""Empaqueta los modelos instalados en este equipo como .zip para subirlos a la release de GitHub.

Uso:  python tools/empaquetar_modelos.py [carpeta_salida]
Por defecto escribe en ~/Downloads/transcriptor-modelos/. Los .zip NO van en el repositorio:
se suben como archivos de la release "modelos-v1" (límite de GitHub: 2 GB por archivo).
"""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import download  # noqa: E402

SP = Path(os.environ.get("LOCALAPPDATA", "")) / "SpeechPulse" / "models"

MIT_WHISPER = """MIT License

Copyright (c) 2022 OpenAI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

PACKAGES = {
    "turbo": {
        "src": SP / "multi_turbo",
        "license": ("LICENSE.txt", MIT_WHISPER),
        "origin": """# Whisper large-v3-turbo (formato CTranslate2)

- Modelo original: OpenAI `whisper-large-v3-turbo` — https://huggingface.co/openai/whisper-large-v3-turbo
- Licencia: MIT (ver LICENSE.txt).
- Conversión a CTranslate2: Mobius Labs — https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo
- Copia redistribuida sin modificaciones para el proyecto transcriptor, por si Hugging Face no es accesible.
""",
    },
    "parakeet": {
        "src": SP / "parakeet_tdt",
        "license": ("LICENSE.txt", """Este modelo se distribuye bajo Creative Commons Attribution 4.0 International (CC-BY-4.0).
Texto completo: https://creativecommons.org/licenses/by/4.0/legalcode

Atribución: "parakeet-tdt-0.6b-v3" por NVIDIA (https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).
Conversión a ONNX (int8) por istupakov (https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx).
Cambios respecto al original: conversión de formato (.nemo -> ONNX) y cuantización int8.
"""),
        "origin": """# Parakeet TDT 0.6B v3 (ONNX int8)

- Modelo original: NVIDIA `parakeet-tdt-0.6b-v3` — https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3
- Licencia: CC-BY-4.0 (ver LICENSE.txt).
- Conversión a ONNX: istupakov — https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx
- Copia redistribuida para el proyecto transcriptor, por si Hugging Face no es accesible.
""",
    },
}


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / "transcriptor-modelos"
    out.mkdir(parents=True, exist_ok=True)
    for key, pkg in PACKAGES.items():
        name = download.SPECS[key]["dir"]
        src = pkg["src"]
        if not src.is_dir():
            print(f"- {key}: no está en {src}, se omite")
            continue
        zpath = out / f"{name}.zip"
        print(f"- {key}: {src} -> {zpath}")
        # model.bin/onnx ya vienen comprimidos: ZIP_STORED es igual de pequeño y mucho más rápido
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for f in sorted(src.iterdir()):
                if f.is_file():
                    z.write(f, f"{name}/{f.name}")
            z.writestr(f"{name}/{pkg['license'][0]}", pkg["license"][1])
            z.writestr(f"{name}/ORIGEN.md", pkg["origin"])
        mb = zpath.stat().st_size / 2**20
        print(f"  {mb:,.0f} MB" + ("  (¡supera 2 GB, GitHub no lo aceptará!)" if mb > 2047 else ""))
    print(f"\nListo. Sube los .zip a la release 'modelos-v1': {out}")


if __name__ == "__main__":
    main()
