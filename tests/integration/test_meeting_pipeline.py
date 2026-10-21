"""S3.1, S3.2, S3.4, S3.5 and S4 of the plan: a whole meeting, headless, on real engines.
reuniao-2vozes.wav (left = "Eu", right = "Participantes") is played by smoke.FileSource 8x faster
than real time into a MeetingSession; the transcript is checked against reuniao-2vozes.json."""
import json
import time
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf

from biraflow import cleanup
from biraflow import meeting as mt
from biraflow import transcribe as tr
from biraflow.config import CleanupCfg, Settings

pytestmark = pytest.mark.integration

SPEED = 8.0
RESULT_AFTER_STOP = {"groq": 40.0, "gateway": 40.0, "local": 60.0}   # s from "Encerrar" to transcricao.md
_played = {}


def play(smoke, run_paths, cfg, env, stereo, drift=0.0):
    """Record `stereo` (N x 2 float32 at 16 kHz) as a meeting; returns (meeting, s after stop)."""
    real0 = time.monotonic()
    clock = lambda: real0 + (time.monotonic() - real0) * SPEED   # noqa: E731
    mic = smoke.FileSource(stereo[:, 0], clock)
    pc = smoke.FileSource(stereo[:, 1], clock, speed=1.0 + drift)
    settings = Settings()
    settings.transcription = cfg
    session = mt.MeetingSession(run_paths, settings, env, sources=(mic, pc), clock=clock, tick=1.0 / SPEED,
                                auto_notes=False)
    m = session.start()
    seconds = len(stereo) / 16000
    deadline = time.monotonic() + seconds / SPEED * 2 + 30
    while session.elapsed < seconds and session.recorder.error is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert session.recorder.error is None and session.elapsed >= seconds, "the recording stopped"
    t_stop = time.monotonic()
    session.stop(wait=600)
    return m, time.monotonic() - t_stop


def meeting_fixture(fixtures):
    stereo, rate = sf.read(str(fixtures / "reuniao-2vozes.wav"), dtype="float32", always_2d=True)
    assert rate == 16000 and stereo.shape[1] == 2
    truth = json.loads((fixtures / "reuniao-2vozes.json").read_text(encoding="utf-8-sig"))["segments"]
    return stereo, truth


@pytest.mark.parametrize("engine", ["groq", "gateway", "local"])
def test_meeting_speakers_and_timing(engine, user, fixtures, smoke, run_paths, meeting_engine):        # S3.1, S3.2
    stereo, truth = meeting_fixture(fixtures)
    m, waited = play(smoke, run_paths, meeting_engine(engine), user[2], stereo)
    rows = [r for r in m.rows() if "who" in r]
    acc, timing = smoke.speaker_accuracy(rows, truth), smoke.turns_on_time(rows, truth)
    statuses = [p["status"] for p in m.state["pieces"]]
    print(f"\nreunião via {engine}: resultado {waited:.1f} s depois de encerrar, {len(statuses)} pedaços, "
          f"{len(rows)} segmentos, falantes {acc:.1f}%, minutagem ±2 s {timing:.1f}%")
    assert m.state["status"] == "pronta" and set(statuses) <= {"feita", "vazia"}
    assert acc >= 95.0 and timing >= 90.0
    assert waited <= RESULT_AFTER_STOP[engine]
    _played[engine] = m


def test_meeting_with_clock_drift(user, fixtures, smoke, run_paths, meeting_engine):                 # S3.5 [R12]
    stereo, truth = meeting_fixture(fixtures)
    m, _ = play(smoke, run_paths, meeting_engine("any"), user[2], stereo, drift=0.0005)
    rows = [r for r in m.rows() if "who" in r]
    acc = smoke.speaker_accuracy(rows, truth)
    print(f"\ncom deriva de +0,05% no canal do PC: falantes {acc:.1f}%")
    assert acc >= 95.0


def test_silent_meeting_sends_nothing(user, fixtures, smoke, run_paths, any_engine, monkeypatch):  # S3.4
    mono, rate = sf.read(str(fixtures / "silencio-3min.wav"), dtype="float32")
    sent = []
    real = tr.transcribe_segments
    monkeypatch.setattr(tr, "transcribe_segments", lambda *a, **k: sent.append(1) or real(*a, **k))
    m, _ = play(smoke, run_paths, any_engine, user[2], np.column_stack((mono, mono)))
    assert sent == [] and not any("text" in r for r in m.rows())
    assert {p["status"] for p in m.state["pieces"]} == {"vazia"}


@pytest.mark.parametrize("provider", ["groq", "anthropic", "vercel"])
def test_meeting_notes_from_a_real_transcript(provider, user, fixtures, smoke, run_paths, meeting_engine):  # S4
    paths, settings, env = user
    key = cleanup.PROVIDERS[provider]["key"]
    if not env.get(key):
        pytest.skip(f"sem {key} no .env")
    m = _played.get("groq") or _played.get("local")
    if m is None:
        stereo, _ = meeting_fixture(fixtures)
        m, _ = play(smoke, run_paths, meeting_engine("any"), env, stereo)
    s = Settings()
    s.cleanup = replace(CleanupCfg(), provider=provider)
    t0 = time.monotonic()
    path = mt.write_notes(m, s, env)
    elapsed = time.monotonic() - t0
    assert path is not None, m.state.get("notes_error")
    notes = path.read_text(encoding="utf-8")
    print(f"\nnotas via {provider}: {len(notes)} caracteres em {elapsed:.1f} s")
    assert not notes.startswith(cleanup.NOTES_WARNING[:9])    # the structure held, no warning
    assert notes.startswith("# ") and cleanup._has_sections(notes)
    assert elapsed <= 30.0
