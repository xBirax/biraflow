"""Local data: the personal dictionary (replacement rules + snippets), dictation
history and usage statistics. Plain JSON files in %APPDATA%\\BiraFlow, nothing
leaves the computer."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .i18n import t as _t
from .textproc import Replacement, Snippet

TYPING_WPM = 40  # average typing speed used for "time saved"


_file_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def file_lock(path: Path) -> threading.RLock:
    """One lock per data file, shared by every thread (jobs, controller, settings window)."""
    with _locks_guard:
        return _file_locks.setdefault(str(path).lower(), threading.RLock())


def _write_json(path: Path, data) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    with file_lock(path):
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            for attempt in range(6):   # Windows refuses to replace a file someone is reading
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.02)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _read_json(path: Path, default):
    with file_lock(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default


# --- dictionary ---------------------------------------------------------------------

@dataclass
class Dictionary:
    replacements: list[Replacement]
    snippets: list[Snippet]

    @classmethod
    def load(cls, path: Path) -> "Dictionary":
        data = _read_json(path, {})
        if not isinstance(data, dict):   # a hand-edited file that is a list: no rules, not a crash
            data = {}
        reps = [Replacement(str(r.get("heard", "")), str(r.get("write", "")))
                for r in data.get("replacements", []) if isinstance(r, dict)]
        snips = [Snippet(str(s.get("trigger", "")), str(s.get("text", "")))
                 for s in data.get("snippets", []) if isinstance(s, dict)]
        return cls([r for r in reps if r.heard.strip()], [s for s in snips if s.trigger.strip()])

    def save(self, path: Path) -> None:
        _write_json(path, {"replacements": [asdict(r) for r in self.replacements],
                           "snippets": [asdict(s) for s in self.snippets]})

    @classmethod
    def learn(cls, path: Path, rules: list[Replacement]) -> list[Replacement]:
        """Add rules to the file now, under the lock every writer of it takes (the controller
        learns from edits while the settings window is open). Returns the ones that changed."""
        with file_lock(path):
            d = cls.load(path)
            changed = d.add_replacements(rules)
            if changed:
                d.save(path)
            return changed

    def add_replacements(self, rules: list[Replacement]) -> list[Replacement]:
        """Add or update rules (keyed by what is heard). Returns the ones that changed."""
        index = {r.heard.lower(): i for i, r in enumerate(self.replacements)}
        changed = []
        for rule in rules:
            i = index.get(rule.heard.lower())
            if i is None:
                index[rule.heard.lower()] = len(self.replacements)
                self.replacements.append(rule)
                changed.append(rule)
            elif self.replacements[i].write != rule.write:
                self.replacements[i] = rule
                changed.append(rule)
        return changed


# --- history ----------------------------------------------------------------------------


class History:
    """Recent dictations, newest first. keep: "always" (up to max_items), "24h" or "never"."""

    def __init__(self, path: Path, keep: str = "always", max_items: int = 500):
        self.path = path
        self.keep = keep
        self.max_items = max_items
        self._lock = threading.Lock()

    def entries(self) -> list[dict]:
        with self._lock:
            return self._load()

    def _load(self) -> list[dict]:
        items = _read_json(self.path, [])
        if not isinstance(items, list):
            return []
        if self.keep == "never":
            return []
        if self.keep == "24h":
            cutoff = time.time() - 86400
            items = [e for e in items if e.get("ts", 0) >= cutoff]
        return items[: self.max_items]

    def add(self, *, raw: str, text: str, app: str, mode: str, seconds: float) -> dict | None:
        if self.keep == "never":
            if self.path.exists():
                self.clear()
            return None
        entry = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "app": app, "mode": mode,
                 "raw": raw, "text": text, "seconds": round(seconds, 2), "words": count_words(text)}
        with self._lock:
            _write_json(self.path, [entry] + self._load())
        return entry

    def update(self, entry_id: str, **fields) -> None:
        with self._lock:
            items = self._load()
            for e in items:
                if e.get("id") == entry_id:
                    e.update(fields)
            _write_json(self.path, items)

    def delete(self, entry_id: str) -> None:
        with self._lock:
            _write_json(self.path, [e for e in self._load() if e.get("id") != entry_id])

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)

    def prune(self) -> None:
        """Apply the retention policy to what is on disk."""
        with self._lock:
            if self.keep == "never":
                self.path.unlink(missing_ok=True)
            elif self.path.exists():
                _write_json(self.path, self._load())


def count_words(text: str) -> int:
    return len([w for w in text.split() if any(ch.isalnum() for ch in w)])


# --- statistics (counters only, no text) ---------------------------------------------------

class Stats:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def add(self, words: int, seconds: float) -> None:
        with self._lock:
            d = _read_json(self.path, {})
            d["dictations"] = d.get("dictations", 0) + 1
            d["words"] = d.get("words", 0) + words
            d["seconds"] = round(d.get("seconds", 0.0) + seconds, 2)
            d.setdefault("since", time.time())
            today = time.strftime("%Y-%m-%d")
            if d.get("day") != today:
                d["day"], d["day_dictations"], d["day_words"] = today, 0, 0
            d["day_dictations"] += 1
            d["day_words"] += words
            _write_json(self.path, d)

    def today(self) -> tuple[int, int]:
        """(dictations, words) since midnight."""
        d = _read_json(self.path, {})
        if d.get("day") != time.strftime("%Y-%m-%d"):
            return 0, 0
        return d.get("day_dictations", 0), d.get("day_words", 0)

    def read(self) -> dict:
        d = _read_json(self.path, {})
        words, seconds = d.get("words", 0), d.get("seconds", 0.0)
        wpm = words / (seconds / 60) if seconds > 0 else 0.0
        saved_min = max(0.0, words / TYPING_WPM - seconds / 60)
        return {"dictations": d.get("dictations", 0), "words": words, "seconds": seconds,
                "wpm": wpm, "saved_minutes": saved_min, "since": d.get("since")}

    def summary(self) -> str:
        s = self.read()
        if not s["dictations"]:
            return _t("Nenhum ditado ainda.")
        saved = s["saved_minutes"]
        saved_txt = f"{saved:.0f} min" if saved < 120 else f"{saved / 60:.1f} h"
        return _t("{0} ditados · {1} palavras · {2} palavras/min falando · "
                  "~{3} economizados em relação a digitar ({4} palavras/min)").format(
            s["dictations"], s["words"], f"{s['wpm']:.0f}", saved_txt, TYPING_WPM)

    def reset(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
