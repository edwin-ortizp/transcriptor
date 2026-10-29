"""Offline, read-only browsing of vendored Hunspell headwords and own terms."""
import json
import re
import threading
from pathlib import Path


class Vocabulary:
    def __init__(self, root):
        self.root = Path(root)
        self._entries = None
        self.lock = threading.RLock()

    def _load(self):
        with self.lock:
            self._populate()

    def _populate(self):
        if self._entries is not None:
            return
        sources = json.loads((self.root/'sources.json').read_text(encoding='utf-8'))
        entries = []
        for language in ('es','en'):
            aff = (self.root/language/'index.aff').read_bytes()
            match = re.search(rb'(?m)^SET\s+(\S+)', aff)
            encoding = match.group(1).decode('ascii') if match else 'UTF-8'
            lines = (self.root/language/'index.dic').read_text(encoding=encoding).splitlines()
            words = set()
            for line in lines[1:]:
                # Hunspell stores lexical entries plus slash flags and optional morphology.
                headword = re.split(r'(?<!\\)/|\t| ', line, maxsplit=1)[0].replace('\\/','/')
                if headword:
                    words.add(headword)
            entries.extend({'term':word, 'language':language, 'source':language, 'note':''} for word in sorted(words,key=str.casefold))
        for line in (self.root/'terms.tsv').read_text(encoding='utf-8-sig').splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            columns = line.split('\t')
            if len(columns) not in (2,3) or columns[1] not in ('es','en','both') or not columns[0].strip():
                raise ValueError('Entrada inválida en el glosario propio del repositorio')
            entries.append({'term':columns[0], 'language':columns[1], 'source':'own', 'note':columns[2] if len(columns)>2 else ''})

        self._entries, self.sources = entries, sources

    def search(self, source='es', query='', page=1, page_size=50):
        self._load()
        if source not in ('es','en','own'):
            raise ValueError('Vocabulario no válido')
        query = query.casefold().strip()
        rows = [entry for entry in self._entries if entry['source'] == source]
        count = len(rows)
        matches = [entry for entry in rows if query in entry['term'].casefold()]
        start = (page-1)*page_size
        meta = self.sources['languages'].get(source)
        return {'entries':matches[start:start+page_size], 'total':len(matches), 'entries_count':count,
                'page':page, 'page_size':page_size, 'source':source,
                'provenance':{'repository':self.sources['repository'], 'commit':self.sources['commit'], 'version':meta['version']} if meta else {'repository':'Glosario propio · dictionaries/terms.tsv'}}
