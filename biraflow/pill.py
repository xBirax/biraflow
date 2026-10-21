"""Always-on-top pill: recording mode, timer and a live voice waveform, job status,
and a short preview of what was just inserted.

It never takes focus and lets clicks through (WS_EX_NOACTIVATE | WS_EX_TRANSPARENT),
so the app being dictated into keeps its caret. All methods run on the Tk thread.
"""
from __future__ import annotations

import ctypes
import math
import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

from .i18n import t as _t
from . import win32
from .statemachine import Mode

KEY = "#010203"  # transparent color key: everything painted this color is see-through
BG = "#18181b"
FG = "#fafafa"
MUTED = "#a1a1aa"
WARN = "#fbbf24"
TONES = {
    "hold": "#ef4444",
    "handsfree": "#a855f7",
    "command": "#14b8a6",
    "busy": "#f59e0b",
    "ok": "#22c55e",
    "error": "#ef4444",
    "info": "#a1a1aa",
}
SIZES = {"small": 1.0, "medium": 1.3, "large": 1.6}
POSITIONS = ("bottom", "top", "bottom-left", "bottom-right", "top-left", "top-right")
BARS = 15          # voice meter: bars that rise and fall with your voice, tallest in the middle
FRAME_MS = 15      # ~60 fps while visible
# per-bar height profile (a soft bell) and wobble, so the meter looks alive, not like a block
WEIGHTS = [0.38 + 0.62 * math.exp(-(((i - (BARS - 1) / 2) / (BARS / 3.4)) ** 2)) for i in range(BARS)]
SPEEDS = [6.0 + (i * 2.3) % 5.0 for i in range(BARS)]
PHASES = [i * 1.9 for i in range(BARS)]


def _timer_resolution(fine: bool) -> None:
    """1 ms timers while the pill animates: Windows' default 15.6 ms tick makes a 60 fps
    animation stutter between 16 and 31 ms frames. Released as soon as the pill hides."""
    try:
        winmm = ctypes.windll.winmm
        (winmm.timeBeginPeriod if fine else winmm.timeEndPeriod)(1)
    except (AttributeError, OSError):
        pass


def _clock(seconds: float) -> str:
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


class Pill:
    def __init__(self, root: tk.Tk, level: Callable[[], float], position: str = "bottom",
                 size: str = "medium", signal: Callable[[], float] | None = None):
        self.position = position
        self.size = size
        self._level = level
        self._signal = signal  # loudest sample so far: 0 after a while = muted microphone
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.configure(bg=KEY)
        self.top.attributes("-topmost", True)
        self.top.attributes("-transparentcolor", KEY)
        self.canvas = tk.Canvas(self.top, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.font = tkfont.Font(family="Segoe UI", size=-13)
        self.font_small = tkfont.Font(family="Segoe UI", size=-11)
        self.top.geometry("10x10+-10000+-10000")  # not -32000: that means "minimized"
        self.top.update_idletasks()
        try:
            self.hwnd = int(self.top.wm_frame(), 16)
        except (tk.TclError, ValueError):
            self.hwnd = win32.GetParent(self.top.winfo_id())
        ex = win32.GetWindowLongPtrW(self.hwnd, win32.GWL_EXSTYLE)
        win32.SetWindowLongPtrW(self.hwnd, win32.GWL_EXSTYLE,
                                ex | win32.WS_EX_NOACTIVATE | win32.WS_EX_TOOLWINDOW
                                | win32.WS_EX_TRANSPARENT | win32.WS_EX_LAYERED | win32.WS_EX_TOPMOST)
        win32.ShowWindow(self.hwnd, win32.SW_HIDE)

        self._visible = False
        self._fine = False   # 1 ms timer resolution requested (only while animating)
        self._after: str | None = None
        self._kind: str | None = None   # "rec" | "busy" | "flash"
        self._mode = Mode.HOLD
        self._started = 0.0
        self._deadline = 0.0
        self._text = ""
        self._tone = "info"
        self._hide_at = 0.0
        self._scale = 1.0
        self._area = (0, 0, 0, 0)
        self._size = (0, 0)
        self._pos = (-10000, -10000)
        self._bars = [0.0] * BARS
        self._smooth = 0.0
        self._last_frame = 0.0
        self._layout: tuple | None = None   # what the canvas items were built for
        self._items: dict = {}
        self._measured: tuple[str, int] = ("", 0)
        self._font_px = 0

    # --- public API (Tk thread) -------------------------------------------------

    def recording(self, mode: Mode, started_at: float, deadline: float) -> None:
        if self._kind != "rec":
            self._bars = [0.0] * BARS
            self._smooth = 0.0
        self._kind, self._mode, self._started, self._deadline = "rec", mode, started_at, deadline
        self._show()

    def busy(self, text: str) -> None:
        self._kind, self._text, self._tone = "busy", text, "busy"
        self._show()

    def flash(self, text: str, tone: str = "info", ms: int = 1200) -> None:
        self._kind, self._text, self._tone = "flash", text, tone
        self._hide_at = time.monotonic() + ms / 1000
        self._show()

    def preview(self, text: str, ms: int = 2600) -> None:
        """What was just inserted, so you can check it without looking at the app."""
        one_line = " ".join(text.split())
        self.flash(_t("✓ ") + one_line, "ok", ms)

    def hide(self) -> None:
        self._kind = None
        if self._after:
            self.top.after_cancel(self._after)
            self._after = None
        if self._visible:
            win32.ShowWindow(self.hwnd, win32.SW_HIDE)
            self._visible = False
        self._set_fine(False)

    # --- internals ----------------------------------------------------------------

    def _show(self) -> None:
        if not self._visible:
            # Follow the monitor of the window being typed into.
            self._area, dpi = win32.work_area_and_dpi()
            self._scale = dpi / 96 * SIZES.get(self.size, 1.3)
            self._size = (0, 0)
        self._render()
        if not self._visible:
            self._place()
            win32.ShowWindow(self.hwnd, win32.SW_SHOWNOACTIVATE)
            self._visible = True
        self._set_fine(self._kind != "flash")
        if self._after is not None:   # a new flash may end sooner than the one scheduled
            self.top.after_cancel(self._after)
        self._after = self.top.after(self._next_frame(), self._tick)

    def _next_frame(self) -> int:
        # a flash is static text: one wake-up at its hide time, not 60 redraws a second
        if self._kind == "flash":
            return max(1, round((self._hide_at - time.monotonic()) * 1000))
        return FRAME_MS

    def _set_fine(self, fine: bool) -> None:
        if fine != self._fine:   # timeBeginPeriod/timeEndPeriod must stay balanced
            self._fine = fine
            _timer_resolution(fine)

    def _place(self) -> None:
        # Position through Win32: Tk ignores geometry changes while the window is
        # hidden behind its back, and re-applies stale ones when it is shown again.
        (x, y), (w, h) = self._pos, self._size
        win32.SetWindowPos(self.hwnd, win32.HWND_TOPMOST, x, y, w, h, win32.SWP_NOACTIVATE)

    def _tick(self) -> None:
        self._after = None
        if self._kind is None:
            return
        if self._kind == "flash" and time.monotonic() >= self._hide_at:
            self.hide()
            return
        self._render()
        self._after = self.top.after(self._next_frame(), self._tick)

    def _origin(self, w: int, h: int) -> tuple[int, int]:
        left, top, right, bottom = self._area
        s = self._scale
        side = round(24 * s)
        pos = self.position if self.position in POSITIONS else "bottom"
        vertical, _, horizontal = pos.partition("-")
        if horizontal == "left":
            x = left + side
        elif horizontal == "right":
            x = right - side - w
        else:
            x = (left + right) // 2 - w // 2
        y = bottom - round(48 * s) - h if vertical == "bottom" else top + round(20 * s)
        return x, y

    def _content(self, now: float) -> tuple[str, str, str, bool, bool]:
        """(text, text color, dot color, pulse, waveform)"""
        if self._kind == "rec":
            elapsed = _clock(now - self._started)
            label, tone = {Mode.HANDSFREE: ("Mãos livres", "handsfree"),
                           Mode.COMMAND: ("Comando", "command")}.get(self._mode, ("Ditando", "hold"))
            left = self._deadline - now if self._deadline else float("inf")
            if left <= 30:   # the auto-stop is near, in every mode
                return f"{label}  {elapsed} · para em {max(0, int(left))}s", WARN, TONES[tone], True, True
            if (tone == "hold" and self._signal is not None and now - self._started > 2.0
                    and self._signal() == 0.0):
                return f"Microfone sem som (mudo?)  {elapsed}", WARN, TONES["hold"], True, True
            return f"{label}  {elapsed}", FG, TONES[tone], True, True
        if self._kind == "busy":
            return self._text, FG, TONES["busy"], True, False
        return self._text, FG, TONES.get(self._tone, TONES["info"]), False, False

    def _advance_meter(self, now: float) -> None:
        dt = min(0.1, max(0.001, now - self._last_frame)) if self._last_frame else 0.016
        self._last_frame = now
        level = max(0.0, min(1.0, self._level()))
        # fast attack, gentle release, frame-rate independent
        rel = 1 - 0.12 ** dt
        self._smooth = level if level > self._smooth else self._smooth + (level - self._smooth) * rel
        up, down = 1 - 0.0005 ** dt, 1 - 0.02 ** dt
        for i in range(BARS):
            wobble = 0.72 + 0.28 * math.sin(now * SPEEDS[i] + PHASES[i])
            target = self._smooth * WEIGHTS[i] * wobble
            cur = self._bars[i]
            self._bars[i] = cur + (target - cur) * (up if target > cur else down)

    def _text_width(self, text: str) -> int:
        if self._measured[0] != text:
            self._measured = (text, self.font.measure(text))
        return self._measured[1]

    def _render(self) -> None:
        """Builds the canvas items when the layout changes, then only moves/recolors
        them each frame (no delete-and-redraw, which made frames uneven)."""
        now = time.monotonic()
        text, fg, dot_color, pulse, wave = self._content(now)
        s = self._scale
        h = round(30 * s)
        pad, gap, dot = round(12 * s), round(8 * s), round(8 * s)
        bar_w, bar_gap = max(2, round(2.6 * s)), max(1, round(2.0 * s))
        font_px = round(12.5 * s)
        if font_px != self._font_px:
            self._font_px = font_px
            self.font.configure(size=-font_px)
            self._measured = ("", 0)
        limit = 72 if self._kind == "flash" else 60
        if len(text) > limit:
            text = text[:limit - 1].rstrip() + "…"
        wave_w = BARS * bar_w + (BARS - 1) * bar_gap if wave else 0
        w = pad + dot + gap + self._text_width(text) + (gap * 2 + wave_w if wave else 0) + pad
        w = max(w, 2 * h)
        if (w, h) != self._size:
            self._size = (w, h)
            self._pos = self._origin(w, h)
            x, y = self._pos
            self.canvas.configure(width=w, height=h)
            self.top.geometry(f"{w}x{h}+{x}+{y}")
            if self._visible:
                self._place()

        c = self.canvas
        cy = h / 2
        layout = (w, h, wave, bar_w, bar_gap)
        if layout != self._layout:
            self._layout = layout
            c.delete("all")
            c.create_oval(0, 0, h, h, fill=BG, outline=BG)
            c.create_oval(w - h, 0, w, h, fill=BG, outline=BG)
            c.create_rectangle(h / 2, 0, w - h / 2, h, fill=BG, outline=BG)
            items = {"dot": c.create_oval(0, 0, 0, 0, fill=dot_color, outline=""),
                     "text": c.create_text(pad + dot + gap, cy, text=text, fill=fg, font=self.font, anchor="w"),
                     "bars": []}
            x0 = w - pad - wave_w
            for i in range(BARS if wave else 0):
                x = x0 + i * (bar_w + bar_gap)
                items["bars"].append((c.create_rectangle(x, cy, x + bar_w, cy, fill=MUTED, outline=""), x))
            self._items = items
            self._drawn: dict = {}

        items, drawn = self._items, self._drawn
        cx = pad + dot / 2
        r = dot / 2 * ((0.8 + 0.2 * math.sin(now * 6)) if pulse else 1.0)
        c.coords(items["dot"], cx - r, cy - r, cx + r, cy + r)
        if drawn.get("dot") != dot_color:
            drawn["dot"] = dot_color
            c.itemconfigure(items["dot"], fill=dot_color)
        if drawn.get("text") != (text, fg):
            drawn["text"] = (text, fg)
            c.itemconfigure(items["text"], text=text, fill=fg)

        if wave:
            self._advance_meter(now)
            max_h = h * 0.62
            min_h = max(2, round(2 * s))
            for i, (item, x) in enumerate(items["bars"]):
                level = self._bars[i]
                bh = max(min_h, max_h * level)
                c.coords(item, x, cy - bh / 2, x + bar_w, cy + bh / 2)
                color = fg if level > 0.05 else MUTED
                if drawn.get(i) != color:
                    drawn[i] = color
                    c.itemconfigure(item, fill=color)
