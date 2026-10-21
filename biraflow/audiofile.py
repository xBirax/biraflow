"""Transcribe audio files: WhatsApp voice notes (.ogg/.opus), recordings, meetings.

With an API engine the file goes up as it is (Ogg/Opus, MP3, M4A... are accepted, and a
voice note is a few hundred KB); long files are cut into 10-minute pieces first. The
local engine, or the fallback when the API fails, needs 16 kHz WAV: Ogg/Opus, MP3, FLAC
and WAV are decoded here (libsndfile); M4A/AAC only go through the API.

transcribe_long() is also the path of long dictations: one audio, any length, any engine.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from .audio import RATE, resample, write_opus, write_wav
from .cancel import CancelToken
from .config import Paths, Settings, TranscriptionCfg

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = (".ogg", ".opus", ".oga", ".mp3", ".m4a", ".wav", ".flac", ".webm", ".mp4", ".aac", ".mpga")
DECODABLE = (".ogg", ".opus", ".oga", ".mp3", ".wav", ".flac")   # read locally by libsndfile
API_MAX_BYTES = 24 * 1024 * 1024        # Groq / OpenAI upload limit is 25 MB
CHUNK_SECONDS = 600                     # long recordings go up in 10-minute pieces
OPUS_AHEAD = 2                          # Opus encoders running at once (every piece is queued up front)
SHORT_SECONDS = 60                      # up to this, a dictation takes the plain WAV path
WAV_UPLOAD_MAX = 5 * 1024 * 1024        # a bigger WAV/FLAC goes up as Opus pieces (a 9.6 MB WAV failed to upload)
DECODE_BLOCK = 60                       # seconds decoded at a time: a 1 h MP3 read whole took ~6.6 GB


class AudioFileError(RuntimeError):
    pass


@dataclass
class Result:
    text: str
    seconds: float          # audio length (0 when unknown)
    engine: str             # "groq", "openai", "local"
    elapsed: float
    notice: str = ""        # e.g. "a API falhou: usei o modelo local"


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def downloads_folder() -> Path:
    """The user's Downloads folder (wherever Windows has it), where browsers save voice notes."""
    from .win32 import known_folder

    return known_folder("374DE290-123F-4565-9164-39C4925E467B") or Path.home() / "Downloads"


def latest_audio(folders: list[Path] | None = None, max_age_hours: float = 72) -> Path | None:
    """The newest audio file in Downloads (WhatsApp Web/Desktop "Baixar"), if recent."""
    newest, when = None, time.time() - max_age_hours * 3600
    for folder in folders or [downloads_folder()]:
        try:
            for f in folder.iterdir():
                if f.is_file() and is_audio(f):
                    mtime = f.stat().st_mtime
                    if mtime > when:
                        newest, when = f, mtime
        except OSError:
            continue
    return newest


def decode(path: Path) -> np.ndarray:
    """Mono float32 at 16 kHz."""
    if path.suffix.lower() not in DECODABLE:
        # offline, or online but over the upload limit (it cannot be cut into pieces here)
        raise AudioFileError(f"Arquivos {path.suffix} só vão inteiros para a API, com internet e até "
                             f"{API_MAX_BYTES // 2 ** 20} MB: este PC não consegue abri-los. "
                             "Converta para MP3 ou OGG e tente de novo.")
    import soundfile as sf

    try:
        with sf.SoundFile(str(path)) as f:
            parts = [resample(b.mean(axis=1), f.samplerate)
                     for b in f.blocks(DECODE_BLOCK * f.samplerate, dtype="float32", always_2d=True)]
    except Exception as e:
        raise AudioFileError(f"Não consegui abrir {path.name}: {e}") from e
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def duration(path: Path) -> float:
    try:
        import soundfile as sf

        return float(sf.info(str(path)).duration)
    except Exception:
        return 0.0


def _cut_points(samples: np.ndarray, step: int, search: int = 5 * RATE, window: int = RATE // 50) -> list[int]:
    """Where to split a long recording: near every `step`, in the quietest 20 ms of the 5 s
    before it (meeting.cut_point), so no word is cut in half. The last piece may run `search`
    longer rather than leave a few seconds for a request of their own."""
    from .meeting import cut_point

    cuts, start = [0], 0
    while samples.size - start > step + search:
        start += cut_point(samples[start:start + step], step, search, window)
        cuts.append(start)
    return cuts + [samples.size]


def transcribe_long(samples: np.ndarray, cfg: TranscriptionCfg, env: dict[str, str], paths: Paths,
                    token: CancelToken, notices: list[str] | None = None) -> str:
    """16 kHz mono samples of any length to text, with the engine in `cfg`.

    Up to a minute: one WAV, exactly the everyday dictation path. Longer: 10-minute pieces
    cut in a pause, each sent to the API as Ogg/Opus (a 5-minute WAV of 9.6 MB failed to
    upload; Opus is ~1 MB) with the WAV kept for the local fallback, and the end of the
    previous piece as Whisper context. A piece the API fails goes to the local model, as in
    transcribe(); the others are unaffected.

    The Opus of the pieces ahead is encoded while the current one goes up: encoding 10 minutes
    costs ~4 s on this PC and the upload ~2 s, and doing them in turn made a 31-minute dictation
    take 24 s of which 16 s were encoding (measured in 18/10/2026). Every piece is queued up
    front: encoding only two ahead of the upload left one encoder idle (60 min: 29.9 s -> 22.7 s
    with a fake 2 s upload, 21/10/2026)."""
    from . import transcribe as tr

    seconds = samples.size / RATE
    long = seconds > SHORT_SECONDS
    api = long and tr.resolve_engine(cfg, env) != "local"
    cuts = _cut_points(samples, CHUNK_SECONDS * RATE) if long else [0, samples.size]
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    stem = paths.tmp_dir / f"rec-{os.getpid()}-{time.monotonic_ns()}"   # rec-*: clean_tmp removes leftovers
    slices = list(zip(cuts, cuts[1:]))
    wavs = [stem.with_name(f"{stem.name}-{n:03d}.wav") for n in range(len(slices))]
    texts: list[str] = []
    encoders = ThreadPoolExecutor(max_workers=OPUS_AHEAD) if api else None
    encoding: dict[int, Future] = {}
    if encoders is not None:   # queued slices are views: no copy of the recording waits in the queue
        encoding = {k: encoders.submit(write_opus, wavs[k].with_suffix(".ogg"), samples[c:d])
                    for k, (c, d) in enumerate(slices)}
    try:
        for n, (a, b) in enumerate(slices):
            token.check()
            wav = wavs[n]
            ogg = wav.with_suffix(".ogg") if api else None
            try:
                write_wav(wav, samples[a:b])
                if ogg is not None:
                    encoding.pop(n).result()   # this piece's own file: the upload needs it
                piece_cfg = replace(cfg, context=texts[-1]) if texts else cfg
                texts.append(tr.transcribe(wav, piece_cfg, env, paths, token, notices, upload=ogg).strip())
            finally:
                wav.unlink(missing_ok=True)
                if ogg is not None:
                    ogg.unlink(missing_ok=True)
    finally:
        if encoders is not None:
            encoders.shutdown(wait=True, cancel_futures=True)   # Esc: never encode the rest first
            for k in encoding:   # pieces queued ahead of a failed or cancelled run
                wavs[k].with_suffix(".ogg").unlink(missing_ok=True)
    if long:
        log.info("long audio: %.0f s in %d piece(s)%s", seconds, len(slices), " (Opus upload)" if api else "")
    return " ".join(t for t in texts if t)


def transcribe_file(path: Path, settings: Settings, env: dict[str, str], paths: Paths,
                    token: CancelToken | None = None) -> Result:
    from . import transcribe as tr
    from .cleanup import sanitize

    token = token or CancelToken()
    if not path.is_file():
        raise AudioFileError(f"Arquivo não encontrado: {path}")
    cfg = replace(settings.transcription, context="", vocabulary=hints(settings.transcription.vocabulary, paths))
    engine = tr.resolve_engine(cfg, env)
    t0 = time.monotonic()
    seconds = duration(path)
    notice = ""
    samples = None   # decoded once, also for the local fallback
    if engine != "local" and not tr.offline():
        try:
            size, suffix = path.stat().st_size, path.suffix.lower()
            if (size <= API_MAX_BYTES and suffix in tr.UPLOAD_TYPES
                    and not (suffix in (".wav", ".flac") and size > WAV_UPLOAD_MAX)):
                text = tr.transcribe_api(engine, path, cfg, env)
            else:   # too big for one upload (or uncompressed): 10-minute Opus pieces
                samples = decode(path)
                text = transcribe_long(samples, replace(cfg, fallback_to_local=False), env, paths, token)
            token.check()
            return Result(sanitize(text), seconds, engine, time.monotonic() - t0)
        except AudioFileError:
            raise
        except Exception as e:
            if not (cfg.fallback_to_local and tr.local_available(cfg, paths)):
                raise AudioFileError(f"A transcrição falhou: {e}") from e
            log.warning("audio file via %s failed (%s); using the local model", engine, e)
            notice = "A API falhou; usei o modelo local."
    if not tr.local_available(cfg, paths):
        raise AudioFileError("Sem internet e sem o modelo local: instale-o em Configurações → Transcrição.")
    text = transcribe_long(decode(path) if samples is None else samples, replace(cfg, engine="local"),
                           env, paths, token)
    return Result(sanitize(text), seconds, "local", time.monotonic() - t0, notice)


MAX_HINTS = 40   # dictionary spellings passed to Whisper as hints


def hints(vocabulary: list[str], paths: Paths) -> list[str]:
    """Whisper hints: your vocabulary plus the spellings you taught the dictionary (newest
    first), so "GitHub" is heard right, not just fixed afterwards."""
    from .store import Dictionary

    out = list(vocabulary)
    learned = [r.write for r in Dictionary.load(paths.dictionary).replacements]
    return out + [w for w in reversed(learned) if w not in out][:MAX_HINTS]


def finish_text(text: str, paths: Paths) -> str:
    """Your dictionary's corrections apply to transcribed files too ("Kate" -> "Kayth")."""
    from . import textproc
    from .store import Dictionary

    return textproc.apply_replacements(text.strip(), Dictionary.load(paths.dictionary).replacements)
