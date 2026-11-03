"""Analysis formats: base formats shipped with the app plus the user's own and edited ones."""
import json
import threading
import uuid
from pathlib import Path

from recording import atomic_json

MAX_NAME, MAX_SECTIONS = 60, 2000


def clean(name, sections):
    name = ' '.join(str(name or '').split())
    sections = str(sections or '').strip()
    if not 1 <= len(name) <= MAX_NAME:
        raise ValueError(f'El nombre debe tener entre 1 y {MAX_NAME} caracteres')
    if not 1 <= len(sections) <= MAX_SECTIONS:
        raise ValueError(f'Describe qué quieres obtener (máximo {MAX_SECTIONS} caracteres)')
    return name, sections


class AnalysisTemplates:
    def __init__(self, base_path, user_path=None):
        """Without user_path the formats are read-only (base formats only)."""
        self.base_path = Path(base_path)
        self.user_path = Path(user_path) if user_path else None
        self.lock = threading.RLock()

    def _base(self):
        return json.loads(self.base_path.read_text(encoding='utf-8'))['templates']

    def _user(self):
        try:
            data = json.loads(self.user_path.read_text(encoding='utf-8')) if self.user_path else {}
        except (OSError, ValueError):
            data = {}
        return {'version':1, 'templates':[], 'overrides':{}, **data}

    def _save(self, data):
        if self.user_path is None:
            raise OSError('Los formatos son de solo lectura')
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.user_path, data)

    def all(self):
        """Ordered {id: {name, sections, base, modified}}."""
        with self.lock:
            user = self._user()
            result = {}
            for item in self._base():
                override = user['overrides'].get(item['id'])
                result[item['id']] = {'name':item['name'], 'sections':(override or item)['sections'],
                                      'base':True, 'modified':bool(override), 'default':item['sections']}
            for item in user['templates']:
                result[item['id']] = {'name':item['name'], 'sections':item['sections'], 'base':False, 'modified':False}
            return result

    def __contains__(self, template_id):
        return template_id in self.all()

    def __getitem__(self, template_id):
        return self.all()[template_id]

    def get(self, template_id, fallback='meeting'):
        items = self.all()
        return items.get(template_id) or items[fallback]

    def create(self, name, sections):
        name, sections = clean(name, sections)
        with self.lock:
            user = self._user()
            item = {'id':'u_' + uuid.uuid4().hex[:10], 'name':name, 'sections':sections}
            user['templates'].append(item)
            self._save(user)
            return item['id']

    def update(self, template_id, name, sections):
        with self.lock:
            user, base = self._user(), {b['id']:b for b in self._base()}
            if template_id in base:
                _, sections = clean(base[template_id]['name'], sections)
                user['overrides'][template_id] = {'sections':sections}
            else:
                item = next((t for t in user['templates'] if t['id'] == template_id), None)
                if item is None:
                    raise KeyError('Formato no encontrado')
                item['name'], item['sections'] = clean(name, sections)
            self._save(user)

    def delete(self, template_id):
        """User formats are removed; base formats go back to their original text."""
        with self.lock:
            user, base = self._user(), {b['id'] for b in self._base()}
            if template_id in base:
                user['overrides'].pop(template_id, None)
            else:
                before = len(user['templates'])
                user['templates'] = [t for t in user['templates'] if t['id'] != template_id]
                if len(user['templates']) == before:
                    raise KeyError('Formato no encontrado')
            self._save(user)
