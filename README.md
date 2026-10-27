# Transcriptor de reuniones (Whisper local)

Transcribe en vivo el audio que suena en el PC y, opcionalmente, el micrófono. Usa faster-whisper en local, así que el audio no sale del equipo.

## Uso

```bash
pip install -r requirements.txt
# Solo en equipos con GPU NVIDIA:
pip uninstall -y onnxruntime
pip install -r requirements-gpu.txt

python app.py                  # abre la ventana propia (puerto 8791 o el siguiente libre)
python app.py --browser        # en el navegador
python app.py --download turbo # descarga Whisper large-v3-turbo (~1,5 GB) y sale; también: parakeet
python app.py --cpu            # forzar CPU
python app.py --threads 4      # núcleos a usar en CPU
```

**Modelos:** la app busca primero en la carpeta `modelos/` y luego en las de SpeechPulse (`%LOCALAPPDATA%\SpeechPulse\models`, cuyas DLL de CUDA también aprovecha). Si un modelo no está, aparece en el selector como "(descargar …)": al pulsar Iniciar o "Descargar ahora" se baja **una sola vez** a `modelos/` con barra de progreso. Más carpetas: variable `TRANSCRIPTOR_MODELS_DIR`.

## Repositorio y modelos

El repositorio Git contiene **solo el código** (< 1 MB, más la fuente Nunito). El `.gitignore` deja fuera:

| Excluido | Por qué |
|---|---|
| `modelos/` | Pesan 0,6–1,5 GB; GitHub rechaza archivos > 100 MB. Cada equipo los obtiene una vez. |
| `transcripciones/` | Son las conversaciones grabadas: privadas. |
| `settings.json` | Preferencias de cada equipo (IDs de micrófono, etc.). Se crea solo con valores por defecto. |
| `transcriptor.log`, `__pycache__/` | Archivos generados. |

**De dónde salen los modelos** (se descargan de Hugging Face; son las mismas fuentes que usan por defecto las librerías faster-whisper y onnx-asr):

| Modelo | Repositorio que se descarga | Modelo original y licencia |
|---|---|---|
| Whisper large-v3-turbo | [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) — conversión a CTranslate2 de Mobius Labs | OpenAI `whisper-large-v3-turbo`, MIT |
| Parakeet TDT v3 | [istupakov/parakeet-tdt-0.6b-v3-onnx](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx) — conversión a ONNX (int8) | NVIDIA `parakeet-tdt-0.6b-v3`, CC-BY-4.0 |

**Copia en GitHub (si Hugging Face está bloqueado):** los mismos modelos están como `.zip` en la release [`modelos-v1`](https://github.com/edwin-ortizp/transcriptor/releases/tag/modelos-v1) de este repositorio, con su licencia y `ORIGEN.md` dentro (`whisper-large-v3-turbo.zip` ~1,5 GB, `parakeet-tdt-v3.zip` ~640 MB). Se generan con `python tools/empaquetar_modelos.py` a partir de los modelos de SpeechPulse. Los `.zip` no están en el historial de Git: son archivos adjuntos de la release.

**Formas de tener el modelo en un equipo:**
1. **Descarga automática** (`python app.py --download turbo` o el botón en la app): intenta Hugging Face y, si no responde, usa la release de GitHub.
   - **Solo GitHub** (equipos donde Hugging Face está bloqueado): `python app.py --download turbo --source github`, o en ⚙ → "Descargar modelos desde" → "Solo GitHub" (queda guardado). Así la app **nunca** contacta Hugging Face: ni para descargar ni las librerías por su cuenta (`HF_HUB_OFFLINE=1`). Con un modelo ya instalado, la app no hace ninguna conexión de red.
2. **Importar un .zip** bajado a mano desde la release (con el navegador): ⚙ → "Importar modelo (.zip)…" o `python app.py --import whisper-large-v3-turbo.zip`.
3. **Copiar la carpeta** de un modelo a `modelos/` (p. ej. `multi_turbo` de SpeechPulse).
4. **Tener SpeechPulse instalado.**

En ⚙, debajo del modelo, se ve de dónde viene el elegido.

## Instalar en un portátil (sin GPU ni SpeechPulse)

1. Instala Python 3.11 o superior y `git clone` del repositorio (trae solo el código).
2. En la carpeta: `pip install -r requirements.txt` (no instales `requirements-gpu.txt`).
3. Consigue el modelo **una vez**, de cualquiera de estas formas:
   - `python app.py --download turbo` (~1,5 GB), o
   - abre la app, elige "Whisper large-v3-turbo (descargar …)" y pulsa Iniciar, o
   - **sin internet:** copia la carpeta `%LOCALAPPDATA%\SpeechPulse\models\multi_turbo` del PC a `transcriptor\modelos\multi_turbo` en el portátil (USB o Sync). Se detecta sola.
4. Abre `transcriptor.bat`. En ⚙ → "Uso del procesador" eliges cuántos núcleos usa (Ligero por defecto).

Para tener un respaldo rápido: `python app.py --download parakeet` (~640 MB).

Las transcripciones se guardan en `transcripciones/AAAA-MM-DD_HHMMSS <título>.txt` (el título se edita en la cabecera de la ventana), una línea por fragmento:

```
[00:12:34] [Otros] texto de la persona remota
[00:12:41] [Yo] mi respuesta
```

**Ventana:** widget sin marco que se arrastra desde la cabecera o el título (doble clic en el título para renombrarlo). 📌 la deja siempre encima, ⚙ abre los ajustes, — minimiza y ✕ cierra (detiene la grabación). Los círculos YO/OT se iluminan cuando cada fuente habla. Tipografía Nunito incluida en `static/fonts` (licencia OFL). Sin consola: doble clic en `transcriptor.bat`.

**Fuentes en el pie, siempre a la mano y cambiables en caliente:** 🎙 enciende/apaga el micrófono (tachado = apagado) y su ▾ elige cuál; 🔊 hace lo mismo con la salida de audio capturada (Otros). Se pueden cambiar en plena grabación sin reiniciar: las horas siguen continuas y el .txt anota el cambio (`[00:12:03] — micrófono: Logitech BRIO —`).

**Ajustes (⚙):** modelo, idioma, transparencia (30–100 %, con opción "opaca al pasar el mouse") y diccionario. Todo se recuerda en `settings.json`; si un dispositivo guardado ya no existe, se usa el predeterminado.

## Diccionario de correcciones

Se configura en ⚙ (ruta, Examinar…, Aplicar, Editar). Por defecto usa `~/Sync/android-apps-backups/dictate-keyboard/dictate_mappings-tcl.tsv` (el mismo del teclado Dictate). Prioridad: `--dict ruta.tsv` > variable `TRANSCRIPTOR_DICT` > lo elegido en la UI > ruta por defecto. Varios archivos se separan con `;`.

Formato TSV: `de	a	word|any	case|nocase`. `word` reemplaza solo palabras completas; `nocase` ignora mayúsculas. Las reglas más largas ganan. Se recarga al iniciar cada grabación si el archivo cambió. Con Whisper, los nombres propios del diccionario también se pasan como `hotwords` para que acierte desde el inicio.

## Texto en vivo

Mientras alguien habla, la frase en curso se retranscribe cada ~0,6 s y se muestra en gris con un cursor (texto provisional). Al detectar una pausa, se reemplaza por la versión final: mejor contexto, diccionario aplicado y es la única que se guarda en el .txt. Se activa o desactiva en ⚙ → "Transcripción en vivo". Si el motor va lento (p. ej. Whisper en CPU), el provisional se pausa solo para no retrasar los finales.

## Whisper vs Parakeet

| | Whisper (large-v3-turbo) | Parakeet TDT v3 |
|---|---|---|
| Retraso | ~8–12 s | ~2–4 s |
| Idioma | se elige en la UI | autodetecta (25 idiomas europeos) |
| Precisión en español | la mejor | un poco menor |

El Parakeet de SpeechPulse (`models\parakeet_tdt`) se carga directo con `onnx-asr`; sin SpeechPulse se descarga de Hugging Face.

## Requisitos y equipos sin GPU

Medido en el PC de desarrollo forzando CPU (int8):

| Modelo | Núcleos | Velocidad | RAM |
|---|---|---|---|
| Whisper large-v3-turbo, bloques largos (~24 s) | 3 | ~2× tiempo real | ~1,9 GB |
| Whisper large-v3-turbo, bloques cortos (3–12 s) | 4 | ~1× (no alcanza) | ~1,9 GB |
| Parakeet TDT v3 | 4 | 14× tiempo real | ~0,8 GB |
| Parakeet TDT v3 | 2 | 10× tiempo real | ~0,8 GB |

Whisper siempre procesa una ventana de 30 s: en CPU tarda casi lo mismo con 3 s que con 24 s de audio. Por eso, **sin GPU, Whisper usa bloques largos (15–28 s) y no muestra texto en vivo**: así rinde ~2× tiempo real. El texto aparece por bloques, con unos 20–40 s de retraso; si se acumula trabajo, el estado muestra "con atraso" y se recupera en las pausas. Al terminar o cerrar, la app sigue hasta transcribir todo lo pendiente (nunca queda incompleto el .txt).

Un portátil de hace varios años puede ser 2–3× más lento que el PC de las mediciones: con Turbo puede ir justo; sube "Uso del procesador" a Equilibrado o Máximo si se atrasa, o usa Parakeet, que va sobrado.

**Mínimo recomendado sin GPU:** CPU de 4 núcleos con AVX2 (Intel Core 6.ª gen. / Ryzen o posterior), **8 GB de RAM** para Turbo (Parakeet funciona con menos) y ~3 GB de disco.

**Para no trabar el equipo:** la app siempre corre con prioridad "por debajo de lo normal" (Windows atiende primero a Teams, navegador, etc.). Sin GPU usa por defecto la mitad de los núcleos (⚙ → "Uso del procesador": Ligero / Equilibrado / Máximo, o `--threads N`). En las pruebas: Turbo en CPU ~20 % del procesador de media; Parakeet ~9 %.

## Notas
- **[Otros]** es todo lo que suena por la salida elegida (Teams, Meet, Zoom…). **[Yo]** es el micrófono elegido.
- Con el micrófono activo, usa audífonos; si no, el mic capta a los demás y el texto sale duplicado.
- No distingue entre las distintas personas remotas: todas quedan como [Otros].
