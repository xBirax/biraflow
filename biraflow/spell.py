"""Windows' own spell checker (ISpellChecker, Windows 8+), used to tell a typo fix from
a content edit: "ese" -> "esse" fixes a misspelling, "casa" -> "caso" swaps one real
word for another and must not become a global rule.

Plain ctypes over the COM vtables (no type library ships for these interfaces). One
checker per thread; every failure means "don't know" (None)."""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
from ctypes import wintypes

log = logging.getLogger(__name__)

_CLSID_FACTORY = "{7AB36653-1796-484B-BDFA-E74F1DB7C1DC}"
_IID_FACTORY = "{8E018A9D-2415-4677-BF08-794EA61F94BB}"
_requests: "queue.Queue" = None  # (text, language, done event, result list) for the checker thread
_start_lock = threading.Lock()


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


def _method(obj: ctypes.c_void_p, index: int, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(obj: ctypes.c_void_p) -> None:
    if obj and obj.value:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
            ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2])(obj)


def system_language() -> str:
    buf = ctypes.create_unicode_buffer(85)
    if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
        return buf.value
    return "pt-BR"


class SpellChecker:
    def __init__(self, language: str):
        self.language = language
        self._checker = ctypes.c_void_p()
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # apartment-threaded; harmless if already set
        factory = ctypes.c_void_p()
        hr = ctypes.windll.ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_FACTORY)), None, 1,
                                                  ctypes.byref(_guid(_IID_FACTORY)), ctypes.byref(factory))
        if hr != 0 or not factory.value:
            raise OSError(f"spell checker unavailable (0x{hr & 0xFFFFFFFF:08x})")
        try:
            supported = wintypes.BOOL()
            _method(factory, 4, wintypes.LPCWSTR, ctypes.POINTER(wintypes.BOOL))(
                factory, language, ctypes.byref(supported))
            if not supported.value:
                raise OSError(f"no dictionary for {language}")
            hr = _method(factory, 5, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))(
                factory, language, ctypes.byref(self._checker))
            if hr != 0 or not self._checker.value:
                raise OSError(f"CreateSpellChecker failed (0x{hr & 0xFFFFFFFF:08x})")
        finally:
            _release(factory)

    def is_correct(self, text: str) -> bool:
        """True when the checker finds no spelling error in `text`."""
        errors = ctypes.c_void_p()
        hr = _method(self._checker, 4, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))(
            self._checker, text, ctypes.byref(errors))
        if hr != 0 or not errors.value:
            raise OSError("Check failed")
        try:
            error = ctypes.c_void_p()
            hr = _method(errors, 3, ctypes.POINTER(ctypes.c_void_p))(errors, ctypes.byref(error))
            _release(error)
            return hr != 0  # S_OK: there is an error; S_FALSE: none
        finally:
            _release(errors)


def is_real_word(text: str, language: str = "", timeout: float = 1.0) -> bool | None:
    """Whether `text` is spelled correctly in `language` (default: the Windows locale).
    None when no checker is available for it (or it did not answer in time).

    All checks run on one long-lived thread that owns the COM checkers: dictation jobs
    are short-lived threads, and a checker per thread would leak COM objects."""
    global _requests
    with _start_lock:
        if _requests is None:
            _requests = queue.Queue()
            threading.Thread(target=_serve, args=(_requests,), name="spell-checker", daemon=True).start()
    done, result = threading.Event(), [None]
    _requests.put((text, language or system_language(), done, result))
    return result[0] if done.wait(timeout) else None


def _serve(requests: "queue.Queue") -> None:
    checkers: dict[str, SpellChecker | None] = {}
    while True:
        text, language, done, result = requests.get()
        try:
            if language not in checkers:
                try:
                    checkers[language] = SpellChecker(language)
                except OSError as e:
                    log.info("spell checking off for %s: %s", language, e)
                    checkers[language] = None
            checker = checkers[language]
            result[0] = checker.is_correct(text) if checker is not None else None
        except Exception:
            result[0] = None
        finally:
            done.set()
