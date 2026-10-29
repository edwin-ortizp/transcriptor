"""Explicit, local word review. No transcription changes or provider requests."""
import hashlib
from contextlib import ExitStack
import json
import math
import re
import threading
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from mapping_store import MappingConflict
from personal_terms import normalized, term_id
from recording import atomic_json

TOKEN = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
ADDRESS = re.compile(r"(?:[a-z][a-z0-9+.\-]*://|www\.)[^\s<>]+|[\w.+'\-]+@[\w.\-]+\.\w+", re.IGNORECASE)


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()


class LocalSpelling:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.loaded_signature = None
        self.file_stamp = None
        self.dictionaries = None

    def signature(self):
        with self.lock:
            files = [self.root/'sources.json',self.root/'terms.tsv'] + [self.root/lang/f'index.{suffix}' for lang in ('es','en') for suffix in ('dic','aff')]
            stamp = tuple((p.stat().st_mtime_ns,p.stat().st_size) for p in files)
            if stamp != self.file_stamp:
                signature = digest([hashlib.sha256(p.read_bytes()).hexdigest() for p in files])
                self.file_stamp, self._signature = stamp, signature
            return self._signature

    def load(self):
        with self.lock:
            signature = self.signature()
            if self.loaded_signature != signature:
                from spylls.hunspell import Dictionary
                from spylls.hunspell import readers
                from spylls.hunspell.readers.file_reader import FileReader
                dictionaries = []
                for language in ('es','en'):
                    # Spylls 0.1.7 reopens readers after SET and does not close them.
                    # Own every opened stream, including those replaced during decoding.
                    with ExitStack() as streams:
                        class ManagedReader(FileReader):
                            def _open(self,path,encoding):
                                return streams.enter_context(super()._open(path,encoding))
                        base = str(self.root/language/'index')
                        aff,context = readers.read_aff(ManagedReader(base+'.aff'))
                        dic = readers.read_dic(ManagedReader(base+'.dic',encoding=context.encoding),aff=aff,context=context)
                        dictionaries.append(Dictionary(aff,dic))
                dictionaries = tuple(dictionaries)
                own = []
                for line in (self.root/'terms.tsv').read_text(encoding='utf-8-sig').splitlines():
                    if not line.strip() or line.startswith('#'):
                        continue
                    cols = line.split('\t')
                    if len(cols) not in (2,3) or cols[1] not in ('es','en','both') or not cols[0].strip():
                        raise ValueError('El glosario del sistema tiene una entrada inválida')
                    own.append(unicodedata.normalize('NFC',cols[0]))
                self.dictionaries, self.own = dictionaries, own
                self.loaded_signature = signature
            return self.dictionaries, tuple(self.own)


def coverage_patterns(mappings, terms):
    groups = {}
    for rule in mappings:
        bucket = groups.setdefault((rule['mode'],rule['case']),set())
        bucket.update(unicodedata.normalize('NFC',v) for v in (rule['source'],rule['target']) if v)
    groups.setdefault(('word','nocase'),set()).update(terms)
    patterns = [ADDRESS]
    for (mode,case), values in groups.items():
        if not values:
            continue
        body = '|'.join(re.escape(value) for value in sorted(values,key=lambda v:(-len(v),v)))
        if mode == 'word':
            body = r'(?<!\w)(?:' + body + r')(?!\w)'
        patterns.append(re.compile(body,0 if case == 'case' else re.IGNORECASE))
    return patterns


def mapping_spans(text, mappings, terms):
    return [match.span() for pattern in coverage_patterns(mappings,terms) for match in pattern.finditer(text)]


def find_candidates(data, dictionaries, own, personal, mappings, progress=lambda *_:None, include_keys=()):
    terms = [unicodedata.normalize('NFC',e['term']) for e in personal['entries']] + list(own)
    known_terms = {normalized(t) for t in terms}
    patterns = coverage_patterns(mappings,terms)
    lookup = {}
    candidates = {}
    blocks = [item for item in data['history'] if item['type'] == 'text']
    for index, item in enumerate(blocks):
        text = unicodedata.normalize('NFC',item['text'])
        address_spans = [match.span() for match in ADDRESS.finditer(text)]
        spans = address_spans + [match.span() for pattern in patterns[1:] for match in pattern.finditer(text)]
        for token in TOKEN.finditer(text):
            word = token.group()
            key = normalized(word)
            if any(a <= token.start() and token.end() <= b for a,b in address_spans):
                continue
            covered = any(a <= token.start() and token.end() <= b for a,b in spans)
            if not any(c.isalpha() for c in word) or (covered and key not in include_keys):
                continue
            if key not in lookup:
                variants = dict.fromkeys((word,word.lower(),word.capitalize()))
                lookup[key] = key in known_terms or any(dictionary.lookup(variant) for variant in variants for dictionary in dictionaries)
            if lookup[key] and key not in include_keys:
                continue
            candidate = candidates.setdefault(key,{'id':term_id(word),'key':key,'term':word,'variants':Counter(),'count':0,'occurrences':[]})
            candidate['variants'][word] += 1
            candidate['count'] += 1
            candidate['recognized'] = candidate.get('recognized',True) and (lookup[key] or covered)
            candidate['occurrences'].append({'fragment_id':item['id'],'start':item.get('start',0),'time':item['time'],
                'source':item.get('source','Otros'), 'context':text[max(0,token.start()-70):token.end()+70]})
        progress(index+1,len(blocks))
    for item in candidates.values():
        item['variants'] = dict(item['variants'])
        item['status'] = 'pending'
    return sorted(candidates.values(),key=lambda x:(-x['count'],x['key']))


class ReviewService:
    def __init__(self, repository, spelling, personal, mappings, notify):
        self.repository, self.spelling, self.personal, self.mappings, self.notify = repository, spelling, personal, mappings, notify
        self.lock = threading.RLock()
        self.scanning = set()
        self.threads = {}
        self.errors = {}
        self.scan_gate = threading.Lock()

    def path(self,sid):
        repo = self.repository()
        with repo.lock:
            path = repo.paths[sid]
        return path.with_suffix('.review.json') if path.suffix.lower()=='.txt' else path.parent/'review.json'

    def read(self,sid):
        path = self.path(sid)
        if not path.exists():
            return {'version':1,'state':'idle','rows':[],'decisions':{},'fingerprint':None}, 'missing'
        raw = path.read_bytes()
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data,dict) or data.get('version') != 1 or not isinstance(data.get('rows'),list) or not isinstance(data.get('decisions'),dict):
            raise ValueError('El archivo de revisión no es compatible; no se sobrescribirá')
        for row in data['rows']:
            if not isinstance(row,dict) or not all(isinstance(row.get(k),str) for k in ('id','key','term')) or row.get('status') not in ('pending','ignored','accepted','mapped') or not isinstance(row.get('occurrences'),list):
                raise ValueError('El archivo de revisión contiene términos inválidos; no se sobrescribirá')
            if normalized(row['term']) != row['key'] or term_id(row['term']) != row['id'] or not isinstance(row.get('count'),int) or row['count'] < 1 or not isinstance(row.get('variants'),dict):
                raise ValueError('La revisión contiene un término o contador inválido')
            for occurrence in row['occurrences']:
                if not isinstance(occurrence,dict) or not all(isinstance(occurrence.get(k),str) for k in ('fragment_id','time','context')) or not isinstance(occurrence.get('start'),(int,float)) or not math.isfinite(occurrence['start']) or occurrence['start'] < 0:
                    raise ValueError('La revisión contiene una referencia temporal inválida')
        if data['rows'] and not isinstance(data.get('fingerprint'),dict):
            raise ValueError('La revisión no tiene una referencia válida del texto y vocabularios')
        if any(not isinstance(decision,dict) or decision.get('action') not in ('ignore','accept','mapping') for decision in data['decisions'].values()):
            raise ValueError('La revisión contiene decisiones inválidas')
        return data, hashlib.sha256(raw).hexdigest()

    def inputs(self,sid):
        data = self.repository().get(sid,include_live=False)
        personal = self.personal.read()
        mappings = self.mappings.snapshot()
        if any(f.get('error') for f in mappings['files']):
            raise ValueError('No se pudieron leer todos los mappings; no se generará una revisión incompleta')
        fingerprint = {'text':digest([{'id':b['id'],'text':b['text'],'start':b.get('start'), 'time':b['time']} for b in data['history'] if b['type']=='text']),
                       'global':self.spelling.signature(),'personal':personal['revision'],
                       'mappings':digest([(f['id'],f['revision']) for f in mappings['files']])}
        return data,personal,mappings,fingerprint

    def get(self,sid,q='',status='pending',page=1,page_size=25):
        with self.lock:
            data,revision = self.read(sid)
            stale,warning = False,None
            if data.get('fingerprint'):
                try:
                    stale = self.inputs(sid)[3] != data['fingerprint']
                except (OSError,ValueError):
                    stale,warning = True,'No se pudieron verificar todos los vocabularios; vuelve a buscar cuando estén disponibles'
            rows = data['rows']
            filtered = [r for r in rows if (r['status']=='pending') == (status=='pending') and normalized(q) in normalized(r['term'])]
            start = (page-1)*page_size
            return {**{k:v for k,v in data.items() if k not in ('rows','decisions','fingerprint')}, 'revision':revision,
                    'rows':filtered[start:start+page_size], 'total':len(filtered), 'page':page,'page_size':page_size,
                    'pending':sum(r['status']=='pending' for r in rows),'reviewed':sum(r['status']!='pending' for r in rows),
                    'state':'error' if sid in self.errors else data['state'],
                    'error':self.errors.get(sid,data.get('error')),
                    'stale':stale,'warning':warning,'scanning':sid in self.scanning}

    def start(self,sid):
        data = self.repository().get(sid)
        if data['state'] != 'finished':
            raise ValueError('Termina la sesión antes de buscar palabras')
        with self.lock:
            if sid in self.scanning:
                raise MappingConflict('Ya se están buscando palabras en esta sesión')
            self.read(sid)  # Do not overwrite an unreadable review.
            self.errors.pop(sid,None)
            self.scanning.add(sid)
            thread = threading.Thread(target=self._scan,args=(sid,),daemon=True)
            self.threads[sid] = thread
            thread.start()
        self.notify({'type':'review','session_id':sid,'state':'scanning'})
        return {'state':'scanning','session_id':sid}

    def _scan(self,sid):
        error = None
        try:
            with self.scan_gate:
                data,personal,mappings,fingerprint = self.inputs(sid)
                dictionaries,own = self.spelling.load()
                def progress(done,total):
                    if done == total or done % 50 == 0:
                        self.notify({'type':'review','session_id':sid,'state':'scanning','checked':done,'total':total})
                with self.lock:
                    previous,_ = self.read(sid)
                    include_keys = set(previous['decisions'])
                candidates = find_candidates(data,dictionaries,own,personal,mappings['rules'],progress,include_keys)
                with self.lock:
                    previous,_ = self.read(sid)
                    decisions = previous['decisions']
                    for row in candidates:
                        decision = decisions.get(row['key'],{}).get('action')
                        if decision == 'ignore':
                            row['status'] = 'ignored'
                        elif row['recognized'] and decision in ('accept','mapping'):
                            row['status'] = 'accepted' if decision == 'accept' else 'mapped'
                        else:
                            row['status'] = 'pending'
                    result = {'version':1,'state':'completed','updated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                              'rows':sorted(candidates,key=lambda r:(-r['count'],r['key'])),'decisions':decisions,'fingerprint':fingerprint}
                    atomic_json(self.path(sid),result)
        except Exception:
            error = 'No se pudo completar la búsqueda o guardar sus resultados. Revisa los vocabularios, la dependencia spylls y los permisos; se conserva la revisión anterior.'
            with self.lock:
                self.errors[sid] = error
                try:
                    previous,_ = self.read(sid)
                    atomic_json(self.path(sid),{**previous,'state':'error','error':error})
                except (OSError,ValueError):
                    pass
        finally:
            with self.lock:
                self.scanning.discard(sid)
            self.notify({'type':'review','session_id':sid,'state':'error' if error else 'completed','error':error})

    def decide(self,sid,entry_id,action,revision):
        with self.lock:
            if sid in self.scanning:
                raise MappingConflict('Espera a que termine la búsqueda')
            data,current = self.read(sid)
            if revision != current:
                raise MappingConflict('La revisión cambió; recarga antes de decidir')
            row = next((r for r in data['rows'] if r['id']==entry_id),None)
            if not row:
                raise KeyError('Término no encontrado en esta revisión')
            if action == 'accept':
                personal = self.personal.read()
                self.personal.add(row['term'],personal['revision'],allow_existing=True)
                data['fingerprint']['personal'] = self.personal.read()['revision']
                row['status'] = 'accepted'
            elif action == 'ignore':
                row['status'] = 'ignored'
            elif action == 'restore':
                if row['status'] != 'ignored':
                    raise ValueError('Solo se puede deshacer una decisión de ignorar; gestiona los términos personales desde Diccionario')
                row['status'] = 'pending'
            elif action == 'mapping':
                source,personal,mappings,_ = self.inputs(sid)
                matched,all_covered = False,True
                for block in source['history']:
                    if block['type']!='text':
                        continue
                    text = unicodedata.normalize('NFC',block['text'])
                    spans = mapping_spans(text,mappings['rules'],[])
                    addresses = [m.span() for m in ADDRESS.finditer(text)]
                    hits = [m for m in TOKEN.finditer(text) if normalized(m.group())==row['key'] and not any(a<=m.start() and m.end()<=b for a,b in addresses)]
                    if hits:
                        matched = True
                        all_covered = all_covered and all(any(a<=m.start() and m.end()<=b for a,b in spans) for m in hits)
                if not matched or not all_covered:
                    raise ValueError('El mapping debe cubrir todas las variantes de este término; ajusta la regla o vuelve a buscar')
                row['status'] = 'mapped'
            else:
                raise ValueError('Acción no válida')
            if action == 'restore':
                data['decisions'].pop(row['key'],None)
            else:
                data['decisions'][row['key']] = {'action':action,'at':datetime.now(timezone.utc).isoformat(timespec='seconds')}
            try:
                atomic_json(self.path(sid),data)
            except OSError:
                if action == 'accept':
                    raise ValueError('El término se guardó en el vocabulario personal, pero no se pudo actualizar esta revisión; vuelve a buscar cuando el almacenamiento esté disponible') from None
                raise
        self.notify({'type':'review','session_id':sid,'state':'updated'})
        return {'ok':True}
