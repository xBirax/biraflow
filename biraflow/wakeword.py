"""Voice activation: say "Ei Bira" and dictation starts, like a smart speaker.

Two stages, so the app reacts only to the phrase and stays cheap the rest of the time:

1. Always on, local: Vosk (small Portuguese model, ~2–3 % of one CPU core) listens with a
   grammar restricted to the wake phrase and words that sound like it. The small model
   does not know names like "Bira", so the phrase is spelled with words it does know
   ("bi ra", "vira", "beira"...). That is cheap and catches the phrase, but also "ei,
   vira aqui".
2. Only for those candidates: the last ~2.5 s are checked by Whisper with the phrase as a
   hint, on this PC when the local model is installed (else by the transcription API). Whisper hears "Ei Bira" and keeps "Ei, vira" as it was, so only
   the real call starts a dictation.

When a call is confirmed the listener keeps the audio from the phrase onward and hands
it, followed by the live stream, to the recorder: nothing said right after the phrase is
lost, even without a pause.
"""
from __future__ import annotations

import itertools
import json
import logging
import queue
import re
import shutil
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np

from .audio import RATE, level_from_rms, resample, rms

log = logging.getLogger(__name__)

MODEL_NAME = "vosk-model-small-pt-0.3"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_MB = 32
CHUNK = 1600            # 100 ms at 16 kHz
SNIPPET_S = 2.5         # audio sent for confirmation (the phrase and a little around it)
RING_S = 10.0
COOLDOWN_S = 2.0        # after a candidate, ignore the next ones for a moment
REOPEN_S = 120.0        # re-read the device list this often while idle (a headset plugged in)
MAX_VARIANTS = 40

# first words people use to call ("ei", "hey"...) as the small model hears them
# (only distinctive ones: "e vira" / "o vira" are everyday speech and would wake it all the time)
_GREETING = {"ei": ["ei", "hei"], "hey": ["ei", "hei"], "ok": ["ok"], "oi": ["oi"], "olá": ["olá"]}


def model_dir(paths) -> Path:
    return paths.local / "vosk" / MODEL_NAME


def model_installed(paths) -> bool:
    return (model_dir(paths) / "final.mdl").is_file()


def download_model(paths, progress, cancel: threading.Event) -> Path:
    from .setup_local import unzipped

    dest = model_dir(paths)
    with unzipped(MODEL_URL, paths.tmp_dir / f"{MODEL_NAME}.zip", "modelo de ativação por voz", "final.mdl",
                  "O arquivo baixado não contém um modelo do Vosk.", progress, cancel) as folder:
        shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(dest))
    return dest


# --- the phrase --------------------------------------------------------------------

def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def phrase_words(phrase: str) -> list[str]:
    return re.findall(r"[\wÀ-ÿ]+", phrase.lower())


def _syllables(word: str) -> list[str]:
    """Rough Portuguese syllables ("bira" -> ["bi", "ra"], "kayth" -> ["kay", "th"])."""
    return re.findall(r"[^aeiouáéíóúâêôãõy]*[aeiouáéíóúâêôãõy]+(?:[^aeiouáéíóúâêôãõy]*$)?", word) or [word]


def _sound_alikes(word: str) -> list[str]:
    """Spellings the recognizer may use for `word` (b/v, e/i, o/u, an extra vowel...)."""
    out = {word}
    swaps = [("b", "v"), ("v", "b"), ("i", "e"), ("e", "i"), ("o", "u"), ("u", "o"), ("k", "c"), ("c", "k"),
             ("y", "i"), ("th", "t"), ("i", "ei"), ("ei", "e")]
    for a, b in swaps:
        if a in word:
            out.add(word.replace(a, b, 1))
    out |= {word + "a", word + "o", word + "e", word[:-1] if len(word) > 3 else word}
    return sorted(out)


def variants(phrase: str, known: Callable[[str], bool]) -> list[str]:
    """Phrases made of words the model knows that sound like `phrase`."""
    options = []
    for word in phrase_words(phrase):
        forms = []
        for w in _GREETING.get(word) or _sound_alikes(word):
            if known(w) and w not in forms:
                forms.append(w)
        syl = _syllables(word)
        if len(syl) > 1 and all(known(s) for s in syl):
            forms.append(" ".join(syl))
        if not forms:
            return []
        options.append(forms[:8])
    return [" ".join(combo) for combo in itertools.islice(itertools.product(*options), MAX_VARIANTS)]


# how Whisper may write the greeting ("E Bira", "Hey Bira"); the name itself must match
_SAID_GREETING = {"ei": ("ei", "e", "hei", "hey", "eh"), "hei": ("ei", "e", "hei", "hey", "eh"),
                  "hey": ("ei", "e", "hei", "hey", "eh"), "ok": ("ok", "okay", "oke")}


def confirmed(text: str, phrase: str) -> bool:
    """Did Whisper hear the phrase itself (not "ei, vira")? Spaces and punctuation are
    ignored ("Eibira", "Ei, Bira." both count), and so is how the greeting was spelled."""
    words = _plain(phrase).split()
    if not words:
        return False
    heard = _plain(text).replace(" ", "")
    first = _SAID_GREETING.get(words[0], (words[0],))
    rest = "".join(words[1:])
    return any(g + rest in heard for g in first)


def _unaccented(text: str) -> str:
    """Accents off, one character for one ("Olá" -> "Ola"): a match found here maps back to `text`."""
    return "".join(unicodedata.normalize("NFD", c)[0] for c in text)


def strip_phrase(text: str, phrase: str) -> str:
    """Remove the wake phrase from the start of a transcript, with anything said just
    before it (the audio starts ~2.5 s before the phrase was recognized) and the comma
    after it: "Olha. Ei, Bira, manda o arquivo" -> "Manda o arquivo". Accents are ignored,
    as in confirmed(): Whisper may write "Ola, Flow" for "Olá Flow"."""
    words = [_unaccented(w) for w in phrase_words(phrase)]
    text = text.strip()
    if not words:
        return text
    first = "|".join(re.escape(g) for g in _SAID_GREETING.get(_plain(words[0]).strip(), (words[0],)))
    pattern = r"[\s,.!?]*".join([f"(?:{first})"] + [re.escape(w) for w in words[1:]])
    m = re.search(rf"(?<!\w){pattern}(?!\w)[\s,.!?:;-]*", _unaccented(text[:120]), flags=re.IGNORECASE)
    if not m:
        return text
    out = text[m.end():]
    return out[:1].upper() + out[1:] if out else ""


# --- the listener -------------------------------------------------------------------

class WakeListener:
    """Keeps the microphone open and calls `on_call()` when the phrase is confirmed.

    The controller then calls take_over(sink): the audio from the phrase onward is
    flushed into `sink`, and every new chunk follows until release()."""

    def __init__(self, model_path: Path, phrase: str, device: str = "",
                 confirm: Callable[[np.ndarray], bool] | None = None,
                 on_call: Callable[[], None] | None = None):
        self.model_path = model_path
        self.phrase = phrase.strip()
        self.device = device
        self.confirm = confirm
        self.on_call = on_call or (lambda: None)
        self.busy = False           # set while a dictation runs: no detection then
        self.status = "parado"
        self.last_heard = ""        # what the detector heard last (shown in the settings)
        self.level = 0.0
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream = None
        self._ring: deque = deque()
        self._ring_samples = 0
        self._lock = threading.Lock()
        self._pending: list[np.ndarray] | None = None  # audio of a confirmed call, until take_over
        self._sink: Callable[[np.ndarray], None] | None = None
        self._cooldown_until = 0.0
        self._rate = RATE

    # lifecycle
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wake-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_stream()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)
        self.status = "parado"

    # hand-over to the recorder
    def take_over(self, sink: Callable[[np.ndarray], None]) -> bool:
        with self._lock:
            if self._pending is None:
                return False
            for chunk in self._pending:
                sink(chunk)
            self._pending, self._sink = None, sink
            return True

    def release(self) -> None:
        with self._lock:
            self._pending = self._sink = None

    # internals
    def _open_stream(self):
        import sounddevice as sd

        from .audio import _PA, open_input_stream

        dev = None
        if self.device:
            needle = self.device.lower()
            dev = next((i for i, d in enumerate(sd.query_devices())
                        if d["max_input_channels"] > 0 and needle in d["name"].lower()), None)
        with _PA:
            info = sd.query_devices(dev, "input")
        last = None
        for rate in (RATE, int(info["default_samplerate"])):
            try:
                stream = open_input_stream(samplerate=rate, channels=1, dtype="float32", device=dev,
                                           blocksize=int(rate * CHUNK / RATE), callback=self._callback)
                self._rate = rate
                return stream
            except Exception as e:
                last = e
        raise RuntimeError(f"microfone indisponível ({last})")

    def _close_stream(self) -> None:
        from .audio import close_input_stream

        stream, self._stream = self._stream, None
        if stream is not None:
            close_input_stream(stream)

    def _callback(self, indata, frames, time_info, status) -> None:
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:  # the worker is stuck: drop audio rather than block the driver
            pass

    def _run(self) -> None:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel

            SetLogLevel(-1)
            model = Model(str(self.model_path))
            grammar = variants(self.phrase, lambda w: model.vosk_model_find_word(w) != -1)
            if not grammar:
                self.status = "nome difícil para o detector: escolha outro"
                log.warning("wake phrase %r: no usable spelling in the model", self.phrase)
                return
            targets = [g.split() for g in grammar]
            rec = KaldiRecognizer(model, RATE, json.dumps(grammar + ["[unk]"], ensure_ascii=False))
        except Exception as e:
            self.status = f"erro: {e}"
            log.warning("voice activation unavailable: %s", e)
            return
        log.info("voice activation listening for %r (%d spellings)", self.phrase, len(grammar))
        try:
            self._stream = self._open_stream()
            self.status = "ouvindo"
            reopen_at = time.monotonic() + REOPEN_S
        except Exception as e:   # a headset that connects after logon: keep trying, like a mic lost mid-run
            log.warning("voice activation has no microphone yet: %s", e)
            self.status = "sem microfone (tentando de novo)"
            reopen_at = time.monotonic() + 10.0
        try:
            while not self._stop.is_set():
                if time.monotonic() >= reopen_at:
                    ok = self._refresh_device()
                    reopen_at = time.monotonic() + (REOPEN_S if ok else 10.0)  # mic gone: retry soon
                try:
                    chunk = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self._rate != RATE:
                    chunk = resample(chunk, self._rate)
                self.level = level_from_rms(rms(chunk))
                self._remember(chunk)
                with self._lock:
                    if self._sink is not None:
                        self._sink(chunk)
                        continue
                    if self._pending is not None:
                        self._pending.append(chunk)
                        if sum(c.size for c in self._pending) > RATE * 8:  # nobody took it over
                            self._pending = None
                        continue
                if self.busy:
                    continue
                pcm = (np.clip(chunk, -1, 1) * 32767).astype("<i2").tobytes()
                text = json.loads(rec.Result()).get("text", "") if rec.AcceptWaveform(pcm) \
                    else json.loads(rec.PartialResult()).get("partial", "")
                heard = [w for w in text.split() if w != "[unk]"]
                if heard:
                    self.last_heard = " ".join(heard)
                if heard and self._has_target(text.split(), targets) and time.monotonic() >= self._cooldown_until:
                    self._cooldown_until = time.monotonic() + COOLDOWN_S
                    rec.Reset()
                    self._candidate()
        finally:
            self._close_stream()

    def _refresh_device(self) -> bool:
        """PortAudio only sees a new or new-default microphone after re-reading its device
        list, which cannot happen while this stream is open: close it for a moment."""
        with self._lock:
            if self._pending is not None or self._sink is not None or self.busy:
                return True
        from .audio import _reinitialize

        self._close_stream()
        try:
            _reinitialize()
            self._stream = self._open_stream()
        except Exception as e:
            log.warning("voice activation lost the microphone: %s", e)
            self.status = "sem microfone (tentando de novo)"
            return False
        self.status = "ouvindo"
        return True

    @staticmethod
    def _has_target(words: list[str], targets: list[list[str]]) -> bool:
        for t in targets:
            n = len(t)
            if any(words[i:i + n] == t for i in range(len(words) - n + 1)):
                return True
        return False

    def _remember(self, chunk: np.ndarray) -> None:
        self._ring.append(chunk)
        self._ring_samples += chunk.size
        while self._ring_samples > RATE * RING_S and len(self._ring) > 1:
            self._ring_samples -= self._ring.popleft().size

    def _recent(self, seconds: float) -> np.ndarray:
        need, out = int(RATE * seconds), []
        for chunk in reversed(self._ring):
            out.append(chunk)
            need -= chunk.size
            if need <= 0:
                break
        return np.concatenate(out[::-1]) if out else np.zeros(0, np.float32)

    def _candidate(self) -> None:
        snippet = self._recent(SNIPPET_S)
        with self._lock:
            self._pending = [snippet]   # keep everything from here on while we check
        log.info("wake candidate heard (%r)", self.last_heard)

        def check() -> None:
            ok = True
            if self.confirm is not None:
                try:
                    ok = self.confirm(snippet)
                except Exception as e:
                    log.warning("wake confirmation failed: %s", e)
                    ok = False
            if not ok:
                self.release()
                return
            self.on_call()

        threading.Thread(target=check, name="wake-confirm", daemon=True).start()
