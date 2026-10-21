"""Long dictations (Fase 1 of docs/PLANO-REUNIOES.md): pieces, Opus uploads, network timeouts,
lossless LLM refinement in blocks, the 1.1 auto-stop migration and the end-of-recording warning.
Test ids (T1.xx) follow the plan's catalog."""
import threading
import time
from dataclasses import replace

import numpy as np
import pytest
import requests
import soundfile as sf

from biraflow import audiofile, cleanup, http, transcribe as tr
from biraflow.audio import RATE, write_wav
from biraflow.cancel import CancelToken
from biraflow.config import CleanupCfg, Paths, Settings, TranscriptionCfg


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path, tmp_path)
    p.ensure()
    return p


def speech(seconds: float, pauses=()) -> np.ndarray:
    """A steady 'voice' with silent gaps at `pauses` (start seconds, 0.5 s each)."""
    x = np.full(int(seconds * RATE), 0.3, np.float32)
    for at in pauses:
        x[int(at * RATE):int((at + 0.5) * RATE)] = 0.0
    return x


@pytest.fixture
def pieces(monkeypatch):
    """Short pieces so a 'long' dictation is a few seconds of audio; records every call to
    transcribe() with what it got (piece length, upload file, context)."""
    monkeypatch.setattr(audiofile, "SHORT_SECONDS", 5)
    monkeypatch.setattr(audiofile, "CHUNK_SECONDS", 20)
    calls = []

    def fake(wav, cfg, env, paths_, token, notices=None, upload=None):
        calls.append({"wav": wav, "seconds": sf.info(str(wav)).duration, "upload": upload,
                      "upload_ok": upload is not None and sf.info(str(upload)).format == "OGG",
                      "context": cfg.context})
        return f"pedaço {len(calls) - 1}."

    monkeypatch.setattr(tr, "transcribe", fake)
    return calls


GROQ = {"GROQ_API_KEY": "k"}


# --- pieces and uploads (T1.01–T1.06, T1.09) ------------------------------------------------

def test_short_dictation_keeps_wav_path(paths, pieces):            # T1.01
    text = audiofile.transcribe_long(speech(4), TranscriptionCfg(engine="groq"), GROQ, paths, CancelToken())
    assert text == "pedaço 0." and len(pieces) == 1
    assert pieces[0]["upload"] is None and pieces[0]["wav"].suffix == ".wav"


def test_long_dictation_uploads_ogg(paths, pieces):                 # T1.02
    audiofile.transcribe_long(speech(12), TranscriptionCfg(engine="groq"), GROQ, paths, CancelToken())
    assert len(pieces) == 1 and pieces[0]["upload_ok"] and pieces[0]["wav"].suffix == ".wav"


def test_long_dictation_split_at_quiet_points(paths, pieces):      # T1.03
    text = audiofile.transcribe_long(speech(50, pauses=(17.0, 36.0)), TranscriptionCfg(engine="groq"), GROQ,
                                     paths, CancelToken())
    assert text == "pedaço 0. pedaço 1. pedaço 2."
    lengths = [c["seconds"] for c in pieces]
    assert len(lengths) == 3 and all(s <= 20.1 for s in lengths) and abs(sum(lengths) - 50) < 0.01
    assert 17.0 <= lengths[0] <= 17.5                    # cut inside the pause, not mid-"word"
    assert 36.0 <= lengths[0] + lengths[1] <= 36.5


def test_local_engine_pieces_are_wav_only(paths, pieces):
    audiofile.transcribe_long(speech(30), TranscriptionCfg(engine="local"), {}, paths, CancelToken())
    assert len(pieces) == 2 and all(c["upload"] is None for c in pieces)


def test_piece_prompt_carries_previous_text(paths, pieces):         # T1.04
    cfg = TranscriptionCfg(engine="groq", context="frase do ditado anterior")
    audiofile.transcribe_long(speech(30), cfg, GROQ, paths, CancelToken())
    assert [c["context"] for c in pieces] == ["frase do ditado anterior", "pedaço 0."]


def test_next_piece_is_encoded_while_this_one_is_uploaded(paths, monkeypatch):   # T1.21
    """Encoding 10 minutes of Opus costs ~4 s on this PC and the upload ~2 s: doing them in turn
    made a 31-minute dictation take 24 s of which 16 s were encoding (measured 18/10/2026)."""
    monkeypatch.setattr(audiofile, "SHORT_SECONDS", 5)
    monkeypatch.setattr(audiofile, "CHUNK_SECONDS", 20)
    real_opus = audiofile.write_opus
    encoding_next = threading.Event()

    def watched_opus(path, samples, rate=RATE):
        if path.stem.endswith("000"):     # the first piece only starts once the next is encoding
            assert encoding_next.wait(5.0), "o pedaço 001 não estava sendo codificado em paralelo"
        else:
            encoding_next.set()
        real_opus(path, samples, rate)

    monkeypatch.setattr(audiofile, "write_opus", watched_opus)
    monkeypatch.setattr(tr, "transcribe", lambda wav, cfg, env, p, token, notices=None, upload=None:
                        f"pedaço {wav.stem[-3:]}.")
    text = audiofile.transcribe_long(speech(50), TranscriptionCfg(engine="groq"), GROQ, paths, CancelToken())
    assert text == "pedaço 000. pedaço 001. pedaço 002."


def test_api_failure_mid_pieces_falls_back_in_order(paths, monkeypatch):   # T1.05
    monkeypatch.setattr(audiofile, "SHORT_SECONDS", 5)
    monkeypatch.setattr(audiofile, "CHUNK_SECONDS", 20)
    sent, local = [], []

    def api(engine, f, cfg, env):
        sent.append(f.suffix)
        if len(sent) == 2:
            raise tr.ApiUnavailable("Groq: HTTP 503")
        return f"api{len(sent)}"

    monkeypatch.setattr(tr, "transcribe_api", api)
    monkeypatch.setattr(tr, "local_available", lambda cfg, p: True)
    monkeypatch.setattr(tr, "transcribe_local", lambda wav, cfg, p, token: local.append(wav.suffix) or "local2")
    notices = []
    text = audiofile.transcribe_long(speech(50), TranscriptionCfg(engine="groq"), GROQ, paths, CancelToken(),
                                     notices)
    assert text == "api1 local2 api3"
    assert sent == [".ogg"] * 3 and local == [".wav"] and "503" in notices[0]


def test_temp_pieces_always_deleted(paths, monkeypatch):            # T1.06
    monkeypatch.setattr(audiofile, "SHORT_SECONDS", 5)
    monkeypatch.setattr(audiofile, "CHUNK_SECONDS", 20)
    n = []

    def flaky(wav, cfg, env, paths_, token, notices=None, upload=None):
        n.append(1)
        if len(n) == 2:
            raise tr.TranscriptionError("falhou")
        return "ok"

    monkeypatch.setattr(tr, "transcribe", flaky)
    with pytest.raises(tr.TranscriptionError):
        audiofile.transcribe_long(speech(50), TranscriptionCfg(engine="groq"), GROQ, paths, CancelToken())
    assert not list(paths.tmp_dir.glob("rec-*"))


def test_cli_transcribe_uses_long_path(paths, monkeypatch, capsys):  # T1.09
    from biraflow import app

    wav = paths.tmp_dir / "fala.wav"
    write_wav(wav, speech(3))
    got = []
    monkeypatch.setattr(audiofile, "transcribe_long",
                        lambda samples, cfg, env, p, token, notices=None: got.append(samples.size) or "texto")
    monkeypatch.setattr("biraflow.config.load_env", lambda p: {})
    assert app.transcribe_file(paths, wav) == 0
    assert got == [3 * RATE] and "texto" in capsys.readouterr().out


def test_long_dictation_job_goes_through_pieces(tmp_path, monkeypatch):
    """The controller sends dictations over a minute to transcribe_long, shorter ones as before."""
    from biraflow import controller as ctl

    paths = Paths(tmp_path, tmp_path)
    paths.ensure()
    c = ctl.Controller(paths, Settings(), type("Ui", (), {"__getattr__": lambda s, n: lambda *a: None})())
    long_calls, short_calls = [], []
    monkeypatch.setattr(audiofile, "transcribe_long",
                        lambda samples, cfg, env, p, token, notices=None: long_calls.append(samples.size) or "longo")
    monkeypatch.setattr(ctl, "transcribe", lambda wav, cfg, env, p, token, notices=None: short_calls.append(1) or "curto")
    job = ctl.Job(1, CancelToken(), ctl.Mode.HOLD, ctl.AppContext("notepad.exe", ""), 61.0)
    assert c._transcribe(job, speech(61), Settings(), {}) == "longo"
    job = ctl.Job(2, CancelToken(), ctl.Mode.HOLD, ctl.AppContext("notepad.exe", ""), 59.0)
    assert c._transcribe(job, speech(59), Settings(), {}) == "curto"
    assert long_calls == [61 * RATE] and short_calls == [1]


# --- network (T1.07, T1.08, T1.10) ----------------------------------------------------------

def test_upload_timeout_scales_with_size(tmp_path):                  # T1.07
    cfg = TranscriptionCfg(timeout_seconds=20)
    small = tmp_path / "a.ogg"
    small.write_bytes(b"x" * 1_000_000)
    assert tr.upload_timeout(small, cfg) == (pytest.approx(6.0), 20.0)
    big = tmp_path / "b.wav"
    write_wav(big, np.zeros(300 * RATE, np.float32))                  # 5 min of WAV: 9.6 MB
    connect, read = tr.upload_timeout(big, cfg)
    assert connect == pytest.approx(3 + 3 * 9.6, abs=0.1) and read == pytest.approx(30.0)


def test_api_upload_uses_scaled_timeout(tmp_path, monkeypatch):
    seen = []

    class Ok:
        status_code = 200

        def json(self):
            return {"text": "oi"}

    monkeypatch.setattr(http, "post", lambda url, **kw: seen.append(kw["timeout"]) or Ok())
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x" * 2_000_000)
    assert tr.transcribe_api("groq", f, TranscriptionCfg(), GROQ) == "oi"
    assert seen[0][0] == pytest.approx(9.0)


def _rig(tmp_path, monkeypatch, error):
    def post(*a, **k):
        raise error

    monkeypatch.setattr(http, "post", post)
    monkeypatch.setattr(tr, "local_available", lambda cfg, p: True)
    monkeypatch.setattr(tr, "transcribe_local", lambda wav, cfg, p, token: "texto local")
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    return tr.transcribe(wav, TranscriptionCfg(engine="groq"), GROQ, Paths(tmp_path, tmp_path), CancelToken())


def test_upload_abort_does_not_mark_offline(tmp_path, monkeypatch):  # T1.08
    from urllib3.exceptions import ProtocolError

    aborted = requests.ConnectionError(ProtocolError("Connection aborted.", TimeoutError("timed out")))
    assert _rig(tmp_path, monkeypatch, aborted) == "texto local"
    assert not tr.offline()                     # the next dictation tries the API again
    assert _rig(tmp_path, monkeypatch, requests.ConnectTimeout("no route")) == "texto local"
    assert tr.offline()


def test_server_timeout_scales_with_duration(tmp_path, monkeypatch):  # T1.10
    from biraflow.whisper_server import LocalServer

    seen = []

    class Ok:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "oi"}

    monkeypatch.setattr(requests, "post", lambda url, **kw: seen.append(kw["timeout"]) or Ok())
    wav = tmp_path / "a.wav"
    write_wav(wav, np.zeros(600 * RATE, np.float32))
    assert LocalServer().transcribe("http://127.0.0.1:1", wav, TranscriptionCfg(), CancelToken()) == "oi"
    assert seen[0][1] >= 1200


# --- refinement without loss (T1.11–T1.15) -----------------------------------------------------

class Reply:
    def __init__(self, payload, status=200, headers=None):
        self._payload, self.status_code, self.headers = payload, status, headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _dictation(kw) -> str:
    """The text a cleanup request carries (Anthropic or OpenAI-style body)."""
    body = kw["json"]
    user = body["messages"][-1]["content"]
    return user.split("<dictation>\n", 1)[1].rsplit("\n</dictation>", 1)[0]


RAW = "isso é um ditado de teste com várias palavras para o refinamento"


def test_truncated_anthropic_reply_returns_raw(monkeypatch):         # T1.11
    monkeypatch.setattr(http, "post", lambda url, **kw: Reply(
        {"content": [{"type": "text", "text": "Isso é um"}], "stop_reason": "max_tokens"}))
    c = cleanup.Cleaner(CleanupCfg(provider="anthropic"), {"ANTHROPIC_API_KEY": "k"})
    assert c.clean(RAW, CancelToken()) == RAW and "truncada" in c.last_error


def test_truncated_openai_reply_returns_raw(monkeypatch):            # T1.12
    monkeypatch.setattr(http, "post", lambda url, **kw: Reply(
        {"choices": [{"message": {"content": "Isso é um"}, "finish_reason": "length"}]}))
    c = cleanup.Cleaner(CleanupCfg(provider="openai"), {"OPENAI_API_KEY": "k"})
    assert c.clean(RAW, CancelToken()) == RAW and "truncada" in c.last_error


def _long_text() -> str:
    sentences = [f"Frase número {i} do ditado longo, com algumas palavras a mais." for i in range(200)]
    return " ".join(sentences[:100]) + "\n\n" + " ".join(sentences[100:])


def test_long_text_cleaned_in_blocks_in_order(monkeypatch):          # T1.13
    raw = _long_text()
    calls = []

    def post(url, **kw):
        calls.append(_dictation(kw))
        return Reply({"choices": [{"message": {"content": _dictation(kw).upper()}, "finish_reason": "stop"}]})

    monkeypatch.setattr(http, "post", post)
    progress = []
    c = cleanup.Cleaner(CleanupCfg(provider="openai"), {"OPENAI_API_KEY": "k"})
    out = c.clean(raw, CancelToken(), progress=lambda i, n: progress.append((i, n)))
    assert out == raw.upper()                                   # order and the paragraph break kept
    assert len(calls) >= 5 and all(len(b) <= cleanup.BLOCK_CHARS for b in calls)
    assert progress == [(i, len(calls)) for i in range(1, len(calls) + 1)]
    assert "".join(cleanup.split_blocks(raw)) == raw


def _groq_rig(monkeypatch, retry_after: str, fail_block: int):
    calls, sleeps = [], []

    def post(url, **kw):
        calls.append(_dictation(kw))
        blocks_seen = len({c for c in calls})
        if blocks_seen == fail_block and not sleeps:
            return Reply({}, 429, {"retry-after": retry_after})
        return Reply({"choices": [{"message": {"content": _dictation(kw).upper()}, "finish_reason": "stop"}]})

    def fake_sleep(s):
        sleeps.append(s)
        cleanup._cooldown.clear()          # as if the wait freed the models

    monkeypatch.setattr(http, "post", post)
    monkeypatch.setattr(cleanup.time, "sleep", fake_sleep)
    return calls, sleeps


def test_rate_limited_block_stays_raw_without_waiting(monkeypatch):  # T1.14
    raw = _long_text()
    calls, sleeps = _groq_rig(monkeypatch, "30", fail_block=2)
    c = cleanup.Cleaner(CleanupCfg(provider="groq"), GROQ)
    out = c.clean(raw, CancelToken())
    blocks = cleanup.split_blocks(raw)
    assert sleeps == []                                          # a dictation never waits 30 s
    assert out.startswith(blocks[0].rstrip().upper())           # block 1 refined
    assert blocks[1].rstrip() in out                             # block 2 (and the rate-limited rest) raw
    assert out.replace(" ", "").upper() == raw.replace(" ", "").upper()   # nothing lost


def test_short_retry_after_is_waited(monkeypatch):                  # T1.15
    raw = _long_text()
    calls, sleeps = _groq_rig(monkeypatch, "2", fail_block=1)
    c = cleanup.Cleaner(CleanupCfg(provider="groq"), GROQ)
    out = c.clean(raw, CancelToken())
    assert len(sleeps) == 1 and 0 < sleeps[0] <= cleanup.SHORT_WAIT
    assert out == raw.upper()


def test_short_text_is_one_request(monkeypatch):
    calls = []
    monkeypatch.setattr(http, "post", lambda url, **kw: calls.append(1) or Reply(
        {"choices": [{"message": {"content": "Isso é um ditado."}, "finish_reason": "stop"}]}))
    c = cleanup.Cleaner(CleanupCfg(provider="openai"), {"OPENAI_API_KEY": "k"})
    assert c.clean(RAW, CancelToken()) == "Isso é um ditado." and calls == [1]


# --- settings, migration (T1.16, T1.17) -------------------------------------------------------

def test_max_seconds_default_1800():                                 # T1.16
    from biraflow.config import DEFAULT_SETTINGS_TOML, parse_settings

    assert Settings().recording.max_seconds == 1800
    assert parse_settings(DEFAULT_SETTINGS_TOML)[0].recording.max_seconds == 1800


def test_max_seconds_300_migrated_once(paths):                       # T1.17
    from biraflow.config import DEFAULT_SETTINGS_TOML, load_settings, load_state

    old = DEFAULT_SETTINGS_TOML.replace("max_seconds = 1800", "max_seconds = 300")
    paths.settings.write_text(old, encoding="utf-8")
    assert load_settings(paths)[0].recording.max_seconds == 1800
    assert "max_seconds = 1800" in paths.settings.read_text(encoding="utf-8")
    assert load_state(paths)["max_seconds_1_1"] is True
    paths.settings.write_text(old, encoding="utf-8")             # the user sets 300 again later
    assert load_settings(paths)[0].recording.max_seconds == 300


def test_user_choice_is_not_migrated(paths):
    from biraflow.config import DEFAULT_SETTINGS_TOML, load_settings

    paths.settings.write_text(DEFAULT_SETTINGS_TOML.replace("max_seconds = 1800", "max_seconds = 600"),
                              encoding="utf-8")
    assert load_settings(paths)[0].recording.max_seconds == 600


# --- pill, warning beep, edit watch (T1.18–T1.20) ---------------------------------------------

def test_hold_mode_warns_last_30s(tk_root):                          # T1.18
    from biraflow.pill import Pill
    from biraflow.statemachine import Mode

    p = Pill(tk_root, lambda: 0.0)
    now = time.monotonic()
    for mode, label in ((Mode.HOLD, "Ditando"), (Mode.COMMAND, "Comando")):
        p._kind, p._mode, p._started, p._deadline = "rec", mode, now - 1780, now + 20
        assert p._content(now)[0].startswith(label) and "para em 20s" in p._content(now)[0]
        p._deadline = now + 600
        assert "para em" not in p._content(now)[0]
    p.top.destroy()


def test_clock_shows_hours():                                         # T1.19
    from biraflow.pill import _clock

    assert _clock(65) == "1:05" and _clock(3725) == "1:02:05"


def test_warning_beep_once_before_auto_stop(tmp_path, monkeypatch):
    from biraflow import controller as ctl

    paths = Paths(tmp_path, tmp_path)
    paths.ensure()
    c = ctl.Controller(paths, Settings(), type("Ui", (), {"__getattr__": lambda s, n: lambda *a: None})())
    played = []
    monkeypatch.setattr(c.sounds, "play", played.append)
    monkeypatch.setattr(ctl.win32, "session_locked", lambda: False)
    now = time.monotonic()
    c._last_voice = now
    c.machine.deadline = now + 30
    c._watch_recording(now)
    assert played == []
    c.machine.deadline = now + 8
    c._watch_recording(now)
    c._watch_recording(now + 1)
    assert played == ["warn"]


def test_long_paste_not_followed():                                   # T1.20
    from biraflow.editwatch import MAX_FOLLOW, EditWatcher

    class EmptyField:
        def text(self):
            return ""

    w = EditWatcher(lambda r: None, reader_factory=EmptyField, foreground=lambda: 7, poll=0.02)
    w.watch("x" * (MAX_FOLLOW + 1), 7)
    assert w._thread is None
    w.watch("Manda o arquivo", 7)
    assert w._thread is not None
    w.stop()
