"""S1.1–S1.5 and S1.4 of the plan: long audio through the dictation's long path on real engines."""
import math
import time
from dataclasses import replace

import pytest

from biraflow import audiofile
from biraflow import transcribe as tr
from biraflow.audio import RATE
from biraflow.cancel import CancelToken
from biraflow.cleanup import Cleaner

pytestmark = pytest.mark.integration

API_BUDGET = {"fala-5min": 6.0, "fala-12min": 12.0, "fala-31min": 25.0}   # seconds, Groq/OpenAI


def budget(engine: str, name: str, seconds: float) -> float:
    """How long this engine may take. Groq/OpenAI: the plan's budgets (measured 4.2 / 9.6 / 20.5 s
    on 18/10/2026). Local: faster than real time. The Gateway sends the audio base64 inside a
    JSON body and its latency follows the network (measured 8.1-8.7, 14.6-16.3 and 34.5-52.9 s),
    so a tight budget there would only be flaky: it must beat real time, and the print keeps the
    number."""
    return seconds if engine in ("local", "gateway") else API_BUDGET[name]


def _run(cfg, wav, env, run_paths, monkeypatch, notices=None):
    samples = audiofile.decode(wav)
    pieces = []
    real = tr.transcribe
    monkeypatch.setattr(tr, "transcribe", lambda *a, **k: pieces.append(1) or real(*a, **k))
    t0 = time.monotonic()
    text = audiofile.transcribe_long(samples, cfg, env, run_paths, CancelToken(), notices)
    return text, time.monotonic() - t0, samples.size / RATE, len(pieces)


@pytest.mark.parametrize("engine", ["groq", "openai", "gateway", "local"])
@pytest.mark.parametrize("name", ["fala-5min", "fala-12min", "fala-31min"])
def test_long_audio_accuracy_and_speed(engine, name, user, fixtures, smoke, run_paths, monkeypatch,
                                       engine_cfg, anchors):
    cfg = engine_cfg(engine)
    wav = fixtures / f"{name}.wav"
    text, elapsed, seconds, pieces = _run(cfg, wav, user[2], run_paths, monkeypatch)
    recall, missing = smoke.anchor_recall(anchors(wav), text)
    print(f"\n{name} via {engine}: {seconds:.0f} s de áudio em {elapsed:.1f} s (RTF {elapsed / seconds:.3f}), "
          f"{pieces} pedaço(s), âncoras {recall:.1f}%")
    assert pieces == math.ceil(seconds / audiofile.CHUNK_SECONDS)
    assert recall >= (85.0 if engine == "local" else 90.0), f"faltando: {missing[:15]}"
    # API: the plan's budgets. Local and Gateway: whatever the PC/network, faster than real time.
    assert elapsed <= budget(engine, name, seconds)
    assert not list(run_paths.tmp_dir.glob("rec-*"))


def test_offline_long_dictation_goes_local(user, fixtures, smoke, run_paths, monkeypatch, engine_cfg,
                                          anchors):   # S1.4
    cfg = replace(engine_cfg("local"), engine="groq")
    tr.mark_offline()
    notices = []
    wav = fixtures / "fala-12min.wav"
    text, elapsed, seconds, pieces = _run(cfg, wav, {"GROQ_API_KEY": "never-sent"}, run_paths, monkeypatch, notices)
    recall, _ = smoke.anchor_recall(anchors(wav), text)
    print(f"\nsem internet: {seconds:.0f} s no modelo local em {elapsed:.1f} s, âncoras {recall:.1f}%")
    assert notices and "sem internet" in notices[0]
    assert recall >= 85.0


def test_long_text_refinement_keeps_everything(user, fixtures, smoke, anchors):            # S1.5
    paths, settings, env = user
    cleaner = Cleaner(settings.cleanup, env)
    if not cleaner.active:
        pytest.skip("sem provedor de LLM configurado")
    words = anchors(fixtures / "fala-31min.wav")
    raw = " ".join(f"item {i} {w} e depois a gente continua falando sobre o assunto" for i, w in enumerate(words))
    t0 = time.monotonic()
    out = cleaner.clean(raw, CancelToken())
    recall, missing = smoke.anchor_recall(words, out)
    print(f"\nrefinamento: {len(raw)} -> {len(out)} caracteres em {time.monotonic() - t0:.1f} s via "
          f"{cleaner.provider}, âncoras {recall:.1f}%" + (f", aviso: {cleaner.last_error[:80]}" if cleaner.last_error else ""))
    assert len(out) >= 0.9 * len(raw)   # cleanup trims a little; a truncated reply would lose half
    assert recall >= 95.0, f"faltando: {missing[:15]}"
