"""Meeting mode (docs/PLANO-REUNIOES.md): record the microphone and what the PC plays, transcribe
it progressively and keep everything in one folder per meeting.

Sources share one small interface: start(), read() (16 kHz mono float32 since the last read),
stop(), `alive` (still delivering audio) and, for the mic, `peak`. The system audio source is
loopback.SystemAudio; MicSource is the microphone.

    MeetingRecorder  sources -> audio/parte-NNN.ogg (stereo Opus: left = mic, right = PC), ~90 s
                     each, cut in a pause, the wall clock keeping both channels aligned
    PieceWorker      each piece -> timed segments labelled "Eu" / "Participantes" by which
                     channel is louder -> segmentos.jsonl -> transcricao.md
    MeetingSession   start/stop of the whole; recover() finishes meetings a crash interrupted
    write_notes      transcricao.md -> notas.md through the LLM (cleanup.Cleaner.meeting_notes)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio, win32
from .cancel import Cancelled, CancelToken
from .config import Paths, Settings, TranscriptionCfg, write_atomic
from .loopback import ALIVE_TIMEOUT, PAUSE_AFTER, Converter

log = logging.getLogger(__name__)

DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"   # FOLDERID_Documents
ME, OTHERS = "Eu", "Participantes"
ROOM = "Sala"            # in-person: one mic hears everyone, nobody can be told apart
ROOM_TURN_GAP = 1.0      # ...so a pause this long starts a new paragraph (likely another voice)
ROOM_MICS = ("grupo de microfones", "matriz de microfones", "conjunto de microfones", "microphone array")
RATE = audio.RATE
PIECE_SECONDS = 90.0     # a crash loses at most this; the end of a meeting waits for at most one piece
CUT_SEARCH = 15.0        # a piece ends in the quietest 300 ms of its last 15 s (talk has few pauses)
CUT_WINDOW = 0.3
CORRECT_AFTER = 0.5      # a source this far off the wall clock is trimmed / padded (devices drift)
MIN_FREE_START = 500e6   # bytes free to start a meeting...
MIN_FREE_RUN = 100e6     # ...and to keep recording
SILENT_MIC_AFTER = 60.0  # a mic giving pure zeros this long is muted or blocked
REOPEN_EVERY = 10.0      # a source that stopped delivering is reopened at most this often
TICK_RETRIES = 30        # ticks in a row that may fail (a file locked for a while) before the recording stops
RETRY_DELAYS = (30.0, 120.0, 300.0)   # a piece that failed is tried again after these
QUIET_RMS = 10 ** (-50 / 20)          # both channels below -50 dBFS: nobody is talking


class MeetingError(RuntimeError):
    pass


def meetings_dir(paths: Paths) -> Path:
    """Documentos\\Bira Flow\\Reuniões, where people look for their notes. A portable or test
    install (one folder for everything: BIRAFLOW_HOME, tests) keeps them in that folder."""
    if paths.data == paths.local:
        return paths.data / "Reuniões"
    return (win32.known_folder(DOCUMENTS) or Path.home() / "Documents") / "Bira Flow" / "Reuniões"


def keep_awake(on: bool) -> None:
    """While a meeting records, the PC must not sleep: a laptop on battery suspends after a
    few idle minutes, and the rest of the meeting would be a "pausa". Per thread."""
    import ctypes

    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except (AttributeError, OSError):   # not Windows
        pass


def room_mic(names: list[str]) -> str:
    """In-person meeting: the laptop's microphone array hears the whole table; a headset (often
    the Windows default) hears only you. "" (the Windows default) when there is none.
    shortcut: matched by name (Windows in pt-BR/en); upgrade: a setting if a PC names it otherwise."""
    return next((n for n in names if any(k in n.lower() for k in ROOM_MICS)), "")


def clock_text(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def duration_text(seconds: float) -> str:
    s = int(round(seconds))
    if s >= 3600:
        return f"{s // 3600} h {s // 60 % 60:02d} min"
    return f"{s // 60} min" if s >= 60 else f"{s} s"


# --- sources ------------------------------------------------------------------------------

class MicSource:
    """The microphone for a whole meeting. Opens 16 kHz mono when the device accepts it, else
    its native format, converted without seams between blocks (loopback.Converter)."""

    def __init__(self, device: str = "") -> None:
        self.device = device
        self.peak = 0.0          # loudest sample so far: exactly 0 means a muted or blocked mic
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream = None
        self._conv: Converter | None = None
        self._last = 0.0

    @property
    def alive(self) -> bool:
        return self._stream is not None and time.monotonic() - self._last < ALIVE_TIMEOUT

    def start(self) -> None:
        if win32.microphone_blocked():
            raise MeetingError(win32.MIC_BLOCKED)
        import sounddevice as sd

        index = audio.input_device_index(sd, self.device) if self.device else None
        if self.device and index is None:
            log.warning("meeting mic \"%s\" not found; using the default", self.device)
        info = sd.query_devices(index, "input")
        native, channels = int(info["default_samplerate"]), max(1, min(2, int(info["max_input_channels"])))
        error: Exception | None = None
        for rate, ch in ((audio.RATE, 1), (native, 1), (native, channels)):
            try:
                self._stream = audio.open_input_stream(samplerate=rate, channels=ch, dtype="float32",
                                                       device=index, callback=self._callback,
                                                       blocksize=rate // 10)   # 100 ms blocks: less CPU
            except Exception as e:   # PortAudioError, ValueError: try the next format
                error = e
                continue
            self._conv = None if rate == audio.RATE else Converter(rate)
            self._last = time.monotonic()
            log.info("meeting mic: %d Hz, %d ch", rate, ch)
            return
        raise MeetingError(f"Não foi possível abrir o microfone ({error}).")

    def _callback(self, indata, frames, time_info, status) -> None:
        mono = indata[:, 0].copy() if indata.shape[1] == 1 else indata.mean(axis=1)
        with self._lock:
            self._chunks.append(mono)
        self._last = time.monotonic()
        if mono.size:
            self.peak = max(self.peak, float(np.abs(mono).max()))

    def read(self) -> np.ndarray:
        with self._lock:
            chunks, self._chunks = self._chunks, []
        x = np.concatenate(chunks).astype(np.float32, copy=False) if chunks else np.zeros(0, np.float32)
        return self._conv.push(x) if self._conv is not None else x

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            audio.close_input_stream(stream)


# --- the meeting folder -------------------------------------------------------------------

class MeetingFolder:
    """One meeting on disk: reuniao.json (state), audio/parte-NNN.ogg, segmentos.jsonl (one
    segment or pause per line) and transcricao.md rendered from it. Thread-safe."""

    VERSION = 1

    def __init__(self, folder: Path, state: dict) -> None:
        self.folder = folder
        self.state = state
        self._lock = threading.RLock()

    @classmethod
    def create(cls, base: Path, now: datetime | None = None) -> "MeetingFolder":
        now = now or datetime.now().astimezone()
        name = now.strftime("%Y-%m-%d_%H%M")
        folder, n = base / name, 2
        while folder.exists():             # two meetings in the same minute
            folder, n = base / f"{name}-{n}", n + 1
        (folder / "audio").mkdir(parents=True)
        m = cls(folder, {"version": cls.VERSION, "started": now.isoformat(timespec="seconds"), "ended": None,
                         "status": "gravando", "pieces": []})
        m.save()
        return m

    @classmethod
    def open(cls, folder: Path) -> "MeetingFolder | None":
        try:
            state = json.loads((folder / "reuniao.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("meeting %s unreadable: %s", folder.name, e)
            return None
        if state.get("version") != cls.VERSION:
            log.warning("meeting %s has version %s; left alone", folder.name, state.get("version"))
            return None
        return cls(folder, state)

    @property
    def transcript(self) -> Path:
        return self.folder / "transcricao.md"

    @property
    def seconds(self) -> float:
        return sum(p["seconds"] for p in self.state["pieces"])

    def save(self) -> None:
        with self._lock:
            write_atomic(self.folder / "reuniao.json", json.dumps(self.state, ensure_ascii=False, indent=1))

    def set(self, **fields) -> None:
        with self._lock:
            self.state.update(fields)
            self.save()

    def add_piece(self, start: float, seconds: float) -> int:
        with self._lock:
            index = len(self.state["pieces"])
            self.state["pieces"].append({"file": f"audio/parte-{index:03d}.ogg", "start": round(start, 3),
                                         "seconds": round(seconds, 3), "status": "pendente", "tries": 0})
            return index

    def piece(self, index: int) -> dict:
        with self._lock:
            return dict(self.state["pieces"][index])

    def set_piece(self, index: int, **fields) -> None:
        with self._lock:
            self.state["pieces"][index].update(fields)
            self.save()

    def append(self, rows: list[dict]) -> None:
        """Add segments / pauses (one write, so a piece's rows land together) and re-render."""
        with self._lock:
            if rows:
                path = self.folder / "segmentos.jsonl"
                torn = path.is_file() and path.stat().st_size and not path.read_bytes().endswith(b"\n")
                with open(path, "a", encoding="utf-8") as f:
                    f.write(("\n" if torn else "") + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            self.render()

    def rows(self) -> list[dict]:
        """Segments and pauses in time order. A torn last line (crash) is skipped, and a piece
        transcribed twice (crash between its rows and its status) keeps only its last run."""
        try:
            lines = (self.folder / "segmentos.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        last_run = {r["piece"]: r["run"] for r in rows if "piece" in r}
        rows = [r for r in rows if "piece" not in r or r["run"] == last_run[r["piece"]]]
        return sorted(rows, key=lambda r: r.get("t0", r.get("t", 0.0)))

    def render(self) -> str:
        with self._lock:
            return self._render()

    def _render(self) -> str:
        started = datetime.fromisoformat(self.state["started"])
        paragraphs = [f"# Reunião — {started:%d/%m/%Y %H:%M} ({duration_text(self.seconds)})"]
        who, last = None, 0.0
        for r in self.rows():
            if "pause" in r:
                paragraphs.append(f"*[{clock_text(r['t'])}] pausa de {duration_text(r['pause'])} (PC suspenso)*")
                who = None
            elif r["who"] == who and (who != ROOM or r["t0"] - last < ROOM_TURN_GAP):
                paragraphs[-1] += " " + r["text"]
            else:
                paragraphs.append(f"**[{clock_text(r['t0'])}] {r['who']}:** {r['text']}")
                who = r["who"]
            last = r.get("t1", last)
        text = "\n\n".join(paragraphs) + "\n"
        write_atomic(self.transcript, text)
        return text


# --- recording ----------------------------------------------------------------------------

def cut_point(mix: np.ndarray, target: int, search: int = int(CUT_SEARCH * RATE),
              window: int = int(CUT_WINDOW * RATE)) -> int:
    """Where a piece of about `target` samples ends: the middle of the quietest `window` in
    the `search` samples before `target`, so no word is cut in half."""
    lo = max(0, target - search)
    seg = mix[lo:target].astype(np.float64)
    if seg.size <= window:
        return target
    energy = np.concatenate(([0.0], np.cumsum(seg ** 2)))
    sums = energy[window:] - energy[:-window]
    if sums.min() > 0.5 * np.median(sums):   # no real pause in sight: keep the piece full length
        return target
    return lo + int(np.argmin(sums)) + window // 2


@dataclass
class Piece:
    index: int
    start: float
    seconds: float


class MeetingRecorder:
    """Every `tick` seconds: read both sources, keep them on the wall clock, and write each
    ~90 s as a stereo Opus piece (left = mic, right = PC), handed to `on_piece`."""

    def __init__(self, meeting: MeetingFolder, mic, system, on_piece: Callable[[Piece], None],
                 piece_seconds: float = PIECE_SECONDS, clock: Callable[[], float] = time.monotonic,
                 tick: float = 1.0, on_warning: Callable[[str], None] = lambda text: None,
                 max_seconds: float | None = None, on_limit: Callable[[], None] = lambda: None) -> None:
        self.meeting = meeting
        self.sources = [mic, system]           # system None: the right channel is silence
        self.on_piece, self.on_warning, self.on_limit = on_piece, on_warning, on_limit
        self.piece_samples = int(piece_seconds * RATE)
        self.max_seconds = max_seconds
        self.clock, self.tick_seconds = clock, tick
        self._bufs = [np.zeros(0, np.float32), np.zeros(0, np.float32)]
        self._counts = [0, 0]                  # samples each channel has had since the start
        self._written = 0                      # samples already in pieces
        self._warned: set[str] = set()
        self._reopened: dict[int, float] = {}   # source -> last reopen attempt (clock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = self._last = 0.0
        self.error: Exception | None = None

    @property
    def elapsed(self) -> float:
        return self._counts[0] / RATE

    def start(self) -> None:
        for s in self.sources:
            if s is not None:
                s.start()
        self._t0 = self._last = self.clock()
        self._thread = threading.Thread(target=self._run, name="meeting-rec", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        keep_awake(True)
        try:
            self._loop()
        finally:
            keep_awake(False)

    def _loop(self) -> None:
        failures = 0
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
                failures = 0
            except Exception as e:   # disk full, a source that broke: keep what is written
                failures += 1
                if not isinstance(e, MeetingError) and failures < TICK_RETRIES:
                    # a piece file locked for a moment: the audio stays in the buffer for the next tick
                    log.warning("meeting tick failed (%d in a row): %s", failures, e)
                    continue
                log.exception("meeting recording stopped")
                self.error = e
                for src in self.sources:
                    if src is not None:
                        try:
                            src.stop()
                        except Exception:
                            log.exception("stopping a meeting source")
                self.on_warning(f"A gravação da reunião parou: {e}")
                return

    def stop(self) -> None:
        """Stop the sources and write what is left as the last piece."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(15)
        try:
            if self.error is None and not (self._thread and self._thread.is_alive()):
                self.tick()
                n = max(b.size for b in self._bufs)
                if n >= RATE // 2:
                    self._bufs = [np.pad(b, (0, n - b.size)) for b in self._bufs]
                    self._write(n)
        finally:
            for s in self.sources:
                if s is not None:
                    s.stop()

    def tick(self) -> None:
        now = self.clock()
        reads = [s.read() if s is not None else np.zeros(0, np.float32) for s in self.sources]
        gap = now - self._last - self.tick_seconds
        # The PC slept: a pause, not hours of silence. A stalled thread is no pause: the
        # devices kept the audio meanwhile, and trimming it "back to the clock" would lose it.
        if now - self._last > PAUSE_AFTER and max(x.size for x in reads) < gap * RATE / 2:
            self._t0 += gap
            log.info("meeting: %.0f s pause (PC asleep?)", gap)
            try:
                self.meeting.append([{"pause": round(gap), "t": round((self._written + self._bufs[0].size) / RATE, 2)}])
            except OSError:   # transcricao.md open in an editor: the pause is only a marker, the audio comes first
                log.exception("writing the pause row")
        self._last = now
        for i, x in enumerate(reads):
            self._bufs[i] = np.concatenate((self._bufs[i], x))
            self._counts[i] += x.size
        self._keep_on_clock(now)
        self._check_sources(now)
        while min(b.size for b in self._bufs) >= self.piece_samples:
            mix = self._bufs[0][:self.piece_samples] + self._bufs[1][:self.piece_samples]
            self._write(cut_point(mix, self.piece_samples))
        if self.max_seconds and self.elapsed >= self.max_seconds and "limit" not in self._warned:
            self._warned.add("limit")
            self.on_limit()

    def _keep_on_clock(self, now: float) -> None:
        """Sound devices run on their own clocks (~0.4 s/h apart) and a dead source delivers
        nothing: trim or pad each channel so both follow the wall clock."""
        expected = int((now - self._t0) * RATE)
        for i in range(2):
            off = self._counts[i] - expected
            if off > CORRECT_AFTER * RATE:
                cut = min(off, self._bufs[i].size)
                self._bufs[i] = self._bufs[i][:self._bufs[i].size - cut]
                self._counts[i] -= cut
            elif off < -CORRECT_AFTER * RATE:
                self._bufs[i] = np.concatenate((self._bufs[i], np.zeros(-off, np.float32)))
                self._counts[i] -= off

    def _check_sources(self, now: float) -> None:
        mic = self.sources[0]
        if (getattr(mic, "peak", 1.0) == 0.0 and now - self._t0 > SILENT_MIC_AFTER
                and "mic" not in self._warned):
            self._warned.add("mic")
            self.on_warning("O microfone não está mandando som (mudo?)."
                            + (" A reunião continua gravando o PC." if self.sources[1] is not None else ""))
        for i, s in enumerate(self.sources):
            if s is None:
                continue
            if s.alive:
                self._warned.discard(f"dead{i}")
                self._reopened.pop(i, None)
                continue
            if now - self._reopened.get(i, -REOPEN_EVERY) < REOPEN_EVERY:
                continue
            failed = i in self._reopened   # reopened 10 s ago and still silent (a loopback's start never raises)
            self._reopened[i] = now   # device unplugged and back, PC woke up: reopen, again every 10 s
            log.info("meeting: source %d stopped delivering audio; reopening it", i)
            try:
                s.stop()
                s.start()
            except Exception as e:
                log.warning("meeting: source %d did not reopen: %s", i, e)
                failed = True
            if failed and f"dead{i}" not in self._warned:
                self._warned.add(f"dead{i}")
                self.on_warning("O microfone parou." if i == 0 else "O áudio do PC parou de chegar.")

    def _write(self, n: int) -> None:
        import soundfile as sf

        if shutil.disk_usage(self.meeting.folder).free < MIN_FREE_RUN:
            raise MeetingError("Disco quase cheio: a gravação da reunião foi encerrada.")
        stereo = np.stack([self._bufs[0][:n], self._bufs[1][:n]], axis=1)
        file = self.meeting.folder / f"audio/parte-{len(self.meeting.state['pieces']):03d}.ogg"
        sf.write(str(file), np.clip(stereo, -1, 1), RATE, format="OGG", subtype="OPUS")
        index = self.meeting.add_piece(self._written / RATE, n / RATE)
        try:
            self.meeting.save()
        except OSError:   # the piece is in the state and its audio on disk: the next save writes it
            log.exception("saving the meeting state")
        self._bufs = [b[n:] for b in self._bufs]
        self._written += n
        self.on_piece(Piece(index, (self._written - n) / RATE, n / RATE))


# --- transcription ------------------------------------------------------------------------

def collapse_repeats(text: str) -> str:
    """Whisper stuck in a loop ("vamos lá vamos lá vamos lá vamos lá..."): keep one."""
    return re.sub(r"(\b.{3,80}?)(?:[\s,.;]*\1){3,}", r"\1", text)


def clean_segments(segments, seconds: float):
    """Drop what Whisper itself flags as non-speech, undo repetition loops, keep times inside
    the piece."""
    from .cleanup import sanitize
    from .transcribe import Segment

    out = []
    for s in segments:
        if s.no_speech > 0.6 and s.logprob < -1.0:
            continue
        text = sanitize(collapse_repeats(s.text) if s.ratio > 2.4 else s.text)
        if text:
            start = min(max(0.0, s.start), seconds)
            out.append(Segment(start, min(max(start, s.end), seconds), text))
    return out


def peaks(mic: np.ndarray, system: np.ndarray) -> tuple[float, float]:
    return (float(np.abs(mic).max()) if mic.size else 0.0, float(np.abs(system).max()) if system.size else 0.0)


def label(mic: np.ndarray, system: np.ndarray, t0: float, t1: float, previous: str,
          piece_peaks: tuple[float, float] | None = None) -> str:
    """Who spoke in [t0, t1]: the louder channel, each measured against its own peak in the
    piece (`piece_peaks`, computed once per piece by the caller: ~0.15 s per piece otherwise).
    Both near silence: whoever spoke before.
    shortcut: without a headset the mic also hears the speakers and "Eu" can be wrong;
    upgrade: subtract the PC channel's echo, or a diarizing model."""
    a, b = int(t0 * RATE), max(int(t0 * RATE) + 1, int(t1 * RATE))
    m, s = mic[a:b], system[a:b]
    rms_m, rms_s = audio.rms(m), audio.rms(s)
    if rms_m < QUIET_RMS and rms_s < QUIET_RMS:
        return previous
    peak_m, peak_s = piece_peaks or peaks(mic, system)
    return OTHERS if rms_s / (peak_s or 1.0) > rms_m / (peak_m or 1.0) else ME


class PieceWorker:
    """Transcribes pieces in order, in its own thread. A failed piece is retried after
    RETRY_DELAYS, then marked "falhou"; dictations go first (`busy`)."""

    def __init__(self, meeting: MeetingFolder, cfg: TranscriptionCfg, env: dict[str, str], paths: Paths,
                 busy: Callable[[], bool] = lambda: False, silence_threshold: float = 0.003,
                 clock: Callable[[], float] = time.monotonic,
                 on_notice: Callable[[str], None] = lambda text: None) -> None:
        self.meeting, self.cfg, self.env, self.paths = meeting, cfg, env, paths
        self.busy, self.silence_threshold, self.clock, self.on_notice = busy, silence_threshold, clock, on_notice
        self.token = CancelToken()
        self._queue: list[tuple[float, int]] = []   # (when, piece index)
        self._cond = threading.Condition()
        self._closing = False
        self._working = False
        self._thread: threading.Thread | None = None
        self._context = ""
        self._who = ME

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="meeting-tx", daemon=True)
        self._thread.start()

    def submit(self, piece: Piece | int) -> None:
        with self._cond:
            self._queue.append((self.clock(), piece if isinstance(piece, int) else piece.index))
            self._cond.notify()

    def pending(self) -> int:
        with self._cond:
            return len(self._queue) + self._working

    def finish(self, timeout: float = 60.0) -> int:
        """Try every queued piece now (retries included) for up to `timeout`; returns how many
        are still not transcribed."""
        with self._cond:
            self._closing = True
            self._queue = [(0.0, i) for _, i in self._queue]
            self._cond.notify()
        if self._thread is not None:
            self._thread.join(timeout)
        left = self.pending()
        self.token.cancel()
        return left

    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    if (not self._queue and self._closing) or self.token.cancelled:
                        return
                    due = min(self._queue)[0] if self._queue else None
                    if due is not None and due <= self.clock():
                        item = min(self._queue)
                        self._queue.remove(item)
                        self._working = True
                        break
                    self._cond.wait(0.5 if due is None else min(0.5, max(0.01, due - self.clock())))
            try:
                while self.busy() and not self._closing:   # a dictation is being transcribed: it goes first
                    time.sleep(0.2)
                self.process(item[1])
            except Exception:   # never die: every later piece would stay "pendente" until recover()
                log.exception("meeting piece %s", item[1])
            finally:
                with self._cond:
                    self._working = False

    def process(self, index: int, retry: bool = True) -> None:
        """Transcribe one piece; on failure schedule a retry (or mark it "falhou")."""
        try:
            self._transcribe(index)
        except Cancelled:
            return   # the meeting ended before this piece: it stays "pendente" for recover()
        except Exception as e:
            tries = self.meeting.piece(index)["tries"] + 1
            again = retry and not self._closing and tries <= len(RETRY_DELAYS)
            # offline (requests' errors, wrapped in TranscriptionError): not a bad piece. A local
            # OSError (disk full, no permission) is: it must end as "falhou", not be retried forever.
            import requests

            network = any(isinstance(x, (requests.RequestException, ConnectionError, TimeoutError))
                          for x in (e, e.__cause__))
            self.meeting.set_piece(index, tries=tries, status="pendente" if again or network else "falhou")
            log.warning("meeting piece %d failed (try %d): %s", index, tries, e)
            if again or (network and retry and not self._closing):   # offline: keep trying every 5 min
                with self._cond:
                    self._queue.append((self.clock() + RETRY_DELAYS[min(tries, len(RETRY_DELAYS)) - 1], index))

    def _transcribe(self, index: int) -> None:
        import soundfile as sf

        from . import audiofile, transcribe as tr

        piece = self.meeting.piece(index)
        stereo, _ = sf.read(str(self.meeting.folder / piece["file"]), dtype="float32", always_2d=True)
        mic = stereo[:, 0]
        system = stereo[:, 1] if stereo.shape[1] > 1 else np.zeros_like(mic)
        mix = np.clip((audio.normalize(mic) + audio.normalize(system)) * 0.7, -1.0, 1.0).astype(np.float32)
        # nobody spoke: nothing to send, nothing invented. Measured before normalize(), which
        # lifts a quiet room's noise up to 8x.
        if max(audio.peak_rms(mic), audio.peak_rms(system)) < self.silence_threshold:
            self.meeting.set_piece(index, status="vazia", tries=piece["tries"] + 1)
            return
        # rec-*: clean_tmp removes leftovers. Unique: recover(), "Tentar de novo" and the live
        # meeting may work on the same piece number at once.
        stem = self.paths.tmp_dir / f"rec-meeting-{os.getpid()}-{index:03d}-{time.monotonic_ns()}"
        wav, ogg = stem.with_suffix(".wav"), None
        notices: list[str] = []
        try:
            self.paths.tmp_dir.mkdir(parents=True, exist_ok=True)
            audio.write_wav(wav, mix)
            engine = tr.resolve_engine(self.cfg, self.env)
            if engine != "local":
                ogg = stem.with_suffix(".ogg")
                audio.write_opus(ogg, mix)
            # The previous text as Whisper's prompt makes the APIs merge whole turns into one
            # segment (measured 18/10/2026: up to 24.5 s covering two speakers), which throws the
            # speaker label and the timing off: with the prompt the meeting gave 78.7 % of the
            # labels right, without it 97.5 %. whisper.cpp keeps splitting with the same prompt,
            # so the local engine goes on using it. The vocabulary hint always goes.
            context = self._context if engine == "local" else ""
            t0 = time.monotonic()
            segments = tr.transcribe_segments(wav, replace(self.cfg, context=context), self.env, self.paths,
                                              self.token, notices, upload=ogg)
        finally:
            wav.unlink(missing_ok=True)
            if ogg is not None:
                ogg.unlink(missing_ok=True)
        if notices:
            self.on_notice(notices[0])
        run = time.time_ns()
        rows = []
        piece_peaks = peaks(mic, system)
        for s in clean_segments(segments, piece["seconds"]):
            self._who = ROOM if self.meeting.state.get("in_person") else label(mic, system, s.start, s.end,
                                                                                 self._who, piece_peaks)
            rows.append({"t0": round(piece["start"] + s.start, 2), "t1": round(piece["start"] + s.end, 2),
                         "who": self._who, "text": audiofile.finish_text(s.text, self.paths),
                         "piece": index, "run": run})
        self.meeting.append(rows)
        self.meeting.set_piece(index, status="feita", tries=piece["tries"] + 1,
                               engine="local" if notices or engine == "local" else engine)
        if rows:
            self._context = " ".join(r["text"] for r in rows)[-200:]
        log.info("meeting piece %d: %.0f s in %.1f s, %d segments, %d chars", index, piece["seconds"],
                 time.monotonic() - t0, len(rows), sum(len(r["text"]) for r in rows))


# --- a whole meeting ----------------------------------------------------------------------

def _transcription_cfg(settings: Settings, paths: Paths) -> TranscriptionCfg:
    from . import audiofile

    cfg = settings.transcription
    return replace(cfg, vocabulary=audiofile.hints(cfg.vocabulary, paths), context="")


class MeetingSession:
    """One meeting: start() opens the sources and the folder; stop() writes the last piece,
    waits for its transcription and leaves transcricao.md ready. With auto_notes it marks the
    notes as due ("notes_pending"): the caller writes them (write_notes) after showing the
    transcript, and recover() does it if the app quits first."""

    def __init__(self, paths: Paths, settings: Settings, env: dict[str, str], *, system_audio: bool = True,
                 keep_audio: bool = True, auto_notes: bool = True, max_hours: float = 4.0,
                 busy: Callable[[], bool] = lambda: False,
                 on_warning: Callable[[str], None] = lambda text: None, on_limit: Callable[[], None] = lambda: None,
                 sources: tuple | None = None, clock: Callable[[], float] = time.monotonic,
                 tick: float = 1.0, in_person: bool = False) -> None:
        """`in_person`: everyone in the room, one microphone: the room's mic (room_mic), no PC
        audio, every line labelled ROOM."""
        self.paths, self.settings, self.env = paths, settings, env
        self.in_person = in_person
        self.mic_device = settings.recording.input_device
        self.system_audio, self.keep_audio, self.max_hours = system_audio, keep_audio, max_hours
        self.auto_notes = auto_notes
        self.busy, self.on_warning, self.on_limit = busy, on_warning, on_limit
        self._sources, self.clock, self.tick = sources, clock, tick
        self.meeting: MeetingFolder | None = None
        self.recorder: MeetingRecorder | None = None
        self.worker: PieceWorker | None = None

    @property
    def elapsed(self) -> float:
        return self.recorder.elapsed if self.recorder else 0.0

    def start(self) -> MeetingFolder:
        base = meetings_dir(self.paths)
        base.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(base).free < MIN_FREE_START:
            raise MeetingError("Pouco espaço em disco (menos de 500 MB livres) para gravar uma reunião.")
        if self._sources is not None:
            mic, system = self._sources
        else:
            from .loopback import SystemAudio

            if self.in_person:
                self.mic_device = room_mic(audio.list_input_devices()) or self.mic_device
            mic = MicSource(self.mic_device)
            system = SystemAudio() if self.system_audio and not self.in_person else None
        self.meeting = MeetingFolder.create(base)
        if self.in_person:
            self.meeting.set(in_person=True)
        self.worker = PieceWorker(self.meeting, _transcription_cfg(self.settings, self.paths), self.env,
                                  self.paths, self.busy, self.settings.recording.silence_threshold,
                                  on_notice=lambda text: log.info("meeting: %s", text))
        self.recorder = MeetingRecorder(self.meeting, mic, system, self.worker.submit, clock=self.clock,
                                        tick=self.tick, on_warning=self.on_warning,
                                        max_seconds=self.max_hours * 3600, on_limit=self.on_limit)
        self.worker.start()
        try:
            self.recorder.start()
        except Exception:
            self.worker.finish(0)
            for s in (mic, system):
                if s is not None:
                    s.stop()
            shutil.rmtree(self.meeting.folder, ignore_errors=True)   # nothing was recorded
            raise
        log.info("meeting started: %s", self.meeting.folder.name)
        return self.meeting

    def stop(self, wait: float = 60.0) -> MeetingFolder:
        assert self.meeting and self.recorder and self.worker
        try:
            self.recorder.stop()
        finally:   # even if the last piece could not be written: close the worker, save the state
            self.meeting.set(status="transcrevendo")
            left = self.worker.finish(wait)
            finish(self.meeting, self.keep_audio, interrupted=bool(left))
            if self.auto_notes and self.meeting.state["status"] == "pronta":   # interrupted: when recovered
                self.meeting.set(notes_pending=True)
        log.info("meeting stopped: %s, %.0f s, %d piece(s) left", self.meeting.folder.name, self.meeting.seconds, left)
        return self.meeting


def notes_input(meeting: MeetingFolder) -> str:
    """The transcript for the LLM: one line per segment, each with its time and speaker, so a
    block cut anywhere still says who spoke when."""
    lines = []
    for r in meeting.rows():
        if "pause" in r:
            lines.append(f"[{clock_text(r['t'])}] (pausa de {duration_text(r['pause'])})")
        else:
            lines.append(f"[{clock_text(r['t0'])}] {r['who']}: {r['text']}")
    return "\n".join(lines)


def write_notes(meeting: MeetingFolder, settings: Settings, env: dict[str, str],
                token: CancelToken | None = None) -> Path | None:
    """notas.md from the transcript. Never raises for an LLM or disk failure: the error goes to
    reuniao.json ("notes_error") and the transcript stays as it is."""
    from .cleanup import MEETING_PROMPT_VERSION, Cleaner, CleanupError

    if not any("text" in r for r in meeting.rows()):
        log.info("meeting %s: nothing said, no notes", meeting.folder.name)
        meeting.set(notes_pending=False)
        return None
    path = meeting.folder / "notas.md"
    try:
        notes = Cleaner(settings.cleanup, env, settings.transcription.vocabulary).meeting_notes(
            notes_input(meeting), token or CancelToken())
        write_atomic(path, notes + "\n")
    except (CleanupError, OSError) as e:
        log.warning("meeting %s: no notes (%s)", meeting.folder.name, type(e).__name__)
        try:
            meeting.set(notes_error=str(e), notes_pending=False)
        except OSError:
            pass
        return None
    meeting.set(prompt_version=MEETING_PROMPT_VERSION, notes_error=None, notes_pending=False)
    log.info("meeting %s: notes written (%d chars)", meeting.folder.name, len(notes))
    return path


def finish(meeting: MeetingFolder, keep_audio: bool, interrupted: bool = False) -> None:
    """Final state; the audio goes only once every piece has its text."""
    pieces = meeting.state["pieces"]
    done = all(p["status"] in ("feita", "vazia") for p in pieces)
    interrupted = interrupted or any(p["status"] == "pendente" for p in pieces)
    meeting.set(status="interrompida" if interrupted else "pronta",
                ended=meeting.state["ended"] or datetime.now().astimezone().isoformat(timespec="seconds"))
    if done and not keep_audio:
        shutil.rmtree(meeting.folder / "audio", ignore_errors=True)
    meeting.render()


def retry_failed(meeting: MeetingFolder, paths: Paths, settings: Settings, env: dict[str, str],
                 keep_audio: bool = True) -> int:
    """"Tentar de novo": the pieces that failed (or were left pending) are transcribed once more.
    Returns how many are still not transcribed."""
    worker = PieceWorker(meeting, _transcription_cfg(settings, paths), env, paths,
                         silence_threshold=settings.recording.silence_threshold)
    for i, p in enumerate(meeting.state["pieces"]):
        if p["status"] in ("falhou", "pendente"):
            worker.process(i, retry=False)
    finish(meeting, keep_audio)
    return sum(p["status"] in ("falhou", "pendente") for p in meeting.state["pieces"])


def recover(paths: Paths, settings: Settings, env: dict[str, str], keep_audio: bool = True,
            auto_notes: bool = True) -> list[MeetingFolder]:
    """At startup: meetings a crash or a quit left unfinished get their pending pieces
    transcribed now (each tried once), their transcript written and (auto_notes) their notes."""
    out = []
    base = meetings_dir(paths)
    for state in sorted(base.glob("*/reuniao.json")) if base.is_dir() else []:
        m = MeetingFolder.open(state.parent)
        if m is not None and m.state.get("status") == "pronta" and m.state.get("notes_pending"):
            if auto_notes:   # the app quit while the notes were being written
                write_notes(m, settings, env)
                out.append(m)
            continue
        if m is None or m.state.get("status") not in ("gravando", "transcrevendo", "interrompida"):
            continue
        worker = PieceWorker(m, _transcription_cfg(settings, paths), env, paths,
                             silence_threshold=settings.recording.silence_threshold)
        if m.state.get("status") == "gravando" and not m.state.get("ended"):   # crashed: not "now"
            end = datetime.fromisoformat(m.state["started"]) + timedelta(seconds=m.seconds)
            m.state["ended"] = end.isoformat(timespec="seconds")
        for i, p in enumerate(m.state["pieces"]):
            if p["status"] == "pendente":
                worker.process(i, retry=False)
        finish(m, keep_audio)
        if auto_notes and m.state["status"] == "pronta" and not (m.folder / "notas.md").exists():
            write_notes(m, settings, env)
        log.info("meeting %s recovered", m.folder.name)
        out.append(m)
    return out
