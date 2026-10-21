"""Regressions from the code review of the meeting mode (Fases 3 and 4 of docs/PLANO-REUNIOES.md):
no audio device, no network."""
import json
import time
from datetime import datetime

import numpy as np
import pytest

from biraflow import cleanup
from biraflow import meeting as mt
from biraflow.cancel import CancelToken
from biraflow.cleanup import Cleaner, CleanupError
from biraflow.config import CleanupCfg, Paths, Settings

GOOD = ("# Orçamento\n## Resumo\nFalamos do orçamento.\n## Decisões\n- Aprovado [00:01:02]\n"
        "## Próximos passos\n- [ ] Ana — enviar planilha — sexta\n## Pontos em aberto\nNenhum.")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Source:
    """A steady level on the fake clock; `alive` and failing `start` are set by the test."""

    def __init__(self, clock):
        self.clock, self.alive, self.started, self.stopped, self.fail = clock, True, 0, 0, False
        self.peak, self._t, self._pos = 0.1, 0.0, 0

    def start(self):
        self.started += 1
        if self.fail:
            raise OSError("dispositivo sumiu")
        self._t = self.clock()

    def read(self):
        n = max(0, int((self.clock() - self._t) * 16000) - self._pos)
        self._pos += n
        return np.full(n, 0.1, np.float32)

    def stop(self):
        self.stopped += 1


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path, tmp_path)
    p.ensure()
    return p


@pytest.fixture
def folder(paths):
    return mt.MeetingFolder.create(mt.meetings_dir(paths), datetime(2026, 10, 16, 14, 30).astimezone())


# --- Fase 3 ---------------------------------------------------------------------------------

def test_torn_last_line_does_not_swallow_the_next_row(folder):
    folder.append([{"t0": 1.0, "t1": 2.0, "who": "Eu", "text": "a", "piece": 0, "run": 1}])
    with open(folder.folder / "segmentos.jsonl", "a", encoding="utf-8") as f:
        f.write('{"t0": 2.0, "te')                                        # a crash mid-write
    folder.append([{"t0": 90.0, "t1": 91.0, "who": "Eu", "text": "b", "piece": 1, "run": 1}])
    assert [r["text"] for r in folder.rows()] == ["a", "b"]


def test_dead_source_is_reopened_again_later_and_warned_once(folder):
    clock, warnings = Clock(), []
    mic, pc = Source(clock), Source(clock)
    r = mt.MeetingRecorder(folder, mic, pc, lambda p: None, clock=clock, on_warning=warnings.append)
    mic.start(), pc.start()
    r._t0 = r._last = clock()
    pc.alive, pc.fail = False, True
    for _ in range(25):                                                   # 25 s dead: tried at 1, 11 and 21 s
        clock.t += 1
        r.tick()
    assert pc.started == 4 and warnings == ["O áudio do PC parou de chegar."]
    pc.alive, pc.fail = True, False
    clock.t += 1
    r.tick()                                                              # back: a new outage warns again
    pc.alive, pc.fail = False, True
    clock.t += 10
    r.tick()
    assert len(warnings) == 2


def test_recorder_error_stops_the_sources(folder, monkeypatch):
    clock, warnings = Clock(), []
    mic, pc = Source(clock), Source(clock)
    r = mt.MeetingRecorder(folder, mic, pc, lambda p: None, clock=time.monotonic, tick=0.01,
                           on_warning=warnings.append)
    monkeypatch.setattr(r, "tick", lambda: (_ for _ in ()).throw(PermissionError("OneDrive")))
    r.start()
    r._thread.join(2)
    assert mic.stopped and pc.stopped and isinstance(r.error, PermissionError)   # buffers no longer grow
    assert "parou" in warnings[0]


def test_a_tick_that_fails_for_a_moment_does_not_end_the_recording(folder, monkeypatch):
    clock, warnings, calls = Clock(), [], []
    r = mt.MeetingRecorder(folder, Source(clock), Source(clock), lambda p: None, clock=time.monotonic, tick=0.01,
                           on_warning=warnings.append)

    def flaky():
        calls.append(1)
        if len(calls) <= 3:
            raise PermissionError("parte-000.ogg aberto no antivírus")

    monkeypatch.setattr(r, "tick", flaky)
    r.start()
    deadline = time.monotonic() + 2
    while len(calls) < 6 and time.monotonic() < deadline:
        time.sleep(0.01)
    r._stop.set()
    r._thread.join(2)
    assert len(calls) >= 6 and r.error is None and warnings == []


def test_source_still_dead_after_a_reopen_is_warned(folder):
    # a loopback's start() never raises: the warning comes when the reopen did not help
    clock, warnings = Clock(), []
    mic, pc = Source(clock), Source(clock)
    r = mt.MeetingRecorder(folder, mic, pc, lambda p: None, clock=clock, on_warning=warnings.append)
    mic.start(), pc.start()
    r._t0 = r._last = clock()
    pc.alive = False
    for _ in range(12):                                                   # reopened at 1 s, still dead at 11 s
        clock.t += 1
        r.tick()
    assert pc.started == 3 and warnings == ["O áudio do PC parou de chegar."]


def test_recorder_keeps_the_pc_awake_while_it_runs(folder, monkeypatch):     # review 3 #1
    calls = []
    monkeypatch.setattr(mt, "keep_awake", calls.append)
    clock = Clock()
    r = mt.MeetingRecorder(folder, Source(clock), Source(clock), lambda p: None, clock=time.monotonic, tick=0.01)
    r.start()
    time.sleep(0.05)
    assert calls == [True]
    r.stop()
    assert calls == [True, False]


def test_a_locked_file_is_retried_not_fatal(tmp_path, monkeypatch):          # review 3 #2
    from biraflow import config

    real, fails = config.os.replace, [PermissionError("antivirus")] * 3

    def replace(a, b):
        if fails:
            raise fails.pop()
        real(a, b)

    monkeypatch.setattr(config.os, "replace", replace)
    config.write_atomic(tmp_path / "reuniao.json", "{}")
    assert (tmp_path / "reuniao.json").read_text() == "{}" and not fails


def test_worker_survives_an_unexpected_error(folder, paths, monkeypatch):   # review 3 #3
    from biraflow.config import TranscriptionCfg

    done = []
    w = mt.PieceWorker(folder, TranscriptionCfg(engine="local"), {}, paths)
    monkeypatch.setattr(w, "process", lambda i: done.append(i) if i else (_ for _ in ()).throw(OSError("locked")))
    w.start()
    w.submit(0)                                                          # raises inside the thread
    w.submit(1)                                                          # still transcribed
    w.finish(2)
    assert done == [1]


def test_failed_write_leaves_no_phantom_piece(folder, monkeypatch):
    import soundfile as sf

    clock = Clock()
    r = mt.MeetingRecorder(folder, Source(clock), Source(clock), lambda p: None, clock=clock)
    r._bufs = [np.zeros(16000 * 5, np.float32)] * 2
    monkeypatch.setattr(sf, "write", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disco")))
    with pytest.raises(RuntimeError):
        r._write(16000 * 5)
    assert folder.state["pieces"] == []


def test_session_stop_closes_the_worker_even_if_the_last_write_fails(paths, monkeypatch):
    clock = Clock()
    session = mt.MeetingSession(paths, Settings(), {}, sources=(Source(clock), Source(clock)), clock=clock,
                                tick=60, auto_notes=False)
    m = session.start()
    monkeypatch.setattr(session.recorder, "stop", lambda: (_ for _ in ()).throw(mt.MeetingError("disco cheio")))
    with pytest.raises(mt.MeetingError):
        session.stop(wait=1)
    assert m.state["status"] == "pronta" and session.worker._closing


def test_finish_with_a_pending_piece_is_interrupted(folder):
    folder.add_piece(0.0, 90.0)
    mt.finish(folder, keep_audio=False)
    assert folder.state["status"] == "interrompida" and (folder.folder / "audio").is_dir()


def test_recovered_crash_ends_when_the_audio_ends(paths):
    m = mt.MeetingFolder.create(mt.meetings_dir(paths), datetime(2026, 10, 16, 14, 30).astimezone())
    m.add_piece(0.0, 600.0)
    m.set_piece(0, status="vazia")                                        # nothing left to send
    mt.recover(paths, Settings(), {}, auto_notes=False)
    ended = datetime.fromisoformat(mt.MeetingFolder.open(m.folder).state["ended"])
    assert ended.strftime("%H:%M") == "14:40"


# --- Fase 4 ---------------------------------------------------------------------------------

def test_expired_cooldowns_are_ignored():
    cleanup._limit("old", -1)       # already expired: a sleep is flaky, Windows' monotonic ticks every ~16 ms
    cleanup._limit("haiku", 45)
    assert 40 < cleanup._cooldown_left() <= 45


def test_line_blocks_keep_every_line_labelled_and_small():
    lines = [f"[00:{i // 60:02d}:{i % 60:02d}] Eu: frase número {i}." for i in range(2000)]
    blocks = cleanup.line_blocks("\n".join(lines), 12000)
    assert len(blocks) > 1 and all(len(b) <= 12000 for b in blocks)
    assert all(b.startswith("[00:") for b in blocks)                      # no block starts unlabelled
    assert "\n".join(blocks) == "\n".join(lines)
    huge = cleanup.line_blocks("x" * 30000, 12000)                        # one line without punctuation
    assert [len(b) for b in huge] == [12000, 12000, 6000]


def test_notes_input_has_a_label_per_row(folder):
    folder.append([{"t0": 5.0, "t1": 9.0, "who": "Eu", "text": "Primeira.", "piece": 0, "run": 1},
                   {"t0": 9.5, "t1": 12.0, "who": "Eu", "text": "Segunda.", "piece": 0, "run": 1}])
    assert mt.notes_input(folder) == "[00:00:05] Eu: Primeira.\n[00:00:09] Eu: Segunda."


@pytest.fixture
def llm(monkeypatch):
    calls, replies = [], []

    def request(self, system, user, timeout_s, max_tokens=2048):
        calls.append({"system": system, "max_tokens": max_tokens})
        reply = replies.pop(0) if replies else GOOD
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(Cleaner, "_request", request)
    return calls, replies


def test_failed_merge_keeps_the_partial_notes(llm):
    calls, replies = llm
    text = "\n".join(f"[00:00:{i % 60:02d}] Eu: " + "palavra " * 40 for i in range(200))
    blocks = len(cleanup.line_blocks(text, cleanup.NOTES_CHARS))
    replies += [GOOD] * blocks + [RuntimeError("timeout")]
    out = Cleaner(CleanupCfg(), {"GROQ_API_KEY": "k"}).meeting_notes(text, CancelToken())
    assert out.startswith(cleanup.NOTES_WARNING) and out.count("## Resumo") == blocks
    assert calls[-1]["max_tokens"] == 4096                                # the merge has more room


def test_portuguese_headings_are_asked_for_in_any_language():
    assert "em português" in cleanup.MEETING_PROMPT and "em português" in cleanup.MEETING_MERGE_PROMPT


def test_notes_pending_are_written_at_the_next_start(paths, llm):
    calls, _ = llm
    m = mt.MeetingFolder.create(mt.meetings_dir(paths))
    m.append([{"t0": 5.0, "t1": 9.0, "who": "Eu", "text": "Vamos aprovar.", "piece": 0, "run": 1}])
    m.set(status="pronta", notes_pending=True)                           # the app quit during the notes
    assert mt.recover(paths, Settings(), {"GROQ_API_KEY": "k"})
    state = json.loads((m.folder / "reuniao.json").read_text(encoding="utf-8"))
    assert (m.folder / "notas.md").exists() and state["notes_pending"] is False and len(calls) == 1
    mt.recover(paths, Settings(), {"GROQ_API_KEY": "k"})
    assert len(calls) == 1                                                # once


def test_write_notes_survives_a_locked_folder(paths, llm, monkeypatch):
    m = mt.MeetingFolder.create(mt.meetings_dir(paths))
    m.append([{"t0": 5.0, "t1": 9.0, "who": "Eu", "text": "Oi.", "piece": 0, "run": 1}])
    real = mt.write_atomic
    monkeypatch.setattr(mt, "write_atomic", lambda path, text: (_ for _ in ()).throw(PermissionError("antivírus"))
                        if path.name == "notas.md" else real(path, text))
    assert mt.write_notes(m, Settings(), {"GROQ_API_KEY": "k"}) is None
    assert "antivírus" in m.state["notes_error"]


def test_notes_error_for_rate_limit_is_not_the_dictation_message(llm, monkeypatch):
    _, replies = llm
    monkeypatch.setattr(cleanup.time, "sleep", lambda s: None)
    replies += [cleanup.RateLimited("429"), cleanup.RateLimited("429")]
    with pytest.raises(CleanupError) as e:
        Cleaner(CleanupCfg(), {"GROQ_API_KEY": "k"}).meeting_notes("[00:00:01] Eu: oi", CancelToken())
    assert "colado" not in str(e.value) and "Gerar notas" in str(e.value)


def test_quiet_room_noise_is_not_sent(folder, paths, monkeypatch):            # found by S3.4
    import soundfile as sf

    from biraflow import transcribe as tr
    from biraflow.config import TranscriptionCfg

    sent = []
    monkeypatch.setattr(tr, "transcribe_segments", lambda *a, **k: sent.append(1) or [])
    noise = (np.random.default_rng(1).standard_normal(16000 * 30) * 0.0006).astype(np.float32)
    index = folder.add_piece(0.0, 30.0)
    sf.write(str(folder.folder / folder.piece(index)["file"]), np.stack([noise, noise], axis=1), 16000,
             format="OGG", subtype="OPUS")
    mt.PieceWorker(folder, TranscriptionCfg(engine="local"), {}, paths).process(index)
    assert sent == [] and folder.piece(index)["status"] == "vazia"   # normalize() would lift it 8x


def test_offline_piece_keeps_being_retried_during_the_meeting(folder, paths, monkeypatch):   # review 2 #2
    from biraflow.config import TranscriptionCfg

    clock = Clock()
    w = mt.PieceWorker(folder, TranscriptionCfg(engine="groq"), {}, paths, clock=clock)
    index = folder.add_piece(0.0, 90.0)
    folder.set_piece(index, tries=3)                                     # the 30/120/300 s retries are used up
    monkeypatch.setattr(mt.PieceWorker, "_transcribe", lambda self, i: (_ for _ in ()).throw(ConnectionError()))
    w.process(index)
    assert folder.piece(index)["status"] == "pendente" and w._queue == [(clock() + 300.0, index)]


def test_local_disk_error_is_not_offline(folder, paths, monkeypatch):
    import requests

    from biraflow import transcribe as tr
    from biraflow.config import TranscriptionCfg

    clock = Clock()
    w = mt.PieceWorker(folder, TranscriptionCfg(engine="groq"), {}, paths, clock=clock)
    index = folder.add_piece(0.0, 90.0)
    folder.set_piece(index, tries=3)                                     # the 30/120/300 s retries are used up
    monkeypatch.setattr(mt.PieceWorker, "_transcribe",
                        lambda self, i: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    w.process(index)
    assert folder.piece(index)["status"] == "falhou" and w._queue == []   # not retried every 5 min forever

    def offline(self, i):
        raise tr.TranscriptionError("Groq inacessível (ConnectionError).") from requests.ConnectionError()

    monkeypatch.setattr(mt.PieceWorker, "_transcribe", offline)
    w.process(index)
    assert folder.piece(index)["status"] == "pendente" and w._queue == [(clock() + 300.0, index)]
