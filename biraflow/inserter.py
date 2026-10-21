"""Insert text at the cursor of the focused app: clipboard + Ctrl+V, or typed keystrokes."""
from __future__ import annotations

import ctypes
import logging
import time
from contextlib import contextmanager
from typing import Callable

from . import win32
from .config import OutputCfg
from .hotkey import parse_hotkey

log = logging.getLogger(__name__)

MASK_VK = 0xE8  # unassigned VK; tapping it stops a lone Win/Alt release from opening menus
_MODIFIERS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift Ctrl Alt LWin RWin

# Clipboard formats that hold GDI handles or private data instead of HGLOBAL memory.
_SKIP_FORMATS = frozenset({2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E})


class ClipboardError(RuntimeError):
    pass


class NotPasted(RuntimeError):
    """The text is on the clipboard but was not pasted (focus moved / keys still held)."""


def _fmt(name: str) -> int:
    return win32.RegisterClipboardFormatW(name)


@contextmanager
def _clipboard():
    # A message-only window as owner: with a NULL owner SetClipboardData can fail.
    hwnd = win32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0,
                                 win32.HWND_MESSAGE, None, None, None)
    try:
        for _ in range(30):
            if win32.OpenClipboard(hwnd):
                break
            time.sleep(0.02)
        else:
            raise ClipboardError("A área de transferência está ocupada por outro programa.")
        try:
            yield
        finally:
            win32.CloseClipboard()
    finally:
        if hwnd:
            win32.DestroyWindow(hwnd)


def _put(fmt: int, data: bytes) -> bool:
    h = win32.GlobalAlloc(win32.GMEM_MOVEABLE, max(len(data), 1))
    if not h:
        return False
    p = win32.GlobalLock(h)
    if not p:
        win32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    win32.GlobalUnlock(h)
    if not win32.SetClipboardData(fmt, h):
        win32.GlobalFree(h)  # ownership passes to the system only on success
        return False
    return True


_DWORD_ZERO = (0).to_bytes(4, "little")


def set_text(text: str, keep_in_history: bool = True) -> int:
    """Put text on the clipboard; returns the clipboard sequence number afterwards.

    Dictation is never synced to the Windows cloud clipboard. When it is only on the
    clipboard temporarily (restore mode) it is also kept out of Win+V history and
    clipboard managers.
    """
    data = (text.replace("\r\n", "\n").replace("\n", "\r\n") + "\0").encode("utf-16-le")
    with _clipboard():
        win32.EmptyClipboard()
        if not _put(win32.CF_UNICODETEXT, data):
            raise ClipboardError("Não foi possível escrever na área de transferência.")
        _put(_fmt("CanUploadToCloudClipboard"), _DWORD_ZERO)
        if not keep_in_history:
            _put(_fmt("CanIncludeInClipboardHistory"), _DWORD_ZERO)
            _put(_fmt("ExcludeClipboardContentFromMonitorProcessing"), b"\0")
    return win32.GetClipboardSequenceNumber()


def get_text() -> str | None:
    with _clipboard():
        h = win32.GetClipboardData(win32.CF_UNICODETEXT)
        if not h:
            return None
        p = win32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else None
        finally:
            win32.GlobalUnlock(h)


def snapshot() -> list[tuple[int, bytes]]:
    """Copy every memory-backed format currently on the clipboard."""
    items: list[tuple[int, bytes]] = []
    with _clipboard():
        fmt = 0
        while True:
            fmt = win32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in _SKIP_FORMATS or 0x200 <= fmt <= 0x3FF:
                continue
            h = win32.GetClipboardData(fmt)
            if not h:
                continue
            size = win32.GlobalSize(h)
            if not size:
                continue
            p = win32.GlobalLock(h)
            if not p:
                continue
            try:
                items.append((fmt, ctypes.string_at(p, size)))
            finally:
                win32.GlobalUnlock(h)
    return items


def restore(items: list[tuple[int, bytes]]) -> None:
    with _clipboard():
        win32.EmptyClipboard()
        for fmt, data in items:
            if not _put(fmt, data):
                log.debug("could not restore clipboard format %s", fmt)
        # It is already in Win+V history from when the user copied it.
        _put(_fmt("CanIncludeInClipboardHistory"), _DWORD_ZERO)


def sequence_number() -> int:
    return win32.GetClipboardSequenceNumber()


# --- keystrokes -----------------------------------------------------------------

def tap_mask_key() -> None:
    win32.send_inputs([win32.key_input(MASK_VK), win32.key_input(MASK_VK, flags=win32.KEYEVENTF_KEYUP)])


def send_chord(spec: str) -> None:
    vks = parse_hotkey(spec).injection_vks()
    downs = [win32.vk_input(vk, up=False) for vk in vks]
    ups = [win32.vk_input(vk, up=True) for vk in reversed(vks)]
    win32.send_inputs(downs + ups)


def type_text(text: str, chunk: int = 48) -> None:
    inputs: list[win32.INPUT] = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            # Shift+Enter: a line break in chat apps, where plain Enter would send the message
            inputs += [win32.vk_input(0xA0, up=False), win32.vk_input(0x0D, up=False),
                       win32.vk_input(0x0D, up=True), win32.vk_input(0xA0, up=True)]
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            inputs.append(win32.key_input(0, code, win32.KEYEVENTF_UNICODE))
            inputs.append(win32.key_input(0, code, win32.KEYEVENTF_UNICODE | win32.KEYEVENTF_KEYUP))
    for i in range(0, len(inputs), chunk * 2):
        win32.send_inputs(inputs[i:i + chunk * 2])
        time.sleep(0.005)


def wait_for_modifiers_released(timeout: float = 1.5) -> bool:
    """Pasting while e.g. Win is still held would send Win+V instead of Ctrl+V."""
    end = time.monotonic() + timeout
    while any(win32.is_key_down(vk) for vk in _MODIFIERS):
        if time.monotonic() >= end:
            return False
        time.sleep(0.01)
    return True


def insert(text: str, cfg: OutputCfg, check: Callable[[], None],
           target: int | None = None) -> Callable[[], None] | None:
    """Insert `text` at the cursor. `check()` raises if the job was cancelled; it runs
    right before anything is sent. Returns a deferred clipboard-restore callable in
    restore mode.

    `target` is the window that had focus when the dictation started. If focus moved
    elsewhere meanwhile, or modifier keys are still held, nothing is typed: the text is
    left on the clipboard and NotPasted is raised, so it never lands in the wrong place."""
    if cfg.trailing_space and not text.endswith((" ", "\n")):
        text += " "
    released = wait_for_modifiers_released()
    check()
    if not released:
        set_text(text)
        raise NotPasted("Teclas ainda pressionadas: o texto ficou na área de transferência (Ctrl+V).")
    if target and win32.foreground_root() != target:
        set_text(text)
        raise NotPasted("A janela mudou: o texto ficou na área de transferência (Ctrl+V).")
    if target and win32.blocked_by_elevation(target):
        set_text(text)
        raise NotPasted("App aberto como administrador: o Windows bloqueia a colagem automática. "
                        "O texto está na área de transferência (Ctrl+V).")
    if cfg.method == "type":
        type_text(text)
        return None

    saved = snapshot() if cfg.restore_clipboard else None
    seq = set_text(text, keep_in_history=not cfg.restore_clipboard)
    try:
        check()
    except BaseException:
        if saved is not None:
            restore(saved)
        raise
    time.sleep(0.02)
    send_chord(cfg.paste_keys)
    if saved is None:
        return None

    def restore_later() -> None:
        time.sleep(0.6)  # let the target app finish reading the clipboard
        if sequence_number() == seq:  # don't clobber something the user copied meanwhile
            restore(saved)

    return restore_later


def backspace(count: int) -> None:
    """Delete `count` characters before the cursor (undo of the last dictation)."""
    for i in range(0, count, 40):
        n = min(40, count - i)
        win32.send_inputs([x for _ in range(n) for x in (win32.vk_input(0x08, up=False), win32.vk_input(0x08, up=True))])
        time.sleep(0.005)


def press_enter() -> None:
    win32.send_inputs([win32.vk_input(0x0D, up=False), win32.vk_input(0x0D, up=True)])


def copy_selection(timeout: float = 1.5) -> str:
    """The focused app's selected text, via Ctrl+C ("" if nothing is selected). The
    clipboard is overwritten: snapshot it first if it must survive."""
    wait_for_modifiers_released()
    before = sequence_number()
    send_chord("ctrl+c")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if sequence_number() != before:
            time.sleep(0.03)  # let the app finish writing every format
            return get_text() or ""
        time.sleep(0.01)
    return ""
