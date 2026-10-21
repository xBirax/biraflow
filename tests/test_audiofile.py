import json
import os
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from biraflow import audiofile, transcribe
from biraflow.audio import RATE
from biraflow.config import Paths, Settings


def voice_note(path: Path, seconds: float = 2.0) -> Path:
    t = np.arange(int(48000 * seconds)) / 48000
    sf.write(str(path), (np.sin(2 * np.pi * 220 * t) * 0.3).astype("float32"), 48000, format="OGG", subtype="OPUS")
    return path


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path, tmp_path)
    p.ensure()
    return p


def test_decodes_a_whatsapp_voice_note_to_16k_mono(tmp_path):
    samples = audiofile.decode(voice_note(tmp_path / "PTT-20261015-WA0001.opus"))
    assert samples.dtype == np.float32 and abs(samples.size / RATE - 2.0) < 0.1
    assert audiofile.duration(tmp_path / "PTT-20261015-WA0001.opus") == pytest.approx(2.0, abs=0.1)
    with pytest.raises(audiofile.AudioFileError):
        audiofile.decode(tmp_path / "x.m4a")       # only through the API


def test_latest_audio_in_downloads(tmp_path):
    old = voice_note(tmp_path / "old.ogg")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    new = voice_note(tmp_path / "new.opus")
    (tmp_path / "notes.txt").write_text("x")
    assert audiofile.latest_audio([tmp_path]) == new
    os.utime(new, (time.time() - 5 * 86400,) * 2)
    os.utime(old, (time.time() - 5 * 86400,) * 2)
    assert audiofile.latest_audio([tmp_path]) is None          # nothing recent


def test_long_recordings_are_cut_in_a_pause():
    loud = np.full(RATE * 8, 0.3, dtype=np.float32)
    samples = np.concatenate([loud, np.zeros(RATE // 2, np.float32), loud])   # a pause at 8.0–8.5 s
    cuts = audiofile._cut_points(samples, step=RATE * 10)
    assert len(cuts) == 3 and 8.0 * RATE <= cuts[1] <= 8.5 * RATE
    steady = np.full(RATE * 25, 0.3, dtype=np.float32)              # no real pause: pieces keep full length
    assert audiofile._cut_points(steady, step=RATE * 10) == [0, RATE * 10, RATE * 25]


def test_api_gets_the_file_as_is(tmp_path, paths, monkeypatch):
    note = voice_note(tmp_path / "PTT.opus")
    sent = []
    monkeypatch.setattr(transcribe, "transcribe_api", lambda engine, f, cfg, env: sent.append((engine, f)) or " Oi, tudo bem?")
    settings = Settings()
    settings.transcription.engine = "groq"
    r = audiofile.transcribe_file(note, settings, {"GROQ_API_KEY": "k"}, paths)
    assert r.text == "Oi, tudo bem?" and r.engine == "groq" and sent == [("groq", note)]


def test_falls_back_to_the_local_model_with_decoded_wav(tmp_path, paths, monkeypatch):
    note = voice_note(tmp_path / "PTT.opus")

    def broken(*a):
        raise transcribe.ApiUnavailable("Groq: HTTP 503")

    got = []

    def local(wav, cfg, paths_, token):
        got.append(wav.suffix)
        return "texto local"

    monkeypatch.setattr(transcribe, "transcribe_api", broken)
    monkeypatch.setattr(transcribe, "local_available", lambda cfg, p: True)
    monkeypatch.setattr(transcribe, "transcribe_local", local)
    settings = Settings()
    settings.transcription.engine = "groq"
    r = audiofile.transcribe_file(note, settings, {"GROQ_API_KEY": "k"}, paths)
    assert r.text == "texto local" and r.engine == "local" and got == [".wav"] and r.notice
    assert not list(paths.tmp_dir.glob("rec-*"))       # temporary pieces deleted


def test_second_launch_hands_files_to_the_running_app(tmp_path, paths):
    from biraflow.app import inbox_dir, send_to_running

    note = voice_note(tmp_path / "a b.opus")
    send_to_running(paths, [note])
    reqs = list(inbox_dir(paths).glob("*.json"))
    assert len(reqs) == 1 and json.loads(reqs[0].read_text(encoding="utf-8")) == [str(note.resolve())]


def test_summary_uses_the_cleanup_provider(monkeypatch):
    from biraflow import cleanup, http
    from biraflow.cancel import CancelToken
    from biraflow.config import CleanupCfg

    class Reply:
        status_code = 200
        text = ""
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": "Reunião amanhã.\n• 10 h"}}]}

        def raise_for_status(self):
            pass

    calls = []
    monkeypatch.setattr(http, "post", lambda url, **kw: calls.append(kw) or Reply())
    out = cleanup.Cleaner(CleanupCfg(), {"GROQ_API_KEY": "k"}).summarize("confirma a reunião amanhã às 10", CancelToken())
    assert out.startswith("Reunião amanhã")
    assert "summarize" in calls[0]["json"]["messages"][0]["content"]


def test_decode_by_blocks_matches_reading_the_whole_file(tmp_path, monkeypatch):
    from biraflow.audio import resample

    path = tmp_path / "longo.wav"
    t = np.arange(int(44100 * 3.5)) / 44100
    stereo = np.stack([np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 330 * t)], axis=1) * 0.3
    sf.write(str(path), stereo.astype("float32"), 44100)
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    whole = resample(data.mean(axis=1), rate)                       # what decode() did before
    monkeypatch.setattr(audiofile, "DECODE_BLOCK", 1)               # 1 s blocks: four of them here
    blocks = audiofile.decode(path)
    assert blocks.dtype == np.float32 and abs(blocks.size - whole.size) <= 1
    n = min(blocks.size, whole.size)
    assert np.abs(blocks[:n] - whole[:n]).max() < 0.03             # each block's grid is off by < 1 input sample


def test_no_tiny_last_piece():
    noise = np.random.default_rng(0).uniform(-0.3, 0.3, RATE * 33).astype(np.float32)
    for t in (6, 12, 18, 24, 30):                                   # pauses 4 s before every 10 s step
        noise[t * RATE:t * RATE + RATE // 50] = 0
    cuts = audiofile._cut_points(noise, step=RATE * 10)
    pieces = np.diff(cuts) / RATE
    assert min(pieces) >= 5 and max(pieces) <= 15                   # was a 3 s last piece


def test_big_wav_goes_up_in_opus_pieces(tmp_path, paths, monkeypatch):
    path = tmp_path / "gravacao.wav"
    sf.write(str(path), np.zeros(RATE * 90, np.float32), RATE, subtype="FLOAT")   # 5.8 MB, 90 s
    whole, pieces = [], []
    monkeypatch.setattr(transcribe, "transcribe_api", lambda engine, f, cfg, env: whole.append(f) or "x")
    monkeypatch.setattr(audiofile, "transcribe_long", lambda samples, *a, **k: pieces.append(samples.size) or "texto")
    settings = Settings()
    settings.transcription.engine = "groq"
    r = audiofile.transcribe_file(path, settings, {"GROQ_API_KEY": "k"}, paths)
    assert r.text == "texto" and whole == [] and pieces == [RATE * 90]
