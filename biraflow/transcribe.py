"""Speech-to-text: whisper.cpp locally (whisper-cli.exe) or the Groq / OpenAI Whisper API."""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from . import http
from .cancel import Cancelled, CancelToken
from .config import Paths, TranscriptionCfg

log = logging.getLogger(__name__)

API = {
    "groq": ("https://api.groq.com/openai/v1/audio/transcriptions", "GROQ_API_KEY", "Groq"),
    "openai": ("https://api.openai.com/v1/audio/transcriptions", "OPENAI_API_KEY", "OpenAI"),
    "gateway": ("https://ai-gateway.vercel.sh/v4/ai/transcription-model", "AI_GATEWAY_API_KEY",
                "Vercel AI Gateway"),
}

# The Gateway's transcription endpoint is not the OpenAI one: the model travels in a header and
# the audio goes base64 inside a JSON body (multipart is not accepted). Measured 18/10/2026:
# spacexai/grok-stt reads 5 min in 3.5 s (Groq's whisper-large-v3-turbo: 4.2 s) with word-level
# times; openai/whisper-1 takes 10.5 s and misses more words. Teams with a Model Allowlist may
# have most transcription models blocked (HTTP 403), so the spare is the one every team allows.
GATEWAY_URL = API["gateway"][0]
GATEWAY_HEADERS = {"ai-gateway-protocol-version": "0.0.1",
                   "ai-transcription-model-specification-version": "4"}
GATEWAY_SPARE_MODEL = "openai/whisper-1"


class TranscriptionError(RuntimeError):
    pass


@dataclass
class Segment:
    """A stretch of speech with its time in the audio (seconds) and Whisper's own signals."""
    start: float
    end: float
    text: str
    no_speech: float = 0.0   # probability that this is not speech
    logprob: float = 0.0     # average token log-probability
    ratio: float = 0.0       # compression ratio: above ~2.4 Whisper is repeating itself


def segments_from(payload: dict, seconds: float) -> list[Segment]:
    """Segments of an OpenAI-style verbose_json reply (Groq, OpenAI, whisper-server); the
    whole text as one segment when the reply has no times."""
    out = [Segment(float(s.get("start", 0.0)), float(s.get("end", seconds)), str(s.get("text", "")).strip(),
                   float(s.get("no_speech_prob", 0.0)), float(s.get("avg_logprob", 0.0)),
                   float(s.get("compression_ratio", 0.0)))
           for s in payload.get("segments") or [] if "start" in s]
    if out or not str(payload.get("text", "")).strip():
        return out
    return [Segment(0.0, seconds, str(payload["text"]).strip())]


WORDS_MAX_SECONDS = 8.0   # how long a merged stretch of speech may get
WORDS_GAP = 0.6           # a pause this long ends the stretch (a turn boundary)


def join_words(segments: list[Segment]) -> list[Segment]:
    """The Gateway's models that send word-level times (grok) would give one line per word in a
    meeting transcript: join while the pause is short and the stretch is not too long, so each
    line stays an utterance and the speaker label still sees where the turns are."""
    out: list[Segment] = []
    for s in segments:
        if out and s.start - out[-1].end <= WORDS_GAP and s.end - out[-1].start <= WORDS_MAX_SECONDS:
            last = out[-1]
            out[-1] = Segment(last.start, s.end, f"{last.text} {s.text}".strip())
        else:
            out.append(s)
    return out


def segments_from_gateway(payload: dict, seconds: float) -> list[Segment]:
    """Segments of a Vercel AI Gateway transcription reply (`startSecond`/`endSecond`; no
    quality signals, like the local engine: the filters stay idle)."""
    out = [Segment(float(s.get("startSecond", 0.0)), float(s.get("endSecond", seconds)),
                   str(s.get("text", "")).strip())
           for s in payload.get("segments") or [] if "startSecond" in s]
    out = join_words([s for s in out if s.text])
    if out or not str(payload.get("text", "")).strip():
        return out
    return [Segment(0.0, seconds, str(payload["text"]).strip())]


PROMPT_CHARS = 600   # Whisper reads at most ~224 tokens of prompt
OFFLINE_COOLDOWN = 60.0
_offline_until = 0.0  # after a network failure, skip the API (and its timeout) for a while


def whisper_prompt(cfg: TranscriptionCfg) -> str:
    """Vocabulary hints, then the previous sentence (context goes last, where Whisper
    weighs it most). Capped: terms are dropped from the end of the list first."""
    terms = list(dict.fromkeys(t.strip() for t in cfg.vocabulary if t and t.strip()))
    context = cfg.context.strip()[-200:]
    while True:
        prompt = ". ".join(p for p in (", ".join(terms), context) if p)
        # Whisper copies the prompt's style: after a bare word list ("gubee, bling, shein") a quarter
        # of the dictations came back unpunctuated, so a question lost its "?" (history, 21/10/2026)
        if prompt[-1:].isalnum():
            prompt += "."
        if len(prompt) <= PROMPT_CHARS or not terms:
            return prompt[-PROMPT_CHARS:]
        terms.pop()


def mark_offline(seconds: float = OFFLINE_COOLDOWN) -> None:
    global _offline_until
    _offline_until = time.monotonic() + seconds


def offline() -> bool:
    return time.monotonic() < _offline_until


class ApiUnavailable(TranscriptionError):
    """The service is down or throttling (5xx / 429): worth falling back to local."""


def resolve_engine(cfg: TranscriptionCfg, env: dict[str, str]) -> str:
    engine = cfg.engine
    if engine == "auto":
        for name in ("groq", "openai", "gateway"):
            if env.get(API[name][1]):
                return name
        return "local"
    if engine in API and not env.get(API[engine][1]):
        raise TranscriptionError(f"{API[engine][1]} não está no .env (transcription.engine = \"{engine}\").")
    return engine


# --- local whisper.cpp ----------------------------------------------------------

def find_whisper_cli(cfg: TranscriptionCfg, paths: Paths) -> Path | None:
    if cfg.whisper_cli:
        p = Path(os.path.expandvars(cfg.whisper_cli))
        return p if p.is_file() else None
    for name in ("whisper-cli.exe", "main.exe"):  # main.exe: whisper.cpp < 1.7
        p = paths.whisper_dir / name
        if p.is_file():
            return p
    found = shutil.which("whisper-cli")
    return Path(found) if found else None


def resolve_model(cfg: TranscriptionCfg, paths: Paths) -> Path:
    value = os.path.expandvars(cfg.local_model.strip())
    if value.lower().endswith(".bin") or "/" in value or "\\" in value:
        return Path(value)
    return paths.models_dir / f"ggml-{value}.bin"


def local_available(cfg: TranscriptionCfg, paths: Paths) -> bool:
    return find_whisper_cli(cfg, paths) is not None and resolve_model(cfg, paths).is_file()


def audio_ctx(seconds: float) -> int:
    """Encoder context covering `seconds` of audio plus margin (Whisper: 1500 = 30 s).

    The encoder always processes a 30 s window; shrinking it to the dictation's
    length cuts encode time several-fold. 0 (full window) for long recordings.
    """
    if seconds >= 28:
        return 0
    ctx = (seconds + 2.0) * 50
    return min(1500, max(256, int(-(-ctx // 64) * 64)))


def audio_seconds(path: Path) -> float:
    """Length of any file libsndfile reads (WAV, Ogg/Opus...); 0 when unknown."""
    try:
        if path.suffix.lower() == ".wav":   # a dictation: importing soundfile costs ~60 ms the first time
            with wave.open(str(path)) as w:
                return w.getnframes() / float(w.getframerate())
        import soundfile as sf

        return float(sf.info(str(path)).duration)
    except Exception:
        return 0.0


def upload_timeout(path: Path, cfg: TranscriptionCfg, body_factor: float = 1.0) -> tuple[float, float]:
    """(connect, read) for an upload. urllib3 keeps the connect timeout on the socket while it
    sends the body, so 3 s aborted big uploads on a slow link (a 9.6 MB WAV failed twice):
    allow 3 s per MB (a 2.7 Mbps link still fits). The read side waits for the transcription:
    1 s per 10 s of audio. `body_factor`: the Gateway wants the audio base64 inside a JSON
    body, a third more bytes than the file (1.34)."""
    try:
        mb = path.stat().st_size / 1e6 * body_factor
    except OSError:
        mb = 0.0
    return 3 + 3 * mb, max(float(cfg.timeout_seconds), audio_seconds(path) / 10)


def wav_seconds(wav: Path) -> float:
    try:
        with wave.open(str(wav)) as w:
            return w.getnframes() / float(w.getframerate())
    except (OSError, wave.Error, ZeroDivisionError):
        return 30.0


def whisper_command(exe: Path, model: Path, wav: Path, out_base: Path,
                    cfg: TranscriptionCfg, seconds: float = 30.0, timed: bool = False) -> list[str]:
    """`timed`: JSON with each segment's time (-oj) instead of plain text; -nt would break
    those times."""
    threads = cfg.threads or max(1, min(8, os.cpu_count() or 4))
    cmd = [str(exe), "-m", argv_path(model), "-f", str(wav), "-l", cfg.language or "auto",
           "-t", str(threads), "-np", "-of", str(out_base)] + (["-oj"] if timed else ["-nt", "-otxt"])
    if cfg.local_fast:
        # Greedy decoding + an encoder window sized to the recording: ~2.5x faster,
        # same text on dictation-length audio.
        cmd += ["-bs", "1", "-bo", "1"]
        if ctx := audio_ctx(seconds):
            cmd += ["-ac", str(ctx)]
    if prompt := whisper_prompt(cfg):
        cmd += ["--prompt", argv_text(prompt)]
    return cmd


# whisper.cpp reads a narrow argv, which Windows fills in the ANSI code page (cp1252). Measured
# 20/10/2026: a model path with "ç"/"ã" (C:\Users\João\...) aborts whisper-cli (0xC0000409),
# while -f/-of with accents work; an accented --prompt arrives as cp1252 bytes that Whisper
# reads as broken UTF-8 (and transcribes worse).
def argv_path(path: Path) -> str:
    """The 8.3 short name (plain ASCII) of a path with accents, where Windows keeps short names
    (it does on the system drive, where the app's models live)."""
    s = str(path)
    if s.isascii():
        return s
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(s, buf, len(buf))
    return buf.value if 0 < n < len(buf) else s


def argv_text(text: str) -> str:
    """UTF-8 bytes dressed as ANSI characters, so they reach whisper.cpp unchanged."""
    return text.encode("utf-8").decode("mbcs", "replace")


def transcribe_local(wav: Path, cfg: TranscriptionCfg, paths: Paths, token: CancelToken) -> str:
    return _local(wav, cfg, paths, token, timed=False)


def segments_local(wav: Path, cfg: TranscriptionCfg, paths: Paths, token: CancelToken) -> list[Segment]:
    return _local(wav, cfg, paths, token, timed=True)


def _local(wav: Path, cfg: TranscriptionCfg, paths: Paths, token: CancelToken, timed: bool):
    """Text, or segments when `timed`, from the GPU server if it runs, else whisper-cli."""
    exe = find_whisper_cli(cfg, paths)
    if exe is None:
        raise TranscriptionError("whisper-cli.exe não encontrado. Rode scripts\\setup-whisper.ps1.")
    model = resolve_model(cfg, paths)
    if not model.is_file():
        raise TranscriptionError(f"Modelo não encontrado: {model.name}. Rode scripts\\setup-whisper.ps1.")
    if cfg.keep_loaded:
        from .whisper_server import SERVER

        # Wait for the GPU server (≈1.5 s to load, joins a prewarm already under way)
        # rather than run whisper-cli: a one-off whisper-cli must load the model onto the
        # GPU itself (4–5 s for large-v3-turbo), and both loading at once is slower still.
        try:
            url = SERVER.ensure(exe, model, cfg, paths.tmp_dir, timeout=25)
        except Exception as e:  # antivirus blocking the server, disk full for its log...
            log.warning("whisper-server unavailable (%s); using whisper-cli", e)
            url = None
        if url:
            try:
                return SERVER.transcribe(url, wav, cfg, token, timed=timed)
            except Cancelled:
                raise
            except Exception as e:  # server crashed or was killed: fall back to whisper-cli
                log.warning("whisper-server failed (%s); using whisper-cli", e)
                SERVER.stop()
    out_base = wav.with_suffix("")
    out_txt = out_base.with_suffix(".json" if timed else ".txt")
    proc = subprocess.Popen(
        whisper_command(exe, model, wav, out_base, cfg, wav_seconds(wav), timed=timed),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(exe.parent),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    token.attach(proc)
    try:
        # a hung GPU driver must not block this and every later dictation forever
        stdout, stderr = proc.communicate(timeout=max(120.0, 4 * wav_seconds(wav) + 60))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise TranscriptionError("whisper.cpp travou e foi encerrado.")
    finally:
        token.detach()
    try:
        token.check()
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise TranscriptionError("whisper.cpp falhou: " + (" | ".join(tail) or f"código {proc.returncode}"))
        if timed:
            import json

            data = json.loads(out_txt.read_text(encoding="utf-8", errors="replace"))
            return [Segment(s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000, s["text"].strip())
                    for s in data.get("transcription", []) if s.get("text", "").strip()]
        if out_txt.is_file():
            return out_txt.read_text(encoding="utf-8", errors="replace")
        return stdout.decode("utf-8", "replace")
    finally:
        out_txt.unlink(missing_ok=True)


# --- APIs -------------------------------------------------------------------------

def transcribe_api(engine: str, wav: Path, cfg: TranscriptionCfg, env: dict[str, str]) -> str:
    if engine == "gateway":
        return str(_post_gateway(wav, cfg, env).get("text", "")).strip()
    return _post(engine, wav, cfg, env, "json").get("text", "")


def segments_api(engine: str, path: Path, cfg: TranscriptionCfg, env: dict[str, str],
                 seconds: float) -> list[Segment]:
    if engine == "gateway":
        return segments_from_gateway(_post_gateway(path, cfg, env), seconds)
    model = cfg.groq_model if engine == "groq" else cfg.openai_model
    if model.startswith("gpt-4o"):   # these models have no verbose_json: one segment per piece
        text = transcribe_api(engine, path, cfg, env).strip()
        return [Segment(0.0, seconds, text)] if text else []
    return segments_from(_post(engine, path, cfg, env, "verbose_json"), seconds)


def _post(engine: str, wav: Path, cfg: TranscriptionCfg, env: dict[str, str], response_format: str) -> dict:
    url, key_name, label = API[engine]
    data = whisper_form(cfg, model=cfg.groq_model if engine == "groq" else cfg.openai_model,
                        response_format=response_format)

    def send():
        with open(wav, "rb") as f:
            return http.post(url, headers={"Authorization": f"Bearer {env[key_name]}"},
                             files={"file": (f"audio{UPLOAD_NAMES.get(wav.suffix.lower(), wav.suffix.lower() or '.wav')}", f,
                                             UPLOAD_TYPES.get(wav.suffix.lower(), "application/octet-stream"))},
                             data=data,
                             timeout=upload_timeout(wav, cfg))

    r = send()
    if r.status_code == 429 and engine == "groq" and data["model"] != GROQ_SPARE_MODEL:
        # Groq limits audio per model: the full large-v3 has its own quota (and is as fast)
        log.info("Groq %s rate-limited; retrying with %s", data["model"], GROQ_SPARE_MODEL)
        data["model"] = GROQ_SPARE_MODEL
        r = send()
    return _reply(r, label)


def whisper_form(cfg: TranscriptionCfg, **fields: str) -> dict[str, str]:
    """The Whisper request fields the APIs and whisper-server share."""
    data = {**fields, "temperature": "0"}
    if cfg.language and cfg.language != "auto":
        data["language"] = cfg.language
    if prompt := whisper_prompt(cfg):
        data["prompt"] = prompt
    return data


def _reply(r, label: str) -> dict:
    """The JSON of a 200. A 429 or 5xx is worth falling back to the local model; the rest is an error."""
    if r.status_code == 429 or r.status_code >= 500:
        raise ApiUnavailable(f"{label}: HTTP {r.status_code} {http.error_message(r)}")
    if r.status_code != 200:
        raise TranscriptionError(f"{label}: HTTP {r.status_code} {http.error_message(r)}")
    return r.json()


def _post_gateway(path: Path, cfg: TranscriptionCfg, env: dict[str, str]) -> dict:
    """Vercel AI Gateway transcription: model in a header, audio base64 in a JSON body."""
    import base64
    import json

    key = env.get(API["gateway"][1], "").strip()
    if not key:
        # resolve_engine() already refuses this; here so a direct call fails clearly too
        raise TranscriptionError(f"{API['gateway'][1]} não está no .env (transcription.engine = \"gateway\").")
    label = API["gateway"][2]

    def send(model: str):
        with open(path, "rb") as f:
            body = json.dumps({"audio": base64.b64encode(f.read()).decode(),
                               "mediaType": UPLOAD_TYPES.get(path.suffix.lower(), "audio/wav")})
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "ai-model-id": model, **GATEWAY_HEADERS}
        return http.post(GATEWAY_URL, headers=headers, data=body, timeout=upload_timeout(path, cfg, 1.34))

    model = (cfg.gateway_model or TranscriptionCfg.gateway_model).strip()
    r = send(model)
    first = r
    if r.status_code == 403 and model != GATEWAY_SPARE_MODEL:
        # "Your team has restricted access to this model" (Model Allowlist): whisper-1 is the
        # one every team allows. A wrong key also gives 403: then this fails too, and the
        # message the user sees is the first one.
        log.info("Gateway refused %s (HTTP 403); retrying with %s", model, GATEWAY_SPARE_MODEL)
        r = send(GATEWAY_SPARE_MODEL)
        if r.status_code != 200:
            r = first
    return _reply(r, label)


GROQ_SPARE_MODEL = "whisper-large-v3"
# what the Whisper APIs accept as is (a WhatsApp voice note is Ogg/Opus)
UPLOAD_TYPES = {".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".oga": "audio/ogg",
                ".mp3": "audio/mpeg", ".mpga": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
                ".webm": "audio/webm", ".flac": "audio/flac"}
UPLOAD_NAMES = {".opus": ".ogg", ".oga": ".ogg", ".mpga": ".mp3"}   # extensions every API knows


def _upload_aborted(e: Exception) -> bool:
    """Connected, then the upload broke (slow link, reset): not a sign that the PC is offline."""
    from urllib3.exceptions import ProtocolError

    return any(isinstance(a, ProtocolError) for a in e.args)


def transcribe(wav: Path, cfg: TranscriptionCfg, env: dict[str, str], paths: Paths,
               token: CancelToken, notices: list[str] | None = None, upload: Path | None = None) -> str:
    """Transcribe with the configured engine. If an API fails for any reason and the
    local model is installed, the local model transcribes instead (the dictation is never
    lost) and the reason is appended to `notices` for the UI. `upload`: a smaller file with
    the same audio (Ogg/Opus) for the API; the local model always reads `wav`."""
    return _with_fallback(cfg, env, paths, token, notices,
                          lambda engine: transcribe_api(engine, upload or wav, cfg, env),
                          lambda: transcribe_local(wav, cfg, paths, token))


def transcribe_segments(wav: Path, cfg: TranscriptionCfg, env: dict[str, str], paths: Paths,
                        token: CancelToken, notices: list[str] | None = None,
                        upload: Path | None = None) -> list[Segment]:
    """Like transcribe(), with the time of each segment (meetings)."""
    seconds = wav_seconds(wav)
    return _with_fallback(cfg, env, paths, token, notices,
                          lambda engine: segments_api(engine, upload or wav, cfg, env, seconds),
                          lambda: segments_local(wav, cfg, paths, token))


def _with_fallback(cfg: TranscriptionCfg, env: dict[str, str], paths: Paths, token: CancelToken,
                   notices: list[str] | None, api, local):
    engine = resolve_engine(cfg, env)
    if engine == "local":
        return local()
    import requests

    local_ok = cfg.fallback_to_local and local_available(cfg, paths)
    if offline() and local_ok:
        # the network just failed: don't wait for another connection timeout
        log.info("offline a moment ago; transcribing locally")
        if notices is not None:
            notices.append("sem internet: usando o modelo local")
        return local()
    try:
        result = api(engine)
    except (requests.RequestException, TranscriptionError) as e:
        # offline, timeout, outage, rate limit, bad key: the local model (GPU) takes over
        if isinstance(e, requests.ConnectionError) and not _upload_aborted(e):
            mark_offline()
        reason = str(e) if isinstance(e, TranscriptionError) else f"{API[engine][2]} inacessível ({type(e).__name__})"
        if local_ok:
            log.warning("API %s falhou (%s); usando whisper.cpp local", engine, reason)
            if notices is not None:
                notices.append(reason)
            token.check()
            return local()
        if isinstance(e, TranscriptionError):
            raise
        raise TranscriptionError(reason + ".") from e
    token.check()
    return result
