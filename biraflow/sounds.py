"""Short, quiet interface sounds (start / stop / cancel), synthesized once to WAV files."""
from __future__ import annotations

import logging
import math
import struct
import wave
from pathlib import Path

log = logging.getLogger(__name__)

RATE = 44_100
_TONES = {
    # name: list of (frequency Hz, duration s)
    "start": [(784.0, 0.06), (1175.0, 0.09)],   # G5 -> D6, rising
    "stop": [(1175.0, 0.06), (880.0, 0.08)],    # falling
    "cancel": [(392.0, 0.12)],                  # low single note
    "warn": [(988.0, 0.12)],                    # the recording stops in 10 s
}


def render(notes: list[tuple[float, float]], volume: float = 0.18) -> bytes:
    frames = bytearray()
    for freq, dur in notes:
        n = int(RATE * dur)
        for i in range(n):
            t = i / RATE
            env = min(1.0, i / (0.004 * RATE)) * min(1.0, (n - i) / (0.03 * RATE))  # click-free
            v = volume * env * (math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t))
            frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    return bytes(frames)


class Sounds:
    def __init__(self, folder: Path):
        self.folder = folder
        self.enabled = True
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        for name, notes in _TONES.items():
            path = self.folder / f"{name}.wav"
            if not path.exists():
                with wave.open(str(path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(RATE)
                    w.writeframes(render(notes))
        self._ready = True

    def play(self, name: str) -> None:
        if not self.enabled:
            return
        try:
            import winsound

            self._ensure()
            winsound.PlaySound(str(self.folder / f"{name}.wav"),
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception as e:  # sounds are a nicety, never an error
            log.debug("sound %s failed: %s", name, e)
