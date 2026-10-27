"""Diccionario de correcciones (TSV: from, to, word|any, case|nocase)."""
import os
import re
from pathlib import Path

DEFAULT = Path.home() / "Sync" / "android-apps-backups" / "dictate-keyboard" / "dictate_mappings-tcl.tsv"


class Corrections:
    def __init__(self, paths=None):
        raw = paths or os.environ.get("TRANSCRIPTOR_DICT") or str(DEFAULT)
        self.paths = [Path(p.strip()) for p in raw.split(";") if p.strip()]
        self._stamp = None
        self.rules = []
        self._patterns = []
        self.reload_if_changed()

    @property
    def missing(self):
        return [str(p) for p in self.paths if not p.is_file()]

    def _current_stamp(self):
        return tuple(p.stat().st_mtime if p.is_file() else None for p in self.paths)

    def reload_if_changed(self):
        stamp = self._current_stamp()
        if stamp == self._stamp:
            return
        self._stamp = stamp
        rules = []
        for p in self.paths:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) < 2 or not cols[0]:
                    continue
                mode = cols[2].strip() if len(cols) > 2 and cols[2].strip() else "word"
                case = cols[3].strip() if len(cols) > 3 and cols[3].strip() else "nocase"
                rules.append((cols[0], cols[1], mode, case))
        self.rules = rules
        self._compile()

    def _compile(self):
        self._patterns = []
        for mode in ("word", "any"):
            for case in ("case", "nocase"):
                group = [r for r in self.rules if r[2] == mode and r[3] == case]
                if not group:
                    continue
                group.sort(key=lambda r: len(r[0]), reverse=True)
                body = "|".join(re.escape(r[0]) for r in group)
                if mode == "word":
                    body = rf"(?<!\w)(?:{body})(?!\w)"
                flags = 0 if case == "case" else re.IGNORECASE
                key = (lambda s: s) if case == "case" else str.lower
                targets = {}
                for frm, to, *_ in group:
                    targets.setdefault(key(frm), to)
                self._patterns.append((re.compile(body, flags), targets, key))

    def apply(self, text: str) -> str:
        for pattern, targets, key in self._patterns:
            text = pattern.sub(lambda m: targets.get(key(m.group(0)), m.group(0)), text)
        return text

    def hotwords(self, limit=200) -> str:
        out, seen = [], set()
        for _, to, *_ in self.rules:
            if to and to[0].isupper() and to.lower() not in seen:
                seen.add(to.lower())
                out.append(to)
        return " ".join(out)[:limit].rsplit(" ", 1)[0] if out else ""
