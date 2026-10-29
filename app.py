"""Transcriptor de reuniones local: python app.py (ventana propia) o python app.py --browser"""
import json
from contextlib import asynccontextmanager, ExitStack, contextmanager
from typing import Literal
import argparse
import asyncio
import os
import re
import socket
import sys
import threading
import time
import webbrowser
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import audio
import recording
import analysis
import insights
import trash
from insight_rules import InsightRules
from session_repository import SessionRepository, file_stem
from session_edits import SessionEdits, text_revision
from probe import AudioProbe
import corrections
from mapping_store import MappingStore, MappingConflict
from vocabulary import Vocabulary
from personal_terms import PersonalTerms, normalized as normalize_term
from review import LocalSpelling, ReviewService
import download
import models
import settings
import transcriber

BASE = Path(__file__).parent
OUT_DIR = BASE / "transcripciones"
OUT_DIR.mkdir(exist_ok=True)

def output_dir(value=None):
    value = settings.load()["output_dir"] if value is None else value
    path = Path(os.path.expandvars(value)).expanduser() if value.strip() else OUT_DIR
    return (BASE / path).resolve() if not path.is_absolute() else path.resolve()


def validate_output(value):
    path = output_dir(value)
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=path):
        pass
    return path


if sys.stdout is None:  # pythonw: sin consola, el log va a un archivo
    sys.stdout = sys.stderr = open(BASE / "transcriptor.log", "a", encoding="utf-8", buffering=1)
DEFAULT_PORT = 8791

parser = argparse.ArgumentParser()
parser.add_argument("--cpu", action="store_true", help="forzar CPU")
parser.add_argument("--browser", action="store_true", help="usar el navegador en vez de la ventana propia")
parser.add_argument("--no-browser", action="store_true", help="solo servidor")
parser.add_argument("--dict", help="TSV(s) de correcciones separados por ;")
parser.add_argument("--port", type=int, default=DEFAULT_PORT)
parser.add_argument("--threads", type=int, help="núcleos máximos en CPU (por defecto, según 'Uso del procesador')")
parser.add_argument("--download", choices=list(download.SPECS), help="descargar un modelo y salir")
parser.add_argument("--source", choices=list(download.SOURCES),
                    help="de dónde descargar: auto (Hugging Face y luego GitHub), github o huggingface")
parser.add_argument("--import", dest="import_zip", metavar="ZIP", help="importar un modelo desde un .zip y salir")
ARGS, _ = parser.parse_known_args()


def lower_priority():
    """Prioridad 'por debajo de lo normal': Windows atiende primero a Teams, el navegador, etc."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass(ctypes.c_void_p(k32.GetCurrentProcess()), 0x00004000)  # BELOW_NORMAL
    except Exception:
        pass


lower_priority()


def download_source():
    return ARGS.source or settings.load()["download_source"]


if download_source() == "github":
    download.github_only()
GPU = (not ARGS.cpu) and transcriber.has_gpu()

@asynccontextmanager
async def lifespan(_app):
    global loop, shutting_down
    loop = asyncio.get_running_loop()
    shutting_down = False
    try:
        repository.list()
        try:
            get_analysis_queue().start()
        except (OSError, ValueError, KeyError):
            broadcast({'type':'analysis', 'session_id':None, 'state':'error', 'error':'No se pudo recuperar la cola guardada'})
        yield
    finally:
        shutting_down = True
        try:
            if analysis_queue:
                analysis_queue.stop()
            await asyncio.to_thread(probe.stop)
            session.stop()
            if session.worker:
                await asyncio.to_thread(session.worker.join)
        finally:
            loop = None


shutting_down = False
app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
clients: set[WebSocket] = set()
loop: asyncio.AbstractEventLoop | None = None
desktop = None
analysis_queue = None
analysis_queue_lock = threading.Lock()
window = None  # ventana pywebview, si se usa
def dictionary_selection():
    if ARGS.dict:
        return ARGS.dict, '--dict'
    if os.environ.get('TRANSCRIPTOR_DICT'):
        return os.environ['TRANSCRIPTOR_DICT'], 'TRANSCRIPTOR_DICT'
    if settings.load()['dict_paths']:
        return settings.load()['dict_paths'], 'Preferencias'
    return str(corrections.DEFAULT), 'Predeterminado (Dictate)'


fixes = corrections.Corrections(dictionary_selection()[0])
dictionary_lock = threading.RLock()


def current_corrections():
    global fixes
    with dictionary_lock:
        raw, _ = dictionary_selection()
        paths = [Path(p.strip()) for p in raw.split(';') if p.strip()]
        if paths != fixes.paths:
            fixes = corrections.Corrections(raw)
        fixes.reload_if_changed()
        return fixes


mapping_store = MappingStore(lambda:current_corrections().paths, protected_roots=(BASE / "dictionaries",))
vocabulary = Vocabulary(BASE / 'dictionaries')


def broadcast(msg: dict):
    if desktop is not None:
        desktop.activity(msg)
    if loop is None or loop.is_closed():
        return
    for ws in list(clients):
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)


class Session:
    def __init__(self):
        self.context = ""
        self.finalized = False
        self.template = "meeting"
        self.created_at = None
        self.backup_enabled = False
        self.backup = None
        self.archive_path = None
        self.archive_lock = threading.RLock()
        self.saved_parts = []
        self.markers = []
        self.insights = None
        self.duration = 0.0
        self.backup_error = ""
        self.archive_error = ""
        self.lock = threading.RLock()
        self.file_lock = threading.Lock()
        self.revision = 0
        self.state = "ready"
        self.session_id = None
        self.status_text = "Listo"
        self.paused_at = None
        self.partial_worker = None
        self.history = []
        self.failure_text = ""
        self.title = "Reunión"
        self.model_name = None
        self.language = None
        self.running = False  # sesión pedida (incluye la carga del modelo)
        self.active = False   # capturas y archivo listos: ya se pueden cambiar fuentes en caliente
        self.models = {}
        self.captures = {}   # etiqueta -> SourceCapture activa
        self.retired = []    # capturas detenidas que aún pueden entregar audio
        self.worker = None
        self.file = None
        self.path = None
        self.queue = None
        self.t0 = 0.0
        self.chunk = (6.0, 15.0)
        self.last_level = 0.0
        self.gpu = threading.Lock()          # finales y parciales no usan el motor a la vez
        self.partials = {}                   # etiqueta -> (inicio, audio) más reciente
        self.partials_cv = threading.Condition()
        self.last_final = {}                 # etiqueta -> inicio del último bloque final
        self.partials_paused_until = 0.0
        self.partial_last = {}
        self.engine = None
        self.base_status = ""
        self.lagging = False
        prefs = settings.load()
        self.live = prefs["live_partials"]
        self.sources = {
            "Yo": {"enabled": prefs["use_mic"], "id": prefs["mic_id"]},
            "Otros": {"enabled": True, "id": prefs["speaker_id"]},
        }

    def snapshot(self):
        return {"type": "status", "revision": self.revision, "state": self.state, "session_id": self.session_id,
                "path": str(self.path) if self.path else None,
                "running": self.active, "text": self.status_text,
                "title": self.title, "model": self.model_name, "language": self.language,
                "context": self.context, "template": self.template, "created_at": self.created_at, "backup_enabled": self.backup_enabled,
                "backup_next": settings.load()["backup_audio"], "backup_active": self.backup is not None,
                "failure_text": self.failure_text,
                "backup_error": self.backup_error, "archive_error": self.archive_error, "archive_path": str(self.archive_path) if self.archive_path else None,
                "markers": list(self.markers), "duration": self.elapsed()}

    def status(self, state, text):
        self.revision += 1
        self.state, self.status_text = state, text
        broadcast(self.snapshot())

    def emit(self, msg):
        msg = {**msg, "session_id": self.session_id}
        if msg["type"] in ("text", "note"):
            msg.setdefault("id", uuid.uuid4().hex)
            self.history.append(msg)
            self.persist()
        broadcast(msg)
        if self.insights and msg["type"] in ("text", "note"):
            self.insights.feed(msg)

    def elapsed(self):
        if not self.t0:
            return self.duration
        if self.state == "finished":
            return self.duration
        return max(0.0, (self.paused_at or time.monotonic()) - self.t0)

    def prompt(self, recent):
        return "\n".join(x for x in (self.context, recent) if x) or None

    def _backup_submit(self, source, start, samples):
        # Captures keep this callback; the backup itself can be switched on and off mid-session.
        backup = self.backup
        if backup is not None:
            backup.submit(source, start, samples)

    def set_backup(self, enabled):
        with self.lock:
            if self.state in ("ready", "finished"):
                # Between sessions the switch is the preference for the next recording.
                settings.save({"backup_audio": enabled})
                broadcast({"type": "preparation", **preparation()})
                broadcast(self.snapshot())
                return self.snapshot()
            if self.state not in ("recording", "paused") or not self.archive_path:
                raise HTTPException(409, "Espera a que la sesión termine de iniciar o de pausar")
            if enabled == bool(self.backup):
                return self.snapshot()
            if enabled:
                self.backup_error = ""
                self.backup = recording.AudioBackup(self.archive_path.parent, self.backup_failed)
            else:
                backup, self.backup = self.backup, None
                backup.close(self.elapsed())
                self.saved_parts = backup.manifest()
            self.backup_enabled = enabled
            self._note("Respaldo de audio activado" if enabled else "Respaldo de audio desactivado")
            self.persist()
            self.revision += 1
            broadcast(self.snapshot())
            return self.snapshot()

    def backup_failed(self, message):
        self.backup_error = message
        broadcast({"type": "backup_error", "session_id": self.session_id, "text": message})

    def persist(self):
        if not self.archive_path:
            return
        with self.archive_lock:
            data = {"version": 2, "template": self.template, "created_at": self.created_at, "session_id": self.session_id, "title": self.title,
                    "context": self.context, "model": self.model_name, "language": self.language,
                    "txt": "../" + self.path.name, "duration": self.elapsed(),
                    "history": list(self.history), "markers": list(self.markers),
                    "insights": self.insights.public() if self.insights else [],
                    "backup_enabled": self.backup_enabled, "backup_error": self.backup_error,
                    "failure_text": self.failure_text,
                    "parts": self.backup.manifest() if self.backup else self.saved_parts}
            try:
                recording.atomic_json(self.archive_path, data)
                self.archive_error = ""
            except (OSError, ValueError) as exc:
                self.archive_error = f"No se pudieron guardar los datos de sesión: {exc}"
                broadcast({"type": "archive_error", "session_id": self.session_id, "text": self.archive_error})

    def marker(self, label=None):
        with self.lock:
            if self.state != "recording":
                raise HTTPException(409, "Solo se puede marcar mientras se graba")
            moment = self.elapsed()
            marker = {"id": uuid.uuid4().hex, "start": moment,
                      "text": label or f"Marcador {len(self.markers) + 1}"}
            self.markers.append(marker)
            self._note(f"Marcador: {marker['text']}", marker["id"], moment)
            self.persist()
            broadcast({"type": "markers", "session_id": self.session_id, "markers": self.markers})
            return marker

    def begin(self, req):
        with self.lock:
            if self.state != "ready":
                raise HTTPException(409, "La sesión no está lista para iniciar")
            try:
                self.corrections = current_corrections().snapshot()
            except (OSError, UnicodeError):
                raise HTTPException(400, "No se pudieron leer los mappings; revísalos en Diccionario")
            prefs = settings.load()
            self.context = (req.context if req.context is not None else prefs["draft_context"]).strip()
            self.template = req.template or prefs["template"]
            self.created_at = datetime.now().isoformat(timespec="seconds")
            self.backup_enabled = req.backup_audio if req.backup_audio is not None else prefs["backup_audio"]
            self.duration = 0.0
            self.finalized = False
            self.title = req.title.strip() or prefs["draft_title"] or "Reunión"
            self.model_name, self.language = req.model, req.language
            self.failure_text = ""
            self.output_folder = output_dir()
            self.session_id = uuid.uuid4().hex
            self.insights = make_insight_engine(self)
            self.status("starting", "Iniciando…")
            self.running = True
            self.worker = threading.Thread(target=self.start, args=(req,), daemon=True)
            self.worker.start()
        return self.snapshot()

    def new_session(self):
        with self.lock:
            if self.state != "finished":
                raise HTTPException(409, "Primero termina la sesión actual")
            self.context = ""
            self.created_at = None
            self.finalized = False
            self.backup = None
            self.archive_path = None
            self.saved_parts = []
            self.backup_error = ""
            self.archive_error = ""
            self.markers = []
            self.duration = 0.0
            self.t0 = 0.0
            self.path = None
            self.queue = None
            self.engine = None
            self.partial_worker = None
            self.partial_last = {}
            self.partials_paused_until = 0.0
            self.session_id = None
            self.history = []
            self.insights = None
            self.title = "Reunión"
            self.model_name = self.language = None
            self.status("ready", "Listo para una nueva sesión")
        return self.snapshot()

    def pause(self):
        with self.lock:
            if self.state != "recording":
                raise HTTPException(409, "Solo se puede pausar mientras se graba")
            self.active = False
            self.paused_at = time.monotonic()
            for label in list(self.captures):
                self._close_capture(label)
            self.status("pausing", "Pausando… procesando el audio pendiente")
        return self.snapshot()

    def resume(self):
        with self.lock:
            if self.state != "paused":
                raise HTTPException(409, "La sesión todavía no está pausada")
            self.t0 += time.monotonic() - self.paused_at
            self.paused_at = None
            self.retired = []
            try:
                for label in ("Otros", "Yo"):
                    if self.sources[label]["enabled"]:
                        self._open_capture(label)
                self.active = True
                self._note("Reanudación")
                self.status("recording", self.base_status)
            except Exception as exc:
                for label in list(self.captures):
                    self._close_capture(label)
                self.running = False
                self.status("finishing", f"Error al reanudar: {exc}. Terminando…")
        return self.snapshot()

    def get_model(self, model_id):
        info = models.resolve(model_id)
        if info is None or info.get("download"):
            raise RuntimeError("ese modelo aún no está descargado")
        threads = ARGS.threads or transcriber.cpu_threads(settings.load()["cpu_mode"])
        key = (model_id, threads)
        if key not in self.models:
            self.models = {k: v for k, v in self.models.items() if k[0] != model_id}  # libera la versión anterior
            broadcast({"type": "status", "text": f"Cargando {info['label']}…"})
            self.models[key] = transcriber.load_model(info, ARGS.cpu or not GPU, threads)
        return self.models[key]

    def pending_seconds(self, extra=0):
        q = self.queue
        if q is None:
            return 0
        with q.mutex:
            samples = sum(len(item[2]) for item in q.queue)
        return int((samples + extra) / audio.TARGET_RATE)

    def _report_backlog(self, current_len):
        pend = self.pending_seconds(current_len)
        if not self.active:
            if pend > 0:
                broadcast({"type": "status", "text": f"{'Pausando' if self.state == 'pausing' else 'Terminando'}… quedan ~{pend} s por transcribir"})
            return
        waiting = self.pending_seconds()  # bloques en cola además del que se está transcribiendo
        if waiting >= 4:
            self.lagging = True
            broadcast({"type": "status", "text": f"Transcribiendo con atraso: ~{pend} s pendientes"})
        elif self.lagging:
            self.lagging = False
            broadcast({"type": "status", "text": self.base_status})

    def level(self, label, rms):
        now = time.monotonic()
        if now - self.last_level > 0.2 or label == "Yo":
            self.last_level = now
            broadcast({"type": "level", "source": label, "value": min(rms * 20, 1.0)})

    def sources_state(self):
        return {"type": "sources", "running": self.active,
                **{k: {"enabled": v["enabled"], "id": v["id"],
                       "name": self.captures[k].device.name if k in self.captures else ""}
                   for k, v in self.sources.items()}}

    def _note(self, text, marker_id=None, moment=None):
        if not self.file:
            return
        moment = self.elapsed() if moment is None else moment
        ts = time.strftime("%H:%M:%S", time.gmtime(moment))
        with self.file_lock:
            self.file.write(f"[{ts}] — {text} —\n")
            self.file.flush()
        self.emit({"type": "note", "time": ts, "start": moment, "marker_id": marker_id, "text": text})

    def _open_capture(self, label):
        """Arranca la captura de una fuente con su dispositivo elegido. Devuelve un aviso o None."""
        src = self.sources[label]
        if label == "Yo":
            dev, fell_back = audio.mic(src["id"])
        else:
            dev, fell_back = audio.system_loopback(src["id"])
        mn, mx = self.chunk
        cap = audio.SourceCapture(label, dev, self.queue, self.t0, self.level, mn, mx, self.on_partial, self._backup_submit)
        self.captures[label] = cap
        cap.start()
        if not cap.ready.wait(5):
            raise RuntimeError(f"No responde el dispositivo de {label}")
        if cap.error:
            raise RuntimeError(f"No se pudo abrir {label}: {cap.error}")
        if fell_back:
            return f"{'micrófono' if label == 'Yo' else 'salida'} elegido no disponible, uso el predeterminado"
        return None

    def _close_capture(self, label):
        cap = self.captures.pop(label, None)
        if cap:
            cap.stop()
            self.retired.append(cap)

    def set_source(self, label, enabled=None, device_id=None):
        with self.lock:
            if self.state in ("starting", "pausing", "finishing"):
                raise HTTPException(409, "Espera a que termine la transición")
            src = self.sources[label]
            changed_dev = device_id is not None and device_id != src["id"]
            if device_id is not None:
                src["id"] = device_id
            if enabled is not None:
                src["enabled"] = enabled
            if label == "Yo":
                settings.save({"use_mic": src["enabled"], "mic_id": src["id"]})
            else:
                settings.save({"speaker_id": src["id"]})
            if self.active:
                active = label in self.captures
                if active and (not src["enabled"] or changed_dev):
                    self._close_capture(label)
                    if not src["enabled"]:
                        self._note(f"{'micrófono' if label == 'Yo' else 'audio de la reunión'} apagado")
                if src["enabled"] and label not in self.captures:
                    warn = self._open_capture(label)
                    name = self.captures[label].device.name
                    self._note(f"{'micrófono' if label == 'Yo' else 'audio de la reunión'}: {name}"
                               + (f" ({warn})" if warn else ""))
        state = self.sources_state()
        broadcast(state)
        return state

    def start(self, req):
        language, model_name = req.language, req.model
        try:
            engine = self.get_model(model_name)
            engine.hotwords = self.corrections.hotwords()
            if models.is_english_only(model_name):
                language = "en"
            with self.lock:
                if not self.running:
                    self.status("finished", "Sesión cancelada antes de grabar")
                    return
                self.t0 = time.monotonic()
                self.paused_at = None
                title = self.title
                folder = validate_output(str(self.output_folder))
                stem = file_stem(f"{datetime.now():%Y-%m-%d_%H%M%S}", title)
                n = 0
                while True:
                    self.path = folder / f"{stem}{f' ({n})' if n else ''}.txt"
                    try:
                        self.file = open(self.path, "x", encoding="utf-8")
                        break
                    except FileExistsError:
                        n += 1
                self.file.write(f"# {title} — {datetime.now():%Y-%m-%d %H:%M}\n\n")
                self.file.flush()
                folder = self.path.with_suffix(".session")
                folder.mkdir()
                self.archive_path = folder / "session.json"
                if self.backup_enabled:
                    self.backup = recording.AudioBackup(folder, self.backup_failed)
                self.persist()
                self.queue = audio.new_queue()
                self.chunk = engine.chunk
                self.retired = []
                notes = []
                for label in ("Otros", "Yo"):
                    if self.sources[label]["enabled"]:
                        warn = self._open_capture(label)
                        if warn:
                            notes.append(warn)
                self.active = True
            self.last_final = {}
            self.partials = {}
            self.engine = engine
            self.lagging = False
            self.partial_worker = threading.Thread(target=self._partials_work,
                args=(engine, language or None), daemon=True)
            self.partial_worker.start()
            self.base_status = (f"Grabando ({engine.name} · {engine.device}) · {len(self.corrections.rules)} reglas"
                                + (" · bloques largos, sin texto en vivo" if not engine.live_ok else "")
                                + (" · " + "; ".join(notes) if notes else ""))
            with self.lock:
                if self.state == "starting":
                    self.status("recording", self.base_status)
            broadcast(self.sources_state())
            self._work(self.queue, engine, language or None)
        except Exception as exc:
            self.failure_text = str(exc)
            with self.lock:
                self.running = self.active = False
                for label in list(self.captures):
                    self._close_capture(label)
            for cap in self.retired:
                cap.join()
            if self.queue is not None and self.file is not None:
                self.status("finishing", f"Error: {exc}. Guardando audio pendiente…")
                self._work(self.queue, engine, language or None)
            else:
                if self.file:
                    self.file.close()
                    self.file = None
                self.status("finished", f"Error: {exc}")

    def on_partial(self, label, start, chunk):
        if not self.live or time.monotonic() < self.partials_paused_until:
            return
        if self.engine is None or not self.engine.live_ok:
            return
        if not GPU:  # en CPU, provisionales más espaciados para no saturar el procesador
            now = time.monotonic()
            if now - self.partial_last.get(label, 0) < 1.2:
                return
            self.partial_last[label] = now
        with self.partials_cv:
            self.partials[label] = (start, chunk)  # solo importa el más reciente
            self.partials_cv.notify()

    def _partials_work(self, engine, language):
        """Texto provisional: retranscribe la frase en curso sin bloquear a los finales."""
        while self.running:
            if not self.active:
                time.sleep(0.05)
                continue
            with self.partials_cv:
                if not self.partials:
                    self.partials_cv.wait(timeout=0.5)
                    continue
                label, (start, chunk) = self.partials.popitem()
            if not self.active:
                continue
            if len(chunk) < audio.TARGET_RATE:  # con menos de 1 s Whisper tiende a inventar
                continue
            if not self.queue.empty() or start <= self.last_final.get(label, -1):
                continue  # hay finales pendientes o esta frase ya se cerró
            t = time.monotonic()
            with self.gpu:
                try:
                    text = engine.transcribe(chunk, language, self.context or None, fast=True)
                except Exception:
                    continue
            if time.monotonic() - t > 2.0:  # motor lento: pausar el texto en vivo un rato
                self.partials_paused_until = time.monotonic() + 3
            if self.active and text and start > self.last_final.get(label, -1):
                self.emit({"type": "partial", "source": label,
                           "time": time.strftime("%H:%M:%S", time.gmtime(start)), "text": text})

    @staticmethod
    def _drop_overlap(prev: str, text: str) -> str:
        """Si el bloque empieza repitiendo las últimas palabras del anterior (palabra partida), las quita."""
        norm = lambda w: re.sub(r"[^\w]", "", w.lower())
        pw, tw = prev.split(), text.split()
        for n in range(min(4, len(pw), len(tw) - 1), 0, -1):
            if [norm(w) for w in pw[-n:]] == [norm(w) for w in tw[:n]] and any(norm(w) for w in tw[:n]):
                rest = " ".join(tw[n:])
                return rest[:1].upper() + rest[1:] if prev.rstrip().endswith((".", "?", "!")) else rest
        return text

    def _alive(self):
        return any(c.is_alive() for c in list(self.captures.values()) + self.retired)

    def _work(self, q, engine, language):
        prompts = {}
        while self.running or not q.empty() or self._alive():
            with self.lock:
                if self.state == "pausing" and q.empty() and not self._alive():
                    # Freeze at capture stop, excluding both drain time and the pause.
                    self.partials.clear()
                    self._note("Pausa")
                    self.status("paused", "En pausa · Reanudar continúa en el mismo archivo")
            try:
                label, start, chunk = q.get(timeout=0.1)
            except Exception:
                continue
            self.last_final[label] = start
            self._report_backlog(len(chunk))
            try:
                with self.gpu:
                    text = engine.transcribe(chunk, language, self.prompt(prompts.get(label)))
            except Exception as exc:
                broadcast({"type": "status", "text": f"Error transcribiendo: {exc}"})
                continue
            text = self._drop_overlap(prompts.get(label, ""), text)
            text = self.corrections.apply(text)
            if not text:
                self.emit({"type": "partial", "source": label, "text": ""})  # limpia el provisional
                continue
            prompts[label] = text[-200:]
            ts = time.strftime("%H:%M:%S", time.gmtime(start))
            with self.lock:
                try:
                    with self.file_lock:
                        self.file.write(f"[{ts}] [{label}] {text}\n")
                        self.file.flush()
                except OSError as exc:
                    self.failure_text = f"Error de escritura; la transcripción puede estar incompleta: {exc}"
                    self.stop()
                self.emit({"type": "text", "time": ts, "start": start,
                           "end": start + len(chunk) / audio.TARGET_RATE, "source": label, "text": text})
        if self.partial_worker and self.partial_worker is not threading.current_thread():
            self.partial_worker.join()
        with self.lock:
            with self.file_lock:
                try:
                    self.file.close()
                except OSError as exc:
                    self.failure_text = f"Error al cerrar el archivo: {exc}"
                self.file = None
            self.duration = self.elapsed()
            if self.backup:
                self.backup.close(self.duration)
            self.partials.clear()
            self.persist()
            self.finalized = True
            session_finished(self)
            self.status("finished", (f"Error: {self.failure_text}. Archivo: {self.path}"
                        if self.failure_text else f"Finalizada. Guardado en {self.path}"))
            broadcast(self.sources_state())

    def stop(self):
        with self.lock:
            if self.state in ("ready", "finished", "finishing"):
                return self.snapshot()
            for label in list(self.captures):
                self._close_capture(label)
            self.duration = self.elapsed()
            if self.paused_at is None:
                self.paused_at = time.monotonic()
            self.running = False
            self.active = False
            self.status("finishing", "Terminando… procesando todo el audio pendiente")
        return self.snapshot()


session = Session()
probe = AudioProbe(broadcast)
discreet = False


class StartReq(BaseModel):
    mic: bool = False
    mic_id: str = ""
    speaker_id: str = ""
    language: str = "es"
    model: str = "multi_turbo"
    title: str = ""
    context: str | None = Field(default=None, max_length=2000)
    backup_audio: bool | None = None
    template: Literal["meeting", "interview", "class"] | None = None


class SettingsReq(BaseModel):
    backup_audio: bool | None = None
    output_dir: str | None = None
    dict_paths: str | None = None
    opacity: float | None = None
    opacity_hover: bool | None = None
    live_partials: bool | None = None
    cpu_mode: str | None = None
    download_source: str | None = None


class SourceReq(BaseModel):
    source: str
    enabled: bool | None = None
    device_id: str | None = None


class OpacityReq(BaseModel):
    value: float


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/manager")
def manager_page():
    return FileResponse(BASE / "static" / "manager.html")


@app.get("/models")
def list_models():
    out = []
    for m in models.discover():
        whisper = m["engine"] == "whisper"
        turbo = whisper and models.is_turbo(m["id"])
        slow = not GPU and whisper and not turbo and not models.is_english_only(m["id"])
        label = m["label"]
        if not GPU and turbo:
            label += " — más preciso, con algo de atraso sin GPU"
        elif slow:
            label += " — lento sin GPU"
        out.append({"id": m["id"], "label": label, "engine": m["engine"], "slow": slow,
                    "turbo": turbo, "download": m.get("download"), "source": m.get("source", "")})
    return {"gpu": GPU, "models": out, "cpu_modes": transcriber.CPU_MODES, "sources": download.SOURCES,
            "cores": os.cpu_count()}


downloading = {}


@app.post("/download/{key}")
def start_download(key: str):
    if key not in download.SPECS:
        return {"ok": False}
    if key in downloading:
        return {"ok": True, "already": True}
    downloading[key] = True
    spec = download.SPECS[key]

    def progress(mb, total):
        broadcast({"type": "download", "key": key, "mb": int(mb), "total": total,
                   "text": f"Descargando {spec['label']}… {int(mb):,} / {total:,} MB".replace(",", ".")})

    def run():
        try:
            path = download.download(key, progress, log=lambda m: broadcast(
                {"type": "download", "key": key, "text": m.replace("[descarga] ", "")}),
                source=download_source())
            broadcast({"type": "download", "key": key, "done": True, "select": path.name,
                       "text": f"{spec['label']} descargado. Ya no se vuelve a descargar."})
        except Exception as exc:
            broadcast({"type": "download", "key": key, "error": True,
                       "text": f"No se pudo descargar {spec['label']}: {exc}"})
        finally:
            downloading.pop(key, None)

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@app.post("/start")
def start(req: StartReq):
    with session.lock:
        try:
            probe.stop()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        result = session.begin(req)
    settings.save({"language": req.language, "model": req.model, "backup_audio": session.backup_enabled,
                   "template": session.template, "draft_context": "", "draft_title": "Reunión"})
    return result


@app.post("/stop")
def stop():
    return session.stop()


@app.post("/pause")
def pause():
    return session.pause()


@app.post("/resume")
def resume():
    return session.resume()


@app.post("/new-session")
def new_session():
    return session.new_session()


class BackupReq(BaseModel):
    enabled: bool


@app.post("/backup")
def toggle_backup(req: BackupReq):
    return session.set_backup(req.enabled)


class MarkerReq(BaseModel):
    text: str = Field(default="", max_length=300)


class OpenSessionReq(BaseModel):
    path: str = ""


class DiscretionReq(BaseModel):
    enabled: bool


@app.post("/discretion")
def discretion(req: DiscretionReq):
    global discreet
    discreet = req.enabled
    if discreet:
        probe.stop()
    if desktop:
        desktop.set_compact(discreet)
    message = {"type": "discretion", "enabled": discreet}
    broadcast(message)
    return message


@app.post("/probe/start")
def probe_start():
    with session.lock:
        if session.state not in ("ready", "finished"):
            raise HTTPException(409, "La prueba solo está disponible antes de iniciar")
        try:
            return probe.start(session.sources)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))


@app.post("/probe/stop")
def probe_stop():
    try:
        return probe.stop()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.post("/markers")
def add_marker(req: MarkerReq):
    return session.marker(req.text.strip() or None)


@app.patch("/markers/{marker_id}")
def edit_marker(marker_id: str, req: MarkerReq):
    return change_marker(marker_id, req.text.strip() or "Marcador")


@app.delete("/markers/{marker_id}")
def delete_marker(marker_id: str):
    return change_marker(marker_id, None)


def change_marker(marker_id, text):
    with session.lock:
        marker = next((m for m in session.markers if m['id'] == marker_id), None)
        if marker is None:
            raise HTTPException(404, "Marcador no encontrado")
        if text is None:
            session.markers.remove(marker)
        else:
            marker['text'] = text
        # Keep the TXT annotations consistent with edits, including reopened sessions.
        with session.file_lock:
            for item in session.history:
                if item.get('marker_id') == marker_id:
                    item['text'] = f"Marcador: {text}" if text is not None else "Marcador eliminado"
            if session.path and session.path.is_file():
                with session.path.open(encoding="utf-8") as current_txt:
                    header = current_txt.readline()
                lines = [header.rstrip("\n") + "\n\n"]
                for item in session.history:
                    if item['type'] == 'text':
                        lines.append(f"[{item['time']}] [{item['source']}] {item['text']}\n")
                    else:
                        lines.append(f"[{item['time']}] — {item['text']} —\n")
                if session.file:
                    session.file.seek(0)
                    session.file.write(''.join(lines))
                    session.file.truncate()
                    session.file.flush()
                else:
                    session.path.write_text(''.join(lines), encoding='utf-8')
        session.persist()
        message = {"type": "markers", "session_id": session.session_id, "markers": session.markers,
                   "history": list(session.history)}
        broadcast(message)
        return message


def live_session():
    with session.lock:
        if not session.session_id or not session.archive_path:
            return None
        return {**session.snapshot(), 'state':'finished' if session.finalized else session.state, 'history':list(session.history)}


repository = SessionRepository(output_dir, live_session, BASE / 'state' / 'session-index.json')
session_edits = SessionEdits(lambda:repository, mapping_store)
credentials = analysis.WindowsCredentials()


def analysis_config():
    saved = settings.load().get('analysis', {})
    config = {**analysis.DEFAULTS, **saved}
    for field in ('partial_output_tokens','consolidation_output_tokens','final_output_tokens'):
        if field not in saved:
            config[field] = min(config[field], config['max_output_tokens'])
    return config


insight_rules = InsightRules(insights.BASE_RULES, BASE / 'state' / 'insight-rules.user.json')


def insights_config():
    return {**insights.DEFAULTS, **settings.load().get('insights', {})}


def insight_completion(config):
    """Small, non-retried call that reuses the analysis provider and its stored key."""
    provider = analysis_config()
    model = config['model'].strip() or provider.get('model', '')
    if not model:
        return None
    base_url = provider.get('base_url', analysis.DEFAULTS['base_url'])
    try:
        key = credentials.get(base_url)
    except Exception:
        key = None
    if not key and urlparse(base_url).hostname not in ('localhost', '127.0.0.1'):
        return None
    call_config = {**analysis.DEFAULTS, **provider, 'model':model}
    call_config['timeout_seconds'] = min(call_config['timeout_seconds'], 30)
    def complete(system, text, max_tokens):
        return analysis.request_completion({**call_config, 'max_output_tokens':max_tokens}, key or 'local', system, text)
    return complete


def make_insight_engine(owner):
    config = insights_config()
    sid = owner.session_id
    def emit(message):
        broadcast({**message, 'session_id':sid})
        if message['type'] == 'insight' and owner.session_id == sid:
            owner.persist()
    return insights.InsightEngine(config, emit, insight_completion(config) if config['mode'] != 'off' else None,
                                  rules=insight_rules)


def current_insights():
    if not session.insights:
        raise HTTPException(409, 'No hay una sesión en curso')
    return session.insights


class InsightModeReq(BaseModel):
    mode: Literal['off', 'local', 'ai']


class InsightUpdateReq(BaseModel):
    pinned: bool | None = None
    dismissed: bool | None = None


class InsightSettingsReq(BaseModel):
    mode: Literal['off', 'local', 'ai'] | None = None
    model: str | None = None
    cooldown_seconds: int | None = None
    max_calls: int | None = None
    threshold: int | None = None


@app.get('/insights')
def get_insights():
    engine = session.insights
    return {'items':engine.public() if engine else [], 'state':engine.state() if engine else None,
            'session_id':session.session_id}


@app.post('/insights/now')
def insight_now():
    return {'ok':current_insights().now()}


@app.put('/insights/mode')
def insight_mode(req: InsightModeReq):
    engine = current_insights()
    if req.mode == 'ai' and engine.complete is None:
        engine.complete = insight_completion(engine.config)
        if engine.complete is None:
            raise HTTPException(400, 'Configura el modelo y la clave del análisis para usar insights con IA')
    engine.set_mode(req.mode)
    return engine.state()


@app.patch('/insights/{insight_id}')
def insight_update(insight_id: str, req: InsightUpdateReq):
    try:
        return current_insights().update(insight_id, pinned=req.pinned, dismissed=req.dismissed)
    except KeyError:
        raise HTTPException(404, 'Insight no encontrado')


class InsightFeedbackReq(BaseModel):
    kind: Literal['false_positive', 'exclude', 'disable_rule']
    phrase: str | None = Field(default=None, max_length=200)


class InsightRuleReq(BaseModel):
    type: Literal['decision', 'commitment', 'risk', 'question']
    phrase: str = Field(max_length=200)
    weight: int = 2
    suggestion_id: str | None = None


class InsightRuleUpdateReq(BaseModel):
    enabled: bool | None = None
    weight: int | None = None


class InsightPhraseReq(BaseModel):
    phrase: str = Field(max_length=200)


class InsightTestReq(BaseModel):
    text: str = Field(max_length=5000)


class InsightReplayReq(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)


@app.post('/insights/{insight_id}/feedback')
def insight_feedback(insight_id: str, req: InsightFeedbackReq):
    """Mark a false positive and optionally exclude its phrase or disable the rules behind it."""
    engine = current_insights()
    try:
        item = engine.false_positive(insight_id)
        if req.kind == 'exclude':
            insight_rules.add_exclusion(req.phrase or '')
        elif req.kind == 'disable_rule':
            for rule_id in item['rule_ids']:
                insight_rules.update_rule(rule_id, enabled=False)
    except KeyError:
        raise HTTPException(404, 'Insight o regla no encontrada')
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return item


def rules_call(action):
    try:
        action()
    except KeyError:
        raise HTTPException(404, 'Regla no encontrada')
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError:
        raise HTTPException(500, 'No se pudieron guardar las reglas de insights')
    return insight_rules.listing()


@app.get('/insight-rules')
def get_insight_rules():
    return insight_rules.listing()


@app.post('/insight-rules')
def add_insight_rule(req: InsightRuleReq):
    def action():
        insight_rules.add_rule(req.type, req.phrase, req.weight)
        if req.suggestion_id:
            insight_rules.drop_suggestion(req.suggestion_id)
    return rules_call(action)


@app.patch('/insight-rules/{rule_id}')
def update_insight_rule(rule_id: str, req: InsightRuleUpdateReq):
    return rules_call(lambda: insight_rules.update_rule(rule_id, enabled=req.enabled, weight=req.weight))


@app.delete('/insight-rules/{rule_id}')
def delete_insight_rule(rule_id: str):
    return rules_call(lambda: insight_rules.delete_rule(rule_id))


@app.post('/insight-rules/exclusions')
def add_insight_exclusion(req: InsightPhraseReq):
    return rules_call(lambda: insight_rules.add_exclusion(req.phrase))


@app.post('/insight-rules/exclusions/remove')
def remove_insight_exclusion(req: InsightPhraseReq):
    return rules_call(lambda: insight_rules.remove_exclusion(req.phrase))


@app.delete('/insight-rules/suggestions/{suggestion_id}')
def drop_insight_suggestion(suggestion_id: str):
    return rules_call(lambda: insight_rules.drop_suggestion(suggestion_id))


@app.post('/insight-rules/test')
def test_insight_rules(req: InsightTestReq):
    return {'result':insight_rules.detect(req.text)}


@app.post('/insight-rules/replay')
def replay_insight_rules(req: InsightReplayReq):
    sessions = []
    for row in repository.list()['sessions'][:req.limit]:
        try:
            sessions.append(repository.get(row['session_id']))
        except (KeyError, OSError, ValueError):
            pass
    return insight_rules.replay(sessions)


@app.get('/insights-settings')
def get_insight_settings():
    return insights_config()


@app.post('/insights-settings')
def save_insight_settings(req: InsightSettingsReq):
    try:
        config = insights.validate_config({**insights_config(), **req.model_dump(exclude_unset=True)})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    settings.save({'insights':config})
    return config


def get_analysis_queue():
    global analysis_queue
    with analysis_queue_lock:
        if analysis_queue is None:
            analysis_queue = analysis.AnalysisQueue(repository, BASE / 'state' / 'analysis-queue.json',
                                                   analysis_config, credentials, broadcast,
                                                   lambda state: desktop.notify(state) if desktop else None)
        return analysis_queue


def session_finished(captured):
    try:
        repository.register(captured.archive_path)
        broadcast({'type':'session_saved', 'session_id':captured.session_id})
        if analysis_config()['automatic'] and not captured.failure_text and not captured.archive_error:
            queue = get_analysis_queue()
            queue.enqueue(captured.session_id, captured.template, automatic=True)
            if not shutting_down and (desktop is None or not desktop.exiting):
                queue.start()
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'No se pudo preparar el análisis. Revisa la configuración y el almacenamiento.'
        try:
            saved = repository.analysis(captured.session_id)
            repository.save_analysis(captured.session_id, {**saved, 'state':'error', 'error':message, 'updated_at':analysis.now()})
        except (OSError, KeyError):
            pass
        broadcast({'type':'analysis', 'session_id':captured.session_id, 'state':'error', 'error':message})


@app.get('/sessions')
def sessions_list():
    return repository.list()


def stored_session(sid):
    try:
        return repository.get(sid)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))


@app.get('/sessions/{sid}')
def get_stored_session(sid: str):
    return stored_session(sid)


@contextmanager
def editing_session(sid, text_change=False):
    # Finished-session readers inside queue/review locks never acquire the capture lock.
    with ExitStack() as stack:
        stack.enter_context(session.lock)
        if sid == session.session_id and not session.finalized:
            raise HTTPException(409,'Termina el procesamiento de esta sesión antes de modificarla')
        if analysis_queue is not None and analysis_queue.repository is repository:
            stack.enter_context(analysis_queue.condition)
            if text_change and any(j['session_id']==sid and j['state'] in ('pending','running') for j in analysis_queue.jobs):
                raise HTTPException(409,'Espera a que termine el análisis de esta sesión antes de cambiar su texto')
        stack.enter_context(reviews.lock)
        if text_change and sid in reviews.scanning:
            raise HTTPException(409,'Espera a que termine la búsqueda de palabras de esta sesión')
        stack.enter_context(repository.lock)
        if discreet: raise HTTPException(409,'Muestra el contenido para modificar la sesión')
        yield


def edit_operation(sid, operation, text_change=False):
    try:
        with editing_session(sid,text_change):
            data = operation()
            if 'history' in data:
                if sid == session.session_id and session.finalized:
                    session.title = data['title']
                    session.history = list(data['history'])
                broadcast({'type':'session_updated','session_id':sid,'title':data['title'],'history':data['history']})
            return data
    except MappingConflict as exc:
        raise HTTPException(409,str(exc))
    except KeyError:
        raise HTTPException(404,'Sesión no encontrada; actualiza el listado')
    except (OSError,ValueError,TypeError) as exc:
        raise HTTPException(400,str(exc) if isinstance(exc,ValueError) else 'No se pudieron leer o guardar los archivos de la sesión. Se conserva el respaldo; revisa permisos y almacenamiento.')


class RenameSessionReq(BaseModel):
    title: str
    revision: str


class EditPreviewReq(BaseModel):
    operation: Literal['mappings','restore'] = 'mappings'


class ApplySessionEditReq(BaseModel):
    token: str


@app.get('/sessions/{sid}/editing')
def session_editing_info(sid: str):
    return edit_operation(sid,lambda:session_edits.info(sid))


@app.delete('/sessions/{sid}')
def delete_session(sid: str):
    """Send a saved session to the Windows Recycle Bin; the live recording cannot be deleted."""
    with session.lock:
        if sid == session.session_id and session.state not in ('finished', 'ready'):
            raise HTTPException(409, 'No se puede eliminar la sesión que se está grabando; termínala primero')
        try:
            if analysis_queue is not None:
                analysis_queue.forget_session(sid)
            if sid == session.session_id and session.state == 'finished':
                session.new_session()  # the widget must not keep showing a deleted session
            paths = repository.delete(sid, trash.send_to_trash)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        except OSError as exc:
            raise HTTPException(500, f'No se pudo eliminar la sesión: {exc}')
    broadcast({'type':'session_deleted', 'session_id':sid})
    return {'ok':True, 'removed':[str(p) for p in paths]}


@app.patch('/sessions/{sid}/title')
def rename_saved_session(sid: str, req: RenameSessionReq):
    data = edit_operation(sid,lambda:session_edits.rename(sid,req.title,req.revision))
    # Files follow the title so the transcripts folder stays readable.
    with session.lock:
        if analysis_queue is not None and any(j['session_id'] == sid and j['state'] == 'running' for j in analysis_queue.jobs):
            return {**data, 'files_warning':'El título cambió, pero los archivos se renombran cuando termine el análisis en curso.'}
        try:
            path = repository.relocate(sid, data['title'])
        except OSError as exc:
            return {**data, 'files_warning':f'El título cambió, pero no se pudieron renombrar los archivos (¿están abiertos?): {exc}'}
        if sid == session.session_id and session.finalized:
            if path.suffix.lower() == '.txt':
                session.path = path
            else:
                session.archive_path = path
                session.path = path.parent.parent / json.loads(path.read_text(encoding='utf-8'))['txt'][3:]
        if analysis_queue is not None:
            analysis_queue.relocate(sid, path)
        data = repository.get(sid, include_live=False)
    broadcast({'type':'session_updated','session_id':sid,'title':data['title']})
    return data


@app.post('/sessions/{sid}/text-preview')
def preview_session_text(sid: str, req: EditPreviewReq):
    return edit_operation(sid,lambda:session_edits.preview(sid,req.operation),True)


@app.post('/sessions/{sid}/text-apply')
def apply_session_text(sid: str, req: ApplySessionEditReq):
    return edit_operation(sid,lambda:session_edits.apply(sid,req.token),True)


@app.get('/sessions/{sid}/audio/{index}')
def stored_audio(sid: str, index: int):
    if session.state not in ('ready','finished') and session.sources['Otros']['enabled']:
        raise HTTPException(409, 'Reproducción bloqueada mientras se captura el audio del equipo')
    data = stored_session(sid)
    if data['state'] != 'finished':
        raise HTTPException(409, 'Termina la sesión antes de reproducirla')
    if index < 0 or index >= len(data['parts']):
        raise HTTPException(404, 'Parte de audio no encontrada')
    try:
        path = repository.audio_path(data['archive_path'], data['parts'][index]['file'])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not path.is_file():
        raise HTTPException(404, 'Falta el archivo de audio')
    return FileResponse(path, media_type='audio/wav')


@app.patch('/sessions/{sid}/markers/{marker_id}')
def stored_marker_edit(sid: str, marker_id: str, req: MarkerReq):
    return stored_marker_change(sid, marker_id, req.text.strip() or 'Marcador')


@app.delete('/sessions/{sid}/markers/{marker_id}')
def stored_marker_delete(sid: str, marker_id: str):
    return stored_marker_change(sid, marker_id, None)


def stored_marker_change(sid, marker_id, text):
    if sid == session.session_id:
        return change_marker(marker_id, text)
    try:
        data = repository.change_marker(sid, marker_id, text)
        broadcast({'type':'markers', 'session_id':sid, 'markers':data['markers'], 'history':data['history']})
        return data
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc))


class PreparationReq(BaseModel):
    title: str = Field(default='Reunión', max_length=120)
    context: str = Field(default='', max_length=2000)
    backup_audio: bool = False
    template: Literal['meeting','interview','class'] = 'meeting'


@app.get('/preparation')
def preparation():
    prefs = settings.load()
    return {'title':prefs['draft_title'], 'context':prefs['draft_context'],
            'backup_audio':prefs['backup_audio'], 'template':prefs['template']}


@app.post('/preparation')
def save_preparation(req: PreparationReq):
    with session.lock:
        if session.state not in ('ready','finished'):
            raise HTTPException(409, 'La preparación se fija antes de iniciar la grabación')
        settings.save({'draft_title':req.title.strip() or 'Reunión', 'draft_context':req.context,
                       'backup_audio':req.backup_audio, 'template':req.template})
    broadcast({'type':'preparation', **preparation()})
    return preparation()


class AnalysisSettingsReq(BaseModel):
    base_url: str | None = None
    model: str | None = None
    automatic: bool | None = None
    api_key: str | None = None
    clear_key: bool = False
    chunk_chars: int | None = None
    max_output_tokens: int | None = None
    context_window_tokens: int | None = None
    partial_output_tokens: int | None = None
    consolidation_output_tokens: int | None = None
    final_output_tokens: int | None = None
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    tokenizer_encoding: Literal['conservative','cl100k_base','o200k_base'] | None = None
    output_token_field: Literal['max_tokens','max_completion_tokens'] | None = None
    max_calls: int | None = None
    timeout_seconds: int | None = None
    input_price: float | None = None
    output_price: float | None = None


@app.get('/analysis-settings')
def get_analysis_settings():
    config = analysis_config()
    try:
        has_key = bool(credentials.get(config['base_url']))
    except Exception:
        has_key = False
    return {**config, 'has_key':has_key, 'templates':analysis.TEMPLATES}


@app.post('/analysis-settings')
def save_analysis_settings(req: AnalysisSettingsReq):
    values = req.model_dump(exclude={'api_key','clear_key'}, exclude_unset=True)
    config = {**analysis_config(), **values}
    try:
        analysis.validate_config(config)
        if req.clear_key and not req.api_key and config['automatic']:
            raise ValueError('Desactiva el análisis automático antes de eliminar la clave')
        if config['automatic'] and not req.api_key and not credentials.get(config['base_url']):
            raise ValueError('Guarda una clave antes de activar el análisis automático')
        if req.clear_key:
            credentials.delete(config['base_url'])
        if req.api_key:
            credentials.set(config['base_url'], req.api_key.strip())
        if config['automatic'] and not credentials.get(config['base_url']):
            raise ValueError('Guarda una clave antes de activar el análisis automático')
        settings.save({'analysis':config})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(400, 'No se pudo guardar la clave en el almacén de credenciales de Windows')
    return get_analysis_settings()


@app.post('/analysis-test')
def test_analysis_connection():
    try:
        config = analysis.validate_config(analysis_config())
        key = credentials.get(config['base_url'])
        if not key:
            raise ValueError('Configura la clave del proveedor')
        text, usage = analysis.request_completion(config, key, 'Responde únicamente OK.', 'Prueba de conexión del Transcriptor. Sin conversaciones.')
        return {'ok':True, 'text':'Conexión y respuesta compatibles', 'usage':usage}
    except (analysis.AnalysisError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(400, 'No se pudo acceder a la credencial de Windows')


class AnalyzeReq(BaseModel):
    template: Literal['meeting','interview','class'] = 'meeting'


@app.get('/sessions/{sid}/analysis')
def get_saved_analysis(sid: str):
    stored_session(sid)
    return repository.analysis(sid)


@app.post('/sessions/{sid}/analysis')
def analyze_session(sid: str, req: AnalyzeReq):
    stored_session(sid)
    try:
        queue = get_analysis_queue()
        job = queue.enqueue(sid, req.template)
        queue.start()
        return queue.public_job(job)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc))


class ContinueAnalysisReq(BaseModel):
    acknowledge_uncertain: bool = False


@app.get('/analysis-jobs/{job_id}/stages')
def get_analysis_stages(job_id: str):
    try:
        return get_analysis_queue().stages(job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post('/analysis-jobs/{job_id}/continue')
def continue_analysis(job_id: str, req: ContinueAnalysisReq):
    try:
        queue = get_analysis_queue()
        result = queue.resume(job_id, req.acknowledge_uncertain)
        queue.start()
        return result
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc))


@app.post('/show-manager')
def show_manager():
    if desktop:
        desktop.show_manager()
    return {'native':desktop is not None, 'url':'/manager'}


@app.post('/show-widget')
def show_widget():
    if desktop:
        desktop.show_widget()
    return {'native':desktop is not None, 'url':'/'}


@app.post('/exit')
def exit_application():
    if desktop:
        desktop.exit()
    return {'ok':desktop is not None}


@app.get("/session-data")
def session_data():
    with session.lock:
        parts = []
        if session.archive_path and session.archive_path.is_file():
            data = json.loads(session.archive_path.read_text(encoding='utf-8'))
            for index, part in enumerate(data.get('parts', [])):
                part = dict(part)
                path = safe_audio_path(part['file'])
                part['available'] = path.is_file()
                part['url'] = f"/session-audio/{session.session_id}/{index}"
                parts.append(part)
        return {**session.snapshot(), 'history': list(session.history), 'parts': parts}


def safe_audio_path(name):
    if not session.archive_path or Path(name).name != name or not name.endswith('.wav'):
        raise HTTPException(400, "Referencia de audio inválida")
    root = session.archive_path.parent.resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise HTTPException(400, "Referencia de audio fuera de la sesión")
    return path


@app.get("/session-audio/{session_id}/{index}")
def session_audio(session_id: str, index: int):
    with session.lock:
        if session.state != 'finished' or session.session_id != session_id:
            raise HTTPException(409, "Audio disponible solo al terminar la sesión actual")
        data = session_data()
        if index < 0 or index >= len(data['parts']):
            raise HTTPException(404, "Parte de audio no encontrada")
        path = safe_audio_path(data['parts'][index]['file'])
        if not path.is_file():
            raise HTTPException(404, "Falta el archivo de audio")
        return FileResponse(path, media_type='audio/wav')


@app.post("/open-session")
def open_session(req: OpenSessionReq):
    selected = req.path
    target_window = desktop.manager if desktop is not None else window
    if not selected and target_window is not None:
        import webview
        result = target_window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                                  file_types=("Sesiones (*.json;*.txt)",))
        selected = result[0] if result else ''
    if not selected:
        raise HTTPException(400, "Selecciona un session.json o TXT")
    try:
        return repository.register(Path(selected).expanduser(), external=True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, f"No se puede abrir la sesión: {exc}")


@app.post("/pin")
def pin():
    if window is None:
        return {"ok": False, "pinned": False}
    window.on_top = not window.on_top
    return {"ok": True, "pinned": window.on_top}


@app.post("/minimize")
def minimize():
    if window is not None:
        window.minimize()
    return {"ok": True}


class CloseReq(BaseModel):
    force: bool = False


@app.post("/close")
def close(req: CloseReq | None = None):
    if window is None:
        return {"ok": False}
    worker_busy = session.worker is not None and session.worker.is_alive()
    pend = session.pending_seconds() if worker_busy else 0
    if pend > 0 and not (req and req.force):
        return {"ok": False, "pending": pend}
    probe.stop()
    session.stop()
    if desktop is not None:
        window.hide()
    else:
        threading.Thread(target=window.destroy, daemon=True).start()
    return {"ok": True}


def personal_terms_path():
    local = Path(os.environ.get('LOCALAPPDATA',str(Path.home() / 'AppData' / 'Local')))
    return local / 'Transcriptor' / 'terms.tsv'


personal_terms = PersonalTerms(personal_terms_path)
local_spelling = LocalSpelling(BASE / 'dictionaries')
reviews = ReviewService(lambda:repository, local_spelling, personal_terms, mapping_store, broadcast)


class PersonalTermReq(BaseModel):
    revision: str
    term: str


class PersonalTermDeleteReq(BaseModel):
    revision: str


class ReviewDecisionReq(BaseModel):
    revision: str
    action: Literal['accept','ignore','restore','mapping']


def review_operation(action):
    try:
        return action()
    except MappingConflict as exc:
        raise HTTPException(409,str(exc))
    except KeyError as exc:
        raise HTTPException(404,str(exc))
    except (ValueError,UnicodeError) as exc:
        raise HTTPException(400,str(exc))
    except OSError:
        raise HTTPException(400,'No se pudo leer o guardar el vocabulario o la revisión. Revisa permisos y almacenamiento; no se confirmó la operación.')


@app.get('/dictionary/personal-terms')
def get_personal_terms(q: str = Query(default='',max_length=200),page: int = Query(default=1,ge=1),page_size: int = Query(default=50,ge=1,le=100)):
    def load():
        data = personal_terms.read()
        matches = [e for e in data['entries'] if normalize_term(q) in normalize_term(e['term'])]
        start = (page-1)*page_size
        return {**data,'entries':matches[start:start+page_size],'entries_count':len(data['entries']),
                'total':len(matches),'page':page,'page_size':page_size}
    return review_operation(load)


@app.post('/dictionary/personal-terms')
def add_personal_term(req: PersonalTermReq):
    result = review_operation(lambda:personal_terms.add(req.term,req.revision))
    broadcast({'type':'personal_terms_changed'})
    return {'ok':True,'revision':result['revision'],'backup':result.get('backup')}


@app.delete('/dictionary/personal-terms/{entry_id}')
def remove_personal_term(entry_id: str, req: PersonalTermDeleteReq):
    result = review_operation(lambda:personal_terms.delete(entry_id,req.revision))
    broadcast({'type':'personal_terms_changed'})
    return {'ok':True,'revision':result['revision'],'backup':result.get('backup')}


@app.get('/sessions/{sid}/suspects')
def get_suspects(sid: str,q: str = Query(default='',max_length=200),status: Literal['pending','reviewed']='pending',
                 page: int = Query(default=1,ge=1),page_size: int = Query(default=25,ge=1,le=100)):
    stored_session(sid)
    return review_operation(lambda:reviews.get(sid,q,status,page,page_size))


@app.post('/sessions/{sid}/suspects/search')
def search_suspects(sid: str):
    stored_session(sid)
    return review_operation(lambda:reviews.start(sid))


@app.post('/sessions/{sid}/suspects/{entry_id}/decision')
def decide_suspect(sid: str,entry_id: str,req: ReviewDecisionReq):
    stored_session(sid)
    result = review_operation(lambda:reviews.decide(sid,entry_id,req.action,req.revision))
    if req.action == 'accept':
        broadcast({'type':'personal_terms_changed'})
    return result


@app.get('/dictionary/terms')
def dictionary_terms(source: Literal['es','en','own'] = 'es', q: str = Query(default='',max_length=200),
                     page: int = Query(default=1,ge=1), page_size: int = Query(default=50,ge=1,le=100)):
    try:
        return vocabulary.search(source,q,page,page_size)
    except (OSError,ValueError):
        raise HTTPException(400,'No se pudo leer el vocabulario incluido en la aplicación')


def personal_mappings_path():
    local = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
    return local / 'Transcriptor' / 'mappings.tsv'


@app.get('/dictionary/mappings')
def dictionary_mappings():
    try:
        data = mapping_store.snapshot()
        _, origin = dictionary_selection()
        return {**data, 'configuration_origin':origin,
                'can_create_personal':not any(f['exists'] for f in data['files']) and origin not in ('--dict','TRANSCRIPTOR_DICT'),
                'personal_path':str(personal_mappings_path())}
    except (OSError,UnicodeError):
        raise HTTPException(400,'No se pudieron leer los archivos configurados; revisa permisos y codificación UTF-8')


class MappingReq(BaseModel):
    revision: str
    source: str
    target: str
    mode: Literal['word','any'] = 'word'
    case: Literal['case','nocase'] = 'nocase'


class MappingDeleteReq(BaseModel):
    revision: str


class MappingPreviewReq(BaseModel):
    text: str = Field(default='',max_length=20000)


def mutate_mapping(file_id, revision, operation, row_id=None, rule=None):
    try:
        result = mapping_store.mutate(file_id,revision,operation,row_id,rule)
        with dictionary_lock:
            fixes._stamp = None
            current_corrections()
        broadcast({'type':'dictionary_changed'})
        return result
    except MappingConflict as exc:
        raise HTTPException(409,str(exc))
    except KeyError as exc:
        raise HTTPException(404,str(exc))
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    except (OSError,UnicodeError):
        raise HTTPException(400,'No se pudo guardar el archivo. Revisa permisos, almacenamiento y codificación; no se confirmó el cambio.')


@app.post('/dictionary/mappings/{file_id}/rules')
def mapping_add(file_id: str, req: MappingReq):
    return mutate_mapping(file_id,req.revision,'add',rule=(req.source,req.target,req.mode,req.case))


@app.patch('/dictionary/mappings/{file_id}/rules/{row_id}')
def mapping_edit(file_id: str, row_id: str, req: MappingReq):
    return mutate_mapping(file_id,req.revision,'edit',row_id,(req.source,req.target,req.mode,req.case))


@app.delete('/dictionary/mappings/{file_id}/rules/{row_id}')
def mapping_delete(file_id: str, row_id: str, req: MappingDeleteReq):
    return mutate_mapping(file_id,req.revision,'delete',row_id)


@app.post('/dictionary/mappings/preview')
def mapping_preview(req: MappingPreviewReq):
    try:
        return {'text':current_corrections().apply(req.text)}
    except (OSError,UnicodeError):
        raise HTTPException(400,'No se pudieron leer los mappings para probar el texto')


@app.post('/dictionary/mappings/create-personal')
def mapping_create_personal():
    with dictionary_lock:
        current = dictionary_mappings()
        if not current['can_create_personal']:
            raise HTTPException(409,'Hay un archivo existente o una configuración --dict/TRANSCRIPTOR_DICT que tiene prioridad')
        path = personal_mappings_path()
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            if not path.exists():
                with path.open('x',encoding='utf-8') as file:
                    file.write('# origen\tdestino\tword|any\tcase|nocase\n')
            corrections.Corrections(str(path))  # Validate before selecting an existing personal file.
            settings.save({'dict_paths':str(path)})
            current_corrections()
        except (OSError,UnicodeError):
            raise HTTPException(400,'No se pudo crear o utilizar el archivo personal; revisa permisos y UTF-8')
    broadcast({'type':'dictionary_changed'})
    return dictionary_mappings()


def dict_state():
    current = current_corrections()
    return {"dict_paths": ";".join(str(p) for p in current.paths), "rules": len(current.rules),
            "missing": current.missing}


@app.get("/info")
def info():
    return {"window": window is not None, **dict_state()}


@app.get("/devices")
def devices():
    return audio.list_devices()


@app.get("/settings")
def get_settings():
    return {**settings.load(), **dict_state(), "effective_output_dir": str(output_dir())}


@app.post("/settings")
def post_settings(req: SettingsReq):
    global fixes
    if req.backup_audio is not None:
        if session.state not in ("ready", "finished"):
            raise HTTPException(409, "El respaldo se elige antes de iniciar")
        settings.save({"backup_audio": req.backup_audio})
    if req.output_dir is not None:
        try:
            path = validate_output(req.output_dir.strip())
        except (OSError, ValueError) as exc:
            raise HTTPException(400, f"No se puede usar esa carpeta: {exc}")
        settings.save({"output_dir": str(path) if req.output_dir.strip() else ""})
    if req.dict_paths is not None:
        settings.save({"dict_paths": req.dict_paths})
        current_corrections()
    if req.opacity is not None:
        settings.save({"opacity": max(0.2, min(1.0, req.opacity))})
    if req.opacity_hover is not None:
        settings.save({"opacity_hover": req.opacity_hover})
    if req.download_source in download.SOURCES:
        settings.save({"download_source": req.download_source})
        if req.download_source == "github":
            download.github_only()
    if req.cpu_mode in transcriber.CPU_MODES:
        if session.state not in ("ready", "finished"):
            raise HTTPException(409, "Termina la sesión antes de cambiar el uso del procesador")
        settings.save({"cpu_mode": req.cpu_mode})
    if req.live_partials is not None:
        settings.save({"live_partials": req.live_partials})
        session.live = req.live_partials
    return get_settings()


@app.get("/sources")
def get_sources():
    return session.sources_state()


@app.post("/source")
def post_source(req: SourceReq):
    if req.source not in ("Yo", "Otros"):
        return {"ok": False}
    with session.lock:
        try:
            probe.stop()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return session.set_source(req.source, req.enabled, req.device_id)


def set_opacity(value):
    if window is None or window.native is None:
        return
    from System import Action  # pythonnet, viene con pywebview en Windows
    form = window.native
    value = max(0.2, min(1.0, value))
    form.Invoke(Action(lambda: setattr(form, "Opacity", value)))


@app.post("/opacity")
def post_opacity(req: OpacityReq):
    set_opacity(req.value)
    return {"ok": True}


dragging = threading.Event()


def _drag_loop(hwnd):
    """Sigue al cursor mientras el botón izquierdo esté presionado (WebView2 retiene la captura del mouse)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    pt, rect = wintypes.POINT(), wintypes.RECT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    dx, dy = pt.x - rect.left, pt.y - rect.top
    try:
        while user32.GetAsyncKeyState(0x01) & 0x8000:  # VK_LBUTTON
            user32.GetCursorPos(ctypes.byref(pt))
            # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            user32.SetWindowPos(hwnd, 0, pt.x - dx, pt.y - dy, 0, 0, 0x0001 | 0x0004 | 0x0010)
            time.sleep(0.008)
    finally:
        dragging.clear()


@app.post("/drag")
def drag():
    if window is None or window.native is None or dragging.is_set():
        return {"ok": False}
    dragging.set()
    hwnd = int(str(window.native.Handle))
    threading.Thread(target=_drag_loop, args=(hwnd,), daemon=True).start()
    return {"ok": True}


@app.post("/browse-output")
def browse_output():
    if window is None:
        return {"path": None}
    import webview
    res = window.create_file_dialog(webview.FileDialog.FOLDER)
    return {"path": res[0] if res else None}


@app.post("/open-output")
def open_output():
    try:
        os.startfile(output_dir())
    except OSError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/browse-dict")
def browse_dict():
    if window is None:
        return {"path": None}
    import webview
    res = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                    file_types=("Diccionario (*.tsv;*.txt)", "Todos (*.*)"))
    return {"path": res[0] if res else None}


@app.post("/import-model")
def import_model():
    """Elige un .zip con el selector de archivos y lo instala en modelos/."""
    if window is None:
        return {"ok": False, "text": "Solo disponible en la ventana de la app"}
    import webview
    res = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                    file_types=("Modelo (*.zip)", "Todos (*.*)"))
    if not res:
        return {"ok": False}
    try:
        broadcast({"type": "status", "text": "Importando modelo… (puede tardar un minuto)"})
        path = download.import_zip(res[0])
    except Exception as exc:
        return {"ok": False, "text": f"No se pudo importar: {exc}"}
    return {"ok": True, "select": path.name, "text": f"Modelo importado en modelos/{path.name}"}


@app.post("/open-dict")
def open_dict():
    for p in current_corrections().paths:
        if p.is_file():
            os.startfile(p)
            break
    return {"ok": True}


@app.post("/open")
def open_file():
    target = session.path if session.path else output_dir()
    os.startfile(target)
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    with session.lock:
        initial = {**session.snapshot(), "history": list(session.history), "discreet": discreet}
    await websocket.send_json(initial)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)


def free_port(start):
    for port in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No hay puertos libres")


def serve(port):
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def run_window(url, port):
    global window, desktop
    import webview
    from desktop import Desktop
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    while True:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    def drain():
        if session.worker:
            session.worker.join()
    desktop = Desktop(url, lambda: session.stop(), drain,
                      lambda: analysis_queue.stop() if analysis_queue else None, lambda: probe.stop())
    window = desktop.widget
    window.events.shown += lambda: set_opacity(settings.load()["opacity"])
    desktop.start_tray()
    # A fixed profile: the default private one is a temp folder that WebView2 still locks on exit.
    webview.start(private_mode=False, storage_path=str(BASE / "state" / "webview"))
    drain()
    if analysis_queue:
        analysis_queue.stop()
    # Everything is saved. Skip interpreter finalization: WinForms/WebView2 threads may still
    # call into Python through pythonnet and crash with NullReferenceException.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__" and ARGS.download:
    download.cli(ARGS.download, download_source())
    sys.exit(0)

if __name__ == "__main__" and ARGS.import_zip:
    download.cli_import(ARGS.import_zip)
    sys.exit(0)

if __name__ == "__main__":
    port = free_port(ARGS.port)
    url = f"http://127.0.0.1:{port}"
    print(f"[transcriptor] {url}")
    if ARGS.browser or ARGS.no_browser:
        if not ARGS.no_browser:
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        serve(port)
    else:
        try:
            run_window(url, port)
        except ImportError:
            print("[transcriptor] pywebview no está instalado; abriendo en el navegador.")
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
            serve(port)
