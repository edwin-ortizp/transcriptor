"""TSV document editing with revision checks and byte-preserving unrelated lines."""
import codecs
import hashlib
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


class MappingConflict(ValueError):
    pass


def parse_rule(line):
    text = line.rstrip('\r\n')
    if not text.strip() or text.startswith('#'):
        return None, None
    columns = text.split('\t')
    if not 2 <= len(columns) <= 4:
        return None, 'Se esperan entre 2 y 4 columnas TSV'
    source, target = columns[:2]
    mode = columns[2].strip() if len(columns) > 2 and columns[2].strip() else 'word'
    case = columns[3].strip() if len(columns) > 3 and columns[3].strip() else 'nocase'
    try:
        validate_rule(source, target, mode, case)
    except ValueError as exc:
        return None, str(exc)
    return (source, target, mode, case), None


def validate_rule(source, target, mode, case):
    if not source.strip():
        raise ValueError('Se reconoce como no puede estar vacío')
    if source.startswith('#'):
        raise ValueError('El origen no puede comenzar por #; está reservado para comentarios')
    if any(c in source + target for c in '\t\r\n\v\f\x85\u2028\u2029'):
        raise ValueError('Los campos no admiten tabulaciones ni saltos de línea')
    if mode not in ('word','any') or case not in ('case','nocase'):
        raise ValueError('Coincidencia o tratamiento de mayúsculas inválido')


def rule_key(rule):
    source, _, mode, case = rule
    return (source.lower() if case == 'nocase' else source, mode, case)


def read_document(path):
    raw = path.read_bytes()
    text = raw.decode('utf-8-sig')
    lines = text.splitlines(keepends=True)
    rules, warnings = [], []
    for number, line in enumerate(lines, 1):
        rule, warning = parse_rule(line)
        if rule:
            rules.append({'id':str(number), 'line':number, 'source':rule[0], 'target':rule[1],
                          'mode':rule[2], 'case':rule[3]})
        if warning:
            warnings.append({'line':number, 'message':warning})
    return {'raw':raw, 'lines':lines, 'bom':raw.startswith(codecs.BOM_UTF8),
            'revision':hashlib.sha256(raw).hexdigest(), 'rules':rules, 'warnings':warnings}


class MappingStore:
    def __init__(self, paths, protected_roots=()):
        self.paths = paths
        self.protected_roots = [Path(p).resolve() for p in protected_roots]
        self.lock = threading.RLock()

    @staticmethod
    def file_id(path):
        return hashlib.sha256(str(Path(path).resolve()).casefold().encode()).hexdigest()[:24]

    def configured(self):
        unique = {}
        for path in self.paths():
            unique[self.file_id(path)] = Path(path).resolve()
        return unique

    def resolve(self, file_id):
        path = self.configured().get(file_id)
        if path is None:
            raise KeyError('Archivo no configurado; recarga los mappings')
        return path

    def editable(self, path):
        return not any(path.is_relative_to(root) for root in self.protected_roots)

    def snapshot(self):
        files, rules, warnings = [], [], []
        for file_id, path in self.configured().items():
            info = {'id':file_id, 'path':str(path), 'name':path.name, 'exists':path.is_file(),
                    'revision':None, 'rules_count':0, 'editable':self.editable(path)}
            if path.is_file():
                try:
                    doc = read_document(path)
                    info.update(revision=doc['revision'], rules_count=len(doc['rules']))
                    rules.extend({**rule, 'file_id':file_id, 'file_name':path.name} for rule in doc['rules'])
                    warnings.extend({**item, 'file_id':file_id, 'file_name':path.name} for item in doc['warnings'])
                except (OSError, UnicodeError):
                    info['error'] = 'No se pudo leer el archivo como UTF-8; no se modificará'
            files.append(info)
        return {'files':files, 'rules':rules, 'warnings':warnings}

    def mutate(self, file_id, revision, operation, row_id=None, rule=None):
        with self.lock:
            path = self.resolve(file_id)
            if not self.editable(path):
                raise ValueError('Los vocabularios del sistema son de solo lectura')
            doc = read_document(path)
            if doc['revision'] != revision:
                raise MappingConflict('El archivo cambió desde su lectura. Recarga antes de guardar; tu borrador se conserva.')
            if operation not in ('add','edit','delete'):
                raise ValueError('Operación no válida')
            if operation != 'add' and not any(r['id'] == row_id for r in doc['rules']):
                raise KeyError('Regla no encontrada; recarga el archivo')
            if operation != 'delete':
                validate_rule(*rule)
                original = next((r for r in doc['rules'] if r['id'] == row_id), None) if operation == 'edit' else None
                unchanged_key = original and rule_key((original['source'],original['target'],original['mode'],original['case'])) == rule_key(rule)
                for candidate in self.snapshot()['rules']:
                    if candidate['file_id'] == file_id and candidate['id'] == row_id and operation == 'edit':
                        continue
                    other = (candidate['source'],candidate['target'],candidate['mode'],candidate['case'])
                    if not unchanged_key and rule_key(other) == rule_key(rule):
                        raise ValueError('Ya existe una regla con el mismo origen, coincidencia y tratamiento de mayúsculas')
            lines = doc['lines'][:]
            newline = next(('\r\n' if l.endswith('\r\n') else '\n' if l.endswith('\n') else '\r' for l in lines if l.endswith(('\n','\r'))), '\n')
            if operation == 'add':
                if lines and not lines[-1].endswith(('\r','\n')):
                    lines[-1] += newline
                lines.append('\t'.join(rule) + newline)
            else:
                index = int(row_id) - 1
                if operation == 'delete':
                    del lines[index]
                else:
                    original = lines[index]
                    ending = '\r\n' if original.endswith('\r\n') else '\n' if original.endswith('\n') else '\r' if original.endswith('\r') else ''
                    lines[index] = '\t'.join(rule) + ending
            content = (codecs.BOM_UTF8 if doc['bom'] else b'') + ''.join(lines).encode('utf-8')
            backup = self._replace(path, doc['revision'], content)
            return {'backup':str(backup), **self.snapshot()}

    @staticmethod
    def _replace(path, revision, content):
        backup = path.with_name(path.name + '.bak-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            if hashlib.sha256(path.read_bytes()).hexdigest() != revision:
                raise MappingConflict('El archivo cambió antes de guardar. Recarga; tu borrador se conserva.')
            shutil.copy2(path, backup)
            if hashlib.sha256(path.read_bytes()).hexdigest() != revision:
                raise MappingConflict('Otro programa modificó el archivo. Recarga antes de guardar.')
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        return backup
