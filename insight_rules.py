"""Editable insight rules: app base rules plus user overrides, exclusions and usage stats."""
import json
import re
import threading
import unicodedata
import uuid
from pathlib import Path

from recording import atomic_json

TYPES = ('decision', 'commitment', 'risk', 'question')
PRIORITY = {'decision':0, 'commitment':0, 'risk':1, 'question':2}
FLAG_MIN_HITS, FLAG_RATIO = 5, .6
EMPTY_USER = {'version':1, 'rules':[], 'disabled':[], 'weights':{}, 'exclusions':[], 'removed_exclusions':[],
              'stats':{}, 'suggestions':[]}


def normalize(text):
    """Lowercase, accent-free words; keeps '?' so question rules still work."""
    text = unicodedata.normalize('NFKD', str(text).lower())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[^a-z0-9?$%.,]+', ' ', text).replace('?', ' ? ').split())


def clean_phrase(phrase):
    if not isinstance(phrase, str):
        raise ValueError('Introduce una frase')
    value = normalize(phrase).strip(' .,')
    if not value or len(value) > 120:
        raise ValueError('La frase debe tener entre 1 y 120 caracteres útiles')
    return value


class InsightRules:
    def __init__(self, base_path, user_path=None):
        """Without user_path the rules are read-only (base rules, no stats)."""
        self.base_path = Path(base_path)
        self.user_path = Path(user_path) if user_path else None
        self.lock = threading.RLock()
        self._cache, self._stamp = None, None

    # --- storage ---
    def _mtime(self, path):
        if path is None:
            return None
        try: return path.stat().st_mtime_ns
        except OSError: return None

    def _base(self):
        return json.loads(self.base_path.read_text(encoding='utf-8'))

    def _user(self):
        try:
            data = json.loads(self.user_path.read_text(encoding='utf-8')) if self.user_path else {}
        except (OSError, ValueError):
            data = {}
        return {**json.loads(json.dumps(EMPTY_USER)), **data}

    def _save(self, data):
        if self.user_path is None:
            return
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.user_path, data)
        self._stamp = None

    def compiled(self):
        """Merged, compiled rules; reloaded when either file changes on disk."""
        stamp = (self._mtime(self.base_path), self._mtime(self.user_path))
        with self.lock:
            if self._cache is not None and stamp == self._stamp:
                return self._cache
            base, user = self._base(), self._user()
            rules = []
            for rule in [{**r, 'origin':'base'} for r in base['rules']] + [{**r, 'origin':r.get('origin', 'user')} for r in user['rules']]:
                rule = {**rule, 'weight':user['weights'].get(rule['id'], rule['weight']),
                        'enabled':rule['id'] not in user['disabled']}
                if 'pattern' in rule:
                    rule['regex'] = re.compile(rule['pattern'])
                else:
                    rule['regex'] = re.compile(r'(?<![a-z0-9])' + re.escape(rule['phrase']) + r'(?![a-z0-9])')
                rules.append(rule)
            exclusions = [e for e in base.get('exclusions', []) if e not in user['removed_exclusions']] + user['exclusions']
            bonus = base.get('bonus')
            self._cache = {'rules':rules, 'exclusions':[normalize(e) for e in exclusions],
                           'bonus':{**bonus, 'regex':re.compile(bonus['pattern'])} if bonus else None, 'user':user}
            self._stamp = stamp
            return self._cache

    # --- detection ---
    def detect(self, text):
        """Return {'type','score','rule_ids','phrases'} for the strongest signal, or None."""
        data = self.compiled()
        norm = ' ' + normalize(text) + ' '
        for phrase in data['exclusions']:
            norm = re.sub(r'(?<![a-z0-9])' + re.escape(phrase) + r'(?![a-z0-9])', ' ', norm)
        matches = []
        for rule in data['rules']:
            if not rule['enabled']:
                continue
            found = rule['regex'].search(norm)
            if found:
                matches.append((rule, found.group(0).strip()))
        if not matches:
            return None
        by_type = {}
        for rule, _ in matches:
            by_type[rule['type']] = max(by_type.get(rule['type'], 0), rule['weight'])
        kind = min(by_type, key=lambda k:(-by_type[k], PRIORITY[k]))
        score = sum(by_type.values())
        if data['bonus'] and data['bonus']['regex'].search(norm):
            score += data['bonus']['weight']
        if score <= 0:
            return None
        return {'type':kind, 'score':score, 'rule_ids':[r['id'] for r, _ in matches],
                'phrases':[p for _, p in matches]}

    # --- listing ---
    def listing(self):
        data = self.compiled()
        stats = data['user']['stats']
        rows = []
        for rule in data['rules']:
            s = {'hits':0, 'pinned':0, 'dismissed':0, **stats.get(rule['id'], {})}
            rows.append({k:rule.get(k) for k in ('id', 'type', 'phrase', 'pattern', 'weight', 'enabled', 'origin')}
                        | {'stats':s, 'flagged':s['hits'] >= FLAG_MIN_HITS and s['dismissed'] / s['hits'] > FLAG_RATIO})
        return {'rules':rows, 'exclusions':data['exclusions'], 'suggestions':data['user']['suggestions'],
                'user_path':str(self.user_path) if self.user_path else None}

    # --- editing ---
    def _edit(self, change):
        with self.lock:
            user = self._user()
            result = change(user)
            self._save(user)
            return result

    def add_rule(self, kind, phrase, weight=2, origin='user'):
        if kind not in TYPES:
            raise ValueError('Tipo de insight no válido')
        if type(weight) is not int or not 1 <= weight <= 5:
            raise ValueError('El peso debe estar entre 1 y 5')
        phrase = clean_phrase(phrase)
        if any(r.get('phrase') == phrase and r['type'] == kind for r in self.compiled()['rules']):
            raise ValueError('Esa frase ya existe para ese tipo')
        rule = {'id':'user-' + uuid.uuid4().hex[:12], 'type':kind, 'phrase':phrase, 'weight':weight, 'origin':origin}
        def change(user):
            user['rules'].append(rule)
            user['disabled'] = [i for i in user['disabled'] if i != rule['id']]
            return rule
        return self._edit(change)

    def update_rule(self, rule_id, *, enabled=None, weight=None):
        if not any(r['id'] == rule_id for r in self.compiled()['rules']):
            raise KeyError(rule_id)
        if weight is not None and (type(weight) is not int or not 0 <= weight <= 5):
            raise ValueError('El peso debe estar entre 0 y 5')
        def change(user):
            if enabled is not None:
                user['disabled'] = [i for i in user['disabled'] if i != rule_id] + ([] if enabled else [rule_id])
            if weight is not None:
                user['weights'][rule_id] = weight
        self._edit(change)

    def delete_rule(self, rule_id):
        """User rules are removed; base rules can only be disabled."""
        rule = next((r for r in self.compiled()['rules'] if r['id'] == rule_id), None)
        if rule is None:
            raise KeyError(rule_id)
        if rule['origin'] == 'base':
            return self.update_rule(rule_id, enabled=False)
        def change(user):
            user['rules'] = [r for r in user['rules'] if r['id'] != rule_id]
            user['stats'].pop(rule_id, None)
            user['weights'].pop(rule_id, None)
        self._edit(change)

    def add_exclusion(self, phrase):
        phrase = clean_phrase(phrase)
        def change(user):
            user['removed_exclusions'] = [e for e in user['removed_exclusions'] if normalize(e) != phrase]
            if phrase not in self.compiled()['exclusions']:
                user['exclusions'].append(phrase)
            return phrase
        return self._edit(change)

    def remove_exclusion(self, phrase):
        phrase = normalize(phrase)
        base = [normalize(e) for e in self._base().get('exclusions', [])]
        def change(user):
            user['exclusions'] = [e for e in user['exclusions'] if normalize(e) != phrase]
            if phrase in base and phrase not in user['removed_exclusions']:
                user['removed_exclusions'].append(phrase)
        self._edit(change)

    # --- learning ---
    def record(self, rule_ids, field):
        if field not in ('hits', 'pinned', 'dismissed') or not rule_ids:
            return
        def change(user):
            for rule_id in rule_ids:
                entry = user['stats'].setdefault(rule_id, {'hits':0, 'pinned':0, 'dismissed':0})
                entry[field] = entry.get(field, 0) + 1
        self._edit(change)

    def suggest(self, kind, text, ref=None):
        """AI found something local rules missed: keep it for the user to turn into a rule."""
        if kind not in TYPES or not text:
            return None
        item = {'id':uuid.uuid4().hex[:12], 'type':kind, 'text':text[:300], 'ref':ref}
        def change(user):
            if any(s['text'] == item['text'] for s in user['suggestions']):
                return None
            user['suggestions'] = (user['suggestions'] + [item])[-50:]
            return item
        return self._edit(change)

    def drop_suggestion(self, suggestion_id):
        def change(user):
            user['suggestions'] = [s for s in user['suggestions'] if s['id'] != suggestion_id]
        self._edit(change)

    # --- replay ---
    def replay(self, sessions):
        """Count what the current rules would detect in saved sessions (run before and after a change)."""
        rows, total = [], 0
        for session in sessions:
            hits = [{'time':b.get('time'), 'text':b['text'][:200], **found}
                    for b in session.get('history', []) if b.get('type') == 'text' and b.get('text')
                    for found in [self.detect(b['text'])] if found and found['score'] >= 2]
            total += len(hits)
            rows.append({'session_id':session.get('session_id'), 'title':session.get('title'),
                         'count':len(hits), 'examples':hits[:5]})
        return {'total':total, 'sessions':rows}
