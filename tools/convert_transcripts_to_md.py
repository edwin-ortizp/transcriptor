"""Offline, preview-first transcript migration. No vaults or transcript copies.

python tools/convert_transcripts_to_md.py           # preview
python tools/convert_transcripts_to_md.py --apply   # app must be closed
python tools/convert_transcripts_to_md.py --recover # undo an interrupted migration
"""
import argparse
import base64
import json
import os
from datetime import datetime
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from recording import atomic_bytes, atomic_json
from session_edits import edit_root, metadata, packed, sha
from session_repository import SessionRepository


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def app_running():
    """Fail closed if process inspection is unavailable; no importing app.py."""
    if os.name != 'nt':
        raise RuntimeError('La comprobación de procesos está disponible solo en Windows')
    result = subprocess.run([
        'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        "@(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { "
        "$_.Name -match '^python(w)?(\\.exe)?$' -and "
        "$_.CommandLine -match '(?i)(^|[\\s\\\\/\"])app\\.py([\\s\"]|$)' }).Count"
    ], capture_output=True, text=True, check=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW)
    return int(result.stdout.strip()) != 0


def ensure_idle(state, running_check):
    if running_check():
        raise RuntimeError('Cierra Transcriptor antes de convertir o recuperar archivos')
    jobs = read_json(state/'analysis-queue.json', [])
    if not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs):
        raise ValueError('Cola de análisis inválida')
    if any(j.get('state') in ('pending', 'running') for j in jobs):
        raise RuntimeError('Hay análisis pendientes o en curso; resuélvelos antes de convertir')


def fingerprint(path, updates=None):
    """Hash files/directories without putting transcript content into the journal."""
    updates = updates or {}
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError(f'No se convierten enlaces: {path}')
    if path.is_file():
        return sha(updates.get(path, path.read_bytes()))
    entries = {}
    for child in path.rglob('*'):
        if child.is_symlink():
            raise ValueError(f'No se convierten enlaces: {child}')
        if child.is_file():
            entries[str(child.relative_to(path))] = sha(updates.get(child, child.read_bytes()))
    for child, content in updates.items():
        if child.is_relative_to(path):
            entries[str(child.relative_to(path))] = sha(content)
    return sha(packed(entries))


def build_plan(folder, state):
    folder, state = Path(folder).resolve(), Path(state).resolve()
    journal = state/'transcript-md-migration.json'
    if journal.exists():
        raise RuntimeError('Hay un registro de conversión; usa --recover o revisa su resultado')
    index_path = state/'session-index.json'
    external = read_json(index_path, [])
    if not isinstance(external, list) or any(not isinstance(p, str) for p in external):
        raise ValueError('Índice de sesiones inválido')
    jobs_path = state/'analysis-queue.json'
    jobs = read_json(jobs_path, [])
    if not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs):
        raise ValueError('Cola de análisis inválida')
    repo = SessionRepository(lambda:folder, lambda:None, index_path)
    manifests = set(folder.glob('*.session/session.json'))
    candidates = set(folder.glob('*.txt'))
    for name in external + [j['source_path'] for j in jobs if j.get('source_path')]:
        path = Path(name).resolve()
        if path.suffix.lower() == '.txt':
            candidates.add(path)
        elif path.suffix.lower() == '.json':
            manifests.add(path)
    covered, writes, moves, mapping, sessions = {}, {}, [], {}, []
    roots = {folder, state}
    for manifest in sorted(manifests):
        data = repo._read_file(manifest)
        txt = Path(data['txt_path'])
        if txt.suffix.lower() == '.txt':
            candidates.add(txt)
            if txt in covered:
                raise ValueError(f'Dos manifiestos apuntan a la misma transcripción: {txt}')
            covered[txt] = manifest
    for txt in sorted(candidates):
        txt = txt.resolve()
        target = txt.with_suffix('.md')
        roots.add(txt.parent)
        if txt.name.lower().endswith('.analysis.txt'):
            raise ValueError(f'Nombre reservado para análisis: {txt}')
        if target.name.lower().endswith('.analysis.md'):
            raise ValueError(f'El destino se confundiría con un análisis: {target}')
        if not txt.is_file() or target.exists():
            raise ValueError(f'Origen ausente o destino ocupado: {txt} → {target}')
        path = covered.get(txt, txt)
        root = edit_root(path)
        if (root/'transaction.json').exists():
            raise ValueError(f'Hay una edición pendiente de recuperar: {path}')
        data = repo._read_file(path)
        date = data.get('created_at') or datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds')
        sessions.append({'session_id':data['session_id'], 'source':str(txt),
                         'target':str(target), 'reader':str(path if path != txt else target),
                         'date':date, 'hash':sha(txt.read_bytes())})
        if txt in covered:
            manifest = covered[txt]
            stored = read_json(manifest, {})
            stored['txt'] = '../' + target.name
            if not stored.get('created_at'):
                stored['created_at'] = date
            writes[manifest] = packed(stored)
        else:
            meta = metadata(txt)
            meta.update(version=1, session_id=data['session_id'], history=data['history'],
                        title=data['title'], txt_hash=sha(txt.read_bytes()))
            meta.setdefault('date', data.get('created_at') or
                            datetime.fromtimestamp(txt.stat().st_mtime).isoformat(timespec='seconds'))
            writes[root/'metadata.json'] = packed(meta)
            dest_root = edit_root(target)
            if dest_root.exists():
                raise ValueError(f'Destino de ediciones ocupado: {dest_root}')
            moves.append((root, dest_root))
        moves.append((txt, target))
        mapping[str(txt)] = str(target)
        # Saved analysis jobs also contain the original standalone source path.
        analysis_path = txt.with_suffix('.analysis.json') if path == txt else path.parent/'analysis.json'
        if analysis_path.exists():
            saved = read_json(analysis_path, {})
            if saved.get('source_path') == str(txt):
                saved['source_path'] = str(target)
                writes[analysis_path] = packed(saved)
    new_external = [mapping.get(p, p) for p in external]
    if new_external != external:
        writes[index_path] = packed(new_external)
    changed_jobs = False
    for job in jobs:
        old = job.get('source_path')
        if old in mapping:
            job['source_path'] = mapping[old]
            changed_jobs = True
    if changed_jobs:
        writes[jobs_path] = packed(jobs)
    operations = []
    for path, content in writes.items():
        before = path.read_bytes() if path.exists() else None
        operations.append({'kind':'write', 'path':str(path),
                           'before':encode(before), 'after':encode(content),
                           'times':([path.stat().st_atime_ns, path.stat().st_mtime_ns]
                                    if path.exists() else None)})
    for source, target in moves:
        # A new standalone metadata directory does not exist yet during preview.
        expected = fingerprint(source, writes)
        if expected is None and source.suffix == '.edits':
            expected = sha(packed({str(p.relative_to(source)):sha(v)
                                   for p,v in writes.items() if p.is_relative_to(source)}))
        operations.append({'kind':'move', 'source':str(source), 'target':str(target), 'hash':expected})
    for op in operations:
        for key in ('path', 'source', 'target'):
            if key in op and not any(Path(op[key]).resolve().is_relative_to(r) for r in roots):
                raise ValueError('Destino fuera de las carpetas de la conversión')
    return {'version':1, 'status':'prepared', 'folder':str(folder), 'state':str(state),
            'roots':[str(r) for r in sorted(roots)], 'sessions':sessions, 'operations':operations}


def encode(content):
    return base64.b64encode(content).decode('ascii') if content is not None else None


def decode(content):
    return base64.b64decode(content, validate=True) if content is not None else None


def recover(folder, state, running_check=app_running):
    folder, state = Path(folder).resolve(), Path(state).resolve()
    ensure_idle(state, running_check)
    journal = state/'transcript-md-migration.json'
    plan = read_json(journal, None)
    if not plan or plan.get('version') != 1 or plan.get('folder') != str(folder) or plan.get('state') != str(state):
        raise ValueError('Registro de conversión incompatible')
    if plan['status'] == 'committed':
        return 'La conversión ya terminó; se conserva el registro como evidencia'
    if plan['status'] == 'rolled_back':
        return 'La conversión ya fue revertida'
    if plan['status'] != 'prepared':
        raise ValueError('Estado de conversión inválido')
    roots = [Path(r).resolve() for r in plan['roots']]
    for op in reversed(plan['operations']):
        for key in ('path', 'source', 'target'):
            if key in op and not any(Path(op[key]).resolve().is_relative_to(r) for r in roots):
                raise ValueError('Destino de recuperación fuera de las carpetas registradas')
        if op['kind'] == 'move':
            source, target = Path(op['source']), Path(op['target'])
            if target.exists():
                if source.exists() or fingerprint(target) != op['hash']:
                    raise ValueError(f'Un destino cambió; se conserva para revisión: {target}')
                target.rename(source)
        elif op['kind'] == 'write':
            path = Path(op['path'])
            before, after = decode(op['before']), decode(op['after'])
            current = path.read_bytes() if path.exists() else None
            if current not in (before, after):
                raise ValueError(f'Un archivo cambió; se conserva para revisión: {path}')
            if before is None:
                if path.exists():
                    path.unlink()
                if path.parent.suffix == '.edits' and path.parent.is_dir() and not any(path.parent.iterdir()):
                    path.parent.rmdir()
            else:
                atomic_bytes(path, before)
                if op.get('times'):
                    os.utime(path, ns=tuple(op['times']))
        else:
            raise ValueError('Operación de recuperación inválida')
    atomic_json(journal, {**plan, 'status':'rolled_back'})
    return 'Conversión revertida; se conserva el registro'


def apply_plan(plan, running_check=app_running):
    folder, state = Path(plan['folder']), Path(plan['state'])
    ensure_idle(state, running_check)
    journal = state/'transcript-md-migration.json'
    if journal.exists():
        raise RuntimeError('Ya existe un registro de conversión; no se sobrescribe')
    # Validate the whole plan again before touching any session.
    for op in plan['operations']:
        if op['kind'] == 'write':
            p = Path(op['path'])
            if (p.read_bytes() if p.exists() else None) != decode(op['before']):
                raise ValueError(f'El archivo cambió desde la vista previa: {p}')
        elif Path(op['target']).exists():
            raise ValueError(f'Destino ocupado: {op["target"]}')
    for item in plan['sessions']:
        if fingerprint(Path(item['source'])) != item['hash']:
            raise ValueError('Una transcripción cambió desde la vista previa')
    if not plan['sessions']:
        return 0
    state.mkdir(parents=True, exist_ok=True)
    atomic_json(journal, plan)
    try:
        for op in plan['operations']:
            if op['kind'] == 'write':
                path = Path(op['path'])
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_bytes(path, decode(op['after']))
                if op.get('times'):
                    os.utime(path, ns=tuple(op['times']))
            else:
                source, target = Path(op['source']), Path(op['target'])
                if target.exists() or fingerprint(source) != op['hash']:
                    raise ValueError(f'Conflicto antes de mover: {source}')
                source.rename(target)
        repo = SessionRepository(lambda:folder, lambda:None, state/'session-index.json')
        for item in plan['sessions']:
            target = Path(item['target'])
            if fingerprint(target) != item['hash']:
                raise ValueError('El contenido convertido no coincide')
            data = repo.register(Path(item['reader']))
            if data['session_id'] != item['session_id']:
                raise ValueError('Cambió el identificador de una sesión')
            if data['date'] != item['date']:
                raise ValueError('Cambió la fecha de una sesión')
        atomic_json(journal, {**plan, 'status':'committed'})
    except Exception:
        recover(folder, state, running_check)
        raise
    return len(plan['sessions'])


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path)
    parser.add_argument('--state', type=Path, default=BASE/'state')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    saved = read_json(BASE/'settings.json', {})
    folder = args.folder or Path(os.path.expandvars(saved.get('output_dir') or str(BASE/'transcripciones'))).expanduser()
    if not folder.is_absolute():
        folder = BASE/folder
    try:
        if args.recover:
            print(recover(folder, args.state))
            return
        plan = build_plan(folder, args.state)
        for item in plan['sessions']:
            print(f'{item["source"]} → {item["target"]}')
        print(f'{len(plan["sessions"])} transcripciones; sin sobrescribir destinos.')
        if args.apply:
            print(f'Convertidas: {apply_plan(plan)}')
        else:
            print('Vista previa. Cierra Transcriptor y usa --apply para convertir.')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'No se completó la conversión: {exc}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
