import base64
import json
import subprocess
import sys
import time
import wave

import numpy as np
import pytest

from biraflow import http
from biraflow import transcribe as tr
from biraflow.audio import peak_rms, resample, write_wav
from biraflow.cancel import CancelToken
from biraflow.config import Paths, TranscriptionCfg
from biraflow.transcribe import (TranscriptionError, resolve_engine, resolve_model,
                                    whisper_command)


def test_write_wav_roundtrip(tmp_path):
    x = np.sin(np.linspace(0, 100, 16000)).astype(np.float32) * 0.5
    p = tmp_path / "a.wav"
    write_wav(p, x)
    with wave.open(str(p)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 16000)


def test_resample_lengths():
    assert resample(np.ones(48000, np.float32), 48000).size == 16000
    assert resample(np.ones(44100, np.float32), 44100).size == 16000
    x = np.ones(1600, np.float32)
    assert resample(x, 16000) is x or np.array_equal(resample(x, 16000), x)


def test_peak_rms_detects_speech_vs_silence():
    silence = np.random.default_rng(0).normal(0, 0.0005, 16000).astype(np.float32)
    speech = silence.copy()
    speech[8000:8800] += 0.2
    assert peak_rms(silence) < 0.003 < peak_rms(speech)


def test_resolve_engine():
    cfg = TranscriptionCfg(engine="auto")
    assert resolve_engine(cfg, {}) == "local"
    assert resolve_engine(cfg, {"OPENAI_API_KEY": "k"}) == "openai"
    assert resolve_engine(cfg, {"OPENAI_API_KEY": "k", "GROQ_API_KEY": "g"}) == "groq"
    assert resolve_engine(cfg, {"AI_GATEWAY_API_KEY": "v"}) == "gateway"
    assert resolve_engine(cfg, {"AI_GATEWAY_API_KEY": "v", "GROQ_API_KEY": "g"}) == "groq"  # o mais rápido
    assert resolve_engine(TranscriptionCfg(engine="gateway"), {"AI_GATEWAY_API_KEY": "v"}) == "gateway"
    with pytest.raises(TranscriptionError):
        resolve_engine(TranscriptionCfg(engine="groq"), {})
    with pytest.raises(TranscriptionError):
        resolve_engine(TranscriptionCfg(engine="gateway"), {})


def test_gateway_sends_base64_and_joins_word_times(tmp_path, monkeypatch):
    """O Gateway não aceita multipart: o modelo vai no cabeçalho e o áudio em base64 no JSON.
    As vezes vêm por palavra (`startSecond`/`endSecond`) e são juntadas em falas."""
    wav = tmp_path / "a.ogg"
    wav.write_bytes(b"AUDIODATA")
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"text": "olá mundo. tudo bem?", "segments": [
                {"text": "olá", "startSecond": 0.0, "endSecond": 0.4},
                {"text": "mundo.", "startSecond": 0.4, "endSecond": 0.9},
                {"text": "tudo", "startSecond": 3.0, "endSecond": 3.4},     # pausa de 2 s: outra fala
                {"text": "bem?", "startSecond": 3.4, "endSecond": 3.9}]}

    def post(url, headers=None, data=None, timeout=None):
        seen.update(url=url, headers=headers, body=json.loads(data), timeout=timeout)
        return Resp()

    monkeypatch.setattr(http, "post", post)
    segs = tr.segments_api("gateway", wav, TranscriptionCfg(engine="gateway"), {"AI_GATEWAY_API_KEY": "k"}, 4.0)
    assert seen["url"] == tr.GATEWAY_URL
    assert seen["headers"]["ai-model-id"] == "spacexai/grok-stt"
    assert seen["headers"]["ai-gateway-protocol-version"] == "0.0.1"
    assert seen["headers"]["ai-transcription-model-specification-version"] == "4"
    assert seen["body"] == {"audio": base64.b64encode(b"AUDIODATA").decode(), "mediaType": "audio/ogg"}
    assert [(s.start, s.end, s.text) for s in segs] == [(0.0, 0.9, "olá mundo."), (3.0, 3.9, "tudo bem?")]


def test_gateway_blocked_model_tries_whisper(tmp_path, monkeypatch):
    """Model Allowlist do time: 403 no modelo escolhido → tenta o openai/whisper-1."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    models = []

    class Resp:
        def __init__(self, status, payload):
            self.status_code, self._payload = status, payload

        @property
        def text(self):
            return json.dumps(self._payload)

        def json(self):
            return self._payload

    def post(url, headers=None, data=None, timeout=None):
        models.append(headers["ai-model-id"])
        if len(models) == 1:
            return Resp(403, {"error": {"message": "Your team has restricted access to this model"}})
        return Resp(200, {"text": "ok", "segments": []})

    monkeypatch.setattr(http, "post", post)
    env = {"AI_GATEWAY_API_KEY": "k"}
    assert tr.transcribe_api("gateway", wav, TranscriptionCfg(engine="gateway"), env) == "ok"
    assert models == ["spacexai/grok-stt", "openai/whisper-1"]


def test_gateway_without_key_says_what_is_missing(tmp_path):
    """Sem a chave, o caminho direto também falha com a mensagem que diz o que fazer."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    with pytest.raises(TranscriptionError) as e:
        tr.transcribe_api("gateway", wav, TranscriptionCfg(engine="gateway"), {})
    assert "AI_GATEWAY_API_KEY" in str(e.value)


def test_resolve_model_and_command(tmp_path):
    paths = Paths(tmp_path, tmp_path)
    assert resolve_model(TranscriptionCfg(local_model="medium"), paths) == paths.models_dir / "ggml-medium.bin"
    assert str(resolve_model(TranscriptionCfg(local_model=r"D:\m\x.bin"), paths)) == r"D:\m\x.bin"
    cfg = TranscriptionCfg(language="pt", vocabulary=["Groq", "Ubiratan"], threads=4)
    cmd = whisper_command(tmp_path / "whisper-cli.exe", tmp_path / "m.bin", tmp_path / "a.wav",
                          tmp_path / "a", cfg)
    assert cmd[cmd.index("-l") + 1] == "pt"
    assert cmd[cmd.index("-t") + 1] == "4"
    assert cmd[cmd.index("--prompt") + 1] == "Groq, Ubiratan."
    assert {"-nt", "-np", "-otxt"} <= set(cmd)


def test_cancel_token_kills_attached_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    token = CancelToken()
    token.attach(proc)
    t0 = time.monotonic()
    token.cancel()
    proc.wait(5)
    assert time.monotonic() - t0 < 5
    assert token.cancelled


def test_audio_ctx_covers_recording_with_margin():
    from biraflow.transcribe import audio_ctx

    assert audio_ctx(9.2) == 576          # (9.2 + 2) s * 50/s = 560 -> next multiple of 64
    assert audio_ctx(0.5) == 256          # floor
    assert audio_ctx(27.9) == 1500        # capped at the full window
    assert audio_ctx(28) == 0             # long recordings use the full window
    assert audio_ctx(300) == 0
    for s in (1, 5, 12.3, 20, 27):
        assert audio_ctx(s) >= s * 50     # never truncates speech


def test_fast_flags_in_command(tmp_path):
    exe, model, wav = tmp_path / "w.exe", tmp_path / "m.bin", tmp_path / "a.wav"
    fast = whisper_command(exe, model, wav, tmp_path / "a", TranscriptionCfg(), seconds=9.2)
    assert fast[fast.index("-ac") + 1] == "576" and fast[fast.index("-bs") + 1] == "1"
    assert "-ac" not in whisper_command(exe, model, wav, tmp_path / "a", TranscriptionCfg(), seconds=60)
    slow = whisper_command(exe, model, wav, tmp_path / "a", TranscriptionCfg(local_fast=False), seconds=9.2)
    assert "-ac" not in slow and "-bs" not in slow


@pytest.mark.parametrize("failure", ["offline", "rate_limit", "outage", "timeout"])
def test_api_failures_fall_back_to_local(tmp_path, monkeypatch, failure):
    import requests

    from biraflow import http, transcribe as tr

    class Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "{}"

        def json(self):
            return {"error": {"message": "x"}}

    def post(*a, **k):
        if failure == "offline":
            raise requests.ConnectionError("no route")
        if failure == "timeout":
            raise requests.ReadTimeout("slow")
        return Resp(429 if failure == "rate_limit" else 503)

    monkeypatch.setattr(http, "post", post)
    monkeypatch.setattr(tr, "local_available", lambda cfg, paths: True)
    monkeypatch.setattr(tr, "transcribe_local", lambda wav, cfg, paths, token: "texto local")
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    cfg = TranscriptionCfg(engine="groq")
    out = tr.transcribe(wav, cfg, {"GROQ_API_KEY": "k"}, Paths(tmp_path, tmp_path), CancelToken())
    assert out == "texto local"


def test_bad_key_falls_back_and_reports_why(tmp_path, monkeypatch):
    from biraflow import http, transcribe as tr

    class Resp:
        status_code = 401
        text = "{}"

        def json(self):
            return {"error": {"message": "Invalid API Key"}}

    monkeypatch.setattr(http, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(tr, "local_available", lambda cfg, paths: True)
    monkeypatch.setattr(tr, "transcribe_local", lambda wav, cfg, paths, token: "texto local")
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    notices = []
    out = tr.transcribe(wav, TranscriptionCfg(engine="groq"), {"GROQ_API_KEY": "k"}, Paths(tmp_path, tmp_path),
                        CancelToken(), notices)
    assert out == "texto local" and "401" in notices[0] and "Invalid API Key" in notices[0]
    monkeypatch.setattr(tr, "local_available", lambda cfg, paths: False)   # nothing to fall back to
    with pytest.raises(TranscriptionError, match="401"):
        tr.transcribe(wav, TranscriptionCfg(engine="groq"), {"GROQ_API_KEY": "k"}, Paths(tmp_path, tmp_path),
                      CancelToken())


def test_trim_silence_keeps_speech_with_padding():
    import numpy as np

    from biraflow.audio import RATE, trim_silence

    rng = np.random.default_rng(1)
    quiet = lambda s: (rng.standard_normal(int(s * RATE)) * 0.002).astype(np.float32)
    speech = (np.sin(np.arange(RATE) * 2 * np.pi * 200 / RATE) * 0.3).astype(np.float32)
    audio = np.concatenate([quiet(1.5), speech, quiet(0.4), speech, quiet(2.0)])
    out = trim_silence(audio)
    assert 2.4 * RATE <= out.size <= 3.2 * RATE        # both words + the pause between them
    assert trim_silence(speech).size == speech.size     # nothing to trim
    assert trim_silence(np.zeros(RATE * 2, dtype=np.float32)).size == RATE * 2



def test_whisper_prompt_hints_then_context_capped():
    from biraflow.transcribe import PROMPT_CHARS, whisper_prompt

    assert whisper_prompt(TranscriptionCfg()) == ""
    cfg = TranscriptionCfg(vocabulary=["Kubernetes", "GitHub", "GitHub"], context="O Kayth chegou.")
    assert whisper_prompt(cfg) == "Kubernetes, GitHub. O Kayth chegou."
    # a bare word list ends in a period, or Whisper answers in its style: no punctuation, no "?"
    assert whisper_prompt(TranscriptionCfg(vocabulary=["gubee", "bling"])) == "gubee, bling."
    long = TranscriptionCfg(vocabulary=[f"Termo{i}" for i in range(200)], context="fim da frase")
    prompt = whisper_prompt(long)
    assert len(prompt) <= PROMPT_CHARS and prompt.endswith("fim da frase.") and prompt.startswith("Termo0")


def _api_rig(tmp_path, monkeypatch, responses):
    from biraflow import http, transcribe as tr

    calls = []

    def post(url, **kw):
        calls.append(kw["data"]["model"])
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(http, "post", post)
    monkeypatch.setattr(tr, "local_available", lambda cfg, paths: True)
    monkeypatch.setattr(tr, "transcribe_local", lambda wav, cfg, paths, token: "texto local")
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    run = lambda notices=None: tr.transcribe(wav, TranscriptionCfg(engine="groq"), {"GROQ_API_KEY": "k"},
                                             Paths(tmp_path, tmp_path), CancelToken(), notices)
    return calls, run


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload, self.text = status, payload or {}, str(payload)

    def json(self):
        return self._payload


def test_offline_goes_local_without_waiting_again(tmp_path, monkeypatch):
    import requests

    calls, run = _api_rig(tmp_path, monkeypatch, [requests.ConnectionError("no route")])
    assert run() == "texto local"
    notices = []
    assert run(notices) == "texto local"            # second dictation: no API call at all
    assert len(calls) == 1 and "sem internet" in notices[0]


def test_groq_rate_limit_tries_the_other_whisper_model(tmp_path, monkeypatch):
    calls, run = _api_rig(tmp_path, monkeypatch, [_Resp(429), _Resp(200, {"text": "ok"})])
    assert run() == "ok"
    assert calls == ["whisper-large-v3-turbo", "whisper-large-v3"]


def test_read_wav_roundtrip(tmp_path):
    from biraflow.audio import RATE, read_wav

    samples = (np.sin(np.arange(RATE) / 10) * 0.5).astype(np.float32)
    write_wav(tmp_path / "x.wav", samples)
    back = read_wav(tmp_path / "x.wav")
    assert back.size == samples.size and np.max(np.abs(back - samples)) < 1e-3


def test_gpu_server_unloads_when_idle(monkeypatch):
    from biraflow.whisper_server import LocalServer

    s = LocalServer()
    stopped = []
    monkeypatch.setattr(LocalServer, "running", property(lambda self: not stopped))
    monkeypatch.setattr(s, "_stop_locked", lambda: stopped.append(True))
    s.idle_limit = None
    s._last_used -= 3600
    assert not s.stop_if_idle()                     # it is the engine: stays loaded
    s.idle_limit = 600
    assert s.stop_if_idle() and stopped
    s2 = LocalServer()
    s2.idle_limit = 600
    monkeypatch.setattr(s2, "_stop_locked", lambda: stopped.append("s2"))
    assert not s2.stop_if_idle() or "s2" not in stopped   # used just now: not unloaded


def test_elevation_check_on_this_process():
    from biraflow import win32

    assert win32.is_elevated() in (True, False)
    assert win32.blocked_by_elevation(0) is False



def test_privacy_and_lock_checks_return_booleans():
    from biraflow import win32

    assert win32.microphone_blocked() in (True, False)
    assert win32.session_locked() in (True, False)
