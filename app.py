"""Transcriptor de reuniones local: python app.py (ventana propia) o python app.py --browser"""
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

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import audio
import corrections
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

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
clients: set[WebSocket] = set()
loop: asyncio.AbstractEventLoop | None = None
window = None  # ventana pywebview, si se usa
fixes = corrections.Corrections(
    ARGS.dict or os.environ.get("TRANSCRIPTOR_DICT") or settings.load()["dict_paths"] or None)


def broadcast(msg: dict):
    if loop is None:
        return
    for ws in list(clients):
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)


class Session:
    def __init__(self):
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
                "title": self.title, "model": self.model_name, "language": self.language}

    def status(self, state, text):
        self.revision += 1
        self.state, self.status_text = state, text
        broadcast(self.snapshot())

    def emit(self, msg):
        msg = {**msg, "session_id": self.session_id}
        if msg["type"] in ("text", "note"):
            self.history.append(msg)
        broadcast(msg)

    def begin(self, req):
        with self.lock:
            if self.state != "ready":
                raise HTTPException(409, "La sesión no está lista para iniciar")
            self.title = req.title.strip() or "Reunión"
            self.model_name, self.language = req.model, req.language
            self.failure_text = ""
            self.output_folder = output_dir()
            self.session_id = uuid.uuid4().hex
            self.status("starting", "Iniciando…")
            self.running = True
            self.worker = threading.Thread(target=self.start, args=(req,), daemon=True)
            self.worker.start()
        return self.snapshot()

    def new_session(self):
        with self.lock:
            if self.state != "finished":
                raise HTTPException(409, "Primero termina la sesión actual")
            self.path = None
            self.queue = None
            self.engine = None
            self.partial_worker = None
            self.partial_last = {}
            self.partials_paused_until = 0.0
            self.session_id = None
            self.history = []
            self.title = "Reunión"
            self.model_name = self.language = None
            self.status("ready", "Listo para una nueva sesión")
        return self.snapshot()

    def pause(self):
        with self.lock:
            if self.state != "recording":
                raise HTTPException(409, "Solo se puede pausar mientras se graba")
            self.active = False
            self.paused_at = time.time()
            for label in list(self.captures):
                self._close_capture(label)
            self.status("pausing", "Pausando… procesando el audio pendiente")
        return self.snapshot()

    def resume(self):
        with self.lock:
            if self.state != "paused":
                raise HTTPException(409, "La sesión todavía no está pausada")
            self.t0 += time.time() - self.paused_at
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
        now = time.time()
        if now - self.last_level > 0.2 or label == "Yo":
            self.last_level = now
            broadcast({"type": "level", "source": label, "value": min(rms * 20, 1.0)})

    def sources_state(self):
        return {"type": "sources", "running": self.active,
                **{k: {"enabled": v["enabled"], "id": v["id"],
                       "name": self.captures[k].device.name if k in self.captures else ""}
                   for k, v in self.sources.items()}}

    def _note(self, text):
        if not self.file:
            return
        ts = time.strftime("%H:%M:%S", time.gmtime((self.paused_at or time.time()) - self.t0))
        with self.file_lock:
            self.file.write(f"[{ts}] — {text} —\n")
            self.file.flush()
        self.emit({"type": "note", "time": ts, "text": text})

    def _open_capture(self, label):
        """Arranca la captura de una fuente con su dispositivo elegido. Devuelve un aviso o None."""
        src = self.sources[label]
        if label == "Yo":
            dev, fell_back = audio.mic(src["id"])
        else:
            dev, fell_back = audio.system_loopback(src["id"])
        mn, mx = self.chunk
        cap = audio.SourceCapture(label, dev, self.queue, self.t0, self.level, mn, mx, self.on_partial)
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
            fixes.reload_if_changed()
            engine.hotwords = fixes.hotwords()
            if models.is_english_only(model_name):
                language = "en"
            with self.lock:
                if not self.running:
                    self.status("finished", "Sesión cancelada antes de grabar")
                    return
                self.t0 = time.time()
                self.paused_at = None
                title = req.title.strip() or "Reunión"
                slug = re.sub(r'[\\/:*?"<>|]+', "", title).strip()[:60]
                folder = validate_output(str(self.output_folder))
                stem = f"{datetime.now():%Y-%m-%d_%H%M%S} {slug}"
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
            self.base_status = (f"Grabando ({engine.name} · {engine.device}) · {len(fixes.rules)} reglas"
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
        if not self.live or time.time() < self.partials_paused_until:
            return
        if self.engine is None or not self.engine.live_ok:
            return
        if not GPU:  # en CPU, provisionales más espaciados para no saturar el procesador
            now = time.time()
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
            t = time.time()
            with self.gpu:
                try:
                    text = engine.transcribe(chunk, language, None, fast=True)
                except Exception:
                    continue
            if time.time() - t > 2.0:  # motor lento: pausar el texto en vivo un rato
                self.partials_paused_until = time.time() + 3
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
                    text = engine.transcribe(chunk, language, prompts.get(label))
            except Exception as exc:
                broadcast({"type": "status", "text": f"Error transcribiendo: {exc}"})
                continue
            text = self._drop_overlap(prompts.get(label, ""), text)
            text = fixes.apply(text)
            if not text:
                self.emit({"type": "partial", "source": label, "text": ""})  # limpia el provisional
                continue
            prompts[label] = text[-200:]
            ts = time.strftime("%H:%M:%S", time.gmtime(start))
            try:
                with self.file_lock:
                    self.file.write(f"[{ts}] [{label}] {text}\n")
                    self.file.flush()
            except OSError as exc:
                self.failure_text = f"Error de escritura; la transcripción puede estar incompleta: {exc}"
                self.stop()
            self.emit({"type": "text", "time": ts, "source": label, "text": text})
        if self.partial_worker and self.partial_worker is not threading.current_thread():
            self.partial_worker.join()
        with self.lock:
            with self.file_lock:
                try:
                    self.file.close()
                except OSError as exc:
                    self.failure_text = f"Error al cerrar el archivo: {exc}"
                self.file = None
            self.partials.clear()
            self.status("finished", (f"Error: {self.failure_text}. Archivo: {self.path}"
                        if self.failure_text else f"Finalizada. Guardado en {self.path}"))
            broadcast(self.sources_state())

    def stop(self):
        with self.lock:
            if self.state in ("ready", "finished", "finishing"):
                return self.snapshot()
            for label in list(self.captures):
                self._close_capture(label)
            self.running = False
            self.active = False
            self.status("finishing", "Terminando… procesando todo el audio pendiente")
        return self.snapshot()


session = Session()


class StartReq(BaseModel):
    mic: bool = False
    mic_id: str = ""
    speaker_id: str = ""
    language: str = "es"
    model: str = "multi_turbo"
    title: str = ""


class SettingsReq(BaseModel):
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


@app.on_event("startup")
async def _startup():
    global loop
    loop = asyncio.get_running_loop()


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


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
    result = session.begin(req)
    settings.save({"language": req.language, "model": req.model})
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
    session.stop()
    threading.Thread(target=window.destroy, daemon=True).start()
    return {"ok": True}


def dict_state():
    return {"dict_paths": ";".join(str(p) for p in fixes.paths), "rules": len(fixes.rules),
            "missing": fixes.missing}


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
    if req.output_dir is not None:
        try:
            path = validate_output(req.output_dir.strip())
        except (OSError, ValueError) as exc:
            raise HTTPException(400, f"No se puede usar esa carpeta: {exc}")
        settings.save({"output_dir": str(path) if req.output_dir.strip() else ""})
    if req.dict_paths is not None:
        settings.save({"dict_paths": req.dict_paths})
        fixes = corrections.Corrections(req.dict_paths or None)
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
    res = window.create_file_dialog(webview.FOLDER_DIALOG)
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
    res = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                    file_types=("Diccionario (*.tsv;*.txt)", "Todos (*.*)"))
    return {"path": res[0] if res else None}


@app.post("/import-model")
def import_model():
    """Elige un .zip con el selector de archivos y lo instala en modelos/."""
    if window is None:
        return {"ok": False, "text": "Solo disponible en la ventana de la app"}
    import webview
    res = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
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
    for p in fixes.paths:
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
        initial = {**session.snapshot(), "history": list(session.history)}
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
    global window
    import webview
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    while socket.socket().connect_ex(("127.0.0.1", port)) != 0:  # espera al servidor
        time.sleep(0.1)
    window = webview.create_window("Transcriptor", url, width=360, height=540,
                                   min_size=(300, 360), resizable=True, frameless=True,
                                   easy_drag=False, background_color="#1c1f24")
    window.events.closing += session.stop
    window.events.shown += lambda: set_opacity(settings.load()["opacity"])
    webview.start()
    if session.worker:
        session.worker.join()  # termina de transcribir todo lo pendiente antes de salir


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
