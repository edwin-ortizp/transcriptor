"""Read sessions independently of the live capture controller."""
import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from recording import atomic_json, load_manifest


STAMP = re.compile(r'^(\d{4}-\d{2}-\d{2}_\d{6})')
INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def file_stem(prefix, title):
    """Same rule as new recordings: timestamp, hyphen and the meeting title."""
    slug = ' '.join(INVALID.sub('', title).split()).strip(' .')[:60]
    return f'{prefix}-{slug}' if slug else prefix


def stamp_of(created_at, path):
    try:
        moment = datetime.fromisoformat(created_at) if created_at else datetime.fromtimestamp(path.stat().st_mtime)
    except (ValueError, OSError):
        moment = datetime.now()
    return f'{moment:%Y-%m-%d_%H%M%S}'


REFERENCE = re.compile(r'\s*\[\d+:\d{2}:\d{2}\]')
INSIGHT_NAMES = {'decision':'Decisión', 'commitment':'Compromiso', 'risk':'Riesgo', 'question':'Pregunta'}


def analysis_markdown(data, result, questions=()):
    """Clean Markdown for reuse: no [HH:MM:SS] references, plus the meeting insights and questions."""
    result = result or {}
    date = (data.get('created_at') or '')[:16].replace('T', ' ')
    lines = [f"# {data.get('title') or 'Sesión'}", '']
    if date:
        lines += [f'*{date}*', '']
    if result.get('text'):
        lines.append(REFERENCE.sub('', result['text']).strip())
    if questions:
        lines += ['', '## Preguntas sobre la reunión']
        for item in questions:
            lines += ['', f"**{item['question']}**", '', REFERENCE.sub('', item.get('answer', '')).strip()]
    shown = [i for i in data.get('insights', []) if not i.get('dismissed')]
    if shown:
        lines += ['', '## Insights de la reunión', '']
        lines += [f"- **{INSIGHT_NAMES.get(i.get('type'), i.get('type'))}**{' 📌' if i.get('pinned') else ''}: {i.get('text', '').strip()}"
                  for i in sorted(shown, key=lambda i:(not i.get('pinned'), i.get('start') or 0))]
    if result:
        lines += ['', '---', f"*Análisis generado con {result.get('model', '')} · {(result.get('created_at') or '')[:16].replace('T', ' ')}*"]
    lines.append('')
    return '\n'.join(lines)


class SessionRepository:
    def __init__(self, folder, current, index_path):
        self.folder, self.current, self.index_path = folder, current, Path(index_path)
        self.paths = {}
        self.external = []
        self.lock = threading.RLock()
        if self.index_path.exists():
            try:
                self.external = json.loads(self.index_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pass

    def register(self, path, external=False):
        path = Path(path).resolve()
        data = self._read(path)
        with self.lock:
            old = self.paths.get(data['session_id'])
            if old and old != path:
                raise ValueError('Dos archivos tienen el mismo identificador de sesión')
            self.paths[data['session_id']] = path
            if external and str(path) not in self.external:
                self.external.append(str(path))
                self.index_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_json(self.index_path, self.external)
        return self.get(data['session_id'])

    def _read(self, path):
        with self.lock:
            from session_edits import recover
            recover(path)
            return self._read_file(path)

    def _read_file(self, path):
        if path.name.lower().endswith('.analysis.md'):
            raise ValueError('Un análisis no es una transcripción de sesión')
        if path.suffix.lower() in ('.txt', '.md'):
            text = path.read_text(encoding='utf-8-sig')
            sid = 'txt_' + hashlib.sha256(str(path).casefold().encode()).hexdigest()[:24]
            history = []
            for number, line in enumerate(text.splitlines()):
                if not line.strip() or line.startswith('#'):
                    continue
                match = re.match(r'^\[(\d+):(\d\d):(\d\d)\]\s*(?:\[([^]]+)\])?\s*(.*)', line)
                start = 0
                source, content = 'Otros', line
                if match:
                    h, m, s, source, content = match.groups()
                    start = int(h) * 3600 + int(m) * 60 + int(s)
                history.append({'id':f'{sid}_{number}', 'type':'text', 'source':source or 'Nota',
                                'start':start, 'end':start + 1, 'time':stamp(start), 'text':content})
            title = text.splitlines()[0].lstrip('# ').split(' — ')[0] if text.startswith('#') else path.stem
            from session_edits import metadata, sha
            meta = metadata(path)
            raw = path.read_bytes()
            if meta.get('txt_hash') == sha(raw):
                history = meta.get('history',history)
                title = meta.get('title',title)
            elif meta.get('history') and len(meta['history']) == len(history):
                for old,new in zip(meta['history'],history): new['id'] = old['id']
            sid = meta.get('session_id', sid)  # kept stable when the file is renamed
            history = [{**h, 'id':h['id']} for h in history]
            return {'version':0, 'session_id':sid, 'title':title or path.stem, 'context':'',
                    'template':'meeting', 'history':history, 'markers':[], 'parts':[],
                    'duration':max((x['end'] for x in history), default=0), 'backup_enabled':False,
                    'legacy':True, 'txt_path':str(path), 'created_at':meta.get('date'), 'insights':[]}
        data = load_manifest(path)
        for item in data['history']:
            if item.get('type') not in ('text', 'note') or not isinstance(item.get('time'), str):
                raise ValueError('Fragmento de sesión inválido')
        txt = (path.parent / data['txt']).resolve()
        if txt.parent != path.parent.parent or (txt.suffix.lower() not in ('.txt', '.md') or txt.name.lower().endswith('.analysis.md')):
            raise ValueError('Referencia de transcripción inválida')
        for part in data['parts']:
            self.audio_path(path, part['file'])
        data.setdefault('insights', [])
        return {**data, 'legacy':False, 'txt_path':str(txt), 'archive_path':str(path),
                'template':data.get('template', 'meeting')}

    @staticmethod
    def audio_path(manifest, name):
        root = Path(manifest).parent.resolve()
        if Path(name).name != name or not name.endswith('.wav'):
            raise ValueError('Referencia de audio inválida')
        path = (root / name).resolve()
        if path.parent != root:
            raise ValueError('Referencia de audio fuera de la sesión')
        return path

    def list(self):
        folder = Path(self.folder())
        warnings = []
        candidates = list(folder.glob('*.session/session.json')) + [Path(p) for p in self.external]
        covered = set()
        items = {}
        for path in candidates:
            try:
                data = self.register(path)
                covered.add(Path(data['txt_path']).resolve())
                items[data['session_id']] = data
            except (OSError, ValueError, KeyError, TypeError) as exc:
                warnings.append(f'{path.name}: {exc}')
                # Do not hide a transcript when its manifest cannot be read.
        for path in list(folder.glob('*.txt')) + list(folder.glob('*.md')):
            if path.name.lower().endswith('.analysis.md'):
                continue
            if path.resolve() not in covered:
                try:
                    data = self.register(path)
                    items[data['session_id']] = data
                except (OSError, ValueError) as exc:
                    warnings.append(f'{path.name}: {exc}')
        live = self.current()
        if live and live.get('session_id') and (live.get('state') != 'finished' or live['session_id'] not in items):
            items[live['session_id']] = {**items.get(live['session_id'], {}), **live}
        rows = []
        for sid, item in items.items():
            analysis = self.analysis(sid) if sid in self.paths else {}
            rows.append({k:item.get(k) for k in ('session_id','title','duration','date','state','legacy','template')} |
                        {'analysis_state':analysis.get('state', 'none')})
        return {'sessions':sorted(rows, key=lambda x:x.get('date') or '', reverse=True), 'warnings':warnings}

    def get(self, sid, include_live=True):
        live = self.current() if include_live else None
        with self.lock:
            path = self.paths.get(sid)
        if path is None and live and sid == live.get('session_id') and live.get('archive_path'):
            path = Path(live['archive_path'])
            with self.lock:
                self.paths[sid] = path
        if path is None:
            raise KeyError('Sesión no encontrada; actualiza el listado')
        data = self._read(path)
        if live and live.get('session_id') == sid and live.get('state') != 'finished':
            data.update(live)
        data.setdefault('state', 'finished')
        data['date'] = data.get('created_at') or datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds')
        for index, part in enumerate(data['parts']):
            part['available'] = self.audio_path(path, part['file']).is_file()
            part['url'] = f'/sessions/{sid}/audio/{index}'
        return data

    def analysis_path(self, sid):
        with self.lock:
            path = self.paths[sid]
        return path.with_suffix('.analysis.json') if path.suffix.lower() in ('.txt', '.md') else path.parent / 'analysis.json'

    def analysis(self, sid):
        # Windows readers must not hold the destination open during replacement.
        with self.lock:
            path = self.analysis_path(sid)
            if not path.exists():
                return {}
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                from session_edits import metadata
                meta = metadata(self.paths[sid])
                if meta.get('text_edited'):
                    source = data['result'].get('source_revision') if data.get('result') else data.get('source_revision')
                    data['stale'] = source != meta.get('text_revision')
                return data
            except (OSError, ValueError):
                return {'state':'error', 'error':'No se pudo leer el análisis guardado'}

    def relocate(self, sid, title, live=None):
        """Rename the transcript and its companions to '<timestamp>-<title>'. Returns the new path."""
        with self.lock:
            path = self.paths.get(sid)
            if path is None:
                raise KeyError('Sesión no encontrada; actualiza el listado')
            data = self._read_file(path)
            txt = Path(data['txt_path'])
            found = STAMP.match(txt.stem)
            prefix = found.group(1) if found else stamp_of(data.get('created_at'), txt)
            stem = file_stem(prefix, title)
            if txt.stem == stem or (txt.stem.startswith(stem + ' (') and txt.stem.endswith(')')):
                return path
            folder = txt.parent
            n = 0
            while True:
                candidate = f"{stem}{f' ({n})' if n else ''}"
                if not any((folder / (candidate + ext)).exists() for ext in ('.txt', '.md', '.session', '.analysis.json', '.analysis.md', '.txt.edits', '.md.edits')):
                    break
                n += 1
            from session_edits import edit_root
            if path.suffix.lower() in ('.txt', '.md'):
                moves = [(txt, folder / f'{candidate}{txt.suffix}'), (self.analysis_path(sid), folder / f'{candidate}.analysis.json'),
                         (self.markdown_path(sid), folder / f'{candidate}.analysis.md'),
                         (edit_root(path), folder / f'{candidate}{txt.suffix}.edits')]
                if sid.startswith('txt_'):
                    self._keep_legacy_id(path, sid)
                new_path = folder / f'{candidate}{txt.suffix}'
            else:
                moves = [(txt, folder / f'{candidate}{txt.suffix}'), (self.markdown_path(sid), folder / f'{candidate}.analysis.md'),
                         (path.parent, folder / f'{candidate}.session')]
                new_path = folder / f'{candidate}.session' / path.name
            done = []
            try:
                for source, target in moves:
                    if source.exists():
                        source.rename(target)
                        done.append((source, target))
                if path.suffix.lower() not in ('.txt', '.md'):
                    manifest = json.loads(new_path.read_text(encoding='utf-8'))
                    manifest['txt'] = f'../{candidate}{txt.suffix}'
                    atomic_json(new_path, manifest)
            except OSError:
                for source, target in reversed(done):
                    try: target.rename(source)
                    except OSError: pass
                raise
            self.paths[sid] = new_path.resolve()
            if str(path) in self.external:
                self.external[self.external.index(str(path))] = str(self.paths[sid])
                atomic_json(self.index_path, self.external)
            return self.paths[sid]

    def _keep_legacy_id(self, path, sid):
        from session_edits import edit_root
        meta_path = edit_root(path) / 'metadata.json'
        meta = json.loads(meta_path.read_text(encoding='utf-8')) if meta_path.exists() else {'version':1}
        if meta.get('session_id') != sid:
            meta['session_id'] = sid
            meta_path.parent.mkdir(exist_ok=True)
            atomic_json(meta_path, meta)

    def files(self, sid):
        """Everything that belongs to a saved session: transcript, folder, analysis and edits."""
        with self.lock:
            path = self.paths.get(sid)
        if path is None:
            raise KeyError('Sesión no encontrada; actualiza el listado')
        from session_edits import edit_root
        if path.suffix.lower() in ('.txt', '.md'):
            candidates = [path, self.analysis_path(sid), self.markdown_path(sid), edit_root(path)]
        else:
            data = self._read_file(path)
            candidates = [Path(data['txt_path']), self.markdown_path(sid), path.parent]
        return [c for c in candidates if c.exists()]

    def delete(self, sid, remove):
        """Remove a saved session with `remove(paths)` (the Recycle Bin in the app) and forget it."""
        with self.lock:
            paths = self.files(sid)
            remove(paths)
            path = self.paths.pop(sid)
            if str(path) in self.external:
                self.external.remove(str(path))
                atomic_json(self.index_path, self.external)
        return paths

    def save_analysis(self, sid, data, document=False):
        """`document` forces rewriting the .md (questions changed); otherwise only a new result does."""
        with self.lock:
            previous = (self.analysis(sid).get('result') or {}).get('created_at')
            atomic_json(self.analysis_path(sid), data)
            # Only a new result rewrites the document, so the user's edits survive progress saves.
            fresh = data.get('result') and (data['result'].get('created_at') != previous or not self.markdown_path(sid).exists())
            if fresh or (document and (data.get('result') or data.get('questions'))):
                try:
                    self.write_markdown(sid)
                except (OSError, ValueError, KeyError):
                    pass  # the JSON result is the source of truth; the document is a convenience copy

    def markdown_path(self, sid):
        """The editable analysis document, next to the transcript in the transcripts folder."""
        with self.lock:
            path = self.paths[sid]
        txt = path if path.suffix.lower() in ('.txt', '.md') else Path(self._read_file(path)['txt_path'])
        return txt.with_suffix('.analysis.md')

    def write_markdown(self, sid):
        with self.lock:
            saved = self.analysis(sid)
            if not saved.get('result') and not saved.get('questions'):
                raise KeyError('Esta sesión todavía no tiene análisis')
            data = self._read(self.paths[sid])
            target = self.markdown_path(sid)
            temporary = target.with_suffix('.md.tmp')
            temporary.write_text(analysis_markdown(data, saved.get('result'), saved.get('questions') or []), encoding='utf-8')
            temporary.replace(target)
            return target

    def change_marker(self, sid, marker_id, text):
        with self.lock:
            path = self.paths[sid]
            data = self._read(path)
            if data['legacy']:
                raise ValueError('El TXT antiguo es de solo lectura')
            marker = next((m for m in data['markers'] if m['id'] == marker_id), None)
            if marker is None:
                raise KeyError('Marcador no encontrado')
            if text is None:
                data['markers'].remove(marker)
            else:
                marker['text'] = text
            for item in data['history']:
                if item.get('marker_id') == marker_id:
                    item['text'] = f'Marcador: {text}' if text is not None else 'Marcador eliminado'
            txt = Path(data['txt_path'])
            if txt.is_file():
                with txt.open(encoding='utf-8') as file:
                    header = file.readline().rstrip('\n')
                lines = [header + '\n\n']
                for item in data['history']:
                    source = f"[{item['source']}] " if item['type'] == 'text' else '— '
                    lines.append(f"[{item['time']}] {source}{item['text']}\n")
                temporary = txt.with_suffix(txt.suffix + '.tmp')
                temporary.write_text(''.join(lines), encoding='utf-8')
                temporary.replace(txt)
            for key in ('legacy','txt_path','archive_path'):
                data.pop(key, None)
            atomic_json(path, data)
        return self.get(sid)


def stamp(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'
