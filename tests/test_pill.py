"""The pill's drawing, without ever showing it (frames are rendered on its hidden canvas)."""
import time
import tkinter as tk

import pytest

from biraflow.pill import BARS, Pill
from biraflow.statemachine import Mode


@pytest.fixture
def root(tk_root):
    return tk_root


def _pill(root, level=lambda: 0.0, signal=None):
    p = Pill(root, level, signal=signal)
    p._scale, p._area = 1.3, (0, 0, 1920, 1080)
    return p


def test_recording_frames_reuse_canvas_items(root):
    p = _pill(root, level=lambda: 0.8)
    now = time.monotonic()
    p._kind, p._mode, p._started, p._deadline = "rec", Mode.HOLD, now, now + 300
    p._render()
    items = set(p.canvas.find_all())
    for _ in range(10):
        p._last_frame -= 0.05          # as if 50 ms passed between frames
        p._render()
    assert set(p.canvas.find_all()) == items          # moved and recolored, never recreated
    assert len(p._items["bars"]) == BARS
    assert max(p._bars) > 0.1                          # the meter follows the voice
    p.top.destroy()


def test_muted_microphone_warning_and_long_text(root):
    p = _pill(root, signal=lambda: 0.0)
    now = time.monotonic()
    p._kind, p._mode, p._started = "rec", Mode.HOLD, now - 3
    assert p._content(now)[0].startswith("Microfone sem som")
    p._kind, p._text, p._tone = "flash", "x" * 300, "info"
    p._render()
    text_item = p._items["text"]
    assert len(p.canvas.itemcget(text_item, "text")) <= 72   # long previews are cut with "…"
    p.top.destroy()


def test_handsfree_countdown(root):
    p = _pill(root)
    now = time.monotonic()
    p._kind, p._mode, p._started, p._deadline = "rec", Mode.HANDSFREE, now - 280, now + 20
    assert "para em" in p._content(now)[0]
    p.top.destroy()


def test_flash_sleeps_until_it_hides(root):
    from biraflow.pill import FRAME_MS

    p = _pill(root)
    p._kind, p._hide_at = "flash", time.monotonic() + 2.0
    assert 1900 < p._next_frame() <= 2000          # static text: one wake-up, not 60 redraws a second
    p._kind = "rec"
    assert p._next_frame() == FRAME_MS
    p.top.destroy()
