"""Microphone capture (16 kHz mono float32) and WAV helpers."""
from __future__ import annotations

import logging
import math
import threading
import time
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RATE = 16_000  # what Whisper expects
# PortAudio is process-wide: its re-initialization (refresh_devices / retry) must never run
# while any stream is open or another thread is listing devices or opening a stream.
_PA = threading.RLock()
_open_streams = 0


def write_wav(path: Path, samples: np.ndarray, rate: int = RATE) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def write_opus(path: Path, samples: np.ndarray, rate: int = RATE) -> None:
    """Ogg/Opus: ~10x smaller than WAV for the same speech, accepted by every Whisper API."""
    import soundfile as sf

    sf.write(str(path), np.clip(samples, -1.0, 1.0), rate, format="OGG", subtype="OPUS")


def read_wav(path: Path) -> np.ndarray:
    """A 16-bit WAV written by write_wav, as float32 samples."""
    with wave.open(str(path)) as w:
        frames = w.readframes(w.getnframes())
        rate, channels = w.getframerate(), w.getnchannels()
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return resample(samples, rate)


def resample(samples: np.ndarray, src: int, dst: int = RATE) -> np.ndarray:
    if src == dst or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    if src % dst == 0:
        # Integer factor (48k/32k -> 16k): box-filter decimation doubles as a low-pass.
        f = src // dst
        n = samples.size // f * f
        return samples[:n].reshape(-1, f).mean(axis=1).astype(np.float32)
    if src > dst:   # low-pass first, or 8–22 kHz sounds ("s", "ch") fold into the speech band
        samples = _lowpass(samples, 0.45 * dst / src)
    n_out = int(round(samples.size * dst / src))
    x_out = np.linspace(0, samples.size - 1, n_out)
    return np.interp(x_out, np.arange(samples.size), samples).astype(np.float32)


def _lowpass(samples: np.ndarray, cutoff: float, taps: int = 63) -> np.ndarray:
    """Windowed-sinc FIR; `cutoff` in cycles per sample (0.5 = Nyquist)."""
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff * n) * np.hamming(taps)
    h /= h.sum()
    return np.convolve(samples, h, mode="same").astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0


def peak_rms(samples: np.ndarray, rate: int = RATE, window: float = 0.05) -> float:
    """Loudest 50 ms window: a quick 'did anyone say anything' check."""
    n = int(rate * window)
    if samples.size < n:
        return rms(samples)
    frames = samples[: samples.size // n * n].reshape(-1, n)
    return float(np.sqrt((frames ** 2).mean(axis=1)).max())


def level_from_rms(rms: float, floor_db: float = -62.0, ceil_db: float = -22.0) -> float:
    """Loudness for the meter, 0..1 on a decibel scale. A linear scale left quiet laptop
    mics (speech around 0.005–0.03 RMS) as an almost flat line."""
    if rms <= 0:
        return 0.0
    db = 20 * math.log10(rms)
    return max(0.0, min(1.0, (db - floor_db) / (ceil_db - floor_db)))


def trim_silence(samples: np.ndarray, rate: int = RATE, pad: float = 0.35, window: float = 0.02) -> np.ndarray:
    """Cut the silence before the first and after the last sound (keeping `pad` seconds):
    less audio to upload, and Whisper invents fewer words ("Obrigado por assistir") in
    trailing silence. Speech in the middle is never touched."""
    n = int(rate * window)
    if samples.size < n * 25:  # under half a second: nothing worth trimming
        return samples
    frames = samples[: samples.size // n * n].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    floor, peak = float(np.percentile(rms, 10)), float(rms.max())
    threshold = max(floor * 2.5, peak * 0.05, 1e-4)
    voiced = np.flatnonzero(rms > threshold)
    if voiced.size == 0:
        return samples
    keep = int(pad * rate)
    start = max(0, int(voiced[0]) * n - keep)
    end = min(samples.size, (int(voiced[-1]) + 1) * n + keep)
    if start + (samples.size - end) < int(0.4 * rate):
        return samples  # not worth it
    return samples[start:end]


def normalize(samples: np.ndarray, target_peak: float = 0.8, max_gain: float = 8.0) -> np.ndarray:
    """Bring quiet speech up to a healthy level (whispering, distant mic). Never
    attenuates, and the gain is capped so background noise is not blown up."""
    if samples.size == 0:
        return samples
    peak = max(float(samples.max()), -float(samples.min()))   # no |x| copy of an hour of audio
    if peak <= 0:
        return samples
    gain = min(max_gain, target_peak / peak)
    if gain <= 1.05:
        return samples
    out = (samples * np.float32(gain)).astype(np.float32, copy=False)
    return np.clip(out, -1.0, 1.0, out=out)


def list_input_devices() -> list[str]:
    import sounddevice as sd

    with _PA:
        names = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
    return list(dict.fromkeys(names))  # each device is listed once per host API


def input_device_index(sd, name: str) -> int | None:
    """The first input device whose name contains `name` (None: not found)."""
    needle = name.lower()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and needle in d["name"].lower():
            return i
    return None


PRE_ROLL = 0.3  # seconds of audio from just before the hotkey, when the mic was kept ready


def open_input_stream(output: bool = False, **kwargs):
    """Open and start a PortAudio input stream (output=True: output stream) that
    _reinitialize() knows about (voice activation keeps one open all the time)."""
    import sounddevice as sd

    global _open_streams
    with _PA:
        stream = (sd.OutputStream if output else sd.InputStream)(**kwargs)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        _open_streams += 1
        return stream


def close_input_stream(stream) -> None:
    global _open_streams
    with _PA:
        try:
            stream.stop()
            stream.close()
        except Exception:
            log.exception("closing input stream")
        _open_streams = max(0, _open_streams - 1)


def _reinitialize() -> bool:
    """Re-read PortAudio's device list (new headset, new default mic). Only when no stream
    is open anywhere in the process; never leaves PortAudio uninitialized."""
    import sounddevice as sd

    with _PA:
        if _open_streams:
            return False
        try:
            sd._terminate()
        except Exception as e:
            log.debug("PortAudio terminate: %s", e)
        for _ in range(2):
            try:
                sd._initialize()
                return True
            except Exception as e:
                log.warning("PortAudio did not initialize: %s", e)
                time.sleep(0.2)
        return False


class Recorder:
    """Opens the mic only while recording, so the Windows mic indicator is honest.

    With `keep_ready` seconds set, the mic stays open that long after a dictation: the
    next one starts instantly and even keeps the 0.3 s before the key press, so its first
    syllable is never cut (opening a mic takes 100–300 ms)."""

    def __init__(self, device: str = "", keep_ready: float = 0.0) -> None:
        self.device = device
        self.keep_ready = keep_ready
        self.missing_device = False   # the configured mic was not found: the default is used
        self._stream = None
        self._recording = False
        self._chunks: list[np.ndarray] = []
        self._ring: list[np.ndarray] = []   # idle audio while the mic is kept ready
        self._rate = RATE
        self._level = 0.0
        self._peak = 0.0
        self._lock = threading.Lock()
        self._idle_timer: threading.Timer | None = None
        self._last_cb = 0.0   # last audio callback: a kept-ready stream that went quiet is dead
        self._refreshed = time.monotonic()
        self._external = False  # recording audio pushed by feed() (voice activation)

    @property
    def level(self) -> float:
        """Recent input level, 0..1 (for the pill's meter)."""
        return self._level

    @property
    def peak(self) -> float:
        """Loudest sample since the recording started: exactly 0 means the mic delivers
        pure digital silence (muted in Windows, privacy switch, wrong device)."""
        return self._peak

    @property
    def active(self) -> bool:
        return self._stream is not None

    def _device_index(self, sd):
        if not self.device:
            return None
        index = input_device_index(sd, self.device)
        self.missing_device = index is None
        if index is None:
            log.warning("Microfone \"%s\" não encontrado; usando o padrão.", self.device)
        return index

    def _callback(self, indata, frames, time_info, status) -> None:
        mono = indata[:, 0] if indata.shape[1] == 1 else indata.mean(axis=1)
        self._last_cb = time.monotonic()
        if not self._recording:  # kept ready: remember only the last moment
            self._ring.append(mono.copy())
            while sum(c.size for c in self._ring) > PRE_ROLL * self._rate * 2 and len(self._ring) > 1:
                self._ring.pop(0)
            return
        self._chunks.append(mono.copy())
        self._level = level_from_rms(rms(mono))
        if self._peak == 0.0:
            self._peak = float(np.max(np.abs(mono))) if mono.size else 0.0

    def _pre_roll(self) -> list[np.ndarray]:
        if not self._ring:
            return []
        audio = np.concatenate(self._ring)[-int(PRE_ROLL * self._rate):]
        self._ring = []
        return [audio]

    def start(self) -> None:
        try:
            self._start()
        except Exception as first:
            log.info("microphone did not open (%s); re-reading the device list", first)
            with self._lock:
                if not _reinitialize():
                    raise first
                self._refreshed = time.monotonic()
            self._start()

    def _start(self) -> None:
        import sounddevice as sd

        with self._lock:
            self._cancel_idle()
            self._external = False
            if self._stream is not None and not self._recording:  # kept ready: instant
                alive = getattr(self._stream, "active", True) and time.monotonic() - self._last_cb < 0.5
                if alive:
                    self._chunks = []
                    self._level = self._peak = 0.0
                    self._recording = True          # from now on the callback records...
                    self._chunks[:0] = self._pre_roll()   # ...after the moment before the key
                    return
                log.info("kept-ready microphone went quiet (unplugged?); reopening it")
            self._close()
            self._chunks = []
            self._level = 0.0
            self._peak = 0.0
            dev = self._device_index(sd)
            info = sd.query_devices(dev, "input")
            native = int(info["default_samplerate"])
            channels = max(1, min(2, int(info["max_input_channels"])))
            last_error: Exception | None = None
            # 16 kHz mono first (no resampling); fall back to the device's native format.
            global _open_streams
            for rate, ch in ((RATE, 1), (native, 1), (native, channels)):
                stream = None
                try:
                    with _PA:
                        stream = sd.InputStream(samplerate=rate, channels=ch, dtype="float32",
                                                device=dev, callback=self._callback)
                        stream.start()
                        _open_streams += 1
                except Exception as e:  # PortAudioError, ValueError
                    last_error = e
                    if stream is not None:  # opened but would not start: don't leak it
                        try:
                            stream.close()
                        except Exception:
                            pass
                    continue
                self._stream, self._rate = stream, rate
                self._last_cb = time.monotonic()
                self._recording = True
                return
            raise RuntimeError(f"não foi possível abrir o microfone ({last_error})")

    def start_external(self, initial: list[np.ndarray] | None = None) -> None:
        """Record audio pushed through feed() instead of opening the mic: voice activation
        already has it open and hands over the audio from the wake phrase onward."""
        with self._lock:
            self._cancel_idle()
            self._close()
            self._chunks = []
            self._rate = RATE
            self._level = self._peak = 0.0
            self._external = self._recording = True
        for chunk in initial or []:
            self.feed(chunk)

    def feed(self, chunk: np.ndarray) -> None:
        if not (self._external and self._recording):
            return
        self._chunks.append(chunk)
        self._level = level_from_rms(rms(chunk))
        if self._peak == 0.0 and chunk.size:
            self._peak = float(np.max(np.abs(chunk)))

    def refresh_devices(self, min_interval: float = 20.0) -> None:
        """PortAudio reads the device list (and which mic is the default) only once: a
        headset plugged in later would be ignored until a restart. Re-read it while idle,
        in the background after a recording, so the next one uses the current default."""
        if time.monotonic() - self._refreshed < min_interval:
            return

        def run() -> None:
            with self._lock:
                if self._stream is not None:
                    return
                _reinitialize()
                self._refreshed = time.monotonic()

        threading.Thread(target=run, name="audio-refresh", daemon=True).start()

    def stop(self) -> np.ndarray:
        with self._lock:
            self._idle_or_close()
            chunks, self._chunks = self._chunks, []
            rate = self._rate
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return resample(np.concatenate(chunks), rate)

    def abort(self) -> None:
        with self._lock:
            self._idle_or_close()
            self._chunks = []

    def close(self) -> None:
        """Close the mic now, even if it was being kept ready (app exit, settings change)."""
        with self._lock:
            self._cancel_idle()
            self._close()

    def _idle_or_close(self) -> None:
        self._recording = False
        self._level = 0.0
        if self._external:
            self._external = False
            return
        if self.keep_ready > 0 and self._stream is not None:
            self._ring = []
            self._cancel_idle()
            self._idle_timer = threading.Timer(self.keep_ready, self._idle_close)
            self._idle_timer.daemon = True
            self._idle_timer.start()
        else:
            self._close()

    def _idle_close(self) -> None:
        with self._lock:
            if not self._recording:
                self._close()

    def _cancel_idle(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _close(self) -> None:
        global _open_streams
        self._recording = False
        self._ring = []
        if self._stream is not None:
            with _PA:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    log.exception("closing input stream")
                _open_streams = max(0, _open_streams - 1)
            self._stream = None
            self._level = 0.0
