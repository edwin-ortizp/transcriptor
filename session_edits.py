"""Explicit, previewed session changes with recoverable multi-file transactions."""
import copy
import hashlib
import json
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from corrections import Corrections
from mapping_store import MappingConflict
from recording import atomic_bytes, atomic_json


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')


def text_revision(data):
    return sha(packed([item for item in data['history'] if item.get('type') == 'text']))


def edit_root(path):
    return path.with_suffix(path.suffix + '.edits') if path.suffix.lower() in ('.txt', '.md') else path.parent/'edits'


def metadata(path):
    target = edit_root(path)/'metadata.json'
    if not target.exists(): return {}
    data = json.loads(target.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or data.get('version') != 1:
        raise ValueError('Metadatos de edición incompatibles; conserva los archivos para revisarlos')
    return data


def targets_for(path, txt_name):
    if Path(txt_name).name != txt_name or (Path(txt_name).suffix.lower() not in ('.txt', '.md') or txt_name.lower().endswith('.analysis.md')):
        raise ValueError('Referencia de transcripción de edición inválida')
    root = edit_root(path)
    if path.suffix.lower() in ('.txt', '.md'):
        if txt_name != path.name: raise ValueError('Referencia de edición inválida')
        return {'txt':path, 'metadata':root/'metadata.json'}
    return {'txt':path.parent.parent/txt_name, 'manifest':path, 'metadata':root/'metadata.json'}


def recover(path):
    """A prepared transaction rolls back; a committed one only needs cleanup."""
    root = edit_root(path)
    journal = root/'transaction.json'
    if not journal.exists(): return
    txn = json.loads(journal.read_text(encoding='utf-8'))
    if txn.get('version') != 1 or not re.fullmatch('[a-f0-9]{32}',txn.get('id','')) or txn.get('state') not in ('prepared','committed'):
        raise ValueError('Transacción de edición incompatible; conserva sus archivos')
    targets = targets_for(path,txn['txt_name'])
    folder = root/'revisions'/txn['id']
    if txn['state'] == 'prepared':
        for key, entry in txn['files'].items():
            if key not in targets: raise ValueError('Destino de recuperación inválido')
            before = (folder/(key+'.before')).read_bytes() if entry['existed'] else None
            after = (folder/(key+'.after')).read_bytes()
            if (before is not None and sha(before) != entry['before']) or sha(after) != entry['after']:
                raise ValueError('El respaldo de recuperación está dañado')
            current = targets[key].read_bytes() if targets[key].exists() else None
            if current not in (before,after):
                raise ValueError('Un archivo cambió durante una recuperación pendiente; requiere revisión manual')
        for key, entry in txn['files'].items():
            if entry['existed']: atomic_bytes(targets[key],(folder/(key+'.before')).read_bytes())
            elif targets[key].exists(): targets[key].unlink()
    journal.unlink()


def commit_files(path, txt_name, files, previous_text, operation, revision=None, expected=None):
    root = edit_root(path)
    root.mkdir(exist_ok=True)
    revision = revision or uuid.uuid4().hex
    folder = root/'revisions'/revision
    folder.mkdir(parents=True)
    targets = targets_for(path,txt_name)
    entries = {}
    for key, content in files.items():
        target = targets[key]
        before = target.read_bytes() if target.exists() else None
        if expected is not None and before != expected[key]:
            raise MappingConflict('Un archivo cambió antes de guardar. Vuelve a calcular la vista previa.')
        if before is not None: atomic_bytes(folder/(key+'.before'),before)
        atomic_bytes(folder/(key+'.after'),content)
        entries[key] = {'existed':before is not None,'before':sha(before) if before is not None else None,'after':sha(content)}
    atomic_json(folder/'text.json',previous_text)
    atomic_json(folder/'operation.json',{'operation':operation,'created_at':datetime.now(timezone.utc).isoformat()})
    for key,target in targets.items():
        if key in files and expected is not None:
            current = target.read_bytes() if target.exists() else None
            if current != expected[key]: raise MappingConflict('Un archivo cambió antes de confirmar la escritura')
    txn = {'version':1,'id':revision,'state':'prepared','txt_name':txt_name,'files':entries}
    journal = root/'transaction.json'
    atomic_json(journal,txn)
    try:
        for key, content in files.items(): atomic_bytes(targets[key],content)
        atomic_json(journal,{**txn,'state':'committed'})
    except Exception:
        recover(path)
        raise
    try: journal.unlink()
    except OSError: pass  # Durable commit is complete; cleanup on next read.
    return revision


def legacy_slots(raw):
    return [i for i,line in enumerate(raw.decode('utf-8-sig').splitlines()) if line.strip() and not line.startswith('#')]


def update_txt(raw, before, after, meta, title=None):
    bom = raw.startswith(b'\xef\xbb\xbf')
    value = raw.decode('utf-8-sig')
    newline = '\r\n' if '\r\n' in value else '\n'
    slots = None
    if before.get('legacy'):
        lines = value.splitlines(keepends=True)
        slots = meta.get('line_slots') if meta.get('txt_hash') == sha(raw) else None
        slots = list(slots) if slots is not None else legacy_slots(raw)
        if len(slots) != len(before['history']):
            raise MappingConflict('La estructura del TXT cambió. Recarga antes de corregirlo.')
        for index,(old,new) in enumerate(zip(before['history'],after['history'])):
            if old['text'] == new['text']: continue
            row = slots[index]
            line = lines[row].rstrip('\r\n')
            match = re.match(r'^\[(\d+):(\d\d):(\d\d)\]\s*(?:\[([^]]+)\])?\s*(.*)',line)
            start = match.start(5) if match else 0
            if line[start:] != old['text']:
                raise MappingConflict('El TXT no coincide con la sesión. Recarga antes de guardar.')
            ending = lines[row][len(line):]
            lines[row] = line[:start] + new['text'] + ending
        value = ''.join(lines)
    else:
        cursor, pieces = 0, []
        for old,new in zip(before['history'],after['history']):
            if old.get('type') != 'text': continue
            prefix = f"[{old['time']}] [{old['source']}] "
            needle = prefix + old['text'].replace('\r\n','\n').replace('\n',newline)
            pos = value.find(needle,cursor)
            if pos < 0:
                raise MappingConflict('El TXT no coincide con los bloques guardados. Revisa los cambios externos.')
            pieces.extend((value[cursor:pos],prefix + new['text'].replace('\r\n','\n').replace('\n',newline)))
            cursor = pos + len(needle)
        value = ''.join(pieces) + value[cursor:]
    if title is not None:
        if value.startswith('#'):
            first,separator,rest = value.partition('\n')
            suffix = (' — ' + first.rstrip('\r').rsplit(' — ',1)[1]) if ' — ' in first else ''
            value = '# ' + title + suffix + (newline + rest if separator else '')
        else:
            value = '# ' + title + newline + value
            if slots is not None: slots = [row+1 for row in slots]
    return (b'\xef\xbb\xbf' if bom else b'') + value.encode('utf-8'), slots


class SessionEdits:
    def __init__(self, repository, mappings):
        self.repository, self.mappings = repository, mappings
        self.previews = {}

    def read(self,sid):
        repo = self.repository()
        data = repo.get(sid,include_live=False)
        if data['state'] != 'finished': raise MappingConflict('Termina el procesamiento de esta sesión antes de modificarla')
        path = repo.paths[sid]
        meta = metadata(path)
        txt = Path(data['txt_path'])
        raw = txt.read_bytes()
        source = path.read_bytes() if not data.get('legacy') else raw
        meta_path = edit_root(path)/'metadata.json'
        meta_hash = sha(meta_path.read_bytes()) if meta_path.exists() else None
        revision = sha(packed([sha(source),sha(raw),meta_hash]))
        return data,path,meta,raw,revision

    def info(self,sid):
        data,path,meta,raw,revision = self.read(sid)
        return {'revision':revision,'title':data['title'],'can_restore':bool(meta.get('text_versions')),
                'has_base':bool(meta.get('base'))}

    def rules(self):
        snapshot = self.mappings.snapshot()
        if not snapshot['files'] or any(not f['exists'] or f.get('error') for f in snapshot['files']):
            raise ValueError('No se pudieron leer todos los mappings configurados. Revisa Diccionario antes de aplicar cambios.')
        engine = object.__new__(Corrections)
        engine.rules = [(r['source'],r['target'],r['mode'],r['case']) for r in snapshot['rules']]
        engine._compile()
        signature = sha(packed([(f['id'],f['revision']) for f in snapshot['files']]))
        return snapshot,engine,signature

    def preview(self,sid,operation):
        data,path,meta,raw,revision = self.read(sid)
        after = copy.deepcopy(data)
        snapshot,signature = None,None
        if operation == 'mappings':
            snapshot,engine,signature = self.rules()
            base = meta.get('base') or {b['id']:b['text'] for b in data['history'] if b['type']=='text'}
            current_ids = {b['id'] for b in data['history'] if b['type']=='text'}
            if set(base) != current_ids: raise MappingConflict('La estructura del texto cambió fuera de la aplicación; revisa la base conservada')
            for block in after['history']:
                if block['type'] == 'text': block['text'] = engine.apply(base[block['id']])
        elif operation == 'restore':
            versions = meta.get('text_versions',[])
            if not versions: raise ValueError('No hay una versión anterior del texto')
            revision_id = versions[-1]
            if not re.fullmatch('[a-f0-9]{32}',revision_id): raise ValueError('Referencia de respaldo inválida')
            base = json.loads((edit_root(path)/'revisions'/revision_id/'text.json').read_text(encoding='utf-8'))
            current_ids = {b['id'] for b in data['history'] if b['type']=='text'}
            if set(base) != current_ids: raise MappingConflict('El respaldo no coincide con los bloques de la sesión')
            for block in after['history']:
                if block['type'] == 'text': block['text'] = base[block['id']]
        else: raise ValueError('Operación de edición inválida')
        proposed,slots = update_txt(raw,data,after,meta)
        changes = [{'id':old['id'],'time':old['time'],'source':old.get('source','Otros'),
                    'before':old['text'],'after':new['text']} for old,new in zip(data['history'],after['history']) if old['text'] != new['text']]
        token = uuid.uuid4().hex
        self.previews = {k:v for k,v in self.previews.items() if time.monotonic()-v['at'] < 1800}
        if len(self.previews) >= 64: self.previews.pop(next(iter(self.previews)))
        self.previews[token] = {'at':time.monotonic(),'sid':sid,'revision':revision,'rules_revision':signature,
                                'operation':operation,'history':after['history'],'txt':proposed,'slots':slots}
        return {'token':token,'revision':revision,'operation':operation,'changes':changes,'changed_blocks':len(changes),
                'files':snapshot['files'] if snapshot else [],'warnings':snapshot['warnings'] if snapshot else [],
                'base_notice':'Se calcula desde la base conservada; no recupera el reconocimiento anterior a mappings aplicados al grabar.'}

    def apply(self,sid,token):
        preview = self.previews.get(token)
        if not preview or preview['sid'] != sid or time.monotonic()-preview['at'] > 1800:
            raise MappingConflict('La vista previa caducó. Vuelve a calcular los cambios.')
        data,path,meta,raw,revision = self.read(sid)
        if revision != preview['revision']: raise MappingConflict('La sesión cambió. Vuelve a calcular los cambios; el borrador se conserva.')
        if preview['operation']=='mappings' and self.rules()[2] != preview['rules_revision']:
            raise MappingConflict('Los mappings cambiaron. Vuelve a calcular la vista previa.')
        after = {**data,'history':copy.deepcopy(preview['history'])}
        if all(a['text']==b['text'] for a,b in zip(data['history'],after['history'])):
            raise ValueError('No hay cambios que guardar')
        result = self.save(data,path,meta,raw,after,preview['txt'],preview['slots'],preview['operation'])
        self.previews.pop(token,None)
        return result

    def rename(self,sid,title,revision):
        if not isinstance(title,str): raise ValueError('Introduce un título')
        if any(unicodedata.category(c) in ('Cc','Cs') for c in title): raise ValueError('El título no admite caracteres de control')
        title = title.strip()
        if not 1 <= len(title) <= 120: raise ValueError('El título debe tener entre 1 y 120 caracteres')
        data,path,meta,raw,current = self.read(sid)
        if revision != current: raise MappingConflict('La sesión cambió. Recarga; tu título se conserva.')
        if title == data['title']: return data
        after = {**data,'title':title}
        proposed,slots = update_txt(raw,data,after,meta,title)
        return self.save(data,path,meta,raw,after,proposed,slots,'rename')

    def save(self,data,path,meta,raw,after,proposed,slots,operation):
        meta_path = edit_root(path)/'metadata.json'
        expected = {'txt':raw,'metadata':meta_path.read_bytes() if meta_path.exists() else None}
        if not data.get('legacy'): expected['manifest'] = path.read_bytes()
        previous = {b['id']:b['text'] for b in data['history'] if b['type']=='text'}
        meta = copy.deepcopy(meta)
        meta.update(version=1,date=meta.get('date',data['date']),txt_hash=sha(proposed),text_revision=text_revision(after))
        if data.get('legacy'):
            meta.update(history=after['history'],line_slots=slots,title=after['title'])
        if operation != 'rename':
            meta.setdefault('base',previous)
            meta['text_edited'] = True
        # Choose transaction id up front so the durable metadata references its backup.
        revision = uuid.uuid4().hex
        if operation != 'rename': meta.setdefault('text_versions',[]).append(revision)
        # commit_files accepts a prepared id via the metadata reference.
        files = {'txt':proposed,'metadata':packed(meta)}
        if not data.get('legacy'):
            stored = json.loads(path.read_text(encoding='utf-8'))
            stored.update(title=after['title'],history=after['history'])
            files['manifest'] = packed(stored)
        commit_files(path,Path(data['txt_path']).name,files,previous,operation,revision,expected)
        return self.repository().get(data['session_id'],include_live=False)
