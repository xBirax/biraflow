"""Learn corrections from the edits you make right after a dictation is pasted.

Like Wispr Flow's dictionary auto-add: if Bira Flow pastes "transcriber" and you fix
it to "transcrever" in the app, the fix becomes a correction rule. The focused text
field is read through Windows UI Automation, only after our own paste, only while the
same window keeps focus (at most two minutes), never in password fields; nothing is
stored except the learned rule.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable, Protocol

from . import textproc, win32
from .textproc import Replacement

log = logging.getLogger(__name__)

ANCHOR = 40          # characters of context kept on each side of the pasted text
MAX_FOLLOW = 5000    # longer pastes (a long dictation) are not followed: the field is read up to
                     # 20 000 chars, and nobody fixes a single word in a page-long text right away
POLL = 0.25          # seconds between checks (a read happens only after a key press)
SETTLE = 6.0         # an edit counts as finished after this long without changes
WATCH_FOR = 300.0    # never watch longer than this (5 min)
MAX_RULES = 3        # more changes than this is a rewrite, not a correction


_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def normalize(text: str) -> str:
    """Browsers report typed spaces as no-break spaces and may add zero-width marks."""
    return text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n").translate(_INVISIBLE)


class Reader(Protocol):
    def text(self) -> str | None: ...


def wake_chromium(hwnd: int) -> bool:
    """Chromium (Chrome, Edge, Brave, WhatsApp Desktop, Electron/WebView2 apps) builds the
    accessibility tree of the page only once a screen reader shows up. Asking for the
    accessible object of its render widget, as screen readers do, switches it on; it
    takes a second or two, so this is called when a recording starts."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")   # own instance: argtypes set here stay here
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumChildWindows.argtypes = [wintypes.HWND, proc, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    widgets: list[int] = []

    def collect(h, _):
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, name, 64)
        if name.value == "Chrome_RenderWidgetHostHWND":
            widgets.append(h)
        return True

    user32.EnumChildWindows(wintypes.HWND(hwnd), proc(collect), 0)
    if not widgets:
        return False
    import comtypes

    iid = comtypes.GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")  # IAccessible
    for h in widgets:
        ptr = ctypes.c_void_p()
        try:
            ctypes.oledll.oleacc.AccessibleObjectFromWindow(wintypes.HWND(h), -4, ctypes.byref(iid),  # OBJID_CLIENT
                                                            ctypes.byref(ptr))
        except OSError:
            continue
        if ptr.value:  # we only needed the request; release the object
            vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(ptr)
    return True


def text_before_caret(chars: int = 3, timeout: float = 0.25) -> str | None:
    """The few characters right before the cursor in the focused field (None when the
    app does not say). Bounded by `timeout`, so a slow app never delays a paste."""
    result: list[str] = []

    def run() -> None:
        try:
            import comtypes
            import comtypes.client

            comtypes.CoInitialize()
            m = comtypes.client.GetModule("UIAutomationCore.dll")
            uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=m.IUIAutomation)
            el = uia.GetFocusedElement()
            if el is None or el.CurrentIsPassword:
                return
            pattern = el.GetCurrentPattern(UiaReader.UIA_TextPatternId)
            if not pattern:
                return
            selection = pattern.QueryInterface(m.IUIAutomationTextPattern).GetSelection()
            if selection is None or selection.Length < 1:
                return
            rng = selection.GetElement(0).Clone()
            rng.MoveEndpointByRange(1, rng, 0)       # End := Start (collapse to the caret)
            rng.MoveEndpointByUnit(0, 0, -chars)     # Start back `chars` characters
            result.append(normalize(rng.GetText(chars + 4)))
        except Exception as e:
            log.debug("text before caret unavailable: %s", e)

    t = threading.Thread(target=run, name="caret-context", daemon=True)
    t.start()
    t.join(timeout)
    return result[0] if result else None


class UiaReader:
    """Reads the text field a dictation was pasted into (Value or Text pattern).

    bind() finds that field: the focused element if it holds the pasted text, otherwise
    the editable element of the target window that does (web pages and chat apps such as
    WhatsApp often report focus on an outer container). text() then reads that same field.
    One instance per thread."""

    UIA_ValuePatternId = 10002
    UIA_TextPatternId = 10014
    UIA_ControlTypePropertyId = 30003
    UIA_EditControlTypeId = 50004
    UIA_DocumentControlTypeId = 50030
    MAX_CHARS = 20000
    MAX_CANDIDATES = 60

    def __init__(self, target: int = 0) -> None:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        self._uia_mod = comtypes.client.GetModule("UIAutomationCore.dll")
        self._uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
                                                 interface=self._uia_mod.IUIAutomation)
        self._target = target
        self._element = None

    def _read(self, el) -> str | None:
        try:
            if el is None or el.CurrentIsPassword:
                return None
            m = self._uia_mod
            pattern = el.GetCurrentPattern(self.UIA_ValuePatternId)
            if pattern:
                value = pattern.QueryInterface(m.IUIAutomationValuePattern).CurrentValue
                if value:
                    return normalize(value)
            pattern = el.GetCurrentPattern(self.UIA_TextPatternId)
            if pattern:
                rng = pattern.QueryInterface(m.IUIAutomationTextPattern).DocumentRange
                return normalize(rng.GetText(self.MAX_CHARS))
            return "" if pattern is not None else None
        except Exception as e:  # app without UIA support, element gone, COM hiccup
            log.debug("UIA read failed: %s", e)
        return None

    def bind(self, pasted: str) -> str | None:
        """Find the field holding `pasted`; returns its text (None if not found)."""
        try:
            focused = self._uia.GetFocusedElement()
        except Exception:
            focused = None
        text = self._read(focused)
        if text and pasted in text:
            self._element = focused
            return text
        if not self._target:
            return None
        try:
            root = self._uia.ElementFromHandle(self._target)
            cond = self._uia.CreateOrCondition(
                self._uia.CreatePropertyCondition(self.UIA_ControlTypePropertyId, self.UIA_EditControlTypeId),
                self._uia.CreatePropertyCondition(self.UIA_ControlTypePropertyId, self.UIA_DocumentControlTypeId))
            found = root.FindAll(4, cond)  # TreeScope_Descendants
            for i in range(min(found.Length, self.MAX_CANDIDATES)):
                el = found.GetElement(i)
                text = self._read(el)
                if text and pasted in text:
                    self._element = el
                    return text
        except Exception as e:
            log.debug("UIA search failed: %s", e)
        return None

    def text(self) -> str | None:
        if self._element is not None:
            return self._read(self._element)
        try:
            return self._read(self._uia.GetFocusedElement())
        except Exception:
            return None

    def close(self) -> None:
        import comtypes

        self._element = None   # release every COM pointer before leaving the apartment
        self._uia = None
        comtypes.CoUninitialize()


def locate(text: str, before: str, after: str, hint: int = 0) -> str | None:
    """The part of `text` between the two anchors (None if they are gone)."""
    start = 0
    if before:
        start = text.find(before, max(0, hint - 400))
        if start < 0:
            start = text.find(before)
        if start < 0:
            return None
        start += len(before)
    end = len(text)
    if after:
        end = text.find(after, start)
        if end < 0:
            return None
    return text[start:end].strip()


def corrections(pasted: str, edited: str) -> list[Replacement]:
    if not edited or edited == pasted:
        return []
    rules = textproc.learn_replacements(pasted, edited)
    return rules if len(rules) <= MAX_RULES else []


class EditWatcher:
    def __init__(self, on_learned: Callable[[list[Replacement]], None],
                 reader_factory: Callable[..., Reader] | None = None,
                 foreground: Callable[[], int] | None = None, clock: Callable[[], float] = time.monotonic,
                 poll: float = POLL, settle: float = SETTLE, watch_for: float = WATCH_FOR,
                 activity: Callable[[], int] | None = None, land: float = 0.35, bind_retry: float = 0.4):
        """`activity` returns a counter of key presses (from the keyboard hook): the
        field is only read again after the user typed something, so an idle watch costs
        nothing and never makes the app being watched (or ours) stutter."""
        self._on_learned = on_learned
        self._activity = activity
        self._reader_takes_target = reader_factory is None
        self._reader_factory = reader_factory or UiaReader
        self._foreground = foreground or win32.foreground_root
        self._clock = clock
        self._poll, self._settle, self._watch_for = poll, settle, watch_for
        self._land, self._bind_retry = land, bind_retry   # let the paste land; a woken browser's field shows up
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()   # watch() runs on job threads, stop() on the controller

    def watch(self, pasted: str, target: int) -> None:
        if len(pasted) > MAX_FOLLOW:
            log.info("edit watch: skipped (%d chars)", len(pasted))
            self.stop()   # the previous dictation's watcher must not read this paste as an edit
            return
        with self._guard:
            if self._stop is not None:
                self._stop.set()
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(target=self._run, args=(pasted.strip(), target, stop),
                                            name="edit-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop watching without waiting (a new dictation starts): the watcher learns from
        the last version it read and exits on its own."""
        with self._guard:
            if self._stop is not None:
                self._stop.set()
            self._stop = self._thread = None

    def finalize(self, timeout: float = 1.5) -> None:
        """Stop watching now and learn from what was seen (before a new dictation lands)."""
        with self._guard:
            stop, thread = self._stop, self._thread
            self._stop = self._thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self, pasted: str, target: int, stop: threading.Event) -> None:
        pasted = normalize(pasted).strip()
        if not pasted:
            return
        try:
            reader = self._reader_factory(target) if self._reader_takes_target else self._reader_factory()
        except Exception as e:
            log.info("edit learning unavailable: %s", e)
            return
        try:
            edited = self._follow(reader, pasted, target, stop)
        finally:
            close = getattr(reader, "close", None)
            if close:
                close()
        rules = corrections(pasted, edited or "")
        if rules:
            log.info("learned %d correction(s) from an edit", len(rules))
            self._on_learned(rules)

    def _follow(self, reader: Reader, pasted: str, target: int, stop: threading.Event) -> str | None:
        stop.wait(self._land)
        if target and self._foreground() != target:
            return None
        bind = getattr(reader, "bind", None)
        first = None
        for _ in range(8):  # a just-woken browser may need a moment to expose the field
            first = bind(pasted) if bind else reader.text()
            if first and pasted in first:
                break
            if stop.wait(self._bind_retry):
                return None
        if not first:
            return None
        idx = first.rfind(pasted)
        if idx < 0:
            return None  # the app does not expose this field's text
        # whitespace next to the dictation (the space a paste adds, a browser's trailing
        # no-break space) would match inside the text itself, so it is no anchor
        before = first[max(0, idx - ANCHOR):idx].rstrip()
        after = first[idx + len(pasted): idx + len(pasted) + ANCHOR].lstrip()
        log.info("edit watch: following the field (%d chars)", len(first))
        last, changed_at, start = pasted, self._clock(), self._clock()
        seen = self._activity() if self._activity else None
        away = False
        while not stop.wait(self._poll):
            now = self._clock()
            if now - start > self._watch_for:
                break
            if target and self._foreground() != target:
                # You switched windows (to check something, take a screenshot...): pause,
                # and pick up again when you come back to fix the text.
                if not away:
                    away = True
                    log.info("edit watch paused: another window is in front")
                if last != pasted and now - changed_at >= self._settle:
                    break  # an edit was already seen and settled
                continue
            if away:
                away = False
                log.info("edit watch resumed")
            if self._activity is not None:
                typed = self._activity()
                if typed == seen:  # nothing typed since the last read: don't touch the app
                    if last != pasted and now - changed_at >= self._settle:
                        break
                    continue
                seen = typed
            text = reader.text()
            if text is None:
                break
            region = locate(text, before, after, idx) if text.strip() else None
            if not region:
                break  # field cleared (message sent) or rewritten: keep the last version seen
            if region != last:
                last, changed_at = region, now
            elif last != pasted and now - changed_at >= self._settle:
                break  # the edit settled
        return last
