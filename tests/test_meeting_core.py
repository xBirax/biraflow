"""Meeting core (Fase 3 of docs/PLANO-REUNIOES.md): recorder, piece worker, folder, recovery.
No device, no network: fake sources on a fake clock, a fake transcriber. Ids T3.xx follow the plan."""
import json
import logging
import shutil
import threading
import time
from datetime import datetime

import numpy as np
import pytest
import soundfile as sf

from biraflow import meeting as mt
from biraflow import transcribe as tr
from biraflow.config import Paths, Settings, TranscriptionCfg
from biraflow.transcribe import Segment

RATE = 16000


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSource:
    """Plays `data` on the fake clock; `speed` > 1 is a device clock running fast."""

    def __init__(self, clock, data=None, speed=1.0, peak=None):
        """`data` None: an endless steady level (cheap for hours of audio)."""
        self.clock, self.speed = clock, speed
        self.data = None if data is None else data.astype(np.float32)
        data = np.full(1, 0.1, np.float32) if data is None else data
        self.alive, self.started, self.stopped = True, 0, 0
        self.peak = float(np.abs(data).max()) if peak is None and data.size else (peak or 0.0)
        self._t = self._pos = 0

    def start(self):
        self.started += 1
        self._t = self.clock()

    def read(self):
        n = int(round((self.clock() - self._t) * RATE * self.speed)) - self._pos
        if self.data is None:
            self._pos += max(0, n)
            return np.full(max(0, n), 0.1, np.float32)
        out = self.data[self._pos:self._pos + max(0, n)]
        self._pos += max(0, n)
        return out

    def stop(self):
        self.stopped += 1


def tone(seconds, level=0.3, freq=220):
    t = np.arange(int(seconds * RATE)) / RATE
    return (np.sin(2 * np.pi * freq * t) * level).astype(np.float32)


def silence(seconds):
    return np.zeros(int(seconds * RATE), np.float32)


@pytest.fixture(autouse=True)
def _wav_instead_of_opus(request, monkeypatch):
    """Opus costs ~0.5 s per 90 s piece: tests store WAV under the same name (libsndfile reads
    either). test_pieces_cover_whole_recording keeps real Opus."""
    if request.node.name == "test_pieces_cover_whole_recording":
        return
    real = sf.write
    monkeypatch.setattr(sf, "write", lambda path, data, rate, *a, **k: real(path, data, rate, format="WAV",
                                                                           subtype="PCM_16"))


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path, tmp_path)
    p.ensure()
    return p


@pytest.fixture
def folder(paths):
    return mt.MeetingFolder.create(mt.meetings_dir(paths), datetime(2026, 10, 16, 14, 30).astimezone())


def record(folder, mic, system, clock, seconds, step=1.0, **kw):
    """Drive a recorder tick by tick on the fake clock; returns (recorder, pieces)."""
    pieces = []
    r = mt.MeetingRecorder(folder, mic, system, pieces.append, clock=clock, **kw)
    for s in (mic, system):
        if s is not None:
            s.start()
    r._t0 = r._last = clock()
    for _ in range(int(seconds / step)):
        clock.t += step
        r.tick()
    r.stop()
    return r, pieces


# --- segments (T3.01–T3.04) -----------------------------------------------------------------

def test_segments_from_verbose_json():                                   # T3.01
    payload = {"text": "a b", "segments": [
        {"start": 0.0, "end": 2.5, "text": " Olá.", "no_speech_prob": 0.01, "avg_logprob": -0.2},
        {"start": 3.0, "end": 5.0, "text": " Tudo bem?", "compression_ratio": 1.1}]}
    segs = tr.segments_from(payload, 90.0)
    assert [(s.start, s.end, s.text) for s in segs] == [(0.0, 2.5, "Olá."), (3.0, 5.0, "Tudo bem?")]
    assert tr.segments_from({"text": "sem tempos"}, 90.0) == [Segment(0.0, 90.0, "sem tempos")]
    assert tr.segments_from({"text": "  "}, 90.0) == []


def test_gpt4o_models_return_single_segment(tmp_path, monkeypatch):      # T3.02
    monkeypatch.setattr(tr, "transcribe_api", lambda engine, f, cfg, env: " texto inteiro ")
    monkeypatch.setattr(tr, "_post", lambda *a: pytest.fail("verbose_json is not for gpt-4o"))
    cfg = TranscriptionCfg(engine="openai", openai_model="gpt-4o-mini-transcribe")
    assert tr.segments_api("openai", tmp_path / "a.ogg", cfg, {}, 88.0) == [Segment(0.0, 88.0, "texto inteiro")]


def test_hallucination_filter_and_clamp():                               # T3.03
    segs = [Segment(0, 3, "fala real", 0.9, -0.3), Segment(3, 6, "Obrigado por assistir", 0.7, -1.5),
            Segment(6, 9, "[música]"), Segment(80, 95, "passou do fim")]
    out = mt.clean_segments(segs, 90.0)
    assert [(s.start, s.end, s.text) for s in out] == [(0, 3, "fala real"), (80, 90.0, "passou do fim")]


def test_repetition_loop_collapsed():                                    # T3.04
    loop = "vamos lá, " * 8 + "e depois o resto."
    out = mt.clean_segments([Segment(0, 9, loop, ratio=3.0)], 90.0)
    assert out[0].text.count("vamos lá") == 1 and out[0].text.endswith("o resto.")
    normal = mt.clean_segments([Segment(0, 9, "não, não, não, não!", ratio=1.2)], 90.0)
    assert normal[0].text.count("não") == 4                     # real repetition is kept


# --- recorder (T3.05–T3.09, T3.19, T3.26) ------------------------------------------------------

def test_pieces_cover_whole_recording(folder):                           # T3.05
    clock = Clock()
    mic, pc = FakeSource(clock, tone(45)), FakeSource(clock, silence(45))
    r, pieces = record(folder, mic, pc, clock, 45, piece_seconds=20)    # real Opus: short pieces keep it fast
    assert [round(p.seconds) for p in pieces] == [20, 20, 5]           # no pause in a steady tone: full pieces
    assert abs(sum(p.seconds for p in pieces) - 45) <= 0.1
    assert [p.start for p in pieces[1:]] == pytest.approx([a.start + a.seconds for a in pieces[:-1]])
    data, rate = sf.read(str(folder.folder / "audio" / "parte-000.ogg"), always_2d=True)
    assert rate == RATE and data.shape[1] == 2
    assert folder.state["pieces"][0]["status"] == "pendente" and mic.stopped == pc.stopped == 1


def test_cut_at_quietest_300ms_in_last_15s():                            # T3.06
    x = tone(90)
    for gap in (78.0, 84.0):                                     # two pauses; 84.0-84.4 s is the longest
        x[int(gap * RATE):int((gap + (0.4 if gap == 84.0 else 0.15)) * RATE)] = 0
    cut = mt.cut_point(x, 90 * RATE)
    assert 84.0 * RATE <= cut <= 84.4 * RATE
    assert mt.cut_point(x, 100) == 100                           # too short to search


def test_lagging_source_zero_padded(folder):                             # T3.07
    clock = Clock()
    mic = FakeSource(clock, tone(30))
    pc = FakeSource(clock, tone(30, freq=440))
    real_read = pc.read
    pc.read = lambda: np.zeros(0, np.float32) if 5 <= clock.t - 1000 < 8 else real_read()   # 3 s with nothing
    r, pieces = record(folder, mic, pc, clock, 30)
    assert abs(r._counts[0] - r._counts[1]) <= mt.CORRECT_AFTER * RATE
    assert abs(sum(p.seconds for p in pieces) - 30) <= 0.6


def test_clock_drift_corrected(folder, monkeypatch):                     # T3.08 [R12]
    monkeypatch.setattr(sf, "write", lambda *a, **k: None)      # alignment only: skip encoding 2 h
    clock = Clock()
    mic = FakeSource(clock)
    pc = FakeSource(clock, speed=1.005)                          # the PC's device runs 0.5 % fast
    r, _ = record(folder, mic, pc, clock, 720, step=10.0, piece_seconds=60)   # 3.6 s of drift, as in 2 h at 0.05 %
    assert abs(r._counts[0] - r._counts[1]) / RATE <= mt.CORRECT_AFTER
    assert abs(r._counts[1] / RATE - 720) <= mt.CORRECT_AFTER + 0.01   # uncorrected it would be 723.6 s


def test_sleep_gap_becomes_pause(folder):                                # T3.09 [R11]
    clock = Clock()
    mic, pc = FakeSource(clock, tone(30)), FakeSource(clock, silence(30))   # only ~20 s is ever read
    r = mt.MeetingRecorder(folder, mic, pc, lambda p: None, clock=clock)
    mic.start(), pc.start()
    r._t0 = r._last = clock()
    for _ in range(10):
        clock.t += 1
        r.tick()
    clock.t += 2400                                              # 40 min asleep
    mic._t = pc._t = clock.t - 10                                # the sources delivered nothing meanwhile
    r.tick()
    assert max(b.size for b in r._bufs) < 30 * RATE              # no 40 minutes of zeros
    pauses = [x for x in folder.rows() if "pause" in x]
    assert len(pauses) == 1 and abs(pauses[0]["pause"] - 2399) <= 1 and pauses[0]["t"] == pytest.approx(10, abs=1)
    r.stop()


def test_stalled_thread_keeps_the_audio(folder):
    """The recorder thread stalled 40 s but the devices kept the audio: no pause, nothing lost
    (it once trimmed the backlog "back to the clock": a minute gone, channels seconds apart)."""
    clock = Clock()
    mic, pc = FakeSource(clock, tone(60)), FakeSource(clock, tone(60, freq=440))
    r = mt.MeetingRecorder(folder, mic, pc, lambda p: None, clock=clock)
    mic.start(), pc.start()
    r._t0 = r._last = clock()
    for _ in range(10):
        clock.t += 1
        r.tick()
    clock.t += 40                                                # stalled; the sources buffered
    r.tick()
    assert not [x for x in folder.rows() if "pause" in x]
    assert [b.size for b in r._bufs] == [50 * RATE, 50 * RATE]
    r.stop()


def test_low_disk_refuses_and_stops(paths, folder, monkeypatch):         # T3.19 [R21]
    usage = shutil.disk_usage(paths.data)
    monkeypatch.setattr(mt.shutil, "disk_usage", lambda p: usage._replace(free=400e6))
    with pytest.raises(mt.MeetingError, match="espaço"):
        mt.MeetingSession(paths, Settings(), {}, sources=(None, None)).start()
    monkeypatch.setattr(mt.shutil, "disk_usage", lambda p: usage._replace(free=90e6))
    clock = Clock()
    r = mt.MeetingRecorder(folder, FakeSource(clock, tone(200)), None, lambda p: None, clock=clock)
    with pytest.raises(mt.MeetingError, match="Disco"):
        for _ in range(100):
            clock.t += 1
            r.tick()


def test_silent_mic_warns_once(folder):                                  # T3.26 [R22]
    clock = Clock()
    warnings = []
    mic = FakeSource(clock, silence(200), peak=0.0)
    record(folder, mic, None, clock, 120, on_warning=warnings.append)
    assert len([w for w in warnings if "microfone" in w]) == 1


def test_dead_source_reopened_once(folder):
    clock = Clock()
    warnings = []
    pc = FakeSource(clock, tone(60))
    pc.alive = False
    record(folder, FakeSource(clock, tone(60)), pc, clock, 5, on_warning=warnings.append)
    assert pc.started == 2 and warnings == []


# --- worker (T3.10–T3.16) ---------------------------------------------------------------------

def stereo_piece(folder, mic, pc, start=0.0):
    index = folder.add_piece(start, mic.size / RATE)
    sf.write(str(folder.folder / folder.piece(index)["file"]), np.stack([mic, pc], axis=1), RATE,
             format="OGG", subtype="OPUS")
    folder.save()
    return index


@pytest.fixture
def fake_tx(monkeypatch):
    """transcribe_segments returning one 10-s segment per 10 s of audio, named after its time."""
    calls = []

    def fake(wav, cfg, env, paths, token, notices=None, upload=None):
        data, _ = sf.read(str(wav), dtype="float32")
        calls.append({"wav": data, "cfg": cfg, "upload": upload})
        n = int(len(data) / RATE // 10)
        return [Segment(10.0 * i, 10.0 * i + 9.5, f"trecho {i} Kate") for i in range(n)]

    monkeypatch.setattr(tr, "transcribe_segments", fake)
    return calls


def worker(folder, paths, **kw):
    return mt.PieceWorker(folder, TranscriptionCfg(engine="local"), {}, paths, **kw)


def test_labels_by_channel_energy(folder, paths, fake_tx):               # T3.10
    mic = np.concatenate([tone(40), silence(40)])
    pc = np.concatenate([silence(40), tone(40, level=0.1, freq=330)])   # quieter, still "Participantes"
    i = stereo_piece(folder, mic, pc, start=90.0)
    worker(folder, paths).process(i)
    rows = folder.rows()
    assert [r["who"] for r in rows] == [mt.ME] * 4 + [mt.OTHERS] * 4
    assert rows[0]["t0"] == 90.0 and rows[-1]["t1"] == 169.5           # piece start + segment time
    assert folder.piece(i)["status"] == "feita" and folder.piece(i)["engine"] == "local"


def test_in_person_labels_everyone_room_and_splits_on_pauses(folder, paths, fake_tx):
    folder.set(in_person=True)
    i = stereo_piece(folder, tone(40), silence(40))                       # the PC channel is silent
    worker(folder, paths).process(i)
    assert {r["who"] for r in folder.rows()} == {mt.ROOM}
    folder.append([{"t0": 50.0, "t1": 52.0, "who": mt.ROOM, "text": "Outra voz.", "piece": 1, "run": 2}])
    text = folder.render()                                                # 0.5 s gaps: one paragraph;
    assert text.count("Sala:**") == 2 and "**[00:00:50] Sala:** Outra voz." in text   # 10.5 s: a new one


def test_room_mic_prefers_the_laptop_array():
    names = ["Mapeador de som da Microsoft - Input", "Microfone (2- H848 Wireless hea",
             "Grupo de microfones (Tecnologia Intel® Smart Sound)"]
    assert mt.room_mic(names) == names[2]
    assert mt.room_mic(["Microphone Array (Realtek(R) Audio)"]) == "Microphone Array (Realtek(R) Audio)"
    assert mt.room_mic(names[:2]) == ""                                   # none: the Windows default


def test_in_person_session_uses_the_room_mic_and_no_pc_audio(paths, monkeypatch):
    opened = []

    class Mic:
        def __init__(self, device=""):
            opened.append(device)
            self.alive, self.peak = True, 0.1

        def start(self):
            pass

        def read(self):
            return np.zeros(0, np.float32)

        def stop(self):
            pass

    monkeypatch.setattr(mt, "MicSource", Mic)
    monkeypatch.setattr(mt.audio, "list_input_devices", lambda: ["Microfone (H848)", "Grupo de microfones (Intel)"])
    s = mt.MeetingSession(paths, Settings(), {}, in_person=True, tick=0.01)
    m = s.start()
    try:
        assert opened == ["Grupo de microfones (Intel)"] and s.mic_device == opened[0]
        assert s.recorder.sources[1] is None and m.state["in_person"] is True   # no loopback opened
    finally:
        s.stop(wait=1)


def test_quiet_segment_inherits_label():                                 # T3.11
    mic, pc = silence(20), np.concatenate([tone(10), silence(10)])
    assert mt.label(mic, pc, 0, 9, mt.ME) == mt.OTHERS
    assert mt.label(mic, pc, 11, 19, mt.OTHERS) == mt.OTHERS


def test_mix_normalizes_channels(folder, paths, fake_tx):                # T3.12 [R14]
    mic = np.concatenate([tone(20, level=0.5), silence(20)])
    pc = np.concatenate([silence(20), tone(20, level=0.05)])            # 20 dB lower
    worker(folder, paths).process(stereo_piece(folder, mic, pc))
    sent = fake_tx[0]["wav"]
    first = np.sqrt(np.mean(sent[: 20 * RATE] ** 2))
    second = np.sqrt(np.mean(sent[20 * RATE:] ** 2))
    assert 0.4 <= second / first < 2 and np.abs(sent).max() <= 1.0     # 20 dB lower -> gain capped at 8x: half, not lost


def test_silent_piece_not_sent(folder, paths, fake_tx):                  # T3.13
    i = stereo_piece(folder, silence(90), silence(90))
    worker(folder, paths).process(i)
    assert fake_tx == [] and folder.piece(i)["status"] == "vazia" and folder.rows() == []


def test_failed_piece_retried_with_backoff(folder, paths, monkeypatch):  # T3.14 [R19]
    def broken(*a, **k):
        raise tr.TranscriptionError("API fora do ar e sem modelo local")

    monkeypatch.setattr(tr, "transcribe_segments", broken)
    clock = Clock()
    w = worker(folder, paths, clock=clock)
    i = stereo_piece(folder, tone(30), silence(30))
    for delay in mt.RETRY_DELAYS:
        w.process(i)
        assert folder.piece(i)["status"] == "pendente" and w._queue[-1] == (clock.t + delay, i)
        w._queue.clear()
    w.process(i)
    assert folder.piece(i)["status"] == "falhou" and folder.piece(i)["tries"] == 4 and w._queue == []


def test_dictation_has_priority(folder, paths, fake_tx):                 # T3.15 [R18]
    busy = threading.Event()
    busy.set()
    w = worker(folder, paths, busy=busy.is_set)
    w.start()
    w.submit(stereo_piece(folder, tone(20), silence(20)))
    time.sleep(0.6)
    assert fake_tx == []                                         # waits while a dictation runs
    busy.clear()
    assert w.finish(5) == 0 and len(fake_tx) == 1


def test_meeting_uses_dictation_hints_and_dictionary(folder, paths, fake_tx):   # T3.16 [R17]
    from biraflow.store import Dictionary, Replacement

    Dictionary([Replacement("Kate", "Kayth")], []).save(paths.dictionary)
    settings = Settings()
    settings.transcription.vocabulary = ["Bira Flow"]
    cfg = mt._transcription_cfg(settings, paths)
    assert cfg.vocabulary == ["Bira Flow", "Kayth"]
    mt.PieceWorker(folder, cfg, {}, paths).process(stereo_piece(folder, tone(20), silence(20)))
    assert all("Kayth" in r["text"] and "Kate" not in r["text"] for r in folder.rows())
    assert fake_tx[0]["cfg"].vocabulary == ["Bira Flow", "Kayth"]


def test_context_carries_to_next_piece(folder, paths, fake_tx):
    w = worker(folder, paths)
    w.process(stereo_piece(folder, tone(20), silence(20)))
    w.process(stereo_piece(folder, tone(20), silence(20), start=20))
    assert fake_tx[0]["cfg"].context == "" and fake_tx[1]["cfg"].context.endswith("trecho 1 Kate")


def test_api_pieces_do_not_get_the_previous_text(folder, paths, fake_tx):   # T3.27
    """The previous text as Whisper's prompt makes the APIs merge whole turns into one segment
    (measured 18/10/2026: up to 24.5 s with two speakers, 78.7 % of labels right against 97.5 %
    without it); whisper.cpp keeps splitting with the same prompt. So the context goes only to
    the local engine — the vocabulary hint always goes (test_meeting_uses_dictation_hints...)."""
    w = mt.PieceWorker(folder, TranscriptionCfg(engine="groq"), {"GROQ_API_KEY": "k"}, paths)
    w.process(stereo_piece(folder, tone(20), silence(20)))
    w.process(stereo_piece(folder, tone(20), silence(20), start=20))
    assert len(fake_tx) == 2 and [c["cfg"].context for c in fake_tx] == ["", ""]


# --- folder, recovery, session (T3.17–T3.25) ----------------------------------------------------

def test_recover_finishes_pending(paths, fake_tx):                       # T3.17
    m = mt.MeetingFolder.create(mt.meetings_dir(paths))
    w = worker(m, paths)
    w.process(stereo_piece(m, tone(20), silence(20)))
    w.process(stereo_piece(m, silence(20), tone(20), start=20))
    stereo_piece(m, tone(20), silence(20), start=40)               # the crash came before this one
    assert m.state["status"] == "gravando"
    done = mt.recover(paths, Settings(), {})
    assert [d.folder for d in done] == [m.folder]
    again = mt.MeetingFolder.open(m.folder)
    assert again.state["status"] == "pronta" and [p["status"] for p in again.state["pieces"]] == ["feita"] * 3
    assert again.transcript.read_text(encoding="utf-8").count("trecho") == 6
    assert mt.recover(paths, Settings(), {}) == []                # nothing left to recover


def test_recover_tolerates_torn_and_duplicate_rows(paths, fake_tx):     # T3.18 [R24]
    m = mt.MeetingFolder.create(mt.meetings_dir(paths))
    i = stereo_piece(m, tone(20), silence(20))
    w = worker(m, paths)
    w.process(i)
    w.process(i)                                                  # transcribed twice (crash before its status)
    with open(m.folder / "segmentos.jsonl", "a", encoding="utf-8") as f:
        f.write('{"t0": 99, "who": "Eu", "te')                    # torn last line
    assert len(m.rows()) == 2
    other = mt.MeetingFolder.create(mt.meetings_dir(paths))
    other.set(version=99, status="gravando")
    assert mt.MeetingFolder.open(other.folder) is None
    assert [d.folder for d in mt.recover(paths, Settings(), {})] == [m.folder]


def test_keep_audio_false_deletes_audio_only_when_done(folder, paths, fake_tx):  # T3.20
    i = stereo_piece(folder, tone(20), silence(20))
    stereo_piece(folder, tone(20), silence(20))
    worker(folder, paths).process(i)
    mt.finish(folder, keep_audio=False, interrupted=True)
    assert (folder.folder / "audio").is_dir() and folder.state["status"] == "interrompida"   # one still pending
    worker(folder, paths).process(1)
    mt.finish(folder, keep_audio=False)
    assert not (folder.folder / "audio").exists() and folder.state["status"] == "pronta"


def test_system_audio_false_opens_no_loopback(paths, monkeypatch, fake_tx):   # T3.21
    from biraflow import loopback

    monkeypatch.setattr(loopback, "SystemAudio", lambda: pytest.fail("no loopback without system_audio"))
    clock = Clock()
    monkeypatch.setattr(mt, "MicSource", lambda device="": FakeSource(clock, tone(10)))
    s = mt.MeetingSession(paths, Settings(), {}, system_audio=False, tick=0.01)
    s.start()
    assert s.recorder.sources[1] is None
    s.stop(wait=5)


def test_transcript_markdown_golden(folder):                             # T3.22
    folder.add_piece(0, 90)
    folder.add_piece(90, 3)
    folder.append([
        {"t0": 5.0, "t1": 9.0, "who": "Eu", "text": "Bom dia a todos.", "piece": 0, "run": 1},
        {"t0": 9.5, "t1": 12.0, "who": "Eu", "text": "Vamos começar.", "piece": 0, "run": 1},
        {"t0": 41.0, "t1": 50.0, "who": "Participantes", "text": "Perfeito.", "piece": 0, "run": 1},
        {"pause": 720, "t": 60.0},
        {"t0": 3725.0, "t1": 3730.0, "who": "Eu", "text": "Voltamos.", "piece": 1, "run": 2},
    ])
    assert folder.transcript.read_text(encoding="utf-8") == (
        "# Reunião — 16/10/2026 14:30 (1 min)\n\n"
        "**[00:00:05] Eu:** Bom dia a todos. Vamos começar.\n\n"
        "**[00:00:41] Participantes:** Perfeito.\n\n"
        "*[00:01:00] pausa de 12 min (PC suspenso)*\n\n"
        "**[01:02:05] Eu:** Voltamos.\n")


def test_folder_name_collision(paths):                                   # T3.23 [R20]
    when = datetime(2026, 10, 16, 14, 30).astimezone()
    a = mt.MeetingFolder.create(mt.meetings_dir(paths), when)
    b = mt.MeetingFolder.create(mt.meetings_dir(paths), when)
    assert (a.folder.name, b.folder.name) == ("2026-10-16_1430", "2026-10-16_1430-2")
    assert json.loads((a.folder / "reuniao.json").read_text(encoding="utf-8"))["status"] == "gravando"


def test_logs_have_no_transcript_text(folder, paths, fake_tx, caplog):  # T3.24 [R31]
    caplog.set_level(logging.DEBUG)
    worker(folder, paths).process(stereo_piece(folder, tone(20), silence(20)))
    assert folder.rows() and "trecho" not in caplog.text and "Kate" not in caplog.text


def test_session_end_to_end_stop_waits_for_worker(paths, monkeypatch, fake_tx):   # T3.25
    """A 'meeting' of 120 s on a clock running 100x: pieces, transcription, final state."""
    real = time.monotonic
    start = real()

    def clock():
        return start + (real() - start) * 100

    mic = FakeSource(clock, np.concatenate([tone(60), silence(60)]))
    pc = FakeSource(clock, np.concatenate([silence(60), tone(60, freq=330)]))
    s = mt.MeetingSession(paths, Settings(), {}, sources=(mic, pc), clock=clock, tick=0.01)
    m = s.start()
    while s.elapsed < 119:
        time.sleep(0.01)
    s.stop(wait=10)
    assert m.state["status"] == "pronta" and {p["status"] for p in m.state["pieces"]} <= {"feita", "vazia"}
    whos = [r["who"] for r in m.rows() if "who" in r]   # a slow tick on the 100x clock may log a pause
    assert whos[0] == mt.ME and whos[-1] == mt.OTHERS
    assert "Participantes" in m.transcript.read_text(encoding="utf-8")
