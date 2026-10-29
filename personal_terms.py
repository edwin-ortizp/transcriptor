"""User-owned accepted vocabulary, separate from replacement mappings."""
import codecs
import hashlib
import os
import tempfile
import threading
import unicodedata

from mapping_store import MappingConflict, MappingStore


def normalized(term):
    return unicodedata.normalize('NFC', term.strip()).casefold()


def term_id(term):
    return hashlib.sha256(normalized(term).encode('utf-8')).hexdigest()[:24]


def validate_term(term):
    if not term.strip() or term.lstrip().startswith('#'):
        raise ValueError('Introduce un término; # está reservado para comentarios')
    if any(c in term for c in '\t\r\n\v\f\x85\u2028\u2029'):
        raise ValueError('El término no admite tabulaciones ni saltos de línea')
    return unicodedata.normalize('NFC', term.strip())


class PersonalTerms:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()

    def read(self):
        path = self.path()
        raw = path.read_bytes() if path.exists() else None
        text = raw.decode('utf-8-sig') if raw is not None else ''
        entries = {}
        for line in text.splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            term = validate_term(line)
            entries.setdefault(normalized(term), {'id':term_id(term), 'term':term})
        return {'path':str(path), 'revision':hashlib.sha256(raw).hexdigest() if raw is not None else 'missing',
                'entries':sorted(entries.values(), key=lambda e:normalized(e['term']))}

    def add(self, term, revision, allow_existing=False):
        term = validate_term(term)
        with self.lock:
            data = self.read()
            if data['revision'] != revision:
                raise MappingConflict('Los términos personales cambiaron. Recarga antes de guardar; tu texto se conserva.')
            if any(normalized(e['term']) == normalized(term) for e in data['entries']):
                if allow_existing:
                    return data
                raise ValueError('El término ya está en tu diccionario personal')
            path = self.path()
            backup = None
            if data['revision'] == 'missing':
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent,prefix='terms-',suffix='.tmp',delete=False) as file:
                        temporary = type(path)(file.name)
                        file.write(('# Términos personales del Transcriptor\n' + term + '\n').encode('utf-8'))
                        file.flush()
                        os.fsync(file.fileno())
                    os.link(temporary,path)  # Atomic creation; never replace an external file.
                except FileExistsError:
                    raise MappingConflict('Otro proceso creó el vocabulario personal. Recarga antes de guardar.') from None
                finally:
                    if temporary and temporary.exists():
                        temporary.unlink()
            else:
                raw = path.read_bytes()
                newline = b'\r\n' if b'\r\n' in raw else b'\r' if b'\r' in raw and b'\n' not in raw else b'\n'
                content = raw + (newline if raw and not raw.endswith((b'\n',b'\r')) else b'') + term.encode('utf-8') + newline
                backup = MappingStore._replace(path, revision, content)
            return {**self.read(), 'backup':str(backup) if backup else None}

    def delete(self, entry_id, revision):
        with self.lock:
            data = self.read()
            if data['revision'] != revision:
                raise MappingConflict('El vocabulario cambió. Recarga antes de eliminar.')
            if not any(e['id'] == entry_id for e in data['entries']):
                raise KeyError('Término no encontrado')
            path = self.path()
            raw = path.read_bytes()
            lines = raw.decode('utf-8-sig').splitlines(keepends=True)
            content = ''.join(line for line in lines if not line.strip() or line.startswith('#') or term_id(line.rstrip('\r\n')) != entry_id)
            encoded = (codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b'') + content.encode('utf-8')
            backup = MappingStore._replace(path, revision, encoded)
            return {**self.read(), 'backup':str(backup)}
