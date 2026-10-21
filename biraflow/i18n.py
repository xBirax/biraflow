"""Translations of the interface.

The source strings live in the code, in Portuguese (pt-BR). The catalogs in
`biraflow/lang/<code>.json` map each source string to its translation in that language:

    {"code": "en", "strings": {"Geral": "General", ...}, "prompts": {...}, "spoken": {...}}

A string with no translation shows the source text, so an incomplete catalog never
breaks the app. `t()` is the lookup used everywhere in the interface:

    label = t("Ditado ativado")

The language is a setting (`language` in settings.toml) and can be changed at runtime.
"""
from __future__ import annotations

import ctypes
import json
import locale
import re
from pathlib import Path

LANG_DIR = Path(__file__).with_name("lang")

# code -> (name in the language itself, name in English)
LANGUAGES: dict[str, tuple[str, str]] = {
    "pt-BR": ("Português (Brasil)", "Portuguese (Brazil)"),
    "en": ("English", "English"),
    "es": ("Español", "Spanish"),
    "fr": ("Français", "French"),
    "de": ("Deutsch", "German"),
    "it": ("Italiano", "Italian"),
    "zh-Hans": ("简体中文", "Chinese (Simplified)"),
    "ja": ("日本語", "Japanese"),
    "ko": ("한국어", "Korean"),
}
DEFAULT_LANG = "pt-BR"

# Windows UI language (LCID) -> our code
_LCID = {
    0x0416: "pt-BR", 0x0816: "pt-BR", 0x0409: "en", 0x0c09: "en", 0x0809: "en",
    0x0c0a: "es", 0x040c: "fr", 0x080c: "fr", 0x0407: "de", 0x0410: "it",
    0x0804: "zh-Hans", 0x0411: "ja", 0x0412: "ko",
}

_current = {"code": DEFAULT_LANG, "strings": {}, "prompts": {}, "spoken": {}}


def available() -> list[str]:
    """Codes with a catalog file, in the order of LANGUAGES."""
    codes = [c for c in LANGUAGES if (LANG_DIR / f"{c}.json").exists()]
    return codes or [DEFAULT_LANG]


def language() -> str:
    return _current["code"]


def _read(code: str) -> dict:
    path = LANG_DIR / f"{code}.json"
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_language(code: str) -> str:
    """Load a catalog and make it the current one. Unknown code: keep the old one."""
    global _current
    code = resolve(code)
    data = _read(code)
    _current = {
        "code": code,
        "strings": data.get("strings") or {},
        "prompts": data.get("prompts") or {},
        "spoken": data.get("spoken") or {},
    }
    return code


def resolve(code: str) -> str:
    """The real language of a setting: "auto" is the language of Windows."""
    code = (code or "").strip()
    if not code or code == "auto":
        return detect_system_language()
    return code if code in LANGUAGES else DEFAULT_LANG


def detect_system_language() -> str:
    """The language of Windows, mapped to our codes (pt-BR when unknown)."""
    try:
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        if lcid in _LCID:
            return _LCID[lcid]
    except Exception:
        pass
    try:
        name = (locale.getdefaultlocale()[0] or "").lower()
    except Exception:
        name = ""
    for code in LANGUAGES:
        if name.startswith(code.lower().split("-")[0]):
            return code
    return DEFAULT_LANG


def t(text: str) -> str:
    """Translate a source string; the source itself when there is no translation."""
    if not text:
        return text
    return _current["strings"].get(text, text)


def prompt(key: str, default: str = "") -> str:
    """A prompt of the LLM in the current language (falls back to the built-in one)."""
    return _current["prompts"].get(key, default)


def spoken(key: str, default: str = "") -> str:
    """A word or phrase the user speaks, in the current language."""
    value = _current["spoken"].get(key, default)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value)


def spoken_list(key: str, default: list[str] | None = None) -> list[str]:
    """The words the user speaks for a command, in the current language."""
    value = _current["spoken"].get(key)
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return list(default) if default else []


def placeholders(text: str) -> list[str]:
    """The {0}, {1}... of a template, in order (they must survive a translation)."""
    return re.findall(r"\{\d+\}", text or "")


def catalog() -> dict:
    """The current catalog (for the tests and for the translation script)."""
    return dict(_current)


def source_strings() -> list[str]:
    """Every source string of the interface, collected from the catalogs and the code.

    The translation script uses it to know what still needs a translation.
    """
    seen: dict[str, None] = {}
    for code in available():
        data = _read(code)
        for key in (data.get("strings") or {}):
            seen.setdefault(key, None)
    return list(seen)


set_language(DEFAULT_LANG)
