"""System audio capture ("what the PC plays") through WASAPI loopback on the default output
device, with raw COM vtables (the bundled PortAudio exposes no loopback devices).

Loopback delivers no packets while nothing is playing, so a silent PortAudio output stream
is kept open to keep the audio engine ticking; any remaining gap (device change, no output
device for a while) is zero-filled by the wall clock, so the timeline stays aligned with the mic.

Self-check: python -m biraflow.loopback SECONDS out.wav
"""
from __future__ import annotations

import ctypes
import logging
import math
import sys
import threading
import time
import uuid
from ctypes import wintypes

import numpy as np

from . import audio
from .mediamute import (CLSCTX_ALL, CLSID_MMDeviceEnumerator, GUID, IID_IMMDeviceEnumerator, _check, _co_init,
                        _method, _ole32, _release)

log = logging.getLogger(__name__)

IID_IAudioClient = GUID.of("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = GUID.of("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
_SUBTYPE_FLOAT = uuid.UUID("00000003-0000-0010-8000-00AA00389B71").bytes_le
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
_INVALIDATED = "0x88890004"  # AUDCLNT_E_DEVICE_INVALIDATED, as _check formats it
BUFFER_100NS = 20_000_000  # 2 s
GAP_SLACK = 0.2  # seconds behind the clock before zero-filling
MAX_FILL = 10.0  # seconds zero-filled at once: memory stays bounded whatever the gap
PAUSE_AFTER = 30.0  # a longer gap (PC asleep, no output device for a while) is a pause, not silence;
                    # the meeting recorder uses the same limit, so both channels agree on a pause
ALIVE_TIMEOUT = 5.0
POLL = 0.05          # the 2 s buffer is read every 50 ms
BLOCK_FRAMES = 4096  # the silent output stream wakes Python ~12x/s instead of ~100x/s
CONSOLE, COMMUNICATIONS = 0, 2  # ERole: the default output, and the one calls (Teams, Zoom) use

_free = ctypes.WinDLL("ole32").CoTaskMemFree
_free.argtypes = (ctypes.c_void_p,)
_free.restype = None


class _WaveFormat(ctypes.Structure):  # WAVEFORMATEXTENSIBLE
    _pack_ = 1
    _fields_ = [("tag", wintypes.WORD), ("channels", wintypes.WORD), ("rate", wintypes.DWORD),
                ("avg_bytes", wintypes.DWORD), ("block_align", wintypes.WORD), ("bits", wintypes.WORD),
                ("cb_size", wintypes.WORD), ("valid_bits", wintypes.WORD), ("channel_mask", wintypes.DWORD),
                ("subformat", ctypes.c_ubyte * 16)]

    @property
    def is_float(self) -> bool:
        return self.tag == 3 or (self.tag == 0xFFFE and bytes(self.subformat) == _SUBTYPE_FLOAT)


def to_mono(data: bytes, channels: int, bits: int, is_float: bool) -> np.ndarray:
    """Interleaved mix-format frames -> mono float32 at the same rate."""
    if is_float and bits == 32:
        x = np.frombuffer(data, "<f4")
    elif not is_float and bits in (16, 32):
        x = np.frombuffer(data, f"<i{bits // 8}").astype(np.float32) / float(2 ** (bits - 1))
    else:
        raise OSError(f"formato de mixagem sem suporte: {bits} bits, float={is_float}")
    return x.reshape(-1, channels).mean(axis=1).astype(np.float32)


class Converter:
    """Native-rate mono -> 16 kHz, carrying the samples that do not fill a whole block."""

    def __init__(self, rate: int) -> None:
        self.rate = rate
        step = rate // math.gcd(rate, audio.RATE)
        # shortcut: non-integer factors (44.1 kHz) resample per 0.1 s block, whose edges are
        # slightly soft; upgrade to a streaming polyphase filter if that is ever audible.
        self.block = step if rate % audio.RATE == 0 else max(step, rate // 10 // step * step)
        self.pending = np.zeros(0, np.float32)

    def push(self, x: np.ndarray) -> np.ndarray:
        x = np.concatenate((self.pending, x))
        n = x.size // self.block * self.block
        self.pending = x[n:]
        return audio.resample(x[:n], self.rate)


def gap_frames(elapsed: float, captured: float, rate: int, slack: float = GAP_SLACK) -> int:
    """Native frames of silence needed to catch up with the clock (0 within `slack` s), at most
    MAX_FILL seconds per call."""
    lag = elapsed - captured
    return int(round(min(lag, MAX_FILL) * rate)) if lag > slack else 0


def capture_roles(console_id: str | None, communications_id: str | None) -> list[int]:
    """Calls often play on the default *communications* device: capture it too when it is
    another endpoint than the default output."""
    if communications_id and communications_id != console_id:
        return [CONSOLE, COMMUNICATIONS]
    return [CONSOLE]


def mix_aligned(carry: list[np.ndarray], new: list[np.ndarray]) -> tuple[np.ndarray, list[np.ndarray]]:
    """Sum streams that run on the same clock: what all of them have so far, the rest is carried."""
    streams = [np.concatenate((c, x)) for c, x in zip(carry, new)]
    n = min(s.size for s in streams)
    out = np.clip(np.sum([s[:n] for s in streams], axis=0), -1.0, 1.0).astype(np.float32)
    return out, [s[n:] for s in streams]


def endpoint_id(role: int) -> str | None:
    """Id of the default output device for `role` (None: there is none)."""
    _co_init()
    enum, device, text = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_wchar_p()
    try:
        _check(_ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                       ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum)),
               "CoCreateInstance(MMDeviceEnumerator)")
        _method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(0, role, ctypes.byref(device))
        _method(device, 5, ctypes.POINTER(ctypes.c_wchar_p))(ctypes.byref(text))  # IMMDevice::GetId
        return text.value
    except OSError:
        return None
    finally:
        if text:
            _free(ctypes.cast(text, ctypes.c_void_p))
        _release(device)
        _release(enum)


def _zeros(outdata, frames, time_info, status) -> None:
    outdata.fill(0)


class Loopback:
    """start(); read() repeatedly (16 kHz mono float32 since the last read); stop().

    `pauses`: (16 kHz samples read before it, seconds) of each gap longer than PAUSE_AFTER
    (the PC slept): those are not filled with silence, the caller marks them."""

    def __init__(self, role: int = CONSOLE) -> None:
        self.role = role
        self.pauses: list[tuple[int, float]] = []
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._emitted = 0  # 16 kHz samples produced since start()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = self._last_ok = 0.0

    @property
    def alive(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and time.monotonic() - self._last_ok < ALIVE_TIMEOUT)

    def start(self) -> None:
        self._t0 = self._last_ok = time.monotonic()
        self._emitted = 0   # a reopen starts a new clock: an old count would turn off the gap filling
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="loopback", daemon=True)
        self._thread.start()

    def read(self) -> np.ndarray:
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _emit(self, y: np.ndarray) -> None:
        if y.size:
            with self._lock:
                self._chunks.append(y)
                self._emitted += y.size

    def _catch_up(self, conv: Converter, slack: float = GAP_SLACK) -> None:
        captured = self._emitted / audio.RATE + conv.pending.size / conv.rate
        lag = time.monotonic() - self._t0 - captured
        if lag > PAUSE_AFTER:   # hours of zeros would fill the memory: mark a pause instead
            log.info("loopback: %.0f s without audio (PC asleep?); marked as a pause", lag)
            with self._lock:
                self.pauses.append((self._emitted, lag))
            self._t0 += lag
            return
        gap = gap_frames(time.monotonic() - self._t0, captured, conv.rate, slack)
        if gap:
            (log.info if gap > conv.rate // 2 else log.debug)("loopback: zero-filled %.2f s", gap / conv.rate)
            self._emit(conv.push(np.zeros(gap, np.float32)))

    def _run(self) -> None:
        _co_init()
        last_error, opened = "", False
        while not self._stop.is_set():
            try:
                self._session(opened)
                opened = True
            except Exception as e:
                msg = str(e)
                if _INVALIDATED in msg:
                    log.info("loopback device changed")
                elif msg != last_error:  # e.g. no output device: retried quietly every 0.5 s
                    log.warning("loopback: %s", msg)
                last_error = msg
                # still silence by the clock: with two parts, one that never opens would
                # otherwise hold back the other's audio (and its carry would grow forever)
                self._catch_up(Converter(audio.RATE))
                self._stop.wait(0.5)

    def _session(self, reopening: bool) -> None:
        enum, device, client, capture = (ctypes.c_void_p() for _ in range(4))
        fmt = ctypes.POINTER(_WaveFormat)()
        silence = None
        try:
            _check(_ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                           ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum)),
                   "CoCreateInstance(MMDeviceEnumerator)")
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, role)
            _method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(0, self.role,
                                                                                    ctypes.byref(device))
            # IMMDevice::Activate(IAudioClient)
            _method(device, 3, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(client))
            _method(client, 8, ctypes.POINTER(ctypes.POINTER(_WaveFormat)))(ctypes.byref(fmt))  # GetMixFormat
            f = fmt.contents
            channels, bits, is_float, align = f.channels, f.bits, f.is_float, f.block_align
            to_mono(bytes(align), channels, bits, is_float)  # unsupported format: fail here, not per packet
            # IAudioClient::Initialize(shared, loopback, 2 s, 0, mix format, no session GUID)
            _method(client, 3, ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p,
                    ctypes.c_void_p)(0, AUDCLNT_STREAMFLAGS_LOOPBACK, BUFFER_100NS, 0, fmt, None)
            _method(client, 14, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(  # GetService
                ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(capture))
            get_buffer = _method(capture, 3, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p)
            release_buffer = _method(capture, 4, ctypes.c_uint32)
            next_packet = _method(capture, 5, ctypes.POINTER(ctypes.c_uint32))
            try:
                silence = audio.open_input_stream(output=True, channels=1, dtype="float32", callback=_zeros,
                                                  blocksize=BLOCK_FRAMES)
            except Exception as e:
                log.warning("loopback: silent output stream failed (%s); silence is zero-filled by the clock", e)
            _method(client, 10)()  # Start
            conv = Converter(f.rate)
            self._catch_up(conv, slack=0)  # opening time, or the gap since the old device vanished
            log.info("loopback %s: %d Hz, %d ch, %d bits%s", "reopened" if reopening else "started",
                     f.rate, channels, bits, " float" if is_float else "")
            size, frames, flags, data = ctypes.c_uint32(), ctypes.c_uint32(), wintypes.DWORD(), ctypes.c_void_p()
            while not self._stop.wait(POLL):
                packets = []
                while True:
                    next_packet(ctypes.byref(size))
                    if not size.value:
                        break
                    get_buffer(ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
                    n = frames.value
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                        packets.append(np.zeros(n, np.float32))
                    else:
                        packets.append(to_mono(ctypes.string_at(data.value, n * align), channels, bits, is_float))
                    release_buffer(n)
                if packets:   # one conversion per poll, not per 10 ms packet: less CPU
                    self._emit(conv.push(np.concatenate(packets)))
                self._last_ok = time.monotonic()
                self._catch_up(conv)
            self._catch_up(conv, slack=0)  # stopped: as long as it ran
        finally:
            if silence is not None:
                audio.close_input_stream(silence)
            if client.value:
                try:
                    _method(client, 11)()  # Stop
                except OSError:
                    pass
            if fmt:
                _free(fmt)
            for p in (capture, client, device, enum):
                _release(p)


class SystemAudio:
    """What the PC plays, for a meeting: the default output, plus the default communications
    output when that is another device. Same start/read/stop/alive/pauses as Loopback."""

    def __init__(self) -> None:
        roles = capture_roles(endpoint_id(CONSOLE), endpoint_id(COMMUNICATIONS))
        # shortcut: devices are chosen at start; if the user later makes both defaults the same
        # device, it is heard twice (louder, still aligned). Upgrade: re-check on device change.
        self.parts = [Loopback(role) for role in roles]
        self._carry = [np.zeros(0, np.float32) for _ in roles]
        log.info("system audio: %s", "console + communications" if len(roles) > 1 else "console")

    @property
    def alive(self) -> bool:
        return any(p.alive for p in self.parts)

    @property
    def pauses(self) -> list[tuple[int, float]]:
        return self.parts[0].pauses

    def start(self) -> None:
        for p in self.parts:
            p.start()

    def read(self) -> np.ndarray:
        if len(self.parts) == 1:
            return self.parts[0].read()
        out, self._carry = mix_aligned(self._carry, [p.read() for p in self.parts])
        return out

    def stop(self) -> None:
        for p in self.parts:
            p.stop()


def main(argv: list[str]) -> int:
    """`python -m biraflow.loopback SECONDS out.wav`: record what the PC plays, print per-second RMS."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    seconds, out = float(argv[0]), argv[1]
    lb = Loopback()
    lb.start()
    time.sleep(seconds)
    lb.stop()
    x = lb.read()
    audio.write_wav(out, x)
    per_sec = [audio.rms(x[i:i + audio.RATE]) for i in range(0, x.size, audio.RATE)]
    print(f"{out}: {x.size / audio.RATE:.2f} s, pico RMS {audio.peak_rms(x):.4f}, RMS {audio.rms(x):.5f}")
    print("RMS por segundo: " + " ".join(f"{i}s={r:.4f}" for i, r in enumerate(per_sec)))
    return 0 if x.size else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
