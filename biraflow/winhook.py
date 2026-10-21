"""System-wide low-level keyboard hook (WH_KEYBOARD_LL) on its own thread.

The hook only observes: every event is passed on to the next hook, so Esc and all
other keys still reach the focused app. A key is swallowed only when the handler
returns True (non-modifier hotkey keys, if suppress_keys is on).
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

from . import win32

log = logging.getLogger(__name__)

WM_TIMER = 0x0113
REINSTALL_MS = 120_000


class KeyboardHook:
    def __init__(self, handler: Callable[[int, bool], bool]):
        self._handler = handler
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: str | None = None
        self._proc = win32.HOOKPROC(self._callback)  # keep a reference: ctypes won't

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="keyboard-hook", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        if self._error:
            raise OSError(self._error)

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            win32.PostThreadMessageW(self._thread_id, win32.WM_QUIT, 0, 0)
            self._thread.join(2)

    def _run(self) -> None:
        self._thread_id = win32.GetCurrentThreadId()
        hook = win32.SetWindowsHookExW(win32.WH_KEYBOARD_LL, self._proc,
                                       win32.GetModuleHandleW(None), 0)
        if not hook:
            self._error = f"SetWindowsHookEx falhou (erro {ctypes.get_last_error()})"
            self._ready.set()
            return
        self._ready.set()
        # Windows silently removes a low-level hook whose callback was once too slow (the
        # PC was busy, waking from sleep...), and the hotkey would just stop working.
        # Re-install it every couple of minutes: the new hook goes in before the old one
        # comes out, so no key press is lost in between.
        user32 = ctypes.windll.user32
        user32.SetTimer.restype = ctypes.c_size_t
        user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
        timer = user32.SetTimer(None, 0, REINSTALL_MS, None)
        msg = wintypes.MSG()
        try:
            while win32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_TIMER and not msg.hWnd:
                    fresh = win32.SetWindowsHookExW(win32.WH_KEYBOARD_LL, self._proc,
                                                    win32.GetModuleHandleW(None), 0)
                    if fresh:
                        win32.UnhookWindowsHookEx(hook)
                        hook = fresh
                    continue
                win32.TranslateMessage(ctypes.byref(msg))
                win32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if timer:
                user32.KillTimer(None, timer)
            win32.UnhookWindowsHookEx(hook)

    def _callback(self, ncode: int, wparam: int, lparam: int) -> int:
        # Windows drops hooks that take too long (~1 s), so: no blocking work here.
        try:
            if ncode == win32.HC_ACTION:
                kb = win32.KBDLLHOOKSTRUCT.from_address(lparam)
                if kb.dwExtraInfo != win32.INJECT_TAG:
                    if wparam in (win32.WM_KEYDOWN, win32.WM_SYSKEYDOWN):
                        if self._handler(kb.vkCode, True):
                            return 1
                    elif wparam in (win32.WM_KEYUP, win32.WM_SYSKEYUP):
                        if self._handler(kb.vkCode, False):
                            return 1
        except Exception:  # never let an exception escape into the hook chain
            log.exception("keyboard hook handler failed")
        return win32.CallNextHookEx(None, ncode, wparam, lparam)
